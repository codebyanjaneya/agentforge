"""Sprint 4: the real side-effecting integration ``email.send@1`` (Resend). CONTRACT tests against a LOCAL MOCK that models ONLY what Resend documents (resend.com/docs, read 2026-11-20): Bearer auth, the
``Idempotency-Key`` header (1-256 chars; same key + same payload = the original response and NO second send; same key + different payload = 409 invalid_idempotent_request; a request while another with the same key is
in progress = 409 concurrent_idempotent_requests; bad key length = 400 invalid_idempotency_key) and ``{"id": ...}`` on success. NOTHING real is contacted: the mock listens on loopback. THIS DOES NOT VERIFY THE LIVE
SERVICE: whether Resend really behaves as documented, and delivery itself, are unverified until a sandbox key is used (docs/operations.md section 16 checklist)."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import agentplatform.tools as tools_pkg
from agentplatform.tools import ToolRegistry, ToolPolicy
from agentplatform.tools import dispatch as dp
from agentplatform.tools import email_resend as er
from agentplatform.tools.registry import ToolInputError
from agentplatform.plan_runs import ReconciliationRequired
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_durable_dispatch import OpenPolicy, _run, invocation_statuses, plan, process, retry, script, states
from tests.test_plan_runs import definition, world  # noqa: F401  (world is a fixture)
from tests.test_tools import asks, call, final

KEY = "test-key-NOT-A-REAL-SECRET-0123456789"
WIRE = "email__send__v1"
ARGS = {"to": "delivered@resend.dev", "subject": "Your launch date", "text": "The launch is confirmed for next week."}


class Mock:
    """A local stand-in for api.resend.com that follows the documented semantics and records everything it receives."""

    def __init__(self):
        self.sent, self.requests, self.by_key, self.mode, self.next_id = 0, [], {}, "ok", 0
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                key = self.headers.get("Idempotency-Key")
                mock.requests.append({"auth": self.headers.get("Authorization"), "key": key, "body": body, "path": self.path})
                if self.headers.get("Authorization") != "Bearer " + KEY:
                    return self.reply(401, {"name": "missing_api_key"})
                if key is None or not 1 <= len(key) <= 256:
                    return self.reply(400, {"name": "invalid_idempotency_key"})
                if mock.mode == "reject":
                    return self.reply(422, {"name": "validation_error"})
                if mock.mode == "429":
                    return self.reply(429, {"name": "rate_limit_exceeded"})
                if mock.mode == "concurrent":
                    return self.reply(409, {"name": "concurrent_idempotent_requests"})
                if mock.mode == "5xx":
                    return self.reply(500, {"name": "application_error"})
                if key in mock.by_key:
                    prior_body, prior_id = mock.by_key[key]
                    if prior_body != body:
                        return self.reply(409, {"name": "invalid_idempotent_request"})
                    return self.reply(200, {"id": prior_id})  # documented: the original response, nothing is sent again
                mock.sent += 1
                mock.next_id += 1
                email_id = f"00000000-0000-4000-8000-{mock.next_id:012d}"
                mock.by_key[key] = (body, email_id)
                if mock.mode == "accept_then_drop":
                    self.close_connection = True  # the request WAS accepted; the answer never arrives
                    self.connection.close()
                    return
                if mock.mode == "garbage_2xx":
                    return self.reply(200, "not-json-at-all")
                self.reply(200, {"id": email_id})

            def reply(self, status, payload):
                data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/emails"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def mock(monkeypatch):
    m = Mock()
    monkeypatch.undo()  # lift the suite's network guard: loopback mock only
    er.configure(er.EmailConfig(api_key=KEY, sender="AgentForge <sandbox@resend.dev>", allowed_domains=("resend.dev",), url=m.url, allow_insecure_test_url=True))
    yield m
    er.configure(None)
    m.close()


def dispatch(key="af-" + "a" * 48 + "-g1"):
    return SimpleNamespace(idempotency_key=key)


def send(to="delivered@resend.dev", key=None, **kw):
    return er.send_email(er.SendEmailIn(to=to, subject=kw.get("subject", "Hi"), text=kw.get("text", "Body")), dispatch(key) if key else dispatch())


def test_a_documented_request_shape_and_success(mock):
    out = send(key="op-key-1")
    assert out.email_id.endswith("000000000001") and out.live and mock.sent == 1
    r = mock.requests[0]
    assert r["auth"] == "Bearer " + KEY and r["key"] == "op-key-1" and r["path"] == "/emails"
    assert r["body"] == {"from": "AgentForge <sandbox@resend.dev>", "to": ["delivered@resend.dev"], "subject": "Hi", "text": "Body"}


def test_the_same_key_with_the_same_payload_is_not_sent_twice_as_the_provider_documents(mock):
    first, second = send(key="same-key"), send(key="same-key")
    assert first.email_id == second.email_id and mock.sent == 1 and len(mock.requests) == 2 and mock.requests[0]["key"] == mock.requests[1]["key"]


@pytest.mark.parametrize("mode", ["reject", "429"])
def test_a_provider_rejection_is_a_definitive_failure(mock, mode):
    mock.mode = mode
    with pytest.raises(dp.DefinitiveFailure):
        send()
    assert mock.sent == 0


@pytest.mark.parametrize("mode", ["5xx", "concurrent", "accept_then_drop", "garbage_2xx"])
def test_anything_that_may_have_been_accepted_is_an_unknown_outcome_never_a_definitive_failure(mock, mode):
    mock.mode = mode
    with pytest.raises(RuntimeError) as unknown:
        send()
    assert not isinstance(unknown.value, dp.DefinitiveFailure)
    assert KEY not in str(unknown.value)


def test_the_same_key_with_a_different_payload_is_unknown_not_failed(mock):
    send(key="reused", subject="One")
    with pytest.raises(RuntimeError) as e:
        send(key="reused", subject="Two")  # 409 invalid_idempotent_request: the key was already used, so something may have been sent
    assert not isinstance(e.value, dp.DefinitiveFailure) and mock.sent == 1


def test_recipients_outside_the_allowed_domains_never_reach_the_network(mock):
    with pytest.raises(ToolInputError):
        send(to="customer@example.com")
    assert mock.requests == []


def test_it_refuses_to_send_without_the_operations_stable_key_or_without_configuration(mock):
    with pytest.raises(dp.DefinitiveFailure):
        er.send_email(er.SendEmailIn(to="delivered@resend.dev", subject="s", text="t"), SimpleNamespace(idempotency_key=None))
    with pytest.raises(dp.DefinitiveFailure):
        er.send_email(er.SendEmailIn(to="delivered@resend.dev", subject="s", text="t"), SimpleNamespace(idempotency_key="k" * 257))
    er.configure(None)
    with pytest.raises(dp.DefinitiveFailure):
        send()
    assert mock.requests == []


@pytest.mark.parametrize("bad", [dict(to="a@b.co, c@d.co"), dict(to="not-an-email"), dict(subject="line1\nBcc: x@y.co")])
def test_input_is_validated_against_injection(bad):
    base = dict(to="delivered@resend.dev", subject="s", text="t")
    with pytest.raises(Exception):
        er.SendEmailIn(**{**base, **bad})


def test_a_non_https_provider_url_is_refused_outside_tests():
    with pytest.raises(ValueError):
        er.configure(er.EmailConfig(api_key="k", sender="a@b.co", url="http://api.resend.com/emails"))


def test_the_tool_spec_states_its_real_guarantee_and_has_no_lookup():
    spec = er.EMAIL_SEND
    assert spec.risk == "external" and spec.side_effect == "non_idempotent" and spec.retry_policy == "never" and spec.remote_idempotency is True
    registry = ToolRegistry(ToolPolicy(), allow_gated=True)
    er.register(registry, er.EmailConfig(api_key="k", sender="a@b.co"))
    assert registry.lookup(spec) is None and registry.gated(spec)  # needs a human for every call; no key-based lookup exists
    assert "NOT exactly-once" in spec.delivery and "24" in spec.delivery
    er.configure(None)


# ---------------------------------------------------------------- through the durable dispatch protocol (real runtime, real database)

@pytest.fixture
def email_rig(mock, world, monkeypatch):  # `mock` first: it lifts the suite's network guard, which must happen before the world's own patches
    registry = ToolRegistry(OpenPolicy(), allow_gated=True)  # the approval path is covered by the existing suite; the dispatch protocol is under test here
    registry.register(er.EMAIL_SEND, er.send_email, takes_dispatch=True)
    monkeypatch.setattr(tools_pkg, "default_registry", lambda: registry)
    world["agents"].create(world["sa"], "sender", definition(allowed_tools=["email.send"]))
    return registry


def test_through_the_runtime_a_success_dispatches_once_with_the_operations_key_and_leaks_no_secret(mock, email_rig, world):
    _, svc = process(world, email_rig, script([asks(call(WIRE, ARGS)), final("Sent.")]))
    view = asyncio.run(_run(world, svc, plan(world, required_tool="email.send@1")))
    assert view.status == "succeeded" and mock.sent == 1 and states(world) == ["succeeded"]
    stored_key = world["db"].scalar("SELECT idempotency_key FROM tool_dispatches")
    assert mock.requests[0]["key"] == stored_key  # the provider received exactly the operation's stable key
    assert world["db"].scalar("SELECT external_ref FROM tool_dispatches").endswith("000000000001")  # the provider's email id is the external reference
    everything = json.dumps(world["db"].rows("SELECT input, output, error_message FROM tool_invocations")) + json.dumps(world["db"].rows("SELECT detail FROM execution_events"), default=str)
    assert KEY not in everything  # the API key is in no stored column and no event


def test_through_the_runtime_a_lost_response_is_unknown_blocks_the_retry_and_is_never_sent_again(mock, email_rig, world):
    mock.mode = "accept_then_drop"
    _, svc = process(world, email_rig, script([asks(call(WIRE, ARGS)), final("Sent.")], [asks(call(WIRE, ARGS)), final("Sent.")]))
    first = asyncio.run(_run(world, svc, plan(world, required_tool="email.send@1")))
    assert first.status == "failed" and states(world) == ["unknown"] and mock.sent == 1  # the provider accepted it; we cannot prove that
    assert world["db"].scalar("SELECT error_code FROM plan_task_runs") == "side_effect_outcome_unknown"
    mock.mode = "ok"
    with pytest.raises(ReconciliationRequired):
        retry(world, svc, first.id)
    assert mock.sent == 1 and len(mock.requests) == 1  # nothing, not even a same-key replay, was sent again


def test_through_the_runtime_a_provider_rejection_is_a_confirmed_failure_that_is_not_retried(mock, email_rig, world):
    mock.mode = "reject"
    _, svc = process(world, email_rig, script([asks(call(WIRE, ARGS)), final("Sent.")]))
    view = asyncio.run(_run(world, svc, plan(world, required_tool="email.send@1")))
    assert view.status == "failed" and states(world) == ["failed"] and invocation_statuses(world) == ["failed"] and mock.sent == 0
    assert world["db"].scalar("SELECT error_code FROM plan_task_runs") == "side_effect_failed"
