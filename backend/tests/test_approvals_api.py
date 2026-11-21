"""M4-C: the approval REST contract through the real app (scripted model, no network). The simulated fixture tool is registered into the real default
registry for the duration of each test only."""

import json
import threading
import time

import pytest

from agentplatform.providers import ProviderRegistry, ProviderResponse, ToolCall
from tests.api_support import api, auth_db, main_module  # noqa: F401  (fixtures)
from tests.test_plan_runs_api import A, AGENT, B, papi  # noqa: F401  (papi is a fixture)
from tests.test_tools_api import Scripted, agent_with_tools, get

ARGS = {"recipient": "customer@example.com", "subject": "Launch date", "body": "The launch is confirmed for next week."}


@pytest.fixture
def demo(papi):  # noqa: F811
    """The simulated fixture tool, registered into the LIVE default registry of this test's app (the fixture reloads the modules) and removed afterwards."""
    import importlib

    tools = importlib.import_module("agentplatform.tools")
    fx = importlib.import_module("agentplatform.tools.fixtures")
    registry = tools.default_registry()
    registry.register(fx.DEMO_SEND_NOTE, fx.send_note)
    fx.OUTBOX.clear()
    yield fx
    registry._tools.pop(("demo.send_note", 1), None)


def post(api, path, who=A, **kw):
    return api.client.post(path, headers=api.auth(*who), **kw)


def run_in_thread(api, slug, who=A):
    box = {}

    def go():
        box["response"] = api.client.post("/autonomous/run", json={"agent": slug, "task": "Tell the customer the launch date."}, headers=api.auth(*who))

    thread = threading.Thread(target=go)
    thread.start()
    return thread, box


