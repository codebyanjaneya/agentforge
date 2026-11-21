"""M6 / Priorities 2 and 3 over HTTP: a MULTI-MEMBER workspace (owner, admin, member, and an unrelated user), the shared approval inbox, who may decide, a single winner among
concurrent approvers, and the durable in-app notifications (recipients, read state, accurate pending count, no duplicates)."""

import importlib
import threading
import time
import uuid

import pytest

from tests.api_support import api, auth_db, main_module  # noqa: F401  (fixtures)
from tests.test_plan_runs_api import A, B, papi, wait  # noqa: F401  (papi is a fixture)

OWNER, ADMIN, MEMBER, OUTSIDER = A, ("user-adm", "adm@example.com"), ("user-mem", "mem@example.com"), B


def h(api, who, wid=None, **extra):
    return {**api.auth(*who), **({"X-Workspace-Id": wid} if wid else {}), **extra}


def req(api, method, path, who, wid=None, **kw):
    return api.client.request(method, path, headers=h(api, who, wid), **kw)


@pytest.fixture
def team(papi):
    """A shared workspace: OWNER owns it, ADMIN is an admin, MEMBER a member. OUTSIDER is signed in and in no shared workspace."""
    for who in (OWNER, ADMIN, MEMBER, OUTSIDER):
        papi.provision(*who)
    made = req(papi, "POST", "/workspaces", OWNER, json={"name": "Launch team"})
    assert made.status_code == 201, made.text
    wid = made.json()["id"]
    for who, role in ((ADMIN, "admin"), (MEMBER, "member")):
        assert req(papi, "POST", f"/workspaces/{wid}/members", OWNER, json={"email": who[1], "role": role}).status_code == 201
    return wid


def start_launch(api, wid):
    """The demo launch-readiness objective in the SHARED workspace, started by the owner, up to the pending approval. Returns (run, approval)."""
    demo = importlib.import_module("agentplatform.demo")
    tools = importlib.import_module("agentplatform.tools")
    fixtures = importlib.import_module("agentplatform.tools.fixtures")
    providers = importlib.import_module("agentplatform.providers")
    registry = tools.default_registry()
    registry.register(fixtures.DEMO_SEND_NOTE, fixtures.send_note)
    fixtures.OUTBOX.clear()
    api.main.providers = providers.ProviderRegistry({"groq": lambda: demo.DemoProvider()})
    for slug, definition, _ in demo.starter_definitions(True):
        assert req(api, "POST", "/agents", OWNER, wid, json={"slug": slug, "definition": definition.model_dump(mode="json")}).status_code == 201
    plan = req(api, "POST", "/plans", OWNER, wid, json={"objective": demo.EXAMPLE_OBJECTIVE})
    assert plan.status_code == 201, plan.text
    pid = plan.json()["id"]
    assert req(api, "POST", f"/plans/{pid}/approve", OWNER, wid, json={"revision": 1}).status_code == 200
    run = req(api, "POST", f"/plans/{pid}/runs", OWNER, wid)
    assert run.status_code == 202, run.text
    deadline, pending = time.time() + 30, []
    while time.time() < deadline and not pending:
        pending = req(api, "GET", "/approvals?status=pending", OWNER, wid).json()["items"]
        time.sleep(0.1)
    assert pending
    return run.json(), pending[0], fixtures


def cleanup():
    importlib.import_module("agentplatform.tools").default_registry()._tools.pop(("demo.send_note", 1), None)


