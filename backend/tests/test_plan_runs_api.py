"""M3-B: the plan-run REST contract through the real app (fake provider and fake built-in agents, no network): the approval gate,
polling progress, cancellation, failures, authentication and isolation."""

import copy
import json
import threading
import time

import anyio.from_thread
import pytest

from tests.api_support import api, auth_db, main_module  # noqa: F401  (fixtures)

A, B = ("user-a", "a@example.com"), ("user-b", "b@example.com")
OBJECTIVE = "Launch our new product to early customers within a month."
AGENT = {"name": "Release Notes Writer", "description": "Turns a changelog into friendly release notes for customers.", "role": "Product marketer",
         "purpose": "Explain what changed and why it matters, without hype.", "instructions": "You write clear release notes from the changelog the user provides.",
         "model": {"provider": "groq", "name": "llama-3.3-70b-versatile"}}


def task(id, agent, depends_on=(), **extra):
    return {"id": id, "title": f"Task {id}", "description": f"Do the work for task {id} carefully.", "expected_output": "A short written result.",
            "agent": agent, "rationale": "This agent fits the work.", "depends_on": list(depends_on), **extra}


@pytest.fixture
def papi(api):
    """One event loop for every request (a real server has one): the background scheduler of a run survives between requests."""
    manager = anyio.from_thread.start_blocking_portal()
    api.client.portal = manager.__enter__()
    yield api
    manager.__exit__(None, None, None)


def make_plan(api, tasks, who=A, approve=True):
    api.provider.reply = lambda request: json.dumps({"feasible": True, "interpretation": "A launch plan for the product.", "tasks": list(tasks), "unsupported": []})
    r = api.client.post("/plans", json={"objective": OBJECTIVE}, headers=api.auth(*who))
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    api.provider.reply = lambda request: f"custom reply: {request.messages[-1].content[-40:]}"
    if approve:
        assert api.client.post(f"/plans/{pid}/approve", json={"revision": 1}, headers=api.auth(*who)).status_code == 200
    return pid


def start(api, pid, who=A, **kw):
    return api.client.post(f"/plans/{pid}/runs", headers=api.auth(*who), **kw)


def wait(api, run_id, who=A, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = api.client.get(f"/plan-runs/{run_id}", headers=api.auth(*who)).json()
        if body["status"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("the run did not finish")


def counts(api):
    return {t: api.db.scalar(f"SELECT count(*) FROM {t}") for t in ("plan_runs", "plan_task_runs", "agent_executions")}


CHAIN = [task("a", "research"), task("b", "sales", ["a"]), task("c", "ceo", ["b"])]


# ------------------------------------------------------------------------------------------------ gate and success

def test_an_approved_plan_runs_in_order_and_progress_is_pollable(papi):
    api = papi
    pid = make_plan(api, CHAIN)
    r = start(api, pid)
    assert r.status_code == 202
    started = r.json()
    assert (started["status"], started["revision"], started["attempt"], started["plan_id"]) == ("running", 1, 1, pid) and started["counts"]["total"] == 3
    body = wait(api, started["id"])
    assert body["status"] == "succeeded" and body["error"] is None and body["completed_at"]
    assert [(t["id"], t["status"], t["level"], t["depends_on"]) for t in body["tasks"]] == [
        ("a", "succeeded", 0, []), ("b", "succeeded", 1, ["a"]), ("c", "succeeded", 2, ["b"])]
    assert all(t["execution_id"] and t["started_at"] and t["completed_at"] and t["agent"]["source"] == "builtin" for t in body["tasks"])
    assert body["counts"]["succeeded"] == 3 and [o["task"] for o in body["outputs"]] == ["c"] and body["outputs"][0]["result"].startswith("reply from ceo:")
    detail = api.client.get(f"/plan-runs/{started['id']}/tasks/b", headers=api.auth(*A)).json()
    assert detail["id"] == "b" and detail["execution"]["status"] == "succeeded" and detail["execution"]["result"].startswith("reply from sales:")
    assert "reply from research" in detail["execution"]["input"] and detail["execution"]["id"] == detail["execution_id"]
    listing = api.client.get(f"/plans/{pid}/runs", headers=api.auth(*A)).json()["items"]
    assert [(i["id"], i["status"], i["attempt"]) for i in listing] == [(started["id"], "succeeded", 1)]
    # task executions are ordinary M2-B records: they also appear in the execution history
    history = api.client.get("/executions", headers=api.auth(*A)).json()["items"]
    assert sorted(i["agent_key"] for i in history) == ["ceo", "research", "sales"]
    assert api.client.get(f"/plan-runs/{started['id']}/tasks/zzz", headers=api.auth(*A)).json()["detail"]["code"] == "task_not_found"


def test_execution_requires_an_approved_plan_and_never_trusts_the_client(papi):
    api = papi
    draft = make_plan(api, CHAIN, approve=False)
    r = start(api, draft)
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "plan_not_approved")
    api.client.post(f"/plans/{draft}/approve", json={"revision": 1}, headers=api.auth(*A))
    api.client.post(f"/plans/{draft}/cancel", headers=api.auth(*A))
    assert start(api, draft).json()["detail"]["code"] == "plan_cancelled"
    pid = make_plan(api, CHAIN)
    for forged in ({"revision": 2}, {"user_id": "x"}, {"workspace_id": "x"}, {"agent_version": 1}, {"tasks": []}):
        assert start(api, pid, json=forged).status_code == 422, forged
    assert counts(api) == {"plan_runs": 0, "plan_task_runs": 0, "agent_executions": 0}
    assert start(api, pid, json={}).status_code == 202  # an empty body is fine


def test_a_succeeded_revision_is_not_executed_twice_and_a_running_plan_cannot_be_started_again(papi):
    api = papi
    api.client.post("/agents", json={"slug": "writer", "definition": copy.deepcopy(AGENT)}, headers=api.auth(*A))
    pid = make_plan(api, [task("a", "writer")])
    gate, started = threading.Event(), threading.Event()
    api.provider.gate, api.provider.started = gate, started
    first = start(api, pid)
    assert first.status_code == 202 and started.wait(5)
    second = start(api, pid)
    assert (second.status_code, second.json()["detail"]["code"]) == (409, "run_in_progress")
    gate.set()
    assert wait(api, first.json()["id"])["status"] == "succeeded"
    third = start(api, pid)
    assert (third.status_code, third.json()["detail"]["code"]) == (409, "already_executed")
    assert counts(api)["plan_runs"] == 1


def test_custom_agent_tasks_execute_the_pinned_version(papi):
    api = papi
    created = api.client.post("/agents", json={"slug": "writer", "definition": copy.deepcopy(AGENT)}, headers=api.auth(*A)).json()
    pid = make_plan(api, [task("a", "writer")])
    edited = copy.deepcopy(AGENT)
    edited["role"] = "A COMPLETELY DIFFERENT ROLE"
    assert api.client.patch(f"/agents/{created['id']}", json={"base_version": 1, "definition": edited}, headers=api.auth(*A)).status_code == 200
    body = wait(api, start(api, pid).json()["id"])
    assert body["status"] == "succeeded" and body["tasks"][0]["agent"]["version"] == 1
    system = api.provider.requests[-1].system
    assert "Product marketer" in system and "COMPLETELY DIFFERENT" not in system
    assert api.db.scalar("SELECT agent_version FROM agent_executions") == 1


def test_an_archived_agent_refuses_the_start_with_a_clear_error(papi):
    api = papi
    created = api.client.post("/agents", json={"slug": "writer", "definition": copy.deepcopy(AGENT)}, headers=api.auth(*A)).json()
    pid = make_plan(api, [task("a", "writer")])
    assert api.client.post(f"/agents/{created['id']}/archive", headers=api.auth(*A)).status_code == 200
    r = start(api, pid)
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "plan_invalid") and r.json()["detail"]["errors"][0]["code"] == "agent_unavailable"
    assert counts(api)["plan_runs"] == 0


