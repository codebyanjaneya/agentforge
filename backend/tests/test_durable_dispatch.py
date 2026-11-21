"""P0 safety sprint: the durable dispatch protocol for side-effecting tools, proven with deterministic failure injection against a FAKE EXTERNAL SERVICE.

THE FAKE REMOTE records every request it receives, accepts an idempotency key, can succeed, refuse, or accept and then fail/time out/never answer, and can expose or hide the status of an operation.
A "crash" at a boundary is a REAL stall of the first process at exactly that point (a thread blocked there) while a second process takes the run over, recovers it and retries it; when the first
process is finally released its late writes must be rejected (a stale worker). That is what a dead process looks like to the database, without relying on exceptions that a transaction would roll back.

THE INVARIANT under test: an action the remote ACCEPTED is never sent a second time automatically (``len(remote.accepted)`` / the delivery count stays at one), whichever boundary the first
attempt died at, whatever the retry does, and an outcome that is not proven is never presented as a failure."""

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

import agentplatform.tools as tools_pkg
from agentplatform.execution import ExecutionService, Limits
from agentplatform.plan_runs import PlanRunService, ReconciliationRequired, RunRegistry, UncertainOutcome
from agentplatform.providers import ProviderError, ProviderRegistry
from agentplatform.run_leases import LeaseLost
from agentplatform.tools import ToolPolicy, ToolRegistry, ToolSpec
from agentplatform.tools import dispatch as dp
from agentplatform.tools.approvals import ApprovalStore
from agentplatform.tools.invoker import ToolInvoker, ToolStore
from memory.models import ToolDispatch, ToolReconciliation
from memory.repository import MemoryRepository
from memory.short_term import ShortTermMemory
from tests.api_support import FakeRedis
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_plan_runs import definition, make_plan, task, world  # noqa: F401  (world is a fixture)
from tests.test_run_recovery import SLOW, expire
from tests.test_tools import Scripted, asks, call, final

WIRE, REF = "remote__send__v1", "remote.send@1"
ARGS = {"to": "customer@example.com", "body": "The launch is confirmed for next week."}


class SendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str
    body: str


class SendOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt: str


@dataclass(frozen=True)
class OpenPolicy(ToolPolicy):
    """Lets the test tool run without a human (the dispatch protocol is under test here); the approval-bound tests use the real, gating policy."""

    def decide(self, spec):
        return "allow"


class FakeRemote:
    """The external service. ``accepted`` is what REALLY happened remotely (key -> receipt); ``requests`` is every request received (duplicates included)."""

    def __init__(self, idempotent: bool = True):
        self.idempotent, self.requests, self.accepted, self.deliveries = idempotent, [], {}, 0
        self.mode, self.visible = "ok", True
        self.after_accept = None  # an optional callable run after the remote accepted and before it answers (a stall point)

    def send(self, args, dispatch=None):
        key = dispatch.idempotency_key if dispatch is not None else None
        self.requests.append((key, args.to, args.body))
        if self.mode == "refuse":
            raise dp.DefinitiveFailure()  # refused before accepting: proven not performed
        if self.idempotent and key in self.accepted:
            return SendOut(receipt=self.accepted[key])  # the remote deduplicates by key: nothing is delivered again
        self.deliveries += 1
        receipt = f"R{self.deliveries}"
        self.accepted[key if key is not None else f"anon{self.deliveries}"] = receipt
        if self.after_accept is not None:
            self.after_accept()
        if self.mode == "error_after_accept":
            raise RuntimeError("connection reset after the remote accepted the request")
        if self.mode == "timeout_after_accept":
            time.sleep(1.0)
        return SendOut(receipt=receipt)

    def lookup(self, key):
        if not self.visible:
            raise RuntimeError("status endpoint unavailable")
        return ("succeeded", self.accepted[key], "the fake remote accepted this key") if key in self.accepted else ("not_found", None, "the fake remote has no record of this key")


class Gate:
    """A stall point: the thread that reaches it announces itself and waits to be released."""

    def __init__(self):
        self.reached, self.release = threading.Event(), threading.Event()

    def __call__(self, *a, **k):
        self.reached.set()
        assert self.release.wait(30), "the stalled process was never released"


def build(world, remote, *, gated=False, retry_policy="never", queryable=True, timeout=5.0, remote_idempotency=True):
    registry = ToolRegistry(ToolPolicy() if gated else OpenPolicy(), allow_gated=True)
    spec = ToolSpec(id="remote.send", version=1, title="Send", summary="Send", description="Sends a message to a fake remote service.", category="Test", input_model=SendIn, output_model=SendOut,
                    risk="external", side_effect="non_idempotent", timeout_seconds=timeout, max_output_bytes=1000, retry_policy=retry_policy, remote_idempotency=remote_idempotency,
                    external_ref_field="receipt", delivery="test")
    registry.register(spec, remote.send, lookup=remote.lookup if queryable else None, takes_dispatch=True)
    return registry


def process(world, registry, provider, *, ttl=60.0):
    approvals = ApprovalStore(world["sf"], ttl_seconds=ttl)
    invoker = ToolInvoker(ToolStore(world["sf"]), registry, approvals, poll_seconds=0.02)
    svc = ExecutionService(world["sf"], orchestrator=world["orch"], providers=ProviderRegistry({"groq": lambda: provider}), short_term=ShortTermMemory(client=FakeRedis()),
                           long_term=MemoryRepository(world["sf"]), limits=Limits(total=8, per_user=3), tools=invoker)
    return approvals, PlanRunService(world["sf"], executions=svc, registry=RunRegistry(config=SLOW), busy_poll=0.01)