def test_membership_is_resolved_server_side_and_a_stranger_is_refused(team, papi):
    wid = team
    assert [m["role"] for m in req(papi, "GET", f"/workspaces/{wid}/members", MEMBER).json()["items"]] == ["owner", "admin", "member"]
    mine = req(papi, "GET", "/workspaces", ADMIN).json()["items"]
    assert [(w["personal"], w["role"]) for w in mine] == [(True, "owner"), (False, "admin")]
    assert req(papi, "GET", f"/workspaces/{wid}/members", OUTSIDER).status_code == 404  # a stranger's workspace is an unknown one
    assert req(papi, "GET", "/approvals", OUTSIDER, wid).status_code == 403  # and cannot be selected through the header either
    assert req(papi, "GET", "/approvals", OUTSIDER, str(uuid.uuid4())).status_code == 403
    # only the owner adds members; roles are only admin|member; never into a personal workspace; the target must exist
    assert req(papi, "POST", f"/workspaces/{wid}/members", ADMIN, json={"email": OUTSIDER[1], "role": "member"}).status_code == 403
    assert req(papi, "POST", f"/workspaces/{wid}/members", MEMBER, json={"email": OUTSIDER[1], "role": "admin"}).status_code == 403
    assert req(papi, "POST", f"/workspaces/{wid}/members", OWNER, json={"email": OUTSIDER[1], "role": "owner"}).status_code == 422
    assert req(papi, "POST", f"/workspaces/{wid}/members", OWNER, json={"email": "nobody@example.com", "role": "member"}).status_code == 404
    assert req(papi, "POST", f"/workspaces/{wid}/members", OWNER, json={"email": ADMIN[1], "role": "member"}).status_code == 409
    personal = [w for w in req(papi, "GET", "/workspaces", OWNER).json()["items"] if w["personal"]][0]["id"]
    assert req(papi, "POST", f"/workspaces/{personal}/members", OWNER, json={"email": OUTSIDER[1], "role": "member"}).status_code == 409
    # no header = the caller's own personal workspace, exactly as before
    assert req(papi, "GET", "/agents", OWNER).json()["items"] == []


