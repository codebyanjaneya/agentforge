"""M3-A: the plans REST contract through the real app (fake provider, no network): auth, isolation, error mapping, no leaks."""

import copy
import json
import sys
import threading

import pytest

from agentplatform.execution import Limits
from tests.api_support import api, auth_db, main_module  # noqa: F401  (fixtures)

A, B = ("user-a", "a@example.com"), ("user-b", "b@example.com")
OBJECTIVE = "Launch our new product to early customers within a month."
AGENT = {"name": "Release Notes Writer", "description": "Turns a changelog into friendly release notes for customers.", "role": "Product marketer",
         "purpose": "Explain what changed and why it matters, without hype.", "instructions": "You write clear release notes from the changelog the user provides.",
         "model": {"provider": "groq", "name": "llama-3.3-70b-versatile"}}


def task(id, agent, depends_on=(), **extra):
    return {"id": id, "title": f"Task {id}", "description": f"Do the work for task {id} carefully.", "expected_output": "A short written result.",
            "agent": agent, "rationale": "This agent fits the work.", "depends_on": list(depends_on), **extra}


def reply(*tasks, feasible=True, unsupported=()):
    return json.dumps({"feasible": feasible, "interpretation": "The user wants a launch plan for the product.", "tasks": list(tasks), "unsupported": list(unsupported)})


def default_reply(request):
    return reply(task("research", "research"), task("draft", "sales", ["research"]), task("review", "ceo", ["draft"]))


def create(api, who=A, objective=OBJECTIVE):
    return api.client.post("/plans", json={"objective": objective}, headers=api.auth(*who))


def make_plan(api, who=A):
    api.provider.reply = default_reply
    r = create(api, who)
    assert r.status_code == 201, r.text
    return r.json()


def revise_body(base=1, *tasks):
    return {"base_revision": base, "tasks": [{k: v for k, v in t.items() if k != "rationale"} for t in tasks]}


def provider_error(code):
    if code == "internal_error":
        return RuntimeError("SECRET-INTERNAL gsk_abcdefghijklmnopqrstuvwxyz123456")
    return sys.modules["agentplatform.providers"].ProviderError(code)


def counts(api):
    return {t: api.db.scalar(f"SELECT count(*) FROM {t}") for t in ("plans", "plan_revisions", "plan_tasks", "agent_executions")}


# ------------------------------------------------------------------------------------------------ create / read

def test_creating_a_plan_returns_a_validated_plan_and_executes_nothing(api):
    body = make_plan(api)
    assert (body["status"], body["latest_revision"], body["approved_revision"], body["objective"]) == ("draft", 1, None, OBJECTIVE)
    rev = body["revision"]
    assert rev["levels"] == [["research"], ["draft"], ["review"]] and rev["origin"] == "model" and rev["interpretation"]
    assert [(t["id"], t["level"], t["depends_on"], t["agent"]["source"], t["agent"]["version"]) for t in rev["tasks"]] == [
        ("research", 0, [], "builtin", None), ("draft", 1, ["research"], "builtin", None), ("review", 2, ["draft"], "builtin", None)]
    assert body["execution"]["started"] is False
    assert counts(api) == {"plans": 1, "plan_revisions": 1, "plan_tasks": 3, "agent_executions": 0}
    assert api.client.get(f"/plans/{body['id']}", headers=api.auth(*A)).json() == body


def test_a_custom_agent_is_pinned_in_the_response(api):
    assert api.client.post("/agents", json={"slug": "writer", "definition": AGENT}, headers=api.auth(*A)).status_code == 201
    api.provider.reply = lambda request: reply(task("t1", "writer"))
    t = create(api).json()["revision"]["tasks"][0]
    assert t["agent"] == {"source": "custom", "key": "writer", "name": "Release Notes Writer", "version": 1, "version_id": __import__("uuid").UUID(api.db.scalar("SELECT id FROM custom_agent_versions")).__str__()}


