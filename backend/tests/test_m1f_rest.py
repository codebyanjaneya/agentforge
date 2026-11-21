"""M1-F: REST authentication, workspace scope, task/improvement/history isolation (real app, disposable SQLite)."""

import time
import uuid
from datetime import datetime

import pytest

from tests.api_support import FakeRedis, api, auth_db, main_module  # noqa: F401  (fixtures)
from tests.auth_support import make_token, new_keypair

AGENT_ID = "00000000-0000-0000-0000-000000000001"

# (method, path, json body) for EVERY protected route. A meta-test below checks that this table is complete.
PROTECTED = [
    ("POST", "/autonomous/run", {"agent": "ceo", "task": "t"}),
    ("POST", "/tasks/schedule", {"agent": "ceo", "task": "t", "interval_minutes": 5}),
    ("GET", "/tasks", None),
    ("DELETE", "/tasks/abc", None),
    ("POST", "/improve/ceo", None),
    ("GET", "/improve/ceo", None),
    ("GET", "/history", None),
    # M2-A: agent templates and custom agents (all routes use get_scope)
    ("GET", "/agent-templates", None),
    ("GET", "/agent-templates/sales-outreach", None),
    ("POST", "/agent-templates/sales-outreach/instantiate", None),
    ("POST", "/agents", {"slug": "x-agent", "definition": {}}),
    ("GET", "/agents", None),
    ("GET", f"/agents/{AGENT_ID}", None),
    ("PATCH", f"/agents/{AGENT_ID}", {"base_version": 1, "definition": {}}),
    ("POST", f"/agents/{AGENT_ID}/archive", None),
    ("POST", f"/agents/{AGENT_ID}/unarchive", None),
    ("GET", f"/agents/{AGENT_ID}/versions", None),
    ("GET", f"/agents/{AGENT_ID}/versions/1", None),
    ("POST", f"/agents/{AGENT_ID}/rollback", {"to_version": 1, "base_version": 1}),
    # M2-B: execution history (all routes use get_scope)
    ("GET", "/executions", None),
    ("GET", f"/executions/{AGENT_ID}", None),
    # M3-A: plans (all routes use get_scope; nothing is executed)
    ("POST", "/plans", {"objective": "Plan the launch of our new product."}),
    ("GET", "/plans", None),
    ("GET", f"/plans/{AGENT_ID}", None),
    ("GET", f"/plans/{AGENT_ID}/revisions", None),
    ("GET", f"/plans/{AGENT_ID}/revisions/1", None),
    ("POST", f"/plans/{AGENT_ID}/validate", None),
    ("POST", f"/plans/{AGENT_ID}/revisions", {"base_revision": 1, "tasks": []}),
    ("POST", f"/plans/{AGENT_ID}/approve", {"revision": 1}),
    ("POST", f"/plans/{AGENT_ID}/cancel", None),
    # M3-B: execution of approved plans (all routes use get_scope; polling contract)
    ("POST", f"/plans/{AGENT_ID}/runs", None),
    ("GET", f"/plans/{AGENT_ID}/runs", None),
    ("GET", f"/plan-runs/{AGENT_ID}", None),
    ("GET", f"/plan-runs/{AGENT_ID}/tasks/t1", None),
    ("POST", f"/plan-runs/{AGENT_ID}/cancel", None),
    ("POST", f"/plan-runs/{AGENT_ID}/retry", None),
    ("GET", f"/plan-runs/{AGENT_ID}/events", None),
    ("GET", "/tools", None),
    ("GET", "/tools/math.calculate", None),
    ("GET", "/tool-invocations", None),
    ("GET", f"/tool-invocations/{AGENT_ID}", None),
    ("GET", f"/plan-runs/{AGENT_ID}/tool-invocations", None),
    ("GET", "/approvals", None),
    ("GET", f"/approvals/{AGENT_ID}", None),
    ("POST", f"/approvals/{AGENT_ID}/approve", {}),
    ("POST", f"/approvals/{AGENT_ID}/reject", {}),
    ("GET", "/demo", None),
    ("POST", "/starter-workforce", {}),
    ("GET", f"/plan-runs/{AGENT_ID}/usage", None),
    ("GET", f"/plan-runs/{AGENT_ID}/provider-events", None),
    ("POST", f"/tool-invocations/{AGENT_ID}/reconcile", {"mode": "query", "reason": "check"}),
    ("GET", "/reconciliations", None),
    ("GET", "/notifications", None),
    ("GET", "/notifications/count", None),
    ("POST", "/notifications/read", {}),
    ("POST", f"/notifications/{AGENT_ID}/read", {}),
]
# M6: these choose or list workspaces, so they authenticate the Principal but cannot use get_scope (there is no workspace yet); they have their own tests
PRINCIPAL_ONLY = [
    ("GET", "/workspaces", None),
    ("POST", "/workspaces", {"name": "Team"}),
    ("GET", f"/workspaces/{AGENT_ID}/members", None),
    ("POST", f"/workspaces/{AGENT_ID}/members", {"email": "x@example.com", "role": "member"}),
    ("PATCH", f"/workspaces/{AGENT_ID}/members", {"email": "x@example.com", "role": "member"}),
    ("POST", f"/workspaces/{AGENT_ID}/members/remove", {"email": "x@example.com"}),
    ("POST", f"/workspaces/{AGENT_ID}/leave", None),
    ("POST", f"/workspaces/{AGENT_ID}/invitations", {"email": "x@example.com", "role": "member"}),
    ("GET", f"/workspaces/{AGENT_ID}/invitations", None),
    ("DELETE", f"/workspaces/{AGENT_ID}/invitations/{AGENT_ID}", None),
    ("POST", "/invitations/accept", {"token": "x" * 43}),
]
PUBLIC = {("GET", "/health"), ("GET", "/ready"), ("GET", "/metrics"), ("POST", "/voice/incoming"), ("POST", "/voice/process")}
FRAMEWORK = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect", "/ws"}