# ------------------------------------------------------------------------------------------------ failure and cancellation

def test_failures_are_honest_safe_and_skip_dependants(papi):
    api = papi
    api.client.post("/agents", json={"slug": "writer", "definition": copy.deepcopy(AGENT)}, headers=api.auth(*A))
    pid = make_plan(api, [task("a", "writer"), task("b", "research", ["a"]), task("c", "sales")])
    import sys

    api.provider.error = sys.modules["agentplatform.providers"].ProviderError("timeout")
    r = start(api, pid)
    body = wait(api, r.json()["id"])
    assert body["status"] == "partial" and {t["id"]: t["status"] for t in body["tasks"]} == {"a": "failed", "b": "skipped", "c": "succeeded"}
    by = {t["id"]: t for t in body["tasks"]}
    assert by["a"]["error"]["code"] == "timeout" and by["b"]["error"]["code"] == "dependency_failed" and by["a"]["error"]["message"]
    assert "Traceback" not in json.dumps(body) and "SECRET" not in json.dumps(body)
    assert body["outputs"][0]["task"] == "c"  # the partial result still shows what did complete
    assert api.db.scalar("SELECT count(*) FROM agent_executions") == 2  # a (failed) and c; b never ran


def test_cancelling_a_run_over_rest(papi):
    api = papi
    api.client.post("/agents", json={"slug": "writer", "definition": copy.deepcopy(AGENT)}, headers=api.auth(*A))
    pid = make_plan(api, [task("a", "writer"), task("b", "research", ["a"])])
    gate, started = threading.Event(), threading.Event()
    api.provider.gate, api.provider.started = gate, started
    run = start(api, pid).json()
    assert started.wait(5)
    cancelled = api.client.post(f"/plan-runs/{run['id']}/cancel", headers=api.auth(*A))
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert {t["id"]: t["status"] for t in cancelled.json()["tasks"]} == {"a": "cancelled", "b": "cancelled"}
    assert api.db.scalar("SELECT status FROM agent_executions") == "cancelled"
    assert api.client.post(f"/plan-runs/{run['id']}/cancel", headers=api.auth(*A)).json()["status"] == "cancelled"  # idempotent
    gate.set()
    assert start(api, pid).status_code == 202  # explicit new attempt


