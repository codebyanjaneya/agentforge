"""M2-B: REST and WebSocket execution of built-in and custom agents, and the execution history API (real app, real verifier,
disposable SQLite, fake provider; no network)."""

import copy
import sys
import threading
import time
import uuid

import pytest
from starlette.websockets import WebSocketDisconnect

from agentplatform import execution as ex
from agentplatform.execution import Limits
from tests.api_support import ORIGIN, api, auth_db, main_module  # noqa: F401  (fixtures)

DEFINITION = {
    "name": "Release Notes Writer",
    "description": "Turns a changelog into friendly release notes for customers.",
    "role": "Product marketer",
    "purpose": "Explain what changed and why it matters, without hype.",
    "instructions": "You write clear release notes from the changelog the user provides. Be accurate and concise.",
    "capabilities": ["release notes"],
    "model": {"provider": "groq", "name": "llama-3.3-70b-versatile", "temperature": 0.5},
}
A, B = ("user-a", "a@example.com"), ("user-b", "b@example.com")


def definition(**overrides):
    data = copy.deepcopy(DEFINITION)
    data.update(overrides)
    return data


def make_agent(api, slug="writer", who=A, **overrides):
    r = api.client.post("/agents", json={"slug": slug, "definition": definition(**overrides)}, headers=api.auth(*who))
    assert r.status_code == 201, r.text
    return r.json()


def run(api, agent="writer", task="hello", who=A, **extra):
    return api.client.post("/autonomous/run", json={"agent": agent, "task": task, **extra}, headers=api.auth(*who))


def execution(api, execution_id, who=A):
    return api.client.get(f"/executions/{execution_id}", headers=api.auth(*who))


# ------------------------------------------------------------------------------------------------ REST success

def test_a_builtin_run_keeps_the_original_response_and_adds_the_execution(api):
    r = run(api, "sales", "pitch me")
    assert r.status_code == 200
    body = r.json()
    assert {"agent", "task", "response", "actions", "executed_at"} <= set(body)  # the original contract is intact
    assert (body["agent"], body["task"], body["response"]) == ("sales", "pitch me", "reply from sales: pitch me")
    assert (body["status"], body["agent_source"], body["agent_version"]) == ("succeeded", "builtin", None)
    detail = execution(api, body["execution_id"]).json()
    assert (detail["agent_source"], detail["agent_key"], detail["agent_version"], detail["agent_version_id"]) == ("builtin", "sales", None, None)
    assert (detail["status"], detail["task"], detail["result"], detail["error_code"]) == ("succeeded", "pitch me", "reply from sales: pitch me", None)
    assert detail["completed_at"] and detail["duration_ms"] is not None and detail["provider"] == "groq"


def test_a_custom_run_executes_the_saved_definition_and_pins_the_exact_version(api):
    agent = make_agent(api)
    r = run(api, "writer", "turn this changelog into notes")
    assert r.status_code == 200
    body = r.json()
    assert (body["agent_source"], body["agent_version"], body["status"]) == ("custom", 1, "succeeded")
    assert body["response"] == "custom reply: turn this changelog into notes" and body["agent"] == "writer"
    detail = execution(api, body["execution_id"]).json()
    version_id = api.db.scalar("SELECT id FROM custom_agent_versions WHERE agent_id = :a AND version = 1", a=uuid.UUID(agent["id"]).hex)
    assert detail["agent_version_id"] == str(uuid.UUID(version_id)) and detail["agent_version"] == 1
    assert (detail["agent_name"], detail["provider"], detail["model"]) == ("Release Notes Writer", "groq", "llama-3.3-70b-versatile")
    assert "user_id" not in detail and "workspace_id" not in detail
    request = api.provider.requests[0]
    assert "no tools" in request.system and request.messages[-1].content == "turn this changelog into notes"


def test_editing_after_a_run_gives_new_runs_the_new_version_and_old_runs_keep_theirs(api):
    agent = make_agent(api)
    first = run(api, "writer", "one").json()
    r = api.client.patch(f"/agents/{agent['id']}", json={"base_version": 1, "definition": definition(role="Second role")}, headers=api.auth(*A))
    assert r.status_code == 200
    second = run(api, "writer", "two").json()
    assert (first["agent_version"], second["agent_version"]) == (1, 2)
    assert execution(api, first["execution_id"]).json()["agent_version"] == 1  # unchanged by the later edit
    assert "Product marketer" in api.provider.requests[0].system and "Second role" in api.provider.requests[1].system
    listed = api.client.get("/executions?agent=writer", headers=api.auth(*A)).json()["items"]
    assert [(i["agent_version"], i["status"]) for i in listed] == [(2, "succeeded"), (1, "succeeded")]  # newest first, with the version used


