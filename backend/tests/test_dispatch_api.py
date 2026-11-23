"""P0 safety sprint over HTTP: an unknown side-effect outcome is shown as UNKNOWN (never as a retryable failure), blocks the retry, and is resolved only by an authorised, audited reconciliation."""

import importlib
import time

import pytest

from tests.api_support import api, auth_db, main_module  # noqa: F401  (fixtures)
from tests.test_plan_runs_api import A, B, papi, start, wait  # noqa: F401  (papi is a fixture)


def get(api, path, who=A, **kw):
    return api.client.get(path, headers=api.auth(*who), **kw)


def post(api, path, who=A, **kw):
    return api.client.post(path, headers=api.auth(*who), **kw)


@pytest.fixture
def unknown_run(papi):
    """The real demo journey, except that the (simulated) remote ACCEPTS the send and then the connection breaks: the outcome is genuinely unknown. Returns (run, invocation)."""
    demo = importlib.import_module("agentplatform.demo")
    tools = importlib.import_module("agentplatform.tools")
    fixtures = importlib.import_module("agentplatform.tools.fixtures")
    providers = importlib.import_module("agentplatform.providers")
    registry = tools.default_registry()
    real = fixtures.send_note

    def broken(args, dispatch=None):
        real(args, dispatch)  # the remote accepted it (recorded in OUTBOX / REMOTE) ...
        raise RuntimeError("connection reset before the answer arrived")  # ... and we never learned that

    registry.register(fixtures.DEMO_SEND_NOTE, broken, lookup=fixtures.lookup_note, takes_dispatch=True)
    fixtures.OUTBOX.clear()
    fixtures.REMOTE.clear()
    papi.main.providers = providers.ProviderRegistry({"groq": lambda: demo.DemoProvider()})
    try:
        for slug, definition, _ in demo.starter_definitions(True):
            assert post(papi, "/agents", json={"slug": slug, "definition": definition.model_dump(mode="json")}).status_code == 201
        pid = post(papi, "/plans", json={"objective": demo.EXAMPLE_OBJECTIVE}).json()["id"]
        assert post(papi, f"/plans/{pid}/approve", json={"revision": 1}).status_code == 200
        run = start(papi, pid).json()
        deadline, pending = time.time() + 30, []
        while time.time() < deadline and not pending:
            pending = get(papi, "/approvals?status=pending").json()["items"]
            time.sleep(0.1)
        assert post(papi, f"/approvals/{pending[0]['id']}/approve", json={"payload_hash": pending[0]["payload_hash"]}).status_code == 200
        final = wait(papi, run["id"], timeout=40)
        calls = get(papi, f"/plan-runs/{run['id']}/tool-invocations").json()["items"]
        invocation = [c for c in calls if c["tool"]["id"] == "demo.send_note"][0]
        yield final, invocation, fixtures
    finally:
        registry._tools.pop(("demo.send_note", 1), None)
        registry._lookups.pop(("demo.send_note", 1), None)
        registry._takes_dispatch.discard(("demo.send_note", 1))


def test_an_unknown_outcome_is_shown_as_unknown_with_the_honest_flags_and_never_as_a_failure(papi, unknown_run):
    run, invocation, fixtures = unknown_run
    d = invocation["dispatch"]
    assert invocation["status"] == "uncertain" and d["state"] == "unknown" and d["outcome"] == "unknown"
    assert d["requires_reconciliation"] is True and d["performed"] is None  # we do NOT know: not False
    assert len(fixtures.OUTBOX) == 1  # the remote really received it exactly once
    detail = get(papi, f"/tool-invocations/{invocation['id']}").json()
    assert detail["dispatch"]["state"] == "unknown" and "idempotency_key" not in detail["dispatch"]  # D075: internal protocol detail is never in a shared view
    task = [t for t in run["tasks"] if t["id"] == "announce"][0]
    assert task["retry"] == "reconcile"  # not "safe", not "uncertain": a distinct class that an acknowledgement cannot clear
    assert run["retry_options"]["allowed"] is False and run["retry_options"]["reason"] == "reconciliation_required" and run["retry_options"]["reconcile"] == ["announce"]
    pending = get(papi, "/reconciliations").json()
    assert pending["can_reconcile"] is True and [i["invocation_id"] for i in pending["items"]] == [invocation["id"]]
    events = [e["type"] for e in get(papi, f"/plan-runs/{run['id']}/events").json()["items"]]
    assert events.count("tool_dispatch_authorized") == 1 and events.count("tool_dispatch_started") == 1 and events.count("tool_dispatch_unknown") == 1
    tools = {t["id"]: t for t in get(papi, "/tools").json()["items"]}
    guarantee = tools["demo.send_note"]["latest"]["delivery"]
    assert guarantee["retry_policy"] == "never" and guarantee["can_query_remote"] is True and "SIMULATED" in guarantee["guarantee"]