# ------------------------------------------------------------------------------------------------ auth and isolation

ROUTES = [("POST", "/plans/{id}/runs"), ("GET", "/plans/{id}/runs"), ("GET", "/plan-runs/{run}"), ("GET", "/plan-runs/{run}/tasks/a"), ("POST", "/plan-runs/{run}/cancel")]


def test_every_route_requires_a_valid_token(papi):
    api = papi
    pid = make_plan(api, CHAIN)
    run = start(api, pid).json()["id"]
    wait(api, run)
    for method, path in ROUTES:
        url = path.replace("{id}", pid).replace("{run}", run)
        for headers in ({}, {"Authorization": "Bearer garbage"}, {"Authorization": "Basic abc"}):
            assert api.client.request(method, url, headers=headers).status_code == 401, (method, url)


def test_other_users_get_404_everywhere_and_nothing_changes(papi):
    api = papi
    pid = make_plan(api, CHAIN)
    run = start(api, pid).json()["id"]
    wait(api, run)
    api.provision(*B)
    before = (api.db.rows("SELECT * FROM plan_runs"), api.db.rows("SELECT * FROM plan_task_runs"))
    for method, path in ROUTES:
        r = api.client.request(method, path.replace("{id}", pid).replace("{run}", run), headers=api.auth(*B))
        assert (r.status_code, r.json()["detail"]["code"]) == (404, "not_found"), (method, path)
    assert (api.db.rows("SELECT * FROM plan_runs"), api.db.rows("SELECT * FROM plan_task_runs")) == before
    other = make_plan(api, CHAIN, who=B)
    assert start(api, other, who=B).status_code == 202  # B's own plan is unaffected by A's run
    import uuid

    assert api.client.get(f"/plan-runs/{uuid.uuid4()}", headers=api.auth(*A)).status_code == 404  # unknown == foreign


def test_accepted_contracts_still_work_beside_plan_runs(papi):
    api = papi
    pid = make_plan(api, CHAIN)
    wait(api, start(api, pid).json()["id"])
    assert api.client.get("/plans", headers=api.auth(*A)).status_code == 200
    assert api.client.get(f"/plans/{pid}", headers=api.auth(*A)).json()["status"] == "approved"  # a run does not change the plan header
    r = api.client.post("/autonomous/run", json={"agent": "research", "task": "hello"}, headers=api.auth(*A))
    assert r.status_code == 200 and r.json()["agent"] == "research"
    paths = api.main.app.openapi()["paths"]
    assert {"/plans/{plan_id}/runs", "/plan-runs/{run_id}", "/plan-runs/{run_id}/tasks/{task_key}", "/plan-runs/{run_id}/cancel"} <= set(paths)


def test_plan_detail_reports_the_latest_run(papi):
    api = papi
    api.client.post("/agents", json={"slug": "writer", "definition": copy.deepcopy(AGENT)}, headers=api.auth(*A))
    pid = make_plan(api, [task("a", "writer")])
    assert api.client.get(f"/plans/{pid}", headers=api.auth(*A)).json()["latest_run"] is None
    gate, started = threading.Event(), threading.Event()
    api.provider.gate, api.provider.started = gate, started
    first = start(api, pid).json()
    assert started.wait(5)
    running = api.client.get(f"/plans/{pid}", headers=api.auth(*A)).json()
    assert running["execution"] == {"started": True} and running["latest_run"]["id"] == first["id"] and running["latest_run"]["status"] == "running"
    api.provider.error = __import__("sys").modules["agentplatform.providers"].ProviderError("timeout")
    gate.set()
    assert wait(api, first["id"])["status"] == "failed"
    api.provider.gate = None
    second = start(api, pid).json()
    wait(api, second["id"])
    detail = api.client.get(f"/plans/{pid}", headers=api.auth(*A)).json()
    assert (detail["latest_run"]["id"], detail["latest_run"]["status"], detail["latest_run"]["attempt"]) == (second["id"], "failed", 2)  # the newest attempt
    assert detail["status"] == "approved" and detail["approved_revision"] == 1 and detail["revision"]["revision"] == 1  # a run never changes the plan
    api.provider.error = None
    api.provider.reply = lambda request: "ok"
    assert api.client.post(f"/plans/{pid}/cancel", headers=api.auth(*A)).json()["latest_run"]["id"] == second["id"]
    api.provision(*B)
    assert api.client.get(f"/plans/{pid}", headers=api.auth(*B)).status_code == 404  # no run summary leaks to another user


def test_a_stopping_process_refuses_new_runs_with_a_fixed_503_and_changes_nothing(papi, monkeypatch):
    """M4-A1: after graceful shutdown began (the registry is closing) no new run work is accepted; the error is fixed text."""
    api = papi
    pid = make_plan(api, CHAIN)
    monkeypatch.setattr(api.main.plan_run_registry, "closing", True)
    r = start(api, pid)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "shutting_down" and "Please try again" in r.json()["detail"]["message"]
    assert counts(api) == {"plan_runs": 0, "plan_task_runs": 0, "agent_executions": 0}
    monkeypatch.setattr(api.main.plan_run_registry, "closing", False)
    assert start(api, pid).status_code == 202
