"""M1-F: /ws Origin policy, first-message authentication, identity binding, limits, jobs and cleanup."""

import asyncio
import sys
import threading
import time
import types
import uuid

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.api_support import ORIGIN, api, auth_db, main_module  # noqa: F401  (fixtures)
from tests.auth_support import make_token, new_keypair

HEADERS = {"origin": ORIGIN}


def connect(api, headers=HEADERS):
    return api.client.websocket_connect("/ws", headers=headers)


def expect_close(ws, code):
    with pytest.raises(WebSocketDisconnect) as info:
        ws.receive_json()
    assert info.value.code == code
    assert not info.value.reason  # no text about why
    return info.value


def login(ws, api, sub="user-a", email="a@example.com", **claims):
    ws.send_json({"type": "auth", "token": api.token(sub, email, **claims)})
    assert ws.receive_json() == {"type": "authenticated"}


def rt():
    return sys.modules["realtime"]


def frame(ws):
    """A job frame without its ``execution_id`` (asserted to be a UUID): everything else is the existing protocol."""
    message = ws.receive_json()
    if "execution_id" in message:
        uuid.UUID(message.pop("execution_id"))
    return message


# ---------------------------------------------------------------- Origin

def test_missing_or_disallowed_origin_is_rejected_before_any_frame(api):
    for headers in ({}, {"origin": "http://evil.example"}, {"origin": "null"}, {"origin": "*"},
                    {"origin": ORIGIN + "/"}, {"origin": ORIGIN.upper()}, {"origin": "http://localhost:3000.evil.example"}):
        with connect(api, headers) as ws:
            expect_close(ws, 4403)
    assert api.db.counts()["users"] == 0


def test_a_valid_token_does_not_bypass_the_origin_check(api):
    with connect(api, {"origin": "http://evil.example"}) as ws:
        try:
            ws.send_json({"type": "auth", "token": api.token()})
        except Exception:  # noqa: BLE001 - the server may already have closed
            pass
        expect_close(ws, 4403)
    assert api.db.counts()["users"] == 0


def test_an_empty_origin_allow_list_fails_closed(api, monkeypatch):
    monkeypatch.setattr(api.main, "settings", api.main.settings.model_copy(update={"allowed_origins": ()}))
    for headers in ({"origin": ORIGIN}, {"origin": "*"}, {}):
        with connect(api, headers) as ws:
            expect_close(ws, 4403)


def test_a_valid_origin_is_accepted(api):
    with connect(api) as ws:
        login(ws, api)


# ---------------------------------------------------------------- first-message authentication

def test_the_authentication_deadline_is_exactly_five_seconds(api, monkeypatch):
    module = rt()
    assert module.AUTH_TIMEOUT_SECONDS == 5
    seen = []
    real = asyncio

    async def spy(awaitable, timeout=None):
        seen.append(timeout)
        return await real.wait_for(awaitable, timeout)

    proxy = types.SimpleNamespace(**{n: getattr(real, n) for n in dir(real) if not n.startswith("__")})
    proxy.wait_for = spy
    monkeypatch.setattr(module, "asyncio", proxy)
    with connect(api) as ws:
        login(ws, api)
    assert seen == [5]  # the very first frame is awaited with exactly 5 seconds


def test_silence_for_the_deadline_closes_with_4401(api, monkeypatch):
    monkeypatch.setattr(rt(), "AUTH_TIMEOUT_SECONDS", 0.3)
    started = time.monotonic()
    with connect(api) as ws:
        expect_close(ws, 4401)
    assert 0.25 <= time.monotonic() - started < 3


def test_missing_and_invalid_first_message_credentials_are_rejected(api):
    now = int(time.time())
    bad_frames = [
        {"type": "auth"}, {"type": "auth", "token": ""}, {"type": "auth", "token": None}, {"type": "auth", "token": 5},
        {"type": "auth", "token": "not-a-jwt"}, {"token": api.token()}, {"type": "hello", "token": api.token()}, {},
        {"type": "auth", "token": make_token(new_keypair()[0])},
        {"type": "auth", "token": make_token(api.private, claims={"iat": now - 900, "exp": now - 300})},
        {"type": "auth", "token": make_token(api.private, claims={"iss": "other"})},
        {"type": "auth", "token": make_token(api.private, claims={"aud": "other"})},
        [1, 2], "just a string", 7,
    ]
    for frame in bad_frames:
        with connect(api) as ws:
            ws.send_json(frame)
            expect_close(ws, 4401)
    for raw in (b"\x00\x01", ):
        with connect(api) as ws:
            ws.send_bytes(raw)
            expect_close(ws, 4401)
    with connect(api) as ws:
        ws.send_text("{not json")
        expect_close(ws, 4401)
    assert api.db.counts()["users"] == 0  # nothing provisioned by any rejected attempt