def test_the_shared_inbox_owner_and_admin_decide_a_member_only_inspects_a_stranger_sees_nothing(team, papi):
    wid = team
    try:
        run, approval, fixtures = start_launch(papi, wid)
        aid = approval["id"]
        # the inbox is shared: the admin (who started nothing) sees the request, with the right to decide; the member sees it but cannot decide; the stranger gets nothing
        seen = req(papi, "GET", f"/approvals/{aid}", ADMIN, wid).json()
        assert seen["status"] == "pending" and seen["can_decide"] is True and seen["arguments"]["recipient"] == "first.customer@example.com"
        assert seen["context"]["started_by"] == "another member" and seen["context"]["objective"] and seen["context"]["task_title"]  # the approver can judge a request of someone else's run
        assert req(papi, "GET", f"/approvals/{aid}", OWNER, wid).json()["context"]["started_by"] == "you"
        shared = req(papi, "GET", f"/plan-runs/{run['id']}", ADMIN, wid)  # sprint 5: members READ the workspace's runs (the earlier rule hid the run from the approver)
        assert shared.status_code == 200 and shared.json()["started_by"]  # and see who started it
        assert req(papi, "GET", f"/plan-runs/{run['id']}", MEMBER, wid).status_code == 200
        assert req(papi, "POST", f"/plan-runs/{run['id']}/cancel", ADMIN, wid).status_code == 404  # reading is not writing: only the member who started a run can cancel it
        assert req(papi, "GET", f"/plan-runs/{run['id']}", OUTSIDER, wid).status_code == 403  # a stranger to the workspace sees nothing
        for sub in ("usage", "tool-invocations", "events", "provider-events"):  # the whole record of the run is readable by members ...
            assert req(papi, "GET", f"/plan-runs/{run['id']}/{sub}", MEMBER, wid).status_code == 200, sub
        assert req(papi, "POST", f"/plan-runs/{run['id']}/retry", MEMBER, wid, json={}).status_code == 404  # ... but not changeable by them
        assert req(papi, "GET", f"/plan-runs/{run['id']}/usage", OUTSIDER, wid).status_code == 403
        assert req(papi, "GET", f"/approvals/{aid}", MEMBER, wid).json()["can_decide"] is False
        assert req(papi, "GET", f"/approvals/{aid}", OUTSIDER, wid).status_code == 403
        assert req(papi, "GET", f"/approvals/{aid}", OUTSIDER).status_code == 404  # in the stranger's own workspace it does not exist
        # a member cannot decide; the refusal changes nothing
        assert req(papi, "POST", f"/approvals/{aid}/approve", MEMBER, wid, json={"payload_hash": approval["payload_hash"]}).status_code == 403
        assert req(papi, "POST", f"/approvals/{aid}/approve", OUTSIDER, wid).status_code == 403
        assert req(papi, "POST", f"/approvals/{aid}/approve", OUTSIDER).status_code == 404
        assert fixtures.OUTBOX == [] and req(papi, "GET", f"/approvals/{aid}", OWNER, wid).json()["status"] == "pending"
        # the ADMIN decides; the actual approver is recorded; the parked task resumes under the run's owner and the run completes
        assert req(papi, "POST", f"/approvals/{aid}/approve", ADMIN, wid, json={"payload_hash": approval["payload_hash"]}).status_code == 200
        admin_id = papi.db.scalar("SELECT id FROM users WHERE email_normalized = 'adm@example.com'")
        assert str(papi.db.scalar("SELECT decided_by FROM tool_approvals")).replace("-", "") == str(admin_id).replace("-", "")
        deadline = time.time() + 40
        while time.time() < deadline:
            body = req(papi, "GET", f"/plan-runs/{run['id']}", OWNER, wid).json()
            if body["status"] != "running":
                break
            time.sleep(0.1)
        assert body["status"] == "succeeded" and len(fixtures.OUTBOX) == 1
        # a second decision (by the owner) on the same approval cannot change anything
        assert req(papi, "POST", f"/approvals/{aid}/approve", OWNER, wid).status_code == 409
        assert req(papi, "POST", f"/approvals/{aid}/reject", OWNER, wid).status_code == 409
        events = [e["type"] for e in req(papi, "GET", f"/plan-runs/{run['id']}/events", OWNER, wid).json()["items"]]
        assert events.count("approval_approved") == 1 and events.count("approval_consumed") == 1 and len(fixtures.OUTBOX) == 1
    finally:
        cleanup()