def test_listing_and_revision_history(api):
    plan = make_plan(api)
    listing = api.client.get("/plans", headers=api.auth(*A)).json()
    assert [(i["id"], i["task_count"], i["status"]) for i in listing["items"]] == [(plan["id"], 3, "draft")] and listing["next_cursor"] is None
    assert api.client.post(f"/plans/{plan['id']}/revisions", json=revise_body(1, task("a", "research")), headers=api.auth(*A)).status_code == 200
    items = api.client.get(f"/plans/{plan['id']}/revisions", headers=api.auth(*A)).json()["items"]
    assert [(i["revision"], i["origin"]) for i in items] == [(2, "revised"), (1, "model")]
    old = api.client.get(f"/plans/{plan['id']}/revisions/1", headers=api.auth(*A)).json()
    assert [t["id"] for t in old["tasks"]] == ["research", "draft", "review"]
    assert api.client.get(f"/plans/{plan['id']}/revisions/9", headers=api.auth(*A)).json()["detail"]["code"] == "revision_not_found"
    assert api.client.get("/plans?limit=0", headers=api.auth(*A)).status_code == 422
    assert api.client.get("/plans?status=bogus", headers=api.auth(*A)).status_code == 422
    assert api.client.get("/plans?cursor=garbage", headers=api.auth(*A)).json()["detail"]["code"] == "invalid_cursor"


# ------------------------------------------------------------------------------------------------ lifecycle

def test_validate_revise_approve_cancel(api):
    plan = make_plan(api)
    pid, h = plan["id"], api.auth(*A)
    report = api.client.post(f"/plans/{pid}/validate", headers=h).json()
    assert report["valid"] is True and report["errors"] == []
    approved = api.client.post(f"/plans/{pid}/approve", json={"revision": 1}, headers=h).json()
    assert (approved["status"], approved["approved_revision"], approved["execution"]["started"]) == ("approved", 1, False)
    assert counts(api)["agent_executions"] == 0 and len(api.provider.requests) == 1  # approval calls no model, runs nothing
    assert api.client.post(f"/plans/{pid}/approve", json={"revision": 3}, headers=h).json()["detail"] == {"code": "revision_conflict", "latest_revision": 1}
    revised = api.client.post(f"/plans/{pid}/revisions", json=revise_body(1, task("a", "research")), headers=h).json()
    assert (revised["status"], revised["approved_revision"], revised["latest_revision"]) == ("draft", None, 2)
    stale = api.client.post(f"/plans/{pid}/revisions", json=revise_body(1, task("b", "research")), headers=h)
    assert stale.status_code == 409 and stale.json()["detail"] == {"code": "revision_conflict", "latest_revision": 2}
    cancelled = api.client.post(f"/plans/{pid}/cancel", headers=h).json()
    assert cancelled["status"] == "cancelled"
    for r in (api.client.post(f"/plans/{pid}/approve", json={"revision": 2}, headers=h), api.client.post(f"/plans/{pid}/revisions", json=revise_body(2, task("c", "research")), headers=h)):
        assert (r.status_code, r.json()["detail"]["code"]) == (409, "plan_cancelled")


def test_invalid_revisions_are_422_with_fixed_codes_and_change_nothing(api):
    plan = make_plan(api)
    r = api.client.post(f"/plans/{plan['id']}/revisions", json=revise_body(1, task("a", "research", ["b"]), task("b", "nobody", ["a"])), headers=api.auth(*A))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "plan_rejected"
    assert "agent_unavailable" in {e["code"] for e in r.json()["detail"]["errors"]}
    r = api.client.post(f"/plans/{plan['id']}/revisions", json=revise_body(1, task("a", "research", ["b"]), task("b", "sales", ["a"])), headers=api.auth(*A))
    assert {e["code"] for e in r.json()["detail"]["errors"]} == {"cycle"}
    for bad in ({"base_revision": "1", "tasks": []}, {"base_revision": 1, "tasks": []}, {"base_revision": 0, "tasks": [task("a", "research")]},
                {"base_revision": 1, "tasks": [task("a", "research")], "user_id": "x"}, {"base_revision": 1, "tasks": [{**task("a", "research"), "state": "running"}]}):
        assert api.client.post(f"/plans/{plan['id']}/revisions", json=bad, headers=api.auth(*A)).status_code == 422, bad
    assert counts(api)["plan_revisions"] == 1