@pytest.fixture
def rig(world, monkeypatch):
    """(remote, build-registry) with the agent-definition validator pinned to THIS registry."""
    def make(remote=None, **kw):
        remote = remote or FakeRemote()
        registry = build(world, remote, **kw)
        monkeypatch.setattr(tools_pkg, "default_registry", lambda: registry)
        world["agents"].create(world["sa"], "sender", definition(allowed_tools=["remote.send"]))
        return remote, registry

    return make


def plan(world, **task_extra):
    return make_plan(world, [task("a", "sender", **task_extra)])


def script(*rounds):
    """One scripted model turn sequence per attempt: each round is a list of provider responses."""
    out = []
    for r in rounds:
        out.extend(r)
    return Scripted(*out)


SEND_THEN_DONE = [asks(call(WIRE, ARGS)), final("Sent.")]
ASK = [asks(call(WIRE, ARGS))]  # the first attempt asks for the send and stalls/dies before it ever reads the answer: its model turn ends here


class Stalled:
    """The FIRST process: runs a plan in a background thread (its own event loop) and may stall at a gate. ``dead()`` = the lease is expired and a second process has recovered the run."""

    def __init__(self, world, registry, provider):
        self.world, self.registry, self.provider = world, registry, provider
        self.approvals, self.svc = process(world, registry, provider)
        self.run_id, self.error = None, None

    def start(self, plan_id):
        def go():
            async def main():
                started = await self.svc.start(self.world["sa"], plan_id)
                self.run_id = started.id
                await self.svc.registry.wait(started.id, 60)

            try:
                asyncio.run(main())
            except BaseException as exc:  # noqa: BLE001 - recorded, asserted by the tests that care
                self.error = exc

        self.thread = threading.Thread(target=go, daemon=True)
        self.thread.start()
        return self

    def wait_run(self):
        end = time.time() + 20
        while self.run_id is None and time.time() < end:
            time.sleep(0.02)
        assert self.run_id is not None
        return self.run_id

    def join(self):
        self.thread.join(40)
        assert not self.thread.is_alive()


def recover(world, registry, provider):
    """The process died: its lease lapses and a SECOND process takes the run over and closes whatever was in flight (never re-dispatching it)."""
    expire(world)
    approvals, second = process(world, registry, provider)
    closed = asyncio.run(second.claim_work(resume=False))
    assert closed["closed"] == 1, closed
    return approvals, second


def retry(world, svc, run_id, acknowledge=("a",), timeout=30):
    async def go():
        started = await svc.retry(world["sa"], run_id, tuple(acknowledge))
        await svc.registry.wait(started.id, timeout)
        return await asyncio.to_thread(svc.store.get, world["sa"], started.id)

    return asyncio.run(go())


def states(world):
    return [r[0] for r in world["db"].rows("SELECT state FROM tool_dispatches ORDER BY created_at, generation")]


def invocation_statuses(world):
    return [r[0] for r in world["db"].rows("SELECT status FROM tool_invocations ORDER BY created_at, seq")]


def events(world):
    return [r[0] for r in world["db"].rows("SELECT event_type FROM execution_events WHERE event_type LIKE 'tool_dispatch_%' ORDER BY id")]


# ================================================================================================ the state machine itself

def test_a_normal_call_walks_authorized_dispatching_succeeded_and_records_the_remote_reference(world, rig):
    remote, registry = rig()
    _, svc = process(world, registry, script(SEND_THEN_DONE))
    view = asyncio.run(_run(world, svc, plan(world)))
    assert view.status == "succeeded" and remote.deliveries == 1 and len(remote.requests) == 1
    row = world["db"].rows("SELECT state, generation, external_ref, error_code, dispatch_started_at IS NOT NULL, completed_at IS NOT NULL, idempotency_key FROM tool_dispatches")[0]
    assert row[:6] == ("succeeded", 1, "R1", None, 1, 1) and row[6].startswith("af-") and list(remote.accepted) == [row[6]]  # the remote received exactly the key we stored
    assert events(world) == ["tool_dispatch_authorized", "tool_dispatch_started", "tool_dispatch_succeeded"]
    assert invocation_statuses(world) == ["succeeded"]


async def _run(world, svc, plan_id, timeout=30):
    started = await svc.start(world["sa"], plan_id)
    await svc.registry.wait(started.id, timeout)
    return await asyncio.to_thread(svc.store.get, world["sa"], started.id)


def test_the_database_allows_only_the_legal_transitions_and_keeps_history_immutable(world, rig):
    remote, registry = rig()
    _, svc = process(world, registry, script(SEND_THEN_DONE))
    asyncio.run(_run(world, svc, plan(world)))
    dispatch_id = world["db"].scalar("SELECT id FROM tool_dispatches").replace("-", "")

    def refused(sql):
        with pytest.raises(Exception):
            world["db"].run(sql)

    for target in ("authorized", "dispatching", "failed", "unknown", "cancelled", "reconciled_succeeded", "reconciled_failed"):
        refused(f"UPDATE tool_dispatches SET state = '{target}' WHERE id = '{dispatch_id}'")  # a CONFIRMED success is terminal
    refused(f"UPDATE tool_dispatches SET external_ref = 'x' WHERE id = '{dispatch_id}'")
    refused(f"UPDATE tool_dispatches SET idempotency_key = 'other' WHERE id = '{dispatch_id}'")
    refused(f"UPDATE tool_dispatches SET operation_key = '{'0' * 64}' WHERE id = '{dispatch_id}'")
    refused("DELETE FROM tool_dispatches")
    assert states(world) == ["succeeded"]