def test_retrying_is_refused_until_reconciled_and_acknowledging_does_not_override_it(papi, unknown_run):
    run, invocation, fixtures = unknown_run
    for body in ({}, {"acknowledge_uncertain": ["announce"]}):
        r = post(papi, f"/plan-runs/{run['id']}/retry", json=body)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "reconciliation_required" and r.json()["detail"]["tasks"] == ["announce"], r.text
    assert len(fixtures.OUTBOX) == 1 and papi.db.scalar("SELECT count(*) FROM plan_runs") == 1


def test_reconciliation_by_remote_query_proves_the_outcome_and_records_the_evidence(papi, unknown_run):
    run, invocation, fixtures = unknown_run
    r = post(papi, f"/tool-invocations/{invocation['id']}/reconcile", json={"mode": "query", "reason": "Checking the provider's record"})
    assert r.status_code == 200, r.text
    d = r.json()["dispatch"]
    assert d["state"] == "reconciled_succeeded" and d["performed"] is True and d["outcome"] == "reconciled"
    assert d["reconciliation"]["evidence_kind"] == "remote_query" and d["reconciliation"]["attestation_only"] is False and d["external_ref"]
    again = post(papi, f"/tool-invocations/{invocation['id']}/reconcile", json={"mode": "query", "reason": "again"})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "already_resolved"
    assert papi.db.scalar("SELECT count(*) FROM tool_reconciliations") == 1
    assert get(papi, "/reconciliations").json()["items"] == []
    detail = get(papi, f"/tool-invocations/{invocation['id']}").json()["dispatch"]
    assert detail["reconciliation"]["reason"] == "Checking the provider's record"
    assert get(papi, f"/plan-runs/{run['id']}").json()["retry_options"]["allowed"] is True  # now a retry is possible (it will REPLAY the confirmed send, never resend)
    assert len(fixtures.OUTBOX) == 1


def test_an_operators_attestation_is_labelled_as_a_statement_and_needs_evidence_and_a_reason(papi, unknown_run):
    run, invocation, fixtures = unknown_run
    path = f"/tool-invocations/{invocation['id']}/reconcile"
    assert post(papi, path, json={"mode": "attest", "decision": "confirmed_failed", "reason": "I checked"}).status_code == 422  # no evidence
    assert post(papi, path, json={"mode": "attest", "reason": "I checked", "evidence": "dashboard"}).status_code == 422  # no decision
    assert post(papi, path, json={"mode": "attest", "decision": "unresolved", "evidence": "dashboard", "reason": "x" * 5}).status_code == 422  # an attestation must decide
    assert post(papi, path, json={"mode": "attest", "decision": "confirmed_failed", "evidence": "dashboard", "reason": "I checked", "actor": "someone-else"}).status_code == 422  # unknown fields refused
    r = post(papi, path, json={"mode": "attest", "decision": "confirmed_failed", "evidence": "Looked in the provider dashboard: no message to this address.", "reason": "Customer says nothing arrived"})
    assert r.status_code == 200, r.text
    d = r.json()["dispatch"]
    assert d["state"] == "reconciled_failed" and d["reconciliation"]["attestation_only"] is True and d["reconciliation"]["evidence_kind"] == "operator_attestation"
    assert d["reconciliation"]["by"] == "a@example.com" and d["reconciliation"]["reason"] == "Customer says nothing arrived"  # who decided, and why
    assert len(fixtures.OUTBOX) == 1  # the remote DID get it: the attestation says "failed" and the record says so was only an attestation, never proof


def test_reconciliation_is_scoped_to_the_workspace_and_refuses_strangers_and_unknown_fields(papi, unknown_run):
    run, invocation, fixtures = unknown_run
    papi.provision(*B)
    path = f"/tool-invocations/{invocation['id']}/reconcile"
    body = {"mode": "query", "reason": "stranger"}
    assert post(papi, path, B, json=body).status_code == 404  # another workspace cannot tell it exists
    assert get(papi, "/reconciliations", B).json()["items"] == []
    assert post(papi, path, json={**body, "workspace_id": "x"}).status_code == 422
    assert api_unauthenticated(papi, path) == 401
    assert papi.db.scalar("SELECT count(*) FROM tool_reconciliations") == 0 and papi.db.scalar("SELECT state FROM tool_dispatches") == "unknown"


def api_unauthenticated(api, path):
    return api.client.post(path, json={"mode": "query", "reason": "anonymous"}).status_code