def call(api, method, path, body=None, **kw):
    return api.client.request(method, path, json=body, **kw)


# ---------------------------------------------------------------- authentication

def test_missing_token_is_401_on_every_protected_route(api):
    for method, path, body in PROTECTED:
        r = call(api, method, path, body)
        assert r.status_code == 401 and r.json() == {"detail": "Not authenticated"}, (method, path)


def test_workspace_routes_need_a_session(api):
    for method, path, body in PRINCIPAL_ONLY:
        assert call(api, method, path, body).status_code == 401, (method, path)
        assert call(api, method, path, body, headers={"Authorization": "Bearer junk"}).status_code == 401


def test_invalid_tokens_are_401_on_every_protected_route(api):
    now = int(time.time())
    tokens = {
        "wrong_key": make_token(new_keypair()[0]),
        "expired": make_token(api.private, claims={"iat": now - 900, "exp": now - 300}),
        "wrong_issuer": make_token(api.private, claims={"iss": "someone-else"}),
        "wrong_audience": make_token(api.private, claims={"aud": "someone-else"}),
        "garbage": "not-a-jwt",
    }
    for kind, token in tokens.items():
        for method, path, body in PROTECTED:
            r = call(api, method, path, body, headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 401 and r.json() == {"detail": "Not authenticated"}, (kind, method, path)
    assert api.db.counts()["users"] == 0  # a rejected token provisions nothing


def test_valid_token_resolves_the_correct_internal_principal(api):
    uid_a, ws_a = api.provision("user-a", "a@example.com")
    uid_b, ws_b = api.provision("user-b", "b@example.com")
    assert uid_a != uid_b and ws_a != ws_b
    call(api, "POST", "/autonomous/run", {"agent": "ceo", "task": "hi"}, headers=api.auth("user-a", "a@example.com"))
    (name, scope), = api.seen_scopes
    assert (scope.user_id, scope.workspace_id) == (uid_a, ws_a)
    assert api.db.counts() == {"users": 2, "workspaces": 2, "memberships": 2, "identities": 2}


def test_inactive_user_is_403(api):
    api.provision()
    api.db.run("UPDATE users SET is_active = 0")
    for method, path, body in PROTECTED:
        r = call(api, method, path, body, headers=api.auth())
        assert r.status_code == 403 and r.json() == {"detail": "Forbidden"}


# ---------------------------------------------------------------- forged identity

def test_forged_email_in_body_query_path_and_headers_cannot_change_identity(api):
    uid_a, ws_a = api.provision("user-a", "a@example.com")
    api.provision("user-b", "b@example.com")
    forged = {"X-User-Email": "b@example.com", "X-Email": "b@example.com", "X-Forwarded-User": "b@example.com"}
    r = call(api, "POST", "/autonomous/run", {"agent": "ceo", "task": "t", "user_email": "b@example.com", "email": "b@example.com"},
             headers={**api.auth("user-a", "a@example.com"), **forged}, params={"user_email": "b@example.com", "email": "b@example.com"})
    assert r.status_code == 200
    assert api.seen_scopes[-1][1].user_id == uid_a
    r = call(api, "GET", "/history", params={"user_email": "b@example.com"}, headers={**api.auth("user-a", "a@example.com"), **forged})
    assert r.status_code == 200 and [m["summary"] for m in r.json()["items"]] == ["reply:t"]  # A's own row only
    assert call(api, "GET", "/history/b@example.com", headers=api.auth("user-a", "a@example.com")).status_code == 404  # route removed


def test_no_protected_contract_accepts_an_email_parameter(api):
    schema = api.main.app.openapi()
    text = str(schema)
    # M6: the ONE place an email is accepted is the owner naming WHO to add to a shared workspace (a lookup aid for the target); it never identifies the caller
    assert text.lower().count("email") == sum(str(schema["components"]["schemas"][n]).lower().count("email") for n in ("AddMember", "MemberTarget")) > 0  # only the owner-managed member contracts name an e-mail


# ---------------------------------------------------------------- workspace authorization

def test_unauthorized_workspace_is_403(api):
    uid, ws = api.provision()
    api.db.run("DELETE FROM workspace_memberships WHERE workspace_id = :w", w=ws.hex)  # membership gone: not authorized
    for method, path, body in PROTECTED:
        r = call(api, method, path, body, headers=api.auth())
        assert r.status_code == 403 and r.json() == {"detail": "Forbidden"}


def test_user_without_a_personal_workspace_is_403(api):
    uid, ws = api.provision()
    api.db.run("DELETE FROM workspace_memberships WHERE workspace_id = :w", w=ws.hex)
    api.db.run("DELETE FROM workspaces WHERE id = :w", w=ws.hex)
    assert call(api, "GET", "/tasks", headers=api.auth()).status_code == 403


def test_inconsistent_workspace_is_403(api):
    uid, ws = api.provision()
    api.db.run("UPDATE workspace_memberships SET role = 'member' WHERE workspace_id = :w", w=ws.hex)  # no owner
    assert call(api, "GET", "/tasks", headers=api.auth()).status_code == 403


def test_the_workspace_header_is_validated_and_membership_verified_on_every_protected_route(api):
    for value in ["x", " ", "default"]:  # not a UUID
        for method, path, body in PROTECTED:
            r = call(api, method, path, body, headers={**api.auth(), "X-Workspace-Id": value})
            assert r.status_code == 400 and r.json() == {"detail": "X-Workspace-Id is invalid"}, (value, method, path)
    for method, path, body in PROTECTED:  # M6: a well-formed workspace the caller is not a member of is refused (unknown == a stranger's)
        r = call(api, method, path, body, headers={**api.auth(), "X-Workspace-Id": str(uuid.uuid4())})
        assert r.status_code == 403 and r.json() == {"detail": "Forbidden"}, (method, path)


def test_workspace_header_does_not_bypass_authentication_and_empty_is_ignored(api):
    assert call(api, "GET", "/tasks", headers={"X-Workspace-Id": "x"}).status_code == 401
    assert call(api, "GET", "/tasks", headers={**api.auth(), "X-Workspace-Id": ""}).status_code == 200


# ---------------------------------------------------------------- tasks, improvements, history isolation

def test_tasks_are_scoped_and_a_task_id_alone_cannot_delete(api):
    ha, hb = api.auth("user-a", "a@example.com"), api.auth("user-b", "b@example.com")
    ta = call(api, "POST", "/tasks/schedule", {"agent": "ceo", "task": "A", "interval_minutes": 5}, headers=ha).json()
    call(api, "POST", "/tasks/schedule", {"agent": "ceo", "task": "B", "interval_minutes": 5}, headers=hb)
    assert [t["task"] for t in call(api, "GET", "/tasks", headers=ha).json()] == ["A"]
    assert [t["task"] for t in call(api, "GET", "/tasks", headers=hb).json()] == ["B"]
    assert "email" not in str(ta)
    r = call(api, "DELETE", f"/tasks/{ta['id']}", headers=hb)  # B knows A's task id
    assert r.status_code == 200 and r.json()["removed"] is False
    assert [t["task"] for t in call(api, "GET", "/tasks", headers=ha).json()] == ["A"]
    assert call(api, "DELETE", f"/tasks/{ta['id']}", headers=ha).json()["removed"] is True
    assert call(api, "GET", "/tasks", headers=ha).json() == []


def test_improvement_reports_are_scoped(api):
    ha, hb = api.auth("user-a", "a@example.com"), api.auth("user-b", "b@example.com")
    call(api, "POST", "/autonomous/run", {"agent": "ceo", "task": "work"}, headers=ha)
    report = call(api, "POST", "/improve/ceo", headers=ha).json()
    assert report["has_history"] is True and "user_email" not in report
    assert call(api, "GET", "/improve/ceo", headers=ha).json()["strengths"] == ["s"]
    other = call(api, "GET", "/improve/ceo", headers=hb).json()
    assert other["has_history"] is False and other["strengths"] == []
    assert call(api, "POST", "/improve/ceo", headers=hb).json()["has_history"] is False


def test_history_is_scoped_per_user(api):
    ha, hb = api.auth("user-a", "a@example.com"), api.auth("user-b", "b@example.com")
    call(api, "POST", "/autonomous/run", {"agent": "ceo", "task": "A-task"}, headers=ha)
    call(api, "POST", "/autonomous/run", {"agent": "ceo", "task": "B-task"}, headers=hb)
    assert [m["summary"] for m in call(api, "GET", "/history", headers=ha).json()["items"]] == ["reply:A-task"]
    assert [m["summary"] for m in call(api, "GET", "/history", headers=hb).json()["items"]] == ["reply:B-task"]


def test_no_global_or_legacy_redis_key_is_read_written_or_deleted(api):
    legacy = {"id": "legacy1", "agent": "ceo", "task": "LEGACY", "interval_minutes": 1}
    api.redis.hset("scheduled_tasks", "legacy1", '{"id": "legacy1", "agent": "ceo", "task": "LEGACY", "interval_minutes": 1}')
    api.redis.set("improvements:ceo", '{"strengths": ["LEGACY"]}')
    api.redis.set("improvements:ceo:a@example.com", '{"strengths": ["LEGACY-EMAIL"]}')
    api.redis.set("short_term:ceo", "[]")
    api.redis.set("short_term:a@example.com:ceo", "[]")
    before = {k: (api.redis.h.get(k) or api.redis.d.get(k)) for k in ("scheduled_tasks", "improvements:ceo", "improvements:ceo:a@example.com")}
    h = api.auth("user-a", "a@example.com")
    assert call(api, "GET", "/tasks", headers=h).json() == []
    assert call(api, "DELETE", "/tasks/legacy1", headers=h).json()["removed"] is False
    assert call(api, "GET", "/improve/ceo", headers=h).json()["strengths"] == []
    call(api, "POST", "/tasks/schedule", {"agent": "ceo", "task": "t", "interval_minutes": 1}, headers=h)
    call(api, "POST", "/autonomous/run", {"agent": "ceo", "task": "t"}, headers=h)
    call(api, "POST", "/improve/ceo", headers=h)
    after = {k: (api.redis.h.get(k) or api.redis.d.get(k)) for k in before}
    assert after == before  # legacy keys untouched
    legacy_keys = {"scheduled_tasks", "improvements:ceo", "improvements:ceo:a@example.com", "short_term:ceo", "short_term:a@example.com:ceo"}
    new_keys = api.redis.keys_used() - legacy_keys
    assert new_keys and all(":v2:" in k for k in new_keys)


def test_unknown_agent_is_a_clean_404(api):
    h = api.auth()
    for method, path, body in [("POST", "/autonomous/run", {"agent": "nope", "task": "t"}),
                               ("POST", "/tasks/schedule", {"agent": "nope", "task": "t", "interval_minutes": 1}),
                               ("POST", "/improve/nope", None), ("GET", "/improve/nope", None)]:
        r = call(api, method, path, body, headers=h)
        assert r.status_code == 404 and r.json() == {"detail": "Unknown agent"}


# ---------------------------------------------------------------- coverage, public routes

def _flatten(routes):
    for route in routes:
        inner = getattr(route, "original_router", None)  # an included router (FastAPI wraps it)
        if inner is not None:
            yield from _flatten(inner.routes)
        else:
            yield route


def _template(path):
    import re

    path = re.sub(r"/plan-runs/[0-9a-f-]{36}/tasks/t1$", "/plan-runs/{run_id}/tasks/{task_key}", path)
    path = re.sub(r"/plan-runs/[0-9a-f-]{36}", "/plan-runs/{run_id}", path)
    path = re.sub(r"/plans/[0-9a-f-]{36}/revisions/1$", "/plans/{plan_id}/revisions/{revision}", path)
    path = re.sub(r"/plans/[0-9a-f-]{36}", "/plans/{plan_id}", path)
    path = re.sub(r"/executions/[0-9a-f-]{36}", "/executions/{execution_id}", path)
    path = re.sub(r"/tool-invocations/[0-9a-f-]{36}", "/tool-invocations/{invocation_id}", path)
    path = re.sub(r"/approvals/[0-9a-f-]{36}", "/approvals/{approval_id}", path)
    path = re.sub(r"/notifications/[0-9a-f-]{36}/read", "/notifications/{notification_id}/read", path)
    path = re.sub(r"/workspaces/[0-9a-f-]{36}/invitations/[0-9a-f-]{36}", "/workspaces/{workspace_id}/invitations/{invitation_id}", path)
    path = re.sub(r"/workspaces/[0-9a-f-]{36}/(members|invitations|leave)", lambda m: "/workspaces/{workspace_id}/" + m.group(1), path)
    path = path.replace("/tools/math.calculate", "/tools/{tool_id}")
    path = re.sub(r"/agents/[0-9a-f-]{36}", "/agents/{agent_id}", path)
    path = re.sub(r"/versions/1$", "/versions/{version}", path)
    path = path.replace("/agent-templates/sales-outreach", "/agent-templates/{template_id}")
    return path.replace("/abc", "/{task_id}").replace("/improve/ceo", "/improve/{agent_name}")


def test_every_protected_route_is_in_the_auth_tables(api):
    seen = set()
    for route in _flatten(api.main.app.routes):
        for method in getattr(route, "methods", None) or ():
            if method in ("HEAD", "OPTIONS") or route.path in FRAMEWORK:
                continue
            seen.add((method, route.path))
    listed = {(m, _template(p)) for m, p, _ in PROTECTED}
    principal_only = {(m, _template(p)) for m, p, _ in PRINCIPAL_ONLY}
    assert seen - PUBLIC - principal_only == listed  # nothing protected is missing from the 401/403/workspace tests, nothing extra
    assert len(seen - PUBLIC) >= 35  # the flattening really found the included router's routes


def test_health_is_public_and_unchanged(api):
    r = api.client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert api.client.get("/health", headers={"Authorization": "Bearer junk"}).json() == {"status": "ok"}


def test_twilio_endpoints_are_public_pending_s2_and_never_touch_user_memory(api, monkeypatch):
    """EXPLICIT TEMPORARY EXCEPTION: these webhooks are public and NOT secure until S2 (signature validation and a
    service principal). They attach no Principal and cannot reach user memory."""
    r = api.client.post("/voice/incoming")
    assert r.status_code == 200 and "<Response>" in r.text
    form = {}

    async def fake_form(self):  # python-multipart is listed in requirements.txt but is not installed in this venv
        return form

    monkeypatch.setattr("starlette.requests.Request.form", fake_form)
    r = api.client.post("/voice/process")  # no RecordingUrl
    assert r.status_code == 200 and "<Response>" in r.text
    import voice_handler

    async def boom(recording_url, orchestrator):
        from authz.errors import ScopeError
        raise ScopeError("scope_required")

    monkeypatch.setattr(api.main, "process_recording", boom)
    form["RecordingUrl"] = "https://example.invalid/x"
    r = api.client.post("/voice/process")
    assert r.status_code == 200 and "not available" in r.text  # fails closed: no scope exists for voice
    assert api.db.counts()["users"] == 0 and api.db.scalar("SELECT count(*) FROM agent_memories") == 0


def test_error_bodies_never_leak_internals(api):
    def broken():
        raise RuntimeError("SECRET-DRIVER-TEXT postgresql://u:pw@host")

    api.main.app.dependency_overrides[api.main.get_session_factory] = broken
    r = api.client.get("/tasks", headers=api.auth())
    assert r.status_code == 500 and "SECRET" not in r.text and "postgresql" not in r.text
