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


# ---------------------------------------------------------------- sprint 5 closure: approvals, message binding, reconciliation audit for the REAL email tool

from agentplatform.tools.approvals import ApprovalStore, payload_hash  # noqa: E402
from agentplatform.tools.builtin import build_default_registry  # noqa: E402
from agentplatform.tools.invoker import ApprovalUnusable, ToolStore, canonical  # noqa: E402
from tests.test_approvals import context as approval_context, human, invoke as invoke_tool, stack as approval_stack  # noqa: E402
import hashlib  # noqa: E402
import uuid  # noqa: E402


@pytest.fixture
def gated(mock, world, monkeypatch):
    """The REAL email tool in a registry that keeps the default approval policy (the dispatch tests above bypass approval on purpose)."""
    registry = build_default_registry()
    er.register(registry, er.EmailConfig(api_key=KEY, sender="AgentForge <sandbox@resend.dev>", allowed_domains=("resend.dev",), url=mock.url, allow_insecure_test_url=True))
    monkeypatch.setattr(tools_pkg, "default_registry", lambda: registry)
    import importlib

    monkeypatch.setattr(importlib.import_module("agentplatform.tools"), "default_registry", lambda: registry)
    assert registry.gated(registry.get("email.send"))
    return registry


def _args_hash(args):
    return hashlib.sha256(canonical(args).encode()).hexdigest()


def test_without_an_approval_path_the_email_is_refused_and_nothing_is_sent(gated, mock, world):
    ctx = approval_context(world, gated, tools=("email.send",))
    _, no_approvals, _ = approval_stack(world, gated, approvals=False)
    out = invoke_tool(no_approvals, ctx, WIRE, ARGS)
    assert (out.status, out.error_code) == ("rejected", "approval_required") and mock.sent == 0 and mock.requests == []


def test_a_rejected_email_approval_sends_nothing_and_an_approved_one_sends_exactly_once_with_the_operations_key(gated, mock, world):
    store, invoker, _ = approval_stack(world, gated)
    ctx = approval_context(world, gated, tools=("email.send",))
    h = human(world, store, "reject")
    refused = invoke_tool(invoker, ctx, WIRE, ARGS)
    h.join()
    assert refused.status == "rejected" and mock.sent == 0 and mock.requests == []

    ctx2 = approval_context(world, gated, tools=("email.send",))
    h = human(world, store, "approve")
    done = invoke_tool(invoker, ctx2, WIRE, ARGS)
    h.join()
    assert done.status == "succeeded" and mock.sent == 1 and len(mock.requests) == 1
    assert mock.requests[0]["key"] == world["db"].scalar("SELECT idempotency_key FROM tool_dispatches") and mock.requests[0]["body"]["to"] == ["delivered@resend.dev"]
    assert [r[0] for r in world["db"].rows("SELECT status FROM tool_approvals ORDER BY created_at")] == ["rejected", "consumed"]


def test_an_approval_authorises_only_the_exact_message_not_a_changed_recipient_subject_or_text(gated, mock, world):
    spec = gated.get("email.send")
    store = ApprovalStore(world["sf"])
    ctx = approval_context(world, gated, tools=("email.send",))
    approved_hash = _args_hash(ARGS)
    approval_id = store.request(ctx, spec, 1, "{}", approved_hash, None)
    store.decide(world["sa"], approval_id, "approve")
    variants = {"recipient": {**ARGS, "to": "bounced@resend.dev"}, "subject": {**ARGS, "subject": "A different subject"}, "text": {**ARGS, "text": "Different words."}}
    for name, changed in variants.items():
        assert _args_hash(changed) != approved_hash, name
        with pytest.raises(ApprovalUnusable) as info:
            ToolStore(world["sf"]).begin(ctx, spec, 1, "{}", _args_hash(changed), uuid.uuid4().hex * 2, approval_id)  # the model changed the message after approval
        assert info.value.reason == "invalid", name
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations") == 0 and mock.sent == 0
    assert payload_hash(workspace_id=uuid.uuid4(), user_id=uuid.uuid4(), execution_id=uuid.uuid4(), seq=1, run_id=None, task_key=None, agent_key="x", agent_version_id=None,
                        tool_id="email.send", tool_version=1, args_hash=approved_hash) != approved_hash


def test_a_non_sandbox_recipient_and_a_malformed_message_never_reach_the_network_or_the_approval_queue(gated, mock, world):
    store, invoker, _ = approval_stack(world, gated)
    ctx = approval_context(world, gated, tools=("email.send",))
    out = invoke_tool(invoker, ctx, WIRE, {**ARGS, "to": "customer@example.com"})
    assert mock.requests == [] and mock.sent == 0
    out_bad = invoke_tool(invoker, approval_context(world, gated, tools=("email.send",)), WIRE, {**ARGS, "to": "a@resend.dev, b@resend.dev"})
    assert out_bad.status == "rejected" and mock.requests == []  # ONE recipient only, validated before any approval is requested
    assert world["db"].scalar("SELECT count(*) FROM tool_approvals WHERE status = 'pending'") == 0
    assert out.status in ("rejected", "failed")


@pytest.mark.parametrize("decision", ["confirmed_succeeded", "confirmed_failed"])
def test_an_unknown_email_outcome_reconciles_either_way_with_an_audit_row_and_nothing_is_sent_again(mock, email_rig, world, decision):
    mock.mode = "accept_then_drop"
    _, svc = process(world, email_rig, script([asks(call(WIRE, ARGS)), final("Sent.")]))
    first = asyncio.run(_run(world, svc, plan(world, required_tool="email.send@1")))
    assert first.status == "failed" and states(world) == ["unknown"] and mock.sent == 1
    invocation_id = uuid.UUID(world["db"].scalar("SELECT invocation_id FROM tool_dispatches"))
    view = dp.reconcile(world["sf"], world["sa"], invocation_id, decision=decision, evidence_kind="operator_attestation",
                        evidence="Checked the Resend sandbox dashboard by hand.", reason="Recorded by the workspace owner")
    expected = "reconciled_succeeded" if decision == "confirmed_succeeded" else "reconciled_failed"
    assert view["state"] == expected and view["reconciliation"]["attestation_only"] is True and states(world) == [expected]
    row = world["db"].rows("SELECT decision, evidence_kind, actor_user_id IS NOT NULL FROM tool_reconciliations")
    assert row == [(decision, "operator_attestation", 1)]  # one audit row with the REAL actor, a statement and not proof
    assert mock.sent == 1 and len(mock.requests) == 1  # reconciliation never sends and nothing was re-dispatched automatically
    assert world["db"].scalar("SELECT count(*) FROM tool_dispatches") == 1
    # the model's own words never turn an unknown/reconciled-by-statement outcome into a verified success of the task
    assert world["db"].scalar("SELECT error_code FROM plan_task_runs") == "side_effect_outcome_unknown"
