"""M4-A2 Part 6: the REST contract for retry attempts, reused tasks, uncertain work, run history and ownership state (real app, fake provider,
no network). Existing M3-B responses keep every field; the new fields are additive."""

import copy
import json
import sys
import threading

import pytest

from tests.api_support import api, auth_db, main_module  # noqa: F401  (fixtures)
from tests.test_plan_runs_api import A, AGENT, B, make_plan, papi, start, task, wait  # noqa: F401  (papi is a fixture)


def error(code):
    return sys.modules["agentplatform.providers"].ProviderError(code)


def retry(api, run_id, who=A, body=None):
    return api.client.post(f"/plan-runs/{run_id}/retry", headers=api.auth(*who), **({"json": body} if body is not None else {}))


def failed_first_run(api, code):
    api.client.post("/agents", json={"slug": "writer", "definition": copy.deepcopy(AGENT)}, headers=api.auth(*A))
    pid = make_plan(api, [task("a", "writer"), task("b", "research", ["a"]), task("c", "sales")])
    api.provider.error = error(code)
    first = wait(api, start(api, pid).json()["id"])
    api.provider.error = None
    return pid, first


def test_a_safe_failure_is_retried_without_acknowledgement_and_reuses_the_finished_task(papi):
    api = papi
    pid, first = failed_first_run(api, "rate_limited")
    assert first["status"] == "partial" and {t["id"]: t["status"] for t in first["tasks"]} == {"a": "failed", "b": "skipped", "c": "succeeded"}
    assert {t["id"]: t["retry"] for t in first["tasks"]} == {"a": "safe", "b": "safe", "c": None}
    assert first["retry_options"] == {"allowed": True, "reason": None, "uncertain": [], "reconcile": []} and first["retry_of"] is None and first["retried_by"] is None
    started = retry(api, first["id"])
    assert started.status_code == 202
    second = wait(api, started.json()["id"])
    assert (second["attempt"], second["status"]) == (2, "succeeded") and second["retry_of"] == {"id": first["id"], "attempt": 1}
    by = {t["id"]: t for t in second["tasks"]}
    assert by["c"]["reused"] == {"run_id": first["id"], "attempt": 1} and by["a"]["reused"] is None and by["b"]["reused"] is None
    assert by["c"]["execution_id"] == {t["id"]: t for t in first["tasks"]}["c"]["execution_id"]  # traceable to the execution that really ran it
    assert second["counts"]["succeeded"] == 3 and api.db.scalar("SELECT count(*) FROM agent_executions") == 4  # a(failed) c, then a b: c was not run again
    again = api.client.get(f"/plan-runs/{first['id']}", headers=api.auth(*A)).json()
    assert again["retried_by"] == {"id": second["id"], "attempt": 2} and again["retry_options"]["allowed"] is False and again["retry_options"]["reason"] == "not_latest_attempt"
    listing = api.client.get(f"/plans/{pid}/runs", headers=api.auth(*A)).json()["items"]
    assert [r["attempt"] for r in listing] == [2, 1] and listing[0]["counts"]["succeeded"] == 3  # the list counts reused tasks too
    task_view = api.client.get(f"/plan-runs/{second['id']}/tasks/c", headers=api.auth(*A)).json()
    assert task_view["reused"]["attempt"] == 1 and task_view["execution"]["status"] == "succeeded"  # a reused task's output is readable


def test_an_uncertain_outcome_needs_an_explicit_acknowledgement_over_rest(papi):
    api = papi
    _pid, first = failed_first_run(api, "timeout")
    assert {t["id"]: t["retry"] for t in first["tasks"]}["a"] == "uncertain" and first["retry_options"]["uncertain"] == ["a"]
    refused = retry(api, first["id"])
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "uncertain_outcome" and refused.json()["detail"]["tasks"] == ["a"]
    assert retry(api, first["id"], body={"acknowledge_uncertain": ["c"]}).status_code == 409  # acknowledging something else does not cover a
    assert api.db.scalar("SELECT count(*) FROM plan_runs") == 1  # nothing was created by the refusals
    started = retry(api, first["id"], body={"acknowledge_uncertain": ["a"]})
    assert started.status_code == 202
    second = wait(api, started.json()["id"])
    assert second["status"] == "succeeded"
    events = api.client.get(f"/plan-runs/{second['id']}/events", headers=api.auth(*A)).json()["items"]
    assert events[0]["type"] == "retry_authorized" and events[0]["actor"] == "user"
    assert events[0]["detail"] == {"acknowledged": ["a"], "attempt": 2, "carried": ["c"], "retried": ["a", "b"], "retry_of_attempt": 1}  # the decision is auditable