def test_approval_fails_when_an_agent_was_archived_after_planning(api):
    api.client.post("/agents", json={"slug": "writer", "definition": AGENT}, headers=api.auth(*A))
    api.provider.reply = lambda request: reply(task("t1", "writer"))
    pid = create(api).json()["id"]
    assert api.client.post(f"/agents/{api.client.get('/agents', headers=api.auth(*A)).json()['items'][0]['id']}/archive", headers=api.auth(*A)).status_code == 200
    r = api.client.post(f"/plans/{pid}/approve", json={"revision": 1}, headers=api.auth(*A))
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "plan_invalid") and r.json()["detail"]["errors"][0]["code"] == "agent_unavailable"
    assert api.client.get(f"/plans/{pid}", headers=api.auth(*A)).json()["status"] == "draft"


# ------------------------------------------------------------------------------------------------ failures

@pytest.mark.parametrize("status,code", [(504, "timeout"), (429, "rate_limited"), (503, "unavailable"), (503, "auth"), (503, "unsupported_model"),
                                         (502, "bad_request"), (502, "invalid_response"), (500, "internal_error")])
def test_provider_failures_are_safe_and_store_nothing(api, status, code):
    api.provider.error = provider_error(code)
    r = create(api)
    assert r.status_code == status and r.json()["detail"]["code"] == code
    assert "SECRET" not in r.text and "gsk_" not in r.text and "Traceback" not in r.text
    assert counts(api)["plans"] == 0 and len(api.provider.requests) == 1


def test_malformed_model_output_is_a_safe_502_and_never_echoed(api):
    api.provider.reply = lambda request: "SECRET-RAW-OUTPUT sk-abcdefghijklmnopqrstuvwxyz123456 not json"
    r = create(api)
    assert (r.status_code, r.json()["detail"]["code"]) == (502, "invalid_output")
    assert "SECRET-RAW" not in r.text and "sk-" not in r.text and counts(api)["plans"] == 0


def test_a_plan_the_validator_rejects_is_422_and_stores_nothing(api):
    api.provider.reply = lambda request: reply(task("a", "research", ["b"]), task("b", "sales", ["a"]))
    r = create(api)
    assert (r.status_code, r.json()["detail"]["code"]) == (422, "plan_rejected") and [e["code"] for e in r.json()["detail"]["errors"]] == ["cycle"]
    assert counts(api)["plans"] == 0


def test_an_unplannable_objective_is_explained(api):
    api.provider.reply = lambda request: json.dumps({"feasible": False, "interpretation": "Charge customers' cards.", "tasks": [],
                                                     "unsupported": [{"requirement": "Charge cards", "reason": "No agent can take payments."}]})
    r = create(api, objective="Charge every customer's card for the annual plan today.")
    detail = r.json()["detail"]
    assert (r.status_code, detail["code"]) == (422, "objective_not_plannable") and detail["unsupported"][0]["requirement"] == "Charge cards"


def test_invalid_objectives_are_422_without_a_model_call(api):
    for bad in ("short", "x" * 2001, "bad\x00objective here", None, 5):
        assert create(api, objective=bad).status_code == 422, repr(bad)
    assert api.client.post("/plans", json={"objective": OBJECTIVE, "user_id": "evil"}, headers=api.auth(*A)).status_code == 422
    assert api.provider.requests == [] and counts(api)["plans"] == 0


def test_a_busy_server_refuses_with_retry_after(api):
    api.main.execution_limits = Limits(total=1, per_user=1)
    gate, started = threading.Event(), threading.Event()
    api.provider.gate, api.provider.started = gate, started
    api.provider.reply = default_reply
    first = {}
    thread = threading.Thread(target=lambda: first.update(r=create(api)))
    thread.start()
    assert started.wait(5)
    r = create(api)
    assert r.status_code == 429 and r.headers["retry-after"] == "5" and r.json()["detail"]["code"] == "busy"
    gate.set()
    thread.join(5)
    assert first["r"].status_code == 201 and counts(api)["plans"] == 1


# ------------------------------------------------------------------------------------------------ auth / isolation

ROUTES = [("POST", "/plans", {"objective": OBJECTIVE}), ("GET", "/plans", None), ("GET", "/plans/{id}", None), ("GET", "/plans/{id}/revisions", None),
          ("GET", "/plans/{id}/revisions/1", None), ("POST", "/plans/{id}/validate", None), ("POST", "/plans/{id}/revisions", None),
          ("POST", "/plans/{id}/approve", {"revision": 1}), ("POST", "/plans/{id}/cancel", None)]