def test_email_based_first_messages_are_rejected_and_run_nothing(api):
    token = api.token()
    for frame in ({"type": "auth", "user_email": "a@example.com"}, {"agent": "ceo", "task": "t", "user_email": "a@example.com"},
                  {"type": "auth", "token": token, "user_email": "a@example.com"},
                  {"type": "auth", "token": token, "email": "a@example.com"},
                  {"type": "auth", "token": token, "workspace_id": "00000000-0000-0000-0000-000000000001"}):
        with connect(api) as ws:
            ws.send_json(frame)
            expect_close(ws, 4401)
    assert api.seen_scopes == [] and api.db.counts()["users"] == 0


def test_a_valid_token_binds_the_correct_principal_and_workspace(api):
    uid_a, ws_a = api.provision("user-a", "a@example.com")
    uid_b, ws_b = api.provision("user-b", "b@example.com")
    with connect(api) as ws:
        login(ws, api, "user-b", "b@example.com")
        ws.send_json({"agent": "sales", "task": "hello"})
        assert frame(ws) == {"agent": "sales", "result": "reply from sales: hello"}
    (name, scope), = api.seen_scopes
    assert (scope.user_id, scope.workspace_id) == (uid_b, ws_b)
    assert [r[0] for r in api.db.rows("SELECT summary FROM agent_memories")] == ["reply:hello"]
    assert api.db.scalar("SELECT user_id FROM agent_memories") == uid_b.hex


def test_later_identity_switch_attempts_are_refused_or_ignored(api):
    uid_a, ws_a = api.provision("user-a", "a@example.com")
    uid_b, _ = api.provision("user-b", "b@example.com")
    with connect(api) as ws:
        login(ws, api, "user-a", "a@example.com")
        ws.send_json({"type": "auth", "token": api.token("user-b", "b@example.com")})
        assert ws.receive_json() == {"error": "Already authenticated."}
        ws.send_json({"agent": "ceo", "task": "t", "user_email": "b@example.com", "email": "b@example.com",
                      "workspace_id": str(uuid_b()), "user_id": str(uid_b), "token": api.token("user-b", "b@example.com")})
        assert ws.receive_json()["agent"] == "ceo"
    assert all(scope.user_id == uid_a and scope.workspace_id == ws_a for _, scope in api.seen_scopes)


def uuid_b():
    import uuid
    return uuid.uuid4()


def test_workspace_membership_and_activity_are_verified_at_connect(api):
    uid, ws_id = api.provision("user-a", "a@example.com")
    api.db.run("DELETE FROM workspace_memberships WHERE workspace_id = :w", w=ws_id.hex)
    with connect(api) as ws:
        ws.send_json({"type": "auth", "token": api.token()})
        expect_close(ws, 4403)
    uid2, ws2 = api.provision("user-c", "c@example.com")
    api.db.run("UPDATE users SET is_active = 0 WHERE id = :u", u=uid2.hex)
    with connect(api) as ws:
        ws.send_json({"type": "auth", "token": api.token("user-c", "c@example.com")})
        expect_close(ws, 4403)


def test_the_binding_ends_when_the_token_expires(api):
    now = int(time.time())
    with connect(api) as ws:
        login(ws, api, iat=now - 10, exp=now + 3)  # (int(now) floors, so the usable window is 2-3 s)
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        time.sleep(3.4)
        ws.send_json({"agent": "ceo", "task": "late"})
        expect_close(ws, 4401)
    assert api.seen_scopes == []


# ---------------------------------------------------------------- limits, jobs, cleanup