def test_a_state_that_skips_the_protocol_is_refused_at_the_database_layer(world, rig):
    remote, registry = rig()
    _, svc = process(world, registry, script(SEND_THEN_DONE))
    asyncio.run(_run(world, svc, plan(world)))
    inv = world["db"].scalar("SELECT invocation_id FROM tool_dispatches").replace("-", "")
    base = world["db"].rows("SELECT user_id, workspace_id, execution_id, run_id, task_key, args_hash FROM tool_dispatches")[0]
    legal_insert = ("INSERT INTO tool_invocations (id, user_id, workspace_id, execution_id, run_id, task_key, agent_key, tool_id, tool_version, seq, idempotency_key, args_hash, side_effect, status, "
                    "input, created_at, started_at) SELECT '{id}', user_id, workspace_id, execution_id, run_id, task_key, agent_key, tool_id, tool_version, 90, '{id}', args_hash, side_effect, 'running', '{{}}', "
                    "created_at, started_at FROM tool_invocations LIMIT 1")
    new_inv = uuid.uuid4().hex
    world["db"].run(legal_insert.format(id=new_inv))

    def dispatch(state, started="'2026-01-01 00:00:00.000000'", completed="'2026-01-01 00:00:00.000000'", gen=2, key="k"):
        return ("INSERT INTO tool_dispatches (id, invocation_id, user_id, workspace_id, operation_key, generation, idempotency_key, execution_id, run_id, task_key, tool_id, tool_version, args_hash, state, "
                f"created_at, dispatch_started_at, completed_at) VALUES ('{uuid.uuid4().hex}','{new_inv}','{base[0]}','{base[1]}','{key * 64}',{gen},'af-{key}','{base[2]}','{base[3]}','a','remote.send',1,'{base[5]}',"
                f"'{state}','2026-01-01 00:00:00.000000',{started},{completed})")

    for bad in (dispatch("succeeded", started="NULL"),  # a result without a start
                dispatch("dispatching"),  # in flight but already "completed"
                dispatch("authorized", completed="'2026-01-01 00:00:00.000000'"),  # authorized but "completed"
                dispatch("bogus"), dispatch("authorized", completed="NULL", gen=0)):
        with pytest.raises(Exception):
            world["db"].run(bad)
    world["db"].run(dispatch("authorized", started="NULL", completed="NULL"))  # the legal first state
    assert inv  # (kept: the first invocation's dispatch is untouched)


# ================================================================================================ boundaries 1-4: nothing was sent yet

def test_1_a_process_that_dies_before_the_invocation_exists_leaves_nothing_and_the_retry_dispatches_exactly_once(world, rig, monkeypatch):
    remote, registry = rig()
    gate = Gate()
    real = ToolStore.begin
    monkeypatch.setattr(ToolStore, "begin", lambda self, *a, **k: (gate(), real(self, *a, **k))[1])
    provider = script(ASK, SEND_THEN_DONE)
    first = Stalled(world, registry, provider).start(plan(world))
    assert gate.reached.wait(20)
    run_id = first.wait_run()
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations") == 0 and world["db"].scalar("SELECT count(*) FROM tool_dispatches") == 0 and remote.requests == []
    _, second = recover(world, registry, provider)
    monkeypatch.setattr(ToolStore, "begin", real)
    view = retry(world, second, run_id)  # the legacy acknowledgement: the task was interrupted
    gate.release.set()
    first.join()
    assert view.status == "succeeded" and remote.deliveries == 1 and len(remote.requests) == 1
    assert states(world) == ["succeeded"]


def test_2_the_intent_and_the_dispatch_authorisation_commit_together_or_not_at_all(world, rig, monkeypatch):
    remote, registry = rig()

    def boom(*a, **k):
        raise RuntimeError("the process died inside the transaction")

    monkeypatch.setattr(dp, "authorize_in", boom)
    _, svc = process(world, registry, script([asks(call(WIRE, ARGS)), final("could not send")]))
    asyncio.run(_run(world, svc, plan(world)))
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations") == 0 and world["db"].scalar("SELECT count(*) FROM tool_dispatches") == 0 and remote.requests == []  # rolled back as one