def test_every_route_requires_a_valid_token(api):
    pid = make_plan(api)["id"]
    for method, path, body in ROUTES:
        url = path.replace("{id}", pid)
        for headers in ({}, {"Authorization": "Bearer garbage"}, {"Authorization": "Basic abc"}):
            r = api.client.request(method, url, json=body, headers=headers)
            assert r.status_code == 401, (method, url, headers)
    assert counts(api)["plans"] == 1


def test_other_users_get_404_for_every_plan_route_and_nothing_changes(api):
    plan = make_plan(api)
    api.provision(*B)
    before = api.db.rows("SELECT * FROM plans")
    for method, path, body in ROUTES[2:]:
        body = revise_body(1, task("a", "research")) if path.endswith("/revisions") and method == "POST" else body
        r = api.client.request(method, path.replace("{id}", plan["id"]), json=body, headers=api.auth(*B))
        assert (r.status_code, r.json()["detail"]["code"]) == (404, "not_found" if "/revisions/1" not in path else "not_found"), (method, path)
    assert api.client.get("/plans", headers=api.auth(*B)).json()["items"] == []
    assert api.db.rows("SELECT * FROM plans") == before


def test_identity_and_workspace_come_only_from_the_verified_token_and_membership(api):
    plan = make_plan(api)
    _, wid_b = api.provision(*B)
    r = api.client.get("/plans", headers={**api.auth(*B), "X-Workspace-Id": str(api.db.scalar("SELECT id FROM workspaces WHERE personal_for_user_id IS NOT NULL LIMIT 1")) })
    assert r.status_code in (400, 403, 404)  # a workspace you are not a member of is refused, never read
    wa = api.db.scalar("SELECT workspace_id FROM plans")
    r = api.client.get(f"/plans/{plan['id']}", headers={**api.auth(*B), "X-Workspace-Id": str(__import__('uuid').UUID(wa))})
    assert r.status_code in (400, 403, 404)
    for forged in ({"objective": OBJECTIVE, "workspace_id": str(wid_b)}, {"objective": OBJECTIVE, "owner": "x"}):
        assert api.client.post("/plans", json=forged, headers=api.auth(*B)).status_code == 422
    assert api.db.scalar("SELECT count(*) FROM plans WHERE user_id != (SELECT user_id FROM plans LIMIT 1)") == 0


def test_the_stored_objective_never_keeps_a_secret(api):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    api.provider.reply = default_reply
    body = create(api, objective=f"Plan our launch; our test key is {secret} and must not be shared.").json()
    assert secret not in json.dumps(body) and secret not in str(api.db.rows("SELECT * FROM plans"))


# ------------------------------------------------------------------------------------------------ regression

def test_accepted_contracts_still_work_beside_plans(api):
    make_plan(api)
    assert api.client.get("/tasks", headers=api.auth(*A)).status_code == 200
    assert api.client.get("/agents", headers=api.auth(*A)).status_code == 200
    assert api.client.get("/executions", headers=api.auth(*A)).status_code == 200
    api.client.post("/agents", json={"slug": "writer", "definition": copy.deepcopy(AGENT)}, headers=api.auth(*A))
    api.provider.reply = lambda request: "custom reply"
    r = api.client.post("/autonomous/run", json={"agent": "writer", "task": "hello"}, headers=api.auth(*A))
    assert r.status_code == 200 and r.json()["agent"] == "writer"
    assert any(p.startswith("/plans") for p in api.main.app.openapi()["paths"])


def test_plan_detail_keeps_every_existing_field_and_latest_run_is_null_without_a_run(api):
    body = make_plan(api)
    old = {"id", "objective", "status", "latest_revision", "approved_revision", "created_at", "updated_at", "approved_at", "revision", "execution"}
    assert old <= set(body) and set(body) == old | {"latest_run", "created_by"}  # created_by (sprint 5): the creator's e-mail, shown to every member of the workspace
    assert body["latest_run"] is None and body["execution"] == {"started": False}  # the static "not executed yet" note is gone
    for response in (api.client.get(f"/plans/{body['id']}", headers=api.auth(*A)), api.client.post(f"/plans/{body['id']}/approve", json={"revision": 1}, headers=api.auth(*A)),
                     api.client.post(f"/plans/{body['id']}/cancel", headers=api.auth(*A))):
        assert response.status_code == 200 and response.json()["latest_run"] is None and response.json()["execution"] == {"started": False}