def test_retry_refusals_are_fixed_scoped_and_never_trust_the_client(papi):
    api = papi
    _pid, first = failed_first_run(api, "rate_limited")
    for forged in ({"revision": 2}, {"user_id": "x"}, {"tasks": []}, {"agent_version": 1}, {"acknowledge_uncertain": "a"}):
        assert retry(api, first["id"], body=forged).status_code == 422, forged
    assert retry(api, first["id"], body={"acknowledge_uncertain": ["nope"]}).json()["detail"]["code"] == "unknown_task"
    assert retry(api, first["id"], who=B).status_code == 404  # another user: indistinguishable from unknown
    assert retry(api, "00000000-0000-4000-8000-000000000001").status_code == 404
    assert api.client.post(f"/plan-runs/{first['id']}/retry").status_code == 401  # a token is required
    assert api.db.scalar("SELECT count(*) FROM plan_runs") == 1 and api.db.scalar("SELECT count(*) FROM plan_run_retries") == 0
    ok = wait(api, retry(api, first["id"]).json()["id"])
    assert ok["status"] == "succeeded"
    done = retry(api, ok["id"])
    assert done.status_code == 409 and done.json()["detail"]["code"] == "run_not_retryable"  # a succeeded run needs no retry
    stale = retry(api, first["id"])
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "not_latest_attempt"


def test_a_stopping_process_refuses_a_retry_with_a_fixed_503(papi, monkeypatch):
    api = papi
    _pid, first = failed_first_run(api, "rate_limited")
    monkeypatch.setattr(api.main.plan_run_registry, "closing", True)
    refused = retry(api, first["id"])
    assert refused.status_code == 503 and refused.json()["detail"]["code"] == "shutting_down"
    assert api.db.scalar("SELECT count(*) FROM plan_runs") == 1


def test_the_event_history_is_ordered_paged_scoped_and_shows_no_infrastructure_identity(papi):
    api = papi
    pid = make_plan(api, [task("a", "research"), task("b", "sales", ["a"])])
    run = wait(api, start(api, pid).json()["id"])
    page = api.client.get(f"/plan-runs/{run['id']}/events?limit=3", headers=api.auth(*A)).json()
    assert [e["type"] for e in page["items"]] == ["run_created", "run_claimed", "task_started"] and page["next_after"] == page["items"][-1]["id"]
    rest = api.client.get(f"/plan-runs/{run['id']}/events?after={page['next_after']}", headers=api.auth(*A)).json()
    everything = page["items"] + rest["items"]
    assert [e["type"] for e in everything] == ["run_created", "run_claimed", "task_started", "task_succeeded", "task_started", "task_succeeded", "run_completed"]
    assert [e["id"] for e in everything] == sorted(e["id"] for e in everything) and rest["next_after"] is None
    assert {e["actor"] for e in everything} <= {"user", "api", "worker", "recovery"}
    dump = json.dumps([page, rest, run])
    owner = api.db.scalar("SELECT owner_id FROM run_leases")
    assert owner not in dump and "run_leases" not in dump and "owner" not in dump.replace("ownership", "")  # no process identity ever leaves the server
    assert set(everything[0]) == {"id", "type", "task", "execution_id", "actor", "detail", "created_at"}
    for bad in ("after=-1", "limit=0", "limit=201"):
        assert api.client.get(f"/plan-runs/{run['id']}/events?{bad}", headers=api.auth(*A)).status_code == 422
    assert api.client.get(f"/plan-runs/{run['id']}/events", headers=api.auth(*B)).status_code == 404  # foreign: indistinguishable from unknown
    assert api.client.get(f"/plan-runs/{run['id']}/events").status_code == 401


def test_a_running_run_reports_that_it_is_owned_without_saying_by_whom(papi):
    api = papi
    api.client.post("/agents", json={"slug": "writer", "definition": copy.deepcopy(AGENT)}, headers=api.auth(*A))
    pid = make_plan(api, [task("a", "writer")])
    gate, started = threading.Event(), threading.Event()
    api.provider.gate, api.provider.started = gate, started  # only after planning: the planner uses the same provider
    run = start(api, pid).json()
    assert started.wait(5)
    live = api.client.get(f"/plan-runs/{run['id']}", headers=api.auth(*A)).json()
    assert live["status"] == "running" and live["ownership"] == "live" and live["retry_options"] is None
    gate.set()
    done = wait(api, run["id"])
    assert done["ownership"] is None and done["status"] == "succeeded"  # only running runs have an ownership state