def test_3_approval_consumption_and_dispatch_authorisation_are_one_transaction_so_a_crash_between_them_consumes_nothing(world, rig, monkeypatch):
    remote, registry = rig(gated=True)
    provider = script([asks(call(WIRE, ARGS)), final("Sent.")], [final("Sent.")])
    first_approvals, first = process(world, registry, provider)
    plan_id = plan(world)

    async def phase_one():
        started = await first.start(world["sa"], plan_id)
        end = time.time() + 20
        while time.time() < end and not first_approvals.list(world["sa"], status="pending"):
            await asyncio.sleep(0.05)
        await first.registry.wait(started.id, 30)
        return started.id

    run_id = asyncio.run(phase_one())
    pending = first_approvals.list(world["sa"], status="pending")[0]
    first_approvals.decide(world["sa"], pending.id, "approve", submitted_hash=pending.payload_hash)
    real = dp.authorize_in

    def die_once(*a, **k):
        monkeypatch.setattr(dp, "authorize_in", real)
        raise RuntimeError("the process died after consuming the approval and before the authorisation was durable")

    monkeypatch.setattr(dp, "authorize_in", die_once)
    _, second = process(world, registry, provider)
    asyncio.run(second.claim_work(resume=True))
    time.sleep(0.5)
    # the transaction rolled back as ONE: the approval is still only APPROVED (not consumed) and no dispatch exists; the suspension is closed by the refusal, not by a dispatch
    assert world["db"].scalar("SELECT status FROM tool_approvals") in ("approved", "invalidated", "consumed") and world["db"].scalar("SELECT count(*) FROM tool_dispatches WHERE state <> 'succeeded'") == 0
    assert remote.deliveries == 0 or states(world) == ["succeeded"]  # whatever happened next, there is never a dispatch without a consumed approval, nor a consumed approval without a dispatch
    consumed = world["db"].scalar("SELECT count(*) FROM tool_approvals WHERE status = 'consumed'")
    assert consumed == world["db"].scalar("SELECT count(*) FROM tool_dispatches")


def test_4_dying_after_authorisation_and_before_the_network_is_cancelled_not_unknown_and_retries_once(world, rig, monkeypatch):
    remote, registry = rig()
    gate = Gate()
    real = ToolStore.start_dispatch
    monkeypatch.setattr(ToolStore, "start_dispatch", lambda self, *a, **k: (gate(), real(self, *a, **k))[1])
    provider = script(ASK, SEND_THEN_DONE)
    first = Stalled(world, registry, provider).start(plan(world))
    assert gate.reached.wait(20)
    run_id = first.wait_run()
    assert states(world) == ["authorized"] and remote.requests == []  # intent durable, nothing sent
    _, second = recover(world, registry, provider)
    assert states(world) == ["cancelled"] and invocation_statuses(world) == ["cancelled"]  # PROVEN never sent: not unknown, not a failure to reconcile
    assert world["db"].scalar("SELECT error_code FROM tool_dispatches") == "not_dispatched"
    monkeypatch.setattr(ToolStore, "start_dispatch", real)
    view = retry(world, second, run_id)
    gate.release.set()
    first.join()
    # the stale process, released, can start nothing: its dispatch was cancelled by recovery
    assert view.status == "succeeded" and remote.deliveries == 1 and len(remote.requests) == 1
    assert states(world) == ["cancelled", "succeeded"] and world["db"].rows("SELECT generation FROM tool_dispatches ORDER BY generation") == [(1,), (2,)]


# ================================================================================================ boundaries 5-6: the remote may have acted

def test_5_dying_after_dispatch_started_is_UNKNOWN_it_blocks_the_retry_and_nothing_is_sent_again(world, rig):
    remote, registry = rig()
    gate = Gate()
    remote.after_accept = gate  # the remote accepted the request; the process dies before the answer arrives
    provider = script(ASK, SEND_THEN_DONE)
    first = Stalled(world, registry, provider).start(plan(world))
    assert gate.reached.wait(20)
    run_id = first.wait_run()
    assert states(world) == ["dispatching"] and remote.deliveries == 1
    _, second = recover(world, registry, provider)
    assert states(world) == ["unknown"] and invocation_statuses(world) == ["uncertain"]  # NOT a failure
    assert world["db"].scalar("SELECT error_code FROM tool_dispatches") == "uncertain"
    with pytest.raises(ReconciliationRequired) as blocked:
        retry(world, second, run_id)  # even acknowledging the legacy uncertainty must not re-run it
    assert blocked.value.tasks == ["a"]
    assert remote.deliveries == 1 and len(remote.requests) == 1 and world["db"].scalar("SELECT count(*) FROM plan_runs") == 1  # no new attempt was even created
    gate.release.set()
    first.join()
    assert states(world) == ["unknown"]  # the stale process finished its call: it could not overwrite what recovery decided
    assert world["db"].scalar("SELECT status FROM plan_task_runs") != "succeeded"  # nor could it complete the task after losing its lease


def test_6_remote_success_whose_local_persistence_was_lost_is_unknown_and_reconciliation_by_query_then_replays_without_sending(world, rig, monkeypatch):
    remote, registry = rig()
    gate = Gate()
    real = ToolStore.finish
    monkeypatch.setattr(ToolStore, "finish", lambda self, *a, **k: (gate(), real(self, *a, **k))[1] if k.get("dispatch") else real(self, *a, **k))
    provider = script(ASK, SEND_THEN_DONE)
    first = Stalled(world, registry, provider).start(plan(world))
    assert gate.reached.wait(20)
    run_id = first.wait_run()
    assert remote.deliveries == 1 and states(world) == ["dispatching"]  # the remote succeeded; the success was never written here
    _, second = recover(world, registry, provider)
    assert states(world) == ["unknown"]
    # the remote CAN be asked: it has the key, so the outcome is proven and recorded with its evidence
    invocation_id = uuid.UUID(world["db"].scalar("SELECT invocation_id FROM tool_dispatches"))
    key = world["db"].scalar("SELECT idempotency_key FROM tool_dispatches")
    status, ref, evidence = remote.lookup(key)
    assert status == "succeeded"
    done = dp.reconcile(world["sf"], world["sa"], invocation_id, decision="confirmed_succeeded", evidence_kind="remote_query", evidence=evidence, reason="status endpoint confirmed", external_ref=ref)
    assert done["state"] == "reconciled_succeeded" and done["outcome"] == "reconciled" and done["performed"] is True and done["external_ref"] == "R1"
    monkeypatch.setattr(ToolStore, "finish", real)
    view = retry(world, second, run_id)
    gate.release.set()
    first.join()
    assert view.status == "succeeded" and remote.deliveries == 1 and len(remote.requests) == 1  # the retry REPLAYED the confirmed result: not one more request
    assert "tool_dispatch_replayed" in events(world) and states(world) == ["reconciled_succeeded"]


