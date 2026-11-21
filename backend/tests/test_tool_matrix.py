"""Sprint 5, workstream B: one matrix over EVERY registered tool (built-ins + the Resend email tool with a placeholder key; nothing is called, no network).

Invariants checked per tool: addressable versioned ref, JSON-serialisable schemas, a documented honest limit list, an example that VALIDATES against the input model,
bounded timeout/output, side-effecting tools are gated by an approval of the exact action, name a retry policy, never claim exactly-once, and are never offered to a
model unless they can run."""

import json

import pytest

from agentplatform.tools import default_registry
from agentplatform.tools import email_resend
from agentplatform.tools.registry import ToolRegistry


@pytest.fixture
def registry():
    reg = default_registry()
    if reg.get("email.send") is None:
        email_resend.register(reg, email_resend.EmailConfig(api_key="re_placeholder_not_a_key", sender="AgentForge <sandbox@resend.dev>"))
    return reg


def specs(reg):
    return reg.list_specs(include_disabled=True)


def test_registry_has_real_tools_and_the_email_tool(registry):
    ids = {s.id for s in specs(registry)}
    assert "email.send" in ids and len(ids) >= 3


def test_every_tool_is_versioned_documented_and_its_example_validates(registry):
    for spec in specs(registry):
        assert spec.ref == f"{spec.id}@{spec.version}" and registry.pin(spec.id).startswith(spec.id + "@")
        assert registry.resolve_pin(spec.ref) is spec
        json.dumps(spec.input_schema())  # serialisable, so the Studio can render it and a model can be offered it
        json.dumps(spec.output_schema())
        assert spec.description.strip() and spec.summary.strip() and spec.limitations, f"{spec.ref} must state what it cannot do"
        spec.input_model.model_validate(spec.example)  # the documented example is a real, valid request
        assert 0 < spec.timeout_seconds <= 30 and 0 < spec.max_output_bytes <= 64000
        assert spec.input_model.model_config.get("extra") == "forbid"


def test_side_effecting_tools_are_gated_name_a_retry_policy_and_never_claim_exactly_once(registry):
    side = [s for s in specs(registry) if s.side_effect != "none"]
    assert side, "the matrix must cover at least the email tool"
    for spec in side:
        assert registry.gated(spec), f"{spec.ref}: a side effect needs the approval of the exact action"
        assert spec.retry_policy in ("never", "after_confirmed_failure")
        words = (spec.delivery + " " + spec.description + " " + " ".join(spec.limitations)).lower()
        assert "exactly once" not in words.replace("no exactly-once", "").replace("not exactly once", "").replace("never exactly once", ""), spec.ref
        assert spec.timeout_seconds <= 30  # a call that can hang has a hard bound; reconciliation covers the rest


def test_reads_are_never_gated_and_every_offered_tool_can_run(registry):
    for spec in specs(registry):
        if spec.side_effect == "none":
            assert not registry.gated(spec)
        if registry.executable(spec):
            assert callable(registry.handler(spec))


def test_the_email_tool_is_sandboxed_by_default_and_declares_its_limits(registry):
    spec = registry.get("email.send")
    assert spec.side_effect != "none"
    assert spec.retry_policy == "never"  # a lost answer is reconciled by a person, never re-sent automatically
    assert spec.remote_idempotency is True and registry.lookup(spec) is None  # Resend honours Idempotency-Key but documents no lookup by key: an unknown outcome stays a person's decision
    assert "resend.dev" in json.dumps(spec.example)  # the example only uses the provider's sandbox recipient