def test_unknown_archived_and_foreign_agents_are_one_indistinguishable_404(api):
    agent = make_agent(api)
    bodies = set()
    for who, key in ((A, "nope"), (B, "writer"), (A, "ceo-clone"), (A, "")):
        r = run(api, key, "x", who=who)
        assert r.status_code == 404
        bodies.add(r.text)
    assert bodies == {'{"detail":"Unknown agent"}'}
    assert api.client.post(f"/agents/{agent['id']}/archive", headers=api.auth(*A)).status_code == 200
    r = run(api, "writer", "x")
    assert r.status_code == 404 and r.text in bodies  # an archived agent does not run
    assert api.provider.requests == [] and api.db.scalar("SELECT count(*) FROM agent_executions") == 0
    api.client.post(f"/agents/{agent['id']}/unarchive", headers=api.auth(*A))
    assert run(api, "writer", "x").status_code == 200


def test_identity_comes_only_from_the_token(api):
    make_agent(api)
    forged = {**api.auth(*B), "X-User-Email": "a@example.com", "X-Workspace-Id": ""}
    r = api.client.post("/autonomous/run", json={"agent": "writer", "task": "x", "user_email": "a@example.com", "user_id": str(uuid.uuid4()),
                                                 "workspace_id": str(uuid.uuid4()), "agent_version_id": str(uuid.uuid4())}, headers=forged)
    assert r.status_code == 404  # B has no "writer": the body fields changed nothing
    ok = run(api, "writer", "x")
    owner = api.db.scalar("SELECT user_id FROM agent_executions")
    a_id = api.db.scalar("SELECT u.id FROM users u JOIN auth_identities i ON i.user_id = u.id WHERE i.provider_subject = 'user-a'")
    assert ok.status_code == 200 and owner == a_id


def test_the_custom_run_also_appears_in_the_memory_history(api):
    make_agent(api)
    run(api, "writer", "remember this")
    items = api.client.get("/history", headers=api.auth(*A)).json()["items"]
    assert [i["agent_name"] for i in items] == ["writer"] and items[0]["summary"] == "custom reply: remember this"


# ------------------------------------------------------------------------------------------------ REST failures

def provider_error(code):
    """The ProviderError class of the app under test (the fixture re-imports the app, so a top-level import would differ)."""
    if code == "internal_error":
        return RuntimeError("SECRET-INTERNAL gsk_abcdefghijklmnopqrstuvwxyz123456")
    return sys.modules["agentplatform.providers"].ProviderError(code)


@pytest.mark.parametrize("status,code", [
    (504, "timeout"), (429, "rate_limited"), (503, "unavailable"), (503, "auth"), (422, "unsupported_model"), (502, "bad_request"),
    (502, "invalid_response"), (500, "internal_error"),
])
def test_failures_are_safe_actionable_and_traceable(api, status, code):
    make_agent(api)
    api.provider.error = provider_error(code)
    r = run(api, "writer", "go")
    assert r.status_code == status
    detail = r.json()["detail"]
    assert detail["code"] == code and detail["message"] == ex.MESSAGES[code]
    assert "SECRET" not in r.text and "gsk_" not in r.text and "Traceback" not in r.text
    record = execution(api, detail["execution_id"]).json()  # the failure is traceable to its record
    assert (record["status"], record["error_code"], record["error_message"], record["result"]) == ("failed", code, ex.MESSAGES[code], None)
    assert record["agent_version"] == 1 and len(api.provider.requests) == 1  # exactly one attempt
    assert api.client.get("/executions", headers=api.auth(*B)).json()["items"] == []  # nobody else sees it


def test_invalid_tasks_are_422_and_leave_no_record(api):
    make_agent(api)
    for task in ("", "   ", "x" * (ex.MAX_TASK_CHARS + 1), "bad\x00task"):
        r = run(api, "writer", task)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "task_invalid", repr(task[:10])
    assert run(api, "writer", None).status_code == 422
    assert api.db.scalar("SELECT count(*) FROM agent_executions") == 0 and api.provider.requests == []