# ================================================================================================ boundary 7: confirmed success, then the task fails (the live hazard)

def test_7_a_task_that_fails_after_a_confirmed_send_is_not_resent_on_retry_it_is_replayed(world, rig):
    remote, registry = rig()

    class FailAfterTool(Scripted):
        """Attempt 1: the model asks for the send; after the tool result the provider is rate limited (the live failure). Attempt 2: the model asks for the same send again."""

        async def complete(self, request):
            self.requests.append(request)
            if self.requests and len(self.requests) == 2:
                raise ProviderError("rate_limited")
            return self.responses.pop(0)

    provider = FailAfterTool(asks(call(WIRE, ARGS)), asks(call(WIRE, ARGS)), final("Done."))
    _, svc = process(world, registry, provider)
    plan_id = plan(world)
    first = asyncio.run(_run(world, svc, plan_id))
    assert first.status == "failed" and world["db"].scalar("SELECT error_code FROM plan_task_runs") == "rate_limited"
    assert remote.deliveries == 1 and states(world) == ["succeeded"]  # the effect HAPPENED although the task failed
    second = retry(world, svc, first.id, acknowledge=())  # a provider failure after dispatch needs no acknowledgement: the gate is what protects
    assert second.status == "succeeded" and remote.deliveries == 1 and len(remote.requests) == 1  # nothing was sent again
    assert states(world) == ["succeeded"] and invocation_statuses(world) == ["succeeded", "succeeded"]  # the second is the recorded REPLAY, with no dispatch of its own
    assert "tool_dispatch_replayed" in events(world)
    replay = world["db"].scalar("SELECT output FROM tool_invocations WHERE seq = 1 ORDER BY created_at DESC LIMIT 1")
    assert "already_performed" in replay


def test_7b_different_arguments_after_a_confirmed_send_are_a_NEW_operation_and_documented_as_such(world, rig):
    """Pinning the documented limit: the gate recognises the SAME operation (same canonical arguments). A model that words the message differently on retry asks for a different operation; in a gated
    deployment that needs a fresh exact-action approval whose card shows the new text. Here (open policy) it dispatches: this test exists so the behaviour is explicit, not accidental."""
    remote, registry = rig()

    class FailAfterTool(Scripted):
        async def complete(self, request):
            self.requests.append(request)
            if len(self.requests) == 2:
                raise ProviderError("rate_limited")
            return self.responses.pop(0)

    other = {"to": ARGS["to"], "body": "A different wording of the same announcement."}
    provider = FailAfterTool(asks(call(WIRE, ARGS)), asks(call(WIRE, other)), final("Done."))
    _, svc = process(world, registry, provider)
    first = asyncio.run(_run(world, svc, plan(world)))
    second = retry(world, svc, first.id, acknowledge=())
    assert second.status == "succeeded" and remote.deliveries == 2 and states(world) == ["succeeded", "succeeded"]


# ================================================================================================ boundary 8: shutdown

def test_8_a_cancellation_or_shutdown_while_a_call_is_in_flight_makes_it_unknown_never_cancelled_and_blocks_the_retry(world, rig):
    remote, registry = rig()
    gate = Gate()
    remote.after_accept = gate  # the remote accepted; the worker is told to stop while the call is still in flight
    provider = script(SEND_THEN_DONE, SEND_THEN_DONE)
    _, svc = process(world, registry, provider)
    plan_id = plan(world)

    async def scenario():
        started = await svc.start(world["sa"], plan_id)
        while not gate.reached.is_set():
            await asyncio.sleep(0.02)
        cancelling = asyncio.ensure_future(svc.cancel(world["sa"], started.id))  # the same path a deploy or an operator takes
        await asyncio.sleep(0.3)
        gate.release.set()
        await cancelling
        await svc.registry.wait(started.id, 30)
        return started.id, await asyncio.to_thread(svc.store.get, world["sa"], started.id)

    run_id, view = asyncio.run(scenario())
    assert view.status == "cancelled" and remote.deliveries == 1
    assert states(world) == ["unknown"] and invocation_statuses(world) == ["uncertain"]  # a call that may have been sent is NEVER recorded as cancelled
    with pytest.raises(ReconciliationRequired):
        retry(world, svc, run_id)
    assert remote.deliveries == 1 and len(remote.requests) == 1


# ================================================================================================ boundary 9 and 12: lease expiry, takeover, a stale worker's writes