def test_oversized_messages_close_with_1009(api, monkeypatch):
    monkeypatch.setattr(rt(), "MAX_MESSAGE_BYTES", 1000)
    with connect(api) as ws:  # after authentication
        login(ws, api)
        ws.send_json({"agent": "ceo", "task": "x" * 1001})
        expect_close(ws, 1009)
    with connect(api) as ws:  # as the first frame
        ws.send_text("x" * 1001)
        expect_close(ws, 1009)
    with connect(api) as ws:  # multi-byte characters count as bytes
        login(ws, api)
        ws.send_json({"agent": "ceo", "task": "é" * 600})
        expect_close(ws, 1009)
    with connect(api) as ws:  # a message at the limit is fine
        login(ws, api)
        ws.send_json({"agent": "ceo", "task": "x" * 900})
        assert ws.receive_json()["agent"] == "ceo"
    assert api.seen_scopes and len(api.seen_scopes) == 1


def test_default_message_limit_is_64_kib(api):
    assert rt().MAX_MESSAGE_BYTES == 65536


def test_only_one_job_runs_per_connection_and_ping_still_answers(api):
    gate, started = threading.Event(), threading.Event()

    async def slow(task, scope):
        started.set()
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return f"done {task}"

    api.main.orchestrator._agents["sales"] = slow
    valve = threading.Timer(4, gate.set)  # safety valve: a missing "already running" refusal must fail, not hang
    valve.daemon = True
    valve.start()
    with connect(api) as ws:
        login(ws, api)
        ws.send_json({"agent": "sales", "task": "one"})
        assert started.wait(5)
        ws.send_json({"agent": "sales", "task": "two"})
        assert ws.receive_json() == {"error": "A job is already running on this connection."}
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        gate.set()
        assert frame(ws) == {"agent": "sales", "result": "done one"}
        ws.send_json({"agent": "sales", "task": "three"})  # free again
        assert frame(ws) == {"agent": "sales", "result": "done three"}
    valve.cancel()


def test_disconnect_cancels_the_running_job(api):
    started, cancelled, finished = threading.Event(), threading.Event(), threading.Event()

    async def forever(task, scope):
        started.set()
        try:
            while True:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            finished.set()

    api.main.orchestrator._agents["sales"] = forever
    with connect(api) as ws:
        login(ws, api)
        ws.send_json({"agent": "sales", "task": "x"})
        assert started.wait(5)
    assert cancelled.wait(5) and finished.wait(5)  # leaving the block closed the socket


def test_handler_returns_and_cancels_the_job_when_the_peer_disconnects(api):
    """Drives the handler directly (no TestClient): its teardown cancels everything and so masks a missing cancel."""
    uid, wid = api.provision()
    state = {"started": False, "cancelled": False}

    async def forever(task, scope):
        state["started"] = True
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    api.main.orchestrator._agents["sales"] = forever

    class FakeSocket:
        headers = {"origin": ORIGIN}

        def __init__(self):
            self.inbox, self.sent, self.closed = asyncio.Queue(), [], None

        async def accept(self):
            pass

        async def receive(self):
            return await self.inbox.get()

        async def send_json(self, data):
            self.sent.append(data)

        async def close(self, code=1000):
            self.closed = code

    async def scenario():
        import json as _json

        sock = FakeSocket()
        await sock.inbox.put({"type": "websocket.receive", "text": _json.dumps({"type": "auth", "token": api.token()})})
        handler = asyncio.create_task(rt().handle_websocket(
            sock, allowed_origins=[ORIGIN], auth_config=api.main.app.dependency_overrides[api.main.get_auth_config](),
            session_factory=api.db.session_factory,
            executions=api.main.build_execution_service(api.db.session_factory)))
        for _ in range(500):
            if sock.sent:
                break
            await asyncio.sleep(0.01)
        assert sock.sent == [{"type": "authenticated"}]
        await sock.inbox.put({"type": "websocket.receive", "text": _json.dumps({"agent": "sales", "task": "x"})})
        for _ in range(500):
            if state["started"]:
                break
            await asyncio.sleep(0.01)
        assert state["started"]
        await sock.inbox.put({"type": "websocket.disconnect", "code": 1001})
        # asyncio.wait never cancels: wait_for would cancel a stuck handler, which also cancels its awaited job and
        # lets the handler swallow that cancellation, hiding a missing job.cancel().
        done, _ = await asyncio.wait({handler}, timeout=2)
        finished, was_cancelled = handler in done, state["cancelled"]
        if not finished:  # cleanup only; the assertions below already describe the failure
            handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)
        assert finished, "the handler did not return after the peer disconnected (running job not cancelled)"
        assert was_cancelled, "the running job was not cancelled"
        return sock

    sock = asyncio.run(scenario())
    assert state["cancelled"] is True
    assert api.db.rows("SELECT status, error_code FROM agent_executions") == [("cancelled", "cancelled")]  # recorded, not left running
    assert sock.sent == [{"type": "authenticated"}]  # nothing else was sent to a gone peer