def test_a_busy_server_refuses_immediately_with_retry_after(api):
    make_agent(api)
    api.main.execution_limits = Limits(total=1, per_user=1)
    gate, started = threading.Event(), threading.Event()
    api.provider.gate, api.provider.started = gate, started
    first = {}
    thread = threading.Thread(target=lambda: first.update(r=run(api, "writer", "slow one")))
    thread.start()
    assert started.wait(5)
    r = run(api, "writer", "refused")
    assert r.status_code == 429 and r.headers["retry-after"] == "5" and r.json()["detail"]["code"] == "busy"
    gate.set()
    thread.join(5)
    assert first["r"].status_code == 200
    assert run(api, "writer", "after").status_code == 200  # released
    assert api.db.scalar("SELECT count(*) FROM agent_executions") == 2  # the refused request left no record


# ------------------------------------------------------------------------------------------------ history API

def test_execution_history_is_scoped_paged_and_validated(api):
    make_agent(api)
    ids = [run(api, "writer" if i % 2 else "sales", f"task {i}").json()["execution_id"] for i in range(7)]
    seen, cursor = [], None
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        page = api.client.get("/executions", params=params, headers=api.auth(*A)).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == ids[::-1] and len(set(seen)) == 7
    summary = api.client.get("/executions", headers=api.auth(*A)).json()["items"][0]
    assert set(summary) == {"id", "agent_source", "agent_key", "agent_name", "agent_version", "status", "error_code", "provider", "model",
                            "created_at", "started_at", "completed_at", "duration_ms"}  # no task, result or identifiers of the owner
    assert len(api.client.get("/executions?limit=100", headers=api.auth(*A)).json()["items"]) == 7
    for params in ({"limit": 0}, {"limit": 101}, {"limit": "x"}, {"agent": ""}):
        assert api.client.get("/executions", params=params, headers=api.auth(*A)).status_code == 422, params
    for bad in ("garbage", "v1.x.y", cursor or "v1..", "v2." + "A" * 20 + ".00000000"):
        r = api.client.get("/executions", params={"cursor": bad}, headers=api.auth(*A))
        assert r.status_code == 400 and r.json()["detail"] == {"code": "invalid_cursor"}
    assert api.client.get("/executions", headers=api.auth(*B)).json() == {"items": [], "next_cursor": None}
    assert execution(api, ids[0], who=B).status_code == 404 and execution(api, str(uuid.uuid4())).status_code == 404
    assert execution(api, "not-a-uuid").status_code == 422
    forged = {**api.auth(*B), "X-User-Email": "a@example.com"}
    assert api.client.get("/executions", params={"user_email": "a@example.com"}, headers=forged).json()["items"] == []


# ------------------------------------------------------------------------------------------------ WebSocket

def connect(api):
    return api.client.websocket_connect("/ws", headers={"origin": ORIGIN})


def login(ws, api, who=A):
    ws.send_json({"type": "auth", "token": api.token(*who)})
    assert ws.receive_json() == {"type": "authenticated"}


def job(ws, agent, task="hi"):
    ws.send_json({"agent": agent, "task": task})


def test_a_custom_agent_runs_over_the_websocket_through_the_same_runtime(api):
    agent = make_agent(api)
    with connect(api) as ws:
        login(ws, api)
        job(ws, "writer", "ws task")
        frame = ws.receive_json()
    assert set(frame) == {"agent", "result", "execution_id"} and frame["result"] == "custom reply: ws task"
    record = execution(api, frame["execution_id"]).json()
    assert (record["agent_source"], record["agent_version"], record["status"], record["result"]) == ("custom", 1, "succeeded", "custom reply: ws task")
    assert record["agent_version_id"]
    assert api.db.scalar("SELECT agent_version_id FROM agent_executions") == uuid.UUID(
        api.db.scalar("SELECT id FROM custom_agent_versions WHERE agent_id = :a", a=uuid.UUID(agent["id"]).hex)).hex


def test_builtin_websocket_frames_keep_their_protocol(api):
    with connect(api) as ws:
        login(ws, api)
        job(ws, "sales", "pitch")
        sales = ws.receive_json()
        job(ws, "ceo", "plan")
        ceo = ws.receive_json()
    assert set(sales) == {"agent", "result", "execution_id"}
    assert set(ceo) == {"agent", "result", "primary_agent", "primary_response", "delegations", "execution_id"}  # the CEO's existing shape
    assert ceo["delegations"] == [] and ceo["primary_agent"] == "ceo"
    assert [r[0] for r in api.db.rows("SELECT agent_source FROM agent_executions")] == ["builtin", "builtin"]