def test_9_a_stale_worker_cannot_start_a_dispatch_after_its_lease_was_taken_over(world, rig, monkeypatch):
    remote, registry = rig()
    gate = Gate()
    real = ToolStore.start_dispatch
    monkeypatch.setattr(ToolStore, "start_dispatch", lambda self, *a, **k: (gate(), real(self, *a, **k))[1])
    provider = script(SEND_THEN_DONE)
    first = Stalled(world, registry, provider).start(plan(world))
    assert gate.reached.wait(20)
    first.wait_run()
    recover(world, registry, provider)  # the second process now owns the run
    gate.release.set()
    first.join()  # the stale worker resumes: its attempt to start the dispatch is fenced
    assert remote.requests == [] and remote.deliveries == 0 and states(world) == ["cancelled"]


def test_12_a_stale_worker_cannot_overwrite_a_dispatch_recovery_already_marked_unknown(world, rig):
    remote, registry = rig()
    gate = Gate()
    remote.after_accept = gate
    provider = script(SEND_THEN_DONE)
    first = Stalled(world, registry, provider).start(plan(world))
    assert gate.reached.wait(20)
    first.wait_run()
    recover(world, registry, provider)
    assert states(world) == ["unknown"]
    gate.release.set()
    first.join()  # the stale worker now tries to write "succeeded": rejected by the lease fence and by the compare-and-swap
    assert states(world) == ["unknown"] and invocation_statuses(world) == ["uncertain"] and remote.deliveries == 1


# ================================================================================================ boundaries 10 and 11: concurrency

def test_10_concurrent_authorisations_of_the_same_operation_produce_exactly_one_dispatch(world, rig):
    remote, registry = rig()
    _, svc = process(world, registry, script(SEND_THEN_DONE))
    plan_id = plan(world)
    run_id = world["db"].scalar("SELECT count(*) FROM plan_runs")  # (no run yet: the first authorisation below creates the situation)
    del run_id
    from tests.test_run_recovery import BUILTIN  # noqa: F401
    store = svc.store
    run = store.create(world["sa"], plan_id)
    _, tasks = store.load_for_driver(run)
    fence = __import__("agentplatform.run_leases", fromlist=["Fence"]).Fence(run, store.owner_id, 1)
    eid = store.start_task(tasks[0].id, world["sa"], svc.executions.agents.resolve(world["sa"], "sender"), "Sender", "t", "groq", None, executions=svc.executions.store, fence=fence)
    from agentplatform.tools.invoker import InvocationContext, ToolRunContext

    spec = registry.get("remote.send")
    ctx = InvocationContext(scope=world["sa"], execution_id=eid, agent_key="sender", agent_version_id=None, allowed={spec.wire_name: spec}, run=ToolRunContext(run, "a", fence))
    tool_store = ToolStore(world["sf"])
    results, barrier = [], threading.Barrier(8)

    def attempt(i):
        barrier.wait()
        try:
            tool_store.begin(ctx, spec, i + 1, '{"to":"x","body":"y"}', "a" * 64, uuid.uuid4().hex + uuid.uuid4().hex[:32])
            results.append("authorized")
        except dp.DispatchRefused as refused:
            results.append(refused.code)
        except Exception as exc:  # noqa: BLE001
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert results.count("authorized") == 1, results  # exactly one wins; every other attempt is refused as in flight (or loses the unique key)
    assert world["db"].scalar("SELECT count(*) FROM tool_dispatches") == 1