def wait_pending(api, who=A, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        items = get(api, "/approvals?status=pending", who=who).json()["items"]
        if items:
            return items[0]
        time.sleep(0.05)
    raise AssertionError("no approval became pending")


def script(api):
    provider = Scripted(ProviderResponse(text="The customer must be told.", model="m", tool_calls=(ToolCall("c1", "demo__send_note__v1", json.dumps(ARGS)),)),
                        ProviderResponse(text="Done: the note was sent (simulated).", model="m"))
    api.main.providers = ProviderRegistry({"groq": lambda: provider})
    return provider


def test_every_approval_route_requires_authentication(papi, demo):
    api = papi
    for method, path in (("get", "/approvals"), ("get", "/approvals/00000000-0000-4000-8000-000000000001"), ("post", "/approvals/00000000-0000-4000-8000-000000000001/approve"),
                         ("post", "/approvals/00000000-0000-4000-8000-000000000001/reject")):
        assert getattr(api.client, method)(path).status_code == 401, path


def test_an_approval_is_requested_shown_decided_and_the_exact_action_runs_once(papi, demo):
    api = papi
    created = agent_with_tools(api, ("demo.send_note",))
    assert created["definition"]["allowed_tools"] == ["demo.send_note@1"]
    catalog = {t["id"]: t for t in get(api, "/tools").json()["items"]}["demo.send_note"]
    assert (catalog["latest"]["requires_approval"], catalog["latest"]["grantable"], catalog["latest"]["executable"]) == (True, True, False)
    provider = script(api)
    thread, box = run_in_thread(api, "analyst")
    pending = wait_pending(api)
    assert pending["status"] == "pending" and demo.OUTBOX == []  # paused: nothing has run
    assert pending["tool"]["ref"] == "demo.send_note@1" and pending["agent"] == "analyst" and pending["arguments"] == ARGS and pending["reason"] == "The customer must be told."
    assert pending["reversible"] is None and "may not be reversible" in pending["impact"] and len(pending["payload_hash"]) == 64
    assert get(api, "/approvals").json()["pending"] == 1 and get(api, "/approvals", who=B).json() == {"items": [], "pending": 0, "can_decide": True}
    assert get(api, f"/approvals/{pending['id']}", who=B).status_code == 404  # another workspace: unknown
    assert post(api, f"/approvals/{pending['id']}/approve", who=B).status_code == 404  # and it cannot decide
    assert post(api, f"/approvals/{pending['id']}/approve", json={"payload_hash": "0" * 64}).status_code == 409  # shown something else: stale
    assert post(api, f"/approvals/{pending['id']}/approve", json={"payload_hash": "xyz"}).status_code == 422 and post(api, f"/approvals/{pending['id']}/approve", json={"who": "me"}).status_code == 422
    assert get(api, f"/approvals/{pending['id']}").json()["status"] == "pending" and demo.OUTBOX == []
    decided = post(api, f"/approvals/{pending['id']}/approve", json={"payload_hash": pending["payload_hash"]})
    assert decided.status_code == 200 and decided.json()["status"] == "approved"
    thread.join(30)
    assert box["response"].status_code == 200 and box["response"].json()["status"] == "succeeded"
    assert demo.OUTBOX == [("customer@example.com", "Launch date", "The launch is confirmed for next week.")]  # exactly once, exactly these values
    final = get(api, f"/approvals/{pending['id']}").json()
    assert final["status"] == "consumed" and final["invocation_id"]
    invocation = get(api, f"/tool-invocations/{final['invocation_id']}").json()
    assert (invocation["status"], invocation["tool"]["id"]) == ("succeeded", "demo.send_note")
    # replays and reversals are refused
    for path in ("approve", "reject"):
        again = post(api, f"/approvals/{pending['id']}/{path}")
        assert again.status_code == 409 and again.json()["detail"]["code"] == "approval_consumed"
    assert len(demo.OUTBOX) == 1 and len(provider.requests) == 2


def test_a_rejected_action_never_runs_and_the_agent_is_told(papi, demo):
    api = papi
    agent_with_tools(api, ("demo.send_note",))
    provider = script(api)
    thread, box = run_in_thread(api, "analyst")
    pending = wait_pending(api)
    assert post(api, f"/approvals/{pending['id']}/reject").json()["status"] == "rejected"
    thread.join(30)
    assert box["response"].status_code == 200 and demo.OUTBOX == []
    assert json.loads([m for m in provider.requests[-1].messages if m.role == "tool"][0].content)["error"] == "approval_rejected"
    assert post(api, f"/approvals/{pending['id']}/approve").json()["detail"]["code"] == "approval_rejected"  # a rejection cannot be reversed into an approval
    assert get(api, "/approvals?status=rejected").json()["items"][0]["id"] == pending["id"] and get(api, "/approvals?status=bogus").status_code == 400


def test_an_agent_that_was_not_granted_the_tool_cannot_ask_for_it_and_no_approval_is_opened(papi, demo):
    api = papi
    agent_with_tools(api, ("math.calculate",))  # a grant is separate from an approval: this agent has no grant for the sensitive tool
    provider = script(api)
    response = api.client.post("/autonomous/run", json={"agent": "analyst", "task": "Tell the customer."}, headers=api.auth(*A))
    assert response.status_code == 200 and demo.OUTBOX == [] and get(api, "/approvals").json()["items"] == []
    tool_result = json.loads([m for m in provider.requests[-1].messages if m.role == "tool"][0].content)
    assert tool_result["error"] == "not_permitted"  # the model was never offered the tool; a call it invents is refused: a grant is separate from an approval, and no approval was opened
    refused = get(api, "/tool-invocations?status=rejected").json()["items"]
    assert [i["error"]["code"] for i in refused] == ["not_permitted"]


def test_a_member_who_is_not_an_owner_or_admin_gets_403_to_decide_and_can_decide_is_reported_false(papi, demo):
    """The API only issues PERSONAL-workspace scopes today (the caller is always the owner there), so a multi-member case cannot be reached over HTTP yet: the role rule
    itself is tested against real team workspaces in test_approvals.py. Here the HTTP contract of a refusal (403 with a fixed code, nothing decided) and the
    ``can_decide`` field are checked with the store dependency replaced."""
    import importlib
    import uuid

    module = importlib.import_module("agentplatform.approvals_api")
    approvals = importlib.import_module("agentplatform.tools.approvals")

    class MemberStore:
        def can_decide(self, scope):
            return False

        def list(self, scope, **kw):
            return []

        def pending_count(self, scope):
            return 0

        def decide(self, scope, approval_id, decision, submitted_hash=None):
            raise approvals.ApprovalForbidden("approver_role_required")

    papi.main.app.dependency_overrides[module.get_approval_store] = lambda: MemberStore()
    try:
        listed = get(papi, "/approvals").json()
        assert listed["can_decide"] is False and listed["items"] == []
        for verb in ("approve", "reject"):
            r = post(papi, f"/approvals/{uuid.uuid4()}/{verb}")
            assert r.status_code == 403 and r.json()["detail"]["code"] == "approver_role_required"
    finally:
        papi.main.app.dependency_overrides.pop(module.get_approval_store, None)