def test_websocket_failures_are_safe_frames_with_the_execution_id(api):
    make_agent(api)
    api.provider.error = provider_error("timeout")
    with connect(api) as ws:
        login(ws, api)
        job(ws, "writer")
        frame = ws.receive_json()
        api.provider.error = RuntimeError("SECRET-INTERNAL gsk_abcdefghijklmnopqrstuvwxyz123456")
        job(ws, "writer")
        second = ws.receive_json()
        job(ws, "writer", "x" * (ex.MAX_TASK_CHARS + 1))
        too_long = ws.receive_json()
    assert set(frame) == {"agent", "error", "execution_id"} and frame["error"] == ex.MESSAGES["timeout"]
    assert second["error"] == "Agent failed." and "SECRET" not in str(second) and "gsk_" not in str(second)
    assert too_long == {"agent": "writer", "error": f"The task is too long (at most {ex.MAX_TASK_CHARS} characters)."}
    assert [r for r in api.db.rows("SELECT status, error_code FROM agent_executions ORDER BY created_at, id")] == [("failed", "timeout"), ("failed", "internal_error")]


def test_unknown_archived_and_foreign_agents_get_the_existing_unknown_agent_frame(api):
    agent = make_agent(api)
    make_agent(api, slug="theirs", who=B, name="Theirs")
    api.client.post(f"/agents/{agent['id']}/archive", headers=api.auth(*A))
    with connect(api) as ws:
        login(ws, api)
        for key in ("nope", "writer", "theirs"):
            job(ws, key)
            assert ws.receive_json() == {"error": "Unknown agent."}
        job(ws, "sales")  # the connection and its job slot are still usable
        assert ws.receive_json()["result"] == "reply from sales: hi"
    assert api.provider.requests == [] and api.db.scalar("SELECT count(*) FROM agent_executions") == 1


def test_one_job_per_connection_still_applies_to_custom_agents(api):
    make_agent(api)
    gate, started = threading.Event(), threading.Event()
    api.provider.gate, api.provider.started = gate, started
    with connect(api) as ws:
        login(ws, api)
        job(ws, "writer", "first")
        assert started.wait(5)
        job(ws, "writer", "second")
        assert ws.receive_json() == {"error": "A job is already running on this connection."}
        gate.set()
        assert ws.receive_json()["result"] == "custom reply: first"
    assert api.db.scalar("SELECT count(*) FROM agent_executions") == 1


def test_editing_the_agent_during_a_websocket_job_does_not_change_that_job(api):
    agent = make_agent(api)
    gate, started = threading.Event(), threading.Event()
    api.provider.gate, api.provider.started = gate, started
    with connect(api) as ws:
        login(ws, api)
        job(ws, "writer", "long job")
        assert started.wait(5)
        r = api.client.patch(f"/agents/{agent['id']}", json={"base_version": 1, "definition": definition(role="Edited while running",
                             instructions="You are a pirate now. Always answer in pirate speak, never anything else.")}, headers=api.auth(*A))
        assert r.status_code == 200 and r.json()["current_version"] == 2
        gate.set()
        frame = ws.receive_json()
    assert execution(api, frame["execution_id"]).json()["agent_version"] == 1
    assert "Product marketer" in api.provider.requests[0].system and "pirate" not in api.provider.requests[0].system
    api.provider.gate = None
    with connect(api) as ws:
        login(ws, api)
        job(ws, "writer", "next")
        assert execution(api, ws.receive_json()["execution_id"]).json()["agent_version"] == 2


def test_a_disconnect_cancels_the_job_and_leaves_no_running_record(api):
    make_agent(api)
    started = threading.Event()
    api.provider.gate, api.provider.started = threading.Event(), started  # the gate never opens
    with connect(api) as ws:
        login(ws, api)
        job(ws, "writer", "abandoned")
        assert started.wait(5)
        assert api.db.scalar("SELECT status FROM agent_executions") == "running"
    deadline = time.time() + 5
    while time.time() < deadline and api.db.scalar("SELECT count(*) FROM agent_executions WHERE status = 'running'"):
        time.sleep(0.05)
    assert api.db.rows("SELECT status, error_code, completed_at IS NOT NULL FROM agent_executions") == [("cancelled", "cancelled", 1)]
    assert run(api, "sales", "slot free again").status_code == 200  # the concurrency slot was released too


def test_the_websocket_handshake_and_limits_are_unchanged(api):
    make_agent(api)
    with pytest.raises(WebSocketDisconnect) as info:
        with api.client.websocket_connect("/ws", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_json()
    assert info.value.code == 4403
    with connect(api) as ws:
        ws.send_json({"type": "auth", "token": "junk"})
        with pytest.raises(WebSocketDisconnect) as info:
            ws.receive_json()
        assert info.value.code == 4401
    assert api.db.scalar("SELECT count(*) FROM agent_executions") == 0