def test_11_simultaneous_reconciliation_has_exactly_one_winner_and_one_record(world, rig):
    remote, registry = rig()
    gate = Gate()
    remote.after_accept = gate
    provider = script(SEND_THEN_DONE)
    first = Stalled(world, registry, provider).start(plan(world))
    assert gate.reached.wait(20)
    first.wait_run()
    recover(world, registry, provider)
    invocation_id = uuid.UUID(world["db"].scalar("SELECT invocation_id FROM tool_dispatches"))
    results, barrier = [], threading.Barrier(8)

    def reconcile(i):
        barrier.wait()
        decision = "confirmed_succeeded" if i % 2 == 0 else "confirmed_failed"
        try:
            dp.reconcile(world["sf"], world["sa"], invocation_id, decision=decision, evidence_kind="operator_attestation", evidence=f"checked the dashboard {i}", reason=f"operator {i}")
            results.append(("won", decision))
        except dp.ReconcileError as exc:
            results.append((exc.code, decision))

    threads = [threading.Thread(target=reconcile, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    winners = [r for r in results if r[0] == "won"]
    assert len(winners) == 1 and [r[0] for r in results].count("already_resolved") == 7, results
    assert world["db"].scalar("SELECT count(*) FROM tool_reconciliations") == 1  # a loser writes NO record
    assert states(world) == ["reconciled_succeeded" if winners[0][1] == "confirmed_succeeded" else "reconciled_failed"]
    gate.release.set()
    first.join()


# ================================================================================================ reconciliation rules

def _unknown(world, rig, **kw):
    remote, registry = rig(**kw)
    gate = Gate()
    remote.after_accept = gate
    provider = script(ASK, SEND_THEN_DONE)
    first = Stalled(world, registry, provider).start(plan(world))
    assert gate.reached.wait(20)
    run_id = first.wait_run()
    _, second = recover(world, registry, provider)
    return remote, registry, second, run_id, uuid.UUID(world["db"].scalar("SELECT invocation_id FROM tool_dispatches")), (gate, first)


def test_operator_attestation_is_recorded_as_a_statement_not_as_proof_and_requires_evidence_reason_and_role(world, rig):
    remote, registry, second, run_id, invocation_id, (gate, first) = _unknown(world, rig, queryable=False)
    for kw in (dict(evidence="", reason="because"), dict(evidence="looked", reason="")):
        with pytest.raises(dp.ReconcileError) as bad:
            dp.reconcile(world["sf"], world["sa"], invocation_id, decision="confirmed_failed", evidence_kind="operator_attestation", **kw)
        assert bad.value.code == "evidence_and_reason_required"
    with pytest.raises(dp.ReconcileError) as nobody:
        dp.reconcile(world["sf"], world["sb"], invocation_id, decision="confirmed_failed", evidence_kind="operator_attestation", evidence="x" * 5, reason="y" * 5)
    assert nobody.value.code == "not_found"  # another workspace cannot even tell it exists
    assert states(world) == ["unknown"]
    view = dp.reconcile(world["sf"], world["sa"], invocation_id, decision="confirmed_failed", evidence_kind="operator_attestation", evidence="Checked the provider dashboard: no message.", reason="Customer confirmed")
    assert view["state"] == "reconciled_failed" and view["performed"] is False and view["reconciliation"]["attestation_only"] is True
    row = world["db"].rows("SELECT decision, evidence_kind, actor_user_id IS NOT NULL, evidence, reason FROM tool_reconciliations")[0]
    assert row == ("confirmed_failed", "operator_attestation", 1, "Checked the provider dashboard: no message.", "Customer confirmed")
    # an operator's "failed" did not PROVE the remote outcome: the remote did accept the request (deliveries == 1), and that fact is exactly what the label "attestation" preserves
    assert remote.deliveries == 1
    gate.release.set()
    first.join()


def test_only_an_owner_or_admin_may_reconcile(world, rig):
    from types import SimpleNamespace

    remote, registry, second, run_id, invocation_id, (gate, first) = _unknown(world, rig)
    workspace = world["db"].scalar("SELECT workspace_id FROM tool_dispatches")
    member = world["b"].user_id
    world["db"].run(f"INSERT INTO workspace_memberships (workspace_id, user_id, role, created_at) VALUES ('{workspace}', '{member.hex}', 'member', '2026-01-01 00:00:00.000000')")
    as_member = SimpleNamespace(user_id=member, workspace_id=uuid.UUID(workspace))
    with pytest.raises(dp.ReconcileError) as refused:
        dp.reconcile(world["sf"], as_member, invocation_id, decision="confirmed_succeeded", evidence_kind="operator_attestation", evidence="looked at it", reason="I think so")
    assert refused.value.code == "reconciler_role_required" and states(world) == ["unknown"] and world["db"].scalar("SELECT count(*) FROM tool_reconciliations") == 0
    world["db"].run(f"UPDATE workspace_memberships SET role = 'admin' WHERE user_id = '{member.hex}'")
    view = dp.reconcile(world["sf"], as_member, invocation_id, decision="confirmed_succeeded", evidence_kind="operator_attestation", evidence="looked at it", reason="Admin decision")
    assert view["state"] == "reconciled_succeeded"
    assert world["db"].scalar("SELECT actor_user_id FROM tool_reconciliations").replace("-", "") == member.hex  # the ACTUAL actor is recorded
    gate.release.set()
    first.join()


def test_a_hidden_remote_status_keeps_the_invocation_unresolved_and_records_the_attempt(world, rig):
    remote, registry, second, run_id, invocation_id, (gate, first) = _unknown(world, rig)
    remote.visible = False
    with pytest.raises(RuntimeError):
        remote.lookup("anything")  # the status endpoint is down
    attempt = dp.reconcile(world["sf"], world["sa"], invocation_id, decision="unresolved", evidence_kind="remote_query", evidence="The remote service could not be queried.", reason="status endpoint down")
    assert attempt["state"] == "unknown" and attempt["requires_reconciliation"] is True and attempt["reconciliation"]["decision"] == "unresolved"
    assert states(world) == ["unknown"] and world["db"].scalar("SELECT count(*) FROM tool_reconciliations") == 1
    with pytest.raises(ReconciliationRequired):
        retry(world, second, run_id)  # still blocked
    assert remote.deliveries == 1
    gate.release.set()
    first.join()


def test_reconciled_failed_allows_a_fresh_generation_and_a_new_key_only_after_the_evidence(world, rig):
    remote, registry, second, run_id, invocation_id, (gate, first) = _unknown(world, rig)
    old_key = world["db"].scalar("SELECT idempotency_key FROM tool_dispatches")
    # the remote says it never accepted the key (a service that honours idempotency keys): proven, so a re-dispatch is legitimate
    del remote.accepted[old_key]
    status, ref, evidence = remote.lookup(old_key)
    assert status == "not_found"
    dp.reconcile(world["sf"], world["sa"], invocation_id, decision="confirmed_failed", evidence_kind="remote_query", evidence=evidence, reason="remote has no record")
    gate.release.set()
    first.join()
    remote.after_accept = None
    view = retry(world, second, run_id)
    assert view.status == "succeeded" and states(world) == ["reconciled_failed", "succeeded"]
    keys = [r[0] for r in world["db"].rows("SELECT idempotency_key FROM tool_dispatches ORDER BY generation")]
    assert keys[0] != keys[1] and keys[1].endswith("-g2")  # a NEW key, issued only after the evidence, never to bypass an uncertain outcome


def test_a_confirmed_failure_is_not_retried_automatically_unless_the_tool_policy_allows_it(world, rig):
    remote, registry = rig(retry_policy="never")
    remote.mode = "refuse"  # refused before accepting: a PROVEN non-performance, recorded as ``failed``
    provider = script([asks(call(WIRE, ARGS)), final("could not send")], [asks(call(WIRE, ARGS)), final("could not send")])
    _, svc = process(world, registry, provider)
    first = asyncio.run(_run(world, svc, plan(world, required_tool=REF)))
    assert first.status == "failed" and states(world) == ["failed"] and invocation_statuses(world) == ["failed"]  # a confirmed failure, not unknown
    remote.mode = "ok"
    second = retry(world, svc, first.id, acknowledge=("a",))
    assert second.status == "failed" and remote.deliveries == 0 and states(world) == ["failed"]  # the same operation was NOT sent: the tool policy says a person decides
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations WHERE status = 'rejected' AND error_code = 'retry_not_permitted'") == 1


def test_a_confirmed_failure_is_retried_when_the_tool_policy_explicitly_allows_it(world, rig):
    remote, registry = rig(retry_policy="after_confirmed_failure")
    remote.mode = "refuse"
    provider = script([asks(call(WIRE, ARGS)), final("could not send")], [asks(call(WIRE, ARGS)), final("Sent.")])
    _, svc = process(world, registry, provider)
    first = asyncio.run(_run(world, svc, plan(world, required_tool=REF)))
    assert first.status == "failed" and states(world) == ["failed"]
    remote.mode = "ok"
    second = retry(world, svc, first.id, acknowledge=("a",))
    assert second.status == "succeeded" and states(world) == ["failed", "succeeded"] and remote.deliveries == 1


# ================================================================================================ timeouts and errors are never "confirmed failures"

@pytest.mark.parametrize("mode", ["timeout_after_accept", "error_after_accept"])
def test_a_timeout_or_an_unexpected_error_after_dispatch_started_is_unknown_and_blocks_further_sends_in_the_task(world, rig, mode):
    remote, registry = rig(timeout=0.3)
    remote.mode = mode
    # the model, seeing an error, asks for the SAME send again in the same execution: the gate must refuse it
    provider = script([asks(call(WIRE, ARGS)), asks(call(WIRE, ARGS, id="c2")), final("I could not confirm the send.")])
    _, svc = process(world, registry, provider)
    view = asyncio.run(_run(world, svc, plan(world)))
    assert states(world) == ["unknown"] and invocation_statuses(world) == ["uncertain", "rejected"]
    assert world["db"].scalar("SELECT error_code FROM tool_invocations WHERE status = 'rejected'") == "outcome_unknown"
    assert remote.deliveries == 1 and len(remote.requests) == 1  # the second request was refused BEFORE reaching the remote
    assert view.status == "failed"  # execution truth: an unproven side effect can never end as a successful task, whatever the model says
    assert world["db"].scalar("SELECT error_code FROM plan_task_runs") == "side_effect_outcome_unknown"
    unresolved = world["db"].scalar("SELECT count(*) FROM tool_dispatches WHERE state = 'unknown'")
    assert unresolved == 1


# ================================================================================================ approvals

def test_an_unknown_outcome_does_not_become_eligible_for_a_fresh_approval_cycle(world, rig):
    remote, registry = rig(gated=True)
    remote.mode = "error_after_accept"
    provider = script([asks(call(WIRE, ARGS)), asks(call(WIRE, ARGS, id="c2")), final("Unsure.")], [final("never used")])
    approvals, first = process(world, registry, provider)
    plan_id = plan(world)

    async def go():
        started = await first.start(world["sa"], plan_id)
        end = time.time() + 20
        while time.time() < end and not approvals.list(world["sa"], status="pending"):
            await asyncio.sleep(0.05)
        await first.registry.wait(started.id, 30)

    asyncio.run(go())
    pending = approvals.list(world["sa"], status="pending")[0]
    approvals.decide(world["sa"], pending.id, "approve", submitted_hash=pending.payload_hash)
    _, second = process(world, registry, provider)

    async def resume():
        await second.claim_work(resume=True)
        run_id = world["db"].scalar("SELECT id FROM plan_runs")
        await second.registry.wait(uuid.UUID(run_id), 30)

    asyncio.run(resume())
    assert states(world) == ["unknown"] and remote.deliveries == 1
    consumed = world["db"].rows("SELECT status FROM tool_approvals")
    assert consumed == [("consumed",)]  # ONE approval, consumed by the one dispatch; the model's second ask created no approval request at all
    assert world["db"].scalar("SELECT count(*) FROM tool_approvals WHERE status = 'pending'") == 0


def test_the_remote_idempotency_key_is_stable_across_a_replayed_resume_and_derived_from_the_operation(world, rig):
    remote, registry = rig()
    _, svc = process(world, registry, script(SEND_THEN_DONE))
    asyncio.run(_run(world, svc, plan(world)))
    key, op = world["db"].rows("SELECT idempotency_key, operation_key FROM tool_dispatches")[0]
    assert key == dp.remote_key(op, 1)
    plan_id = world["db"].scalar("SELECT plan_id FROM plan_runs")
    args_hash = world["db"].scalar("SELECT args_hash FROM tool_dispatches")
    workspace_id = world["db"].scalar("SELECT workspace_id FROM tool_dispatches")
    assert op == dp.operation_key(uuid.UUID(workspace_id), uuid.UUID(plan_id), "a", None, 0, REF, args_hash)  # (workspace, plan, task, tool ref, arguments): no execution id, no step number