def test_two_concurrent_approvers_have_exactly_one_winner(team, papi):
    wid = team
    try:
        run, approval, fixtures = start_launch(papi, wid)
        aid, results, barrier = approval["id"], {}, threading.Barrier(2)

        def decide(name, who, verb):
            barrier.wait()
            results[name] = req(papi, "POST", f"/approvals/{aid}/{verb}", who, wid, json={"payload_hash": approval["payload_hash"]}).status_code

        threads = [threading.Thread(target=decide, args=("owner", OWNER, "approve")), threading.Thread(target=decide, args=("admin", ADMIN, "reject"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert sorted(results.values()) == [200, 409], results  # one winner; the other is told it was already decided
        winner = [n for n, c in results.items() if c == 200][0]
        status = req(papi, "GET", f"/approvals/{aid}", OWNER, wid).json()["status"]
        assert status == ("approved" if winner == "owner" else "rejected")
        assert papi.db.scalar("SELECT count(*) FROM execution_events WHERE event_type IN ('approval_approved', 'approval_rejected')") == 1
        deadline = time.time() + 40
        while time.time() < deadline:
            body = req(papi, "GET", f"/plan-runs/{run['id']}", OWNER, wid).json()
            if body["status"] != "running":
                break
            time.sleep(0.1)
        assert len(fixtures.OUTBOX) == (1 if status == "approved" else 0)  # a rejection sends nothing; an approval sends exactly once
    finally:
        cleanup()


def test_notifications_go_to_the_owner_and_admin_only_with_accurate_counts_and_read_state(team, papi):
    wid = team
    try:
        run, approval, fixtures = start_launch(papi, wid)
        aid = approval["id"]
        assert papi.db.scalar("SELECT count(*) FROM notifications") == 2  # the owner and the admin; not the member, not the stranger
        for who, expected in ((OWNER, 1), (ADMIN, 1), (MEMBER, 0)):
            c = req(papi, "GET", "/notifications/count", who, wid).json()
            assert c == {"pending": expected, "unread": expected}, (who, c)
        assert req(papi, "GET", "/notifications", OUTSIDER, wid).status_code == 403
        assert req(papi, "GET", "/notifications/count", OUTSIDER).json() == {"pending": 0, "unread": 0}
        item = req(papi, "GET", "/notifications", ADMIN, wid).json()["items"][0]
        assert item["approval_id"] == aid and item["approval"]["status"] == "pending" and item["read_at"] is None and item["run_id"] == run["id"]
        # read state is per recipient; a foreign notification id is an unknown one
        assert req(papi, "POST", f"/notifications/{item['id']}/read", OWNER, wid).status_code == 404
        assert req(papi, "POST", f"/notifications/{item['id']}/read", ADMIN, wid).json() == {"changed": 1}
        assert req(papi, "POST", f"/notifications/{item['id']}/read", ADMIN, wid).json() == {"changed": 0}  # once
        assert req(papi, "GET", "/notifications/count", ADMIN, wid).json() == {"pending": 1, "unread": 0}  # read, but still actionable: the badge keeps counting it
        assert req(papi, "GET", "/notifications/count", OWNER, wid).json() == {"pending": 1, "unread": 1}  # the owner's own state is untouched
        # a decision empties the badge for BOTH approvers (the request is no longer pending for anyone)
        assert req(papi, "POST", f"/approvals/{aid}/reject", OWNER, wid).status_code == 200
        for who in (OWNER, ADMIN):
            assert req(papi, "GET", "/notifications/count", who, wid).json()["pending"] == 0
        assert req(papi, "GET", "/notifications", ADMIN, wid).json()["items"][0]["approval"]["status"] == "rejected"
        assert req(papi, "POST", "/notifications/read", OWNER, wid).json() == {"changed": 1}
        assert papi.db.scalar("SELECT count(*) FROM notifications") == 2  # never duplicated, never deleted
    finally:
        cleanup()


def test_the_badge_stops_counting_an_expired_request(team, papi, monkeypatch):
    wid = team
    try:
        run, approval, _ = start_launch(papi, wid)
        assert req(papi, "GET", "/notifications/count", OWNER, wid).json()["pending"] == 1
        from datetime import datetime, timedelta

        store = importlib.import_module("agentplatform.notifications").NotificationStore
        monkeypatch.setitem(store.__init__.__kwdefaults__, "clock", lambda: datetime.utcnow() + timedelta(hours=2))  # two hours later: past the approval window
        assert req(papi, "GET", "/notifications/count", OWNER, wid).json()["pending"] == 0
        assert req(papi, "GET", "/notifications", ADMIN, wid).json()["items"][0]["approval"]["status"] == "expired"
    finally:
        cleanup()


def test_a_retried_request_cannot_notify_twice_and_the_database_refuses_a_duplicate(team, papi):
    wid = team
    try:
        _, approval, _ = start_launch(papi, wid)
        row = papi.db.rows("SELECT workspace_id, user_id, kind, ref_id, run_id FROM notifications LIMIT 1")[0]
        with pytest.raises(Exception):
            papi.db.run("INSERT INTO notifications (id, workspace_id, user_id, kind, ref_id, run_id, created_at) VALUES ('%s','%s','%s','%s','%s',NULL,'2026-01-01 00:00:00.000000')"
                        % (uuid.uuid4().hex, row[0], row[1], row[2], row[3]))
        assert papi.db.scalar("SELECT count(*) FROM notifications") == 2
    finally:
        cleanup()