def test_other_close_paths_also_cancel_the_job(api, monkeypatch):
    started, cancelled = threading.Event(), threading.Event()

    async def forever(task, scope):
        started.set()
        try:
            while True:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    api.main.orchestrator._agents["sales"] = forever
    monkeypatch.setattr(rt(), "MAX_MESSAGE_BYTES", 1000)
    with connect(api) as ws:
        login(ws, api)
        ws.send_json({"agent": "sales", "task": "x"})
        assert started.wait(5)
        ws.send_json({"agent": "sales", "task": "y" * 2000})  # oversized: the server closes the socket
        expect_close(ws, 1009)
    assert cancelled.wait(5)


def test_invalid_job_messages_get_fixed_errors_and_the_socket_stays_usable(api):
    with connect(api) as ws:
        login(ws, api)
        for frame, expected in [({"agent": "ceo"}, "Both 'agent' and 'task' are required."),
                                ({"task": "x"}, "Both 'agent' and 'task' are required."),
                                ({"agent": 5, "task": "x"}, "Both 'agent' and 'task' are required."),
                                ({"agent": "nope", "task": "x"}, "Unknown agent.")]:
            ws.send_json(frame)
            assert ws.receive_json() == {"error": expected}
        ws.send_text("not json")
        assert ws.receive_json() == {"error": "Invalid message."}
        ws.send_json([1])
        assert ws.receive_json() == {"error": "Invalid message."}
        ws.send_bytes(b"x")
        assert ws.receive_json() == {"error": "Invalid message."}
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
    assert api.seen_scopes == []


def test_no_token_claim_key_or_driver_detail_reaches_the_client(api):
    secret = "SECRET-DRIVER-TEXT sqlite3.OperationalError postgresql://u:pw@host"

    async def explode(task, scope):
        raise RuntimeError(secret)

    api.main.orchestrator._agents["sales"] = explode
    token = api.token()
    seen = []
    with connect(api) as ws:
        ws.send_json({"type": "auth", "token": token})
        seen.append(ws.receive_json())
        ws.send_json({"agent": "sales", "task": "x"})
        seen.append(frame(ws))
        ws.send_json({"type": "auth", "token": token})
        seen.append(ws.receive_json())
        ws.send_json({"agent": "ceo", "task": "ok"})
        seen.append(frame(ws))
    assert seen[1] == {"agent": "sales", "error": "Agent failed."}
    blob = str(seen)
    for forbidden in (secret, "SECRET", "sqlite", "postgresql", token, "a@example.com", "BEGIN", "kid", "user-a"):
        assert forbidden not in blob, forbidden
    with connect(api) as ws:
        ws.send_json({"type": "auth", "token": token[:-3] + "abc"})
        err = expect_close(ws, 4401)
    assert "Signature" not in str(err) and token not in str(err)


def test_an_authentication_configuration_failure_fails_closed(api):
    from fastapi import HTTPException

    def broken():
        raise HTTPException(status_code=500, detail="Authentication is not available")

    api.main.app.dependency_overrides[api.main.get_auth_config] = broken
    with connect(api) as ws:  # Origin is still checked first; then the server closes with 1011 (internal failure)
        expect_close(ws, 1011)
    with connect(api, {"origin": "http://evil.example"}) as ws:
        expect_close(ws, 4403)


def test_legacy_email_protocol_cannot_run_agents_even_after_authenticating(api):
    with connect(api) as ws:
        login(ws, api)
        ws.send_json({"type": "ping", "user_email": "b@example.com"})
        assert ws.receive_json() == {"type": "pong"}
        ws.send_json({"agent": "ceo", "task": "t", "user_email": "b@example.com"})
        assert ws.receive_json()["agent"] == "ceo"
    assert api.db.scalar("SELECT count(*) FROM auth_identities WHERE provider_subject = 'user-b'") == 0  # B never provisioned
    assert api.db.scalar("SELECT count(*) FROM users") == 1
