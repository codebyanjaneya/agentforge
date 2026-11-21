"""M4-C: human approval of ONE exact tool invocation. Service level on a migrated disposable SQLite database; a scripted provider stands in for a model.

Every test uses the SIMULATED fixture tool ``demo.send_note`` (agentplatform/tools/fixtures.py): it sends nothing anywhere and records each dispatch in
``OUTBOX`` so the tests can prove how many times it ran."""

import asyncio
import json
import threading
import time
import uuid
from datetime import datetime, timedelta

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import DatabaseError

import agentplatform.tools as tools_pkg
from agentplatform.execution import ExecutionService, ExecutionStore, Limits
from agentplatform.plan_runs import PlanRunService, PlanRunStore, RunRegistry
from agentplatform.providers import ProviderRegistry, ProviderResponse, ToolCall
from agentplatform.run_leases import Fence, LeaseLost
from agentplatform.tools import ToolPolicy, ToolRegistry, ToolSpec
from agentplatform.tools.approvals import ApprovalConflict, ApprovalNotFound, ApprovalStale, ApprovalStore, payload_hash
from agentplatform.tools.builtin import build_default_registry
from agentplatform.tools.fixtures import OUTBOX, register_demo_tools
from agentplatform.tools.invoker import ApprovalUnusable, InvocationContext, ToolInvoker, ToolRunContext, ToolStore, close_open
from memory.repository import MemoryRepository
from memory.short_term import ShortTermMemory
from tests.api_support import FakeRedis
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_plan_runs import definition, make_plan, task, world  # noqa: F401  (world is a fixture)
from tests.test_run_recovery import FAST, expire
from tests.test_tools import BUILTIN, Scripted, asks, call, final

ARGS = {"recipient": "customer@example.com", "subject": "Launch date", "body": "The launch is confirmed for next week."}


@pytest.fixture
def reg(monkeypatch):
    registry = build_default_registry()
    register_demo_tools(registry)
    monkeypatch.setattr(tools_pkg, "default_registry", lambda: registry)  # the agent-definition validator pins through the default registry
    import importlib

    monkeypatch.setattr(importlib.import_module("agentplatform.tools"), "default_registry", lambda: registry)  # ...and the CURRENT module, which a module purge by an earlier test may have replaced
    OUTBOX.clear()
    return registry


def stack(world, reg, provider=None, *, ttl=60.0, clock=datetime.utcnow, approvals=True):
    store = ApprovalStore(world["sf"], ttl_seconds=ttl, clock=clock) if approvals else None
    invoker = ToolInvoker(ToolStore(world["sf"]), reg, store, poll_seconds=0.02)
    svc = None
    if provider is not None:
        svc = ExecutionService(world["sf"], orchestrator=world["orch"], providers=ProviderRegistry({"groq": lambda: provider}), short_term=ShortTermMemory(client=FakeRedis()),
                               long_term=MemoryRepository(world["sf"]), limits=Limits(total=8, per_user=3), tools=invoker)
    return store, invoker, svc


def context(world, reg, run=None, tools=("demo.send_note",)):
    eid = ExecutionStore(world["sf"]).start(world["sa"], BUILTIN, "Research", "t", provider="groq", model=None)
    return InvocationContext(scope=world["sa"], execution_id=eid, agent_key="research", agent_version_id=None, allowed={reg.get(t).wire_name: reg.get(t) for t in tools}, run=run)


def human(world, store, decision, *, scope=None, after=0.0, results=None):
    """A person who decides on the first pending approval they see (in a thread, like a browser would)."""
    def act():
        deadline = time.time() + 20
        while time.time() < deadline:
            pending = store.list(scope or world["sa"], status="pending")
            if pending:
                time.sleep(after)
                try:
                    out = store.decide(scope or world["sa"], pending[0].id, decision)
                    if results is not None:
                        results.append(out)
                except Exception as exc:  # noqa: BLE001
                    if results is not None:
                        results.append(exc)
                return
            time.sleep(0.02)
    thread = threading.Thread(target=act)
    thread.start()
    return thread


def invoke(invoker, ctx, wire, args, seq=1, reason=None):
    return asyncio.run(invoker.invoke(ctx, wire, args, seq, reason=reason))


def approval_rows(world):
    return world["db"].rows("SELECT status, tool_id, tool_version, seq, invocation_id IS NOT NULL FROM tool_approvals ORDER BY created_at")


# ------------------------------------------------------------------------------------------------ binding

def test_the_payload_hash_binds_every_field_of_the_action():
    base = dict(workspace_id=uuid.uuid4(), user_id=uuid.uuid4(), execution_id=uuid.uuid4(), seq=1, run_id=uuid.uuid4(), task_key="a", agent_key="x", agent_version_id=uuid.uuid4(),
                tool_id="demo.send_note", tool_version=1, args_hash="a" * 64)
    ref = payload_hash(**base)
    assert ref == payload_hash(**base) and len(ref) == 64  # deterministic
    changes = {"workspace_id": uuid.uuid4(), "user_id": uuid.uuid4(), "execution_id": uuid.uuid4(), "seq": 2, "run_id": uuid.uuid4(), "task_key": "b", "agent_key": "y",
               "agent_version_id": uuid.uuid4(), "tool_id": "demo.other", "tool_version": 2, "args_hash": "b" * 64}
    for field, value in changes.items():
        assert payload_hash(**{**base, field: value}) != ref, field  # change ANY bound field and the old approval authorises nothing


# ------------------------------------------------------------------------------------------------ the lifecycle through the real invoker

def test_an_approved_call_runs_exactly_once_and_the_approval_is_consumed_and_linked(world, reg):
    store, invoker, _ = stack(world, reg)
    ctx = context(world, reg)
    wire = reg.get("demo.send_note").wire_name
    who = []
    thread = human(world, store, "approve", results=who, after=0.15)
    out = invoke(invoker, ctx, wire, ARGS, reason="The customer asked to be told the launch date.")
    thread.join(10)
    assert (out.ok, out.status) == (True, "succeeded") and json.loads(out.content)["simulated"] is True
    assert OUTBOX == [("customer@example.com", "Launch date", "The launch is confirmed for next week.")]
    assert approval_rows(world) == [("consumed", "demo.send_note", 1, 1, 1)]  # linked to the exact invocation it authorised
    assert world["db"].scalar("SELECT a.invocation_id = i.id AND a.execution_id = i.execution_id AND a.seq = i.seq FROM tool_approvals a JOIN tool_invocations i ON i.id = a.invocation_id") == 1
    assert world["db"].scalar("SELECT reason FROM tool_approvals") == "The customer asked to be told the launch date."
    assert world["db"].scalar("SELECT decided_by FROM tool_approvals") is not None
    # a replay of the same step is refused (unique step) and can never dispatch again
    with pytest.raises(Exception):
        invoke(invoker, ctx, wire, ARGS)
    assert len(OUTBOX) == 1


def test_a_rejected_action_never_executes_and_is_recorded_as_such(world, reg):
    store, invoker, _ = stack(world, reg)
    ctx = context(world, reg)
    thread = human(world, store, "reject")
    out = invoke(invoker, ctx, reg.get("demo.send_note").wire_name, ARGS)
    thread.join(10)
    assert (out.ok, out.status, out.error_code) == (False, "rejected", "approval_rejected") and OUTBOX == []
    assert approval_rows(world) == [("rejected", "demo.send_note", 1, 1, 0)]
    assert world["db"].rows("SELECT status, error_code FROM tool_invocations") == [("rejected", "approval_rejected")]


def test_an_expired_approval_cannot_be_decided_or_consumed(world, reg):
    now = [datetime.utcnow()]
    store, invoker, _ = stack(world, reg, ttl=5.0, clock=lambda: now[0])
    ctx = context(world, reg)
    wire = reg.get("demo.send_note").wire_name

    async def scenario():
        job = asyncio.ensure_future(invoker.invoke(ctx, wire, ARGS, 1))
        await asyncio.sleep(0.3)
        now[0] += timedelta(seconds=6)  # nobody decided in time
        return await job

    out = asyncio.run(scenario())
    assert (out.status, out.error_code) == ("rejected", "approval_expired") and OUTBOX == []
    assert approval_rows(world) == [("expired", "demo.send_note", 1, 1, 0)]
    approval_id = world["db"].scalar("SELECT id FROM tool_approvals")
    with pytest.raises(ApprovalConflict):
        store.decide(world["sa"], uuid.UUID(hex=approval_id.replace("-", "")), "approve")  # too late
    assert OUTBOX == []


def test_an_approval_that_expires_after_it_was_granted_cannot_be_consumed(world, reg):
    store = ApprovalStore(world["sf"], ttl_seconds=0.4)
    ctx = context(world, reg)
    spec = reg.get("demo.send_note")
    approval_id = store.request(ctx, spec, 1, json.dumps(ARGS, sort_keys=True, separators=(",", ":")), "h" * 64, None)
    store.decide(world["sa"], approval_id, "approve")
    time.sleep(0.6)  # approved, but the window closed before the action was dispatched
    with pytest.raises(ApprovalUnusable) as info:
        ToolStore(world["sf"]).begin(ctx, spec, 1, "{}", "h" * 64, uuid.uuid4().hex * 2, approval_id)
    assert info.value.reason == "expired" and world["db"].scalar("SELECT count(*) FROM tool_invocations") == 0 and OUTBOX == []


# ------------------------------------------------------------------------------------------------ nothing but the exact action is authorised

def _approved(world, reg, args_hash="h" * 64, seq=1):
    store = ApprovalStore(world["sf"])
    ctx = context(world, reg)
    spec = reg.get("demo.send_note")
    approval_id = store.request(ctx, spec, seq, "{}", args_hash, None)
    store.decide(world["sa"], approval_id, "approve")
    return store, ctx, spec, approval_id


def test_changed_arguments_invalidate_the_approval(world, reg):
    store, ctx, spec, approval_id = _approved(world, reg)
    with pytest.raises(ApprovalUnusable) as info:
        ToolStore(world["sf"]).begin(ctx, spec, 1, "{}", "x" * 64, uuid.uuid4().hex * 2, approval_id)  # the model changed the arguments after approval
    assert info.value.reason == "invalid" and world["db"].scalar("SELECT count(*) FROM tool_invocations") == 0
    store.invalidate(approval_id)  # (the invoker does this on ApprovalUnusable)
    assert world["db"].scalar("SELECT status FROM tool_approvals") == "invalidated"


def test_a_changed_tool_version_step_agent_run_or_workspace_never_matches(world, reg):
    from types import SimpleNamespace

    store, ctx, spec, approval_id = _approved(world, reg)
    v2 = type(spec)(**{**{f: getattr(spec, f) for f in ("id", "title", "summary", "description", "category", "input_model", "output_model", "risk", "side_effect", "timeout_seconds",
                                                          "max_output_bytes", "capabilities", "limitations", "example", "lifecycle")}, "version": 2})
    attempts = [dict(spec=v2, seq=1), dict(spec=spec, seq=2)]  # another tool version, another step
    for attempt in attempts:
        with pytest.raises(ApprovalUnusable):
            ToolStore(world["sf"]).begin(ctx, attempt["spec"], attempt["seq"], "{}", "h" * 64, uuid.uuid4().hex * 2, approval_id)
    other = InvocationContext(scope=ctx.scope, execution_id=ctx.execution_id, agent_key="someone-else", agent_version_id=None, allowed=ctx.allowed)  # another agent
    with pytest.raises(ApprovalUnusable):
        ToolStore(world["sf"]).begin(other, spec, 1, "{}", "h" * 64, uuid.uuid4().hex * 2, approval_id)
    run_ctx = InvocationContext(scope=ctx.scope, execution_id=ctx.execution_id, agent_key=ctx.agent_key, agent_version_id=None, allowed=ctx.allowed, run=ToolRunContext(uuid.uuid4(), "a", None))
    with pytest.raises(Exception):  # another run (unknown run: also refused by the foreign key)
        ToolStore(world["sf"]).begin(run_ctx, spec, 1, "{}", "h" * 64, uuid.uuid4().hex * 2, approval_id)
    foreign = InvocationContext(scope=world["sb"], execution_id=ctx.execution_id, agent_key=ctx.agent_key, agent_version_id=None, allowed=ctx.allowed)
    with pytest.raises(ApprovalUnusable):  # another workspace/user can neither see nor consume it
        ToolStore(world["sf"]).begin(foreign, spec, 1, "{}", "h" * 64, uuid.uuid4().hex * 2, approval_id)
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations") == 0 and OUTBOX == []


def test_an_approval_is_single_use_and_a_replay_cannot_dispatch(world, reg):
    store, ctx, spec, approval_id = _approved(world, reg)
    tool_store = ToolStore(world["sf"])
    first = tool_store.begin(ctx, spec, 1, "{}", "h" * 64, uuid.uuid4().hex * 2, approval_id)
    assert world["db"].rows("SELECT status, invocation_id IS NOT NULL FROM tool_approvals") == [("consumed", 1)]
    with pytest.raises(Exception):  # same step again: the unique step (and the consumed approval) refuse it
        tool_store.begin(ctx, spec, 1, "{}", "h" * 64, uuid.uuid4().hex * 2, approval_id)
    with pytest.raises(ApprovalConflict):
        store.decide(world["sa"], approval_id, "approve")  # cannot be decided again
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations") == 1 and first is not None


def test_concurrent_decisions_and_concurrent_consumption_have_exactly_one_winner(world, reg):
    store = ApprovalStore(world["sf"])
    ctx = context(world, reg)
    spec = reg.get("demo.send_note")
    approval_id = store.request(ctx, spec, 1, "{}", "h" * 64, None)
    barrier, outcomes = threading.Barrier(8), []

    def decide(i):
        barrier.wait()
        try:
            store.decide(world["sa"], approval_id, "approve" if i % 2 == 0 else "reject")
            outcomes.append("won")
        except ApprovalConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=decide, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert outcomes.count("won") == 1 and outcomes.count("conflict") == 7
    status = world["db"].scalar("SELECT status FROM tool_approvals")
    if status != "approved":
        return  # the winner rejected: nothing can be consumed, which is the point
    barrier2, results = threading.Barrier(6), []

    def consume():
        barrier2.wait()
        try:
            ToolStore(world["sf"]).begin(ctx, spec, 1, "{}", "h" * 64, uuid.uuid4().hex * 2, approval_id)
            results.append("ok")
        except Exception as exc:  # noqa: BLE001
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=consume) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert results.count("ok") == 1 and world["db"].scalar("SELECT count(*) FROM tool_invocations") == 1


def test_a_stale_view_and_a_foreign_workspace_cannot_decide(world, reg):
    store = ApprovalStore(world["sf"])
    ctx = context(world, reg)
    approval_id = store.request(ctx, reg.get("demo.send_note"), 1, "{}", "h" * 64, None)
    with pytest.raises(ApprovalNotFound):
        store.decide(world["sb"], approval_id, "approve")  # another user's workspace: indistinguishable from unknown
    with pytest.raises(ApprovalNotFound):
        store.get(world["sb"], approval_id)
    assert store.list(world["sb"]) == [] and store.pending_count(world["sb"]) == 0
    with pytest.raises(ApprovalStale):
        store.decide(world["sa"], approval_id, "approve", submitted_hash="0" * 64)  # the client was shown something else: nothing is decided
    assert world["db"].scalar("SELECT status FROM tool_approvals") == "pending"
    assert store.decide(world["sa"], approval_id, "approve", submitted_hash=world["db"].scalar("SELECT payload_hash FROM tool_approvals")).status == "approved"


# ------------------------------------------------------------------------------------------------ policy fails closed

def test_without_an_approval_path_or_with_an_unknown_policy_nothing_sensitive_runs(world, reg):
    ctx = context(world, reg)
    wire = reg.get("demo.send_note").wire_name
    _, no_approvals, _ = stack(world, reg, approvals=False)
    out = invoke(no_approvals, ctx, wire, ARGS)
    assert (out.status, out.error_code) == ("rejected", "approval_required") and OUTBOX == []  # no approval service: fail closed

    class Odd(ToolPolicy):
        def decide(self, spec):
            return "something-new"  # an unknown verdict is never 'allow'

    odd = ToolRegistry(Odd(), allow_gated=True)
    register_demo_tools(odd)
    assert odd.executable(odd.get("demo.send_note")) is False and odd.gated(odd.get("demo.send_note")) is True
    safe = reg.get("math.calculate")
    assert reg.executable(safe) and not reg.gated(safe)  # a read-only tool still runs with no approval at all
    ctx2 = context(world, reg, tools=("math.calculate",))
    store, invoker, _ = stack(world, reg)
    assert invoke(invoker, ctx2, safe.wire_name, {"expression": "6*7"}).status == "succeeded" and world["db"].scalar("SELECT count(*) FROM tool_approvals") == 0


def test_a_grant_is_not_an_approval_and_gated_grants_need_the_registry_to_allow_them(world, reg):
    created = world["agents"].create(world["sa"], "notifier", definition(allowed_tools=["demo.send_note"]))
    assert created.definition.allowed_tools == ["demo.send_note@1"] and world["db"].scalar("SELECT count(*) FROM tool_invocations") == 0  # granted and pinned: this alone runs nothing
    strict = ToolRegistry()
    register_demo_tools(strict)
    with pytest.raises(ValueError):
        strict.pin("demo.send_note")  # a registry that does not allow gated grants refuses to pin it (the M4-B behaviour)


# ------------------------------------------------------------------------------------------------ redaction and the table's own rules

def test_arguments_and_reason_are_redacted_in_the_stored_approval(world, reg):
    store = ApprovalStore(world["sf"])
    ctx = context(world, reg)
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    store.request(ctx, reg.get("demo.send_note"), 1, json.dumps({"body": f"key {secret}"}), "h" * 64, f"because {secret}")
    stored = json.dumps(world["db"].rows("SELECT args_json, reason FROM tool_approvals"))
    assert secret not in stored and "[redacted]" in stored


def test_the_approval_table_enforces_its_own_rules(world, reg):
    store, ctx, spec, approval_id = _approved(world, reg)
    hexid = approval_id.hex
    for statement in (f"UPDATE tool_approvals SET tool_id = 'other.tool' WHERE id = '{hexid}'", f"UPDATE tool_approvals SET args_hash = '{'z' * 64}' WHERE id = '{hexid}'",
                      f"UPDATE tool_approvals SET payload_hash = '{'z' * 64}' WHERE id = '{hexid}'", f"UPDATE tool_approvals SET status = 'pending' WHERE id = '{hexid}'",
                      f"UPDATE tool_approvals SET status = 'rejected' WHERE id = '{hexid}'", f"UPDATE tool_approvals SET status = 'consumed' WHERE id = '{hexid}'",  # no invocation link
                      "DELETE FROM tool_approvals"):
        with pytest.raises(DatabaseError):
            world["db"].run(statement)
    ToolStore(world["sf"]).begin(ctx, spec, 1, "{}", "h" * 64, uuid.uuid4().hex * 2, approval_id)
    for statement in (f"UPDATE tool_approvals SET status = 'approved' WHERE id = '{hexid}'", f"UPDATE tool_approvals SET status = 'invalidated' WHERE id = '{hexid}'"):
        with pytest.raises(DatabaseError):
            world["db"].run(statement)  # consumed is final


# ------------------------------------------------------------------------------------------------ recovery and fencing

def test_recovery_invalidates_open_approvals_so_nothing_runs_after_a_restart(world, reg):
    store = ApprovalStore(world["sf"])
    pending_ctx, approved_ctx = context(world, reg), context(world, reg)
    spec = reg.get("demo.send_note")
    store.request(pending_ctx, spec, 1, "{}", "a" * 64, None)
    approved = store.request(approved_ctx, spec, 1, "{}", "b" * 64, None)
    store.decide(world["sa"], approved, "approve")  # approved, but the process died before the action was dispatched
    with world["sf"]() as session, session.begin():
        close_open(session, [pending_ctx.execution_id, approved_ctx.execution_id])
    assert sorted(r[0] for r in approval_rows(world)) == ["invalidated", "invalidated"]
    with pytest.raises(ApprovalUnusable):
        ToolStore(world["sf"]).begin(approved_ctx, spec, 1, "{}", "b" * 64, uuid.uuid4().hex * 2, approved)  # the old approval cannot be used later
    assert OUTBOX == []


def test_a_stale_owner_cannot_open_an_approval_request(world, reg):
    from agentplatform.tools.invoker import ToolRunContext as Run

    plan_id = make_plan(world, [task("a", "research")])
    old = PlanRunStore(world["sf"], owner_id="owner-old", config=FAST)
    run_id = old.create(world["sa"], plan_id)
    fence = Fence(run_id, "owner-old", 1)
    _, tasks = old.load_for_driver(run_id)
    eid = old.start_task(tasks[0].id, world["sa"], BUILTIN, "Research", "t", "groq", None, executions=world["executions"].store, fence=fence)
    spec = reg.get("demo.send_note")
    ctx = InvocationContext(scope=world["sa"], execution_id=eid, agent_key="research", agent_version_id=None, allowed={spec.wire_name: spec}, run=Run(run_id, "a", fence))
    store = ApprovalStore(world["sf"])
    assert store.request(ctx, spec, 1, "{}", "h" * 64, None) is not None  # the owner can
    expire(world)
    assert PlanRunStore(world["sf"], owner_id="owner-new", config=FAST).leases.takeover(run_id, "owner-new", 30) is not None
    with pytest.raises(LeaseLost):
        store.request(ctx, spec, 2, "{}", "h" * 64, None)  # a stale owner can open nothing


def test_cancelling_a_waiting_task_invalidates_its_approval(world, reg):
    store, invoker, _ = stack(world, reg)
    ctx = context(world, reg)
    wire = reg.get("demo.send_note").wire_name

    async def scenario():
        job = asyncio.ensure_future(invoker.invoke(ctx, wire, ARGS, 1))
        await asyncio.sleep(0.3)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job

    asyncio.run(scenario())
    assert approval_rows(world) == [("invalidated", "demo.send_note", 1, 1, 0)] and OUTBOX == []


# ------------------------------------------------------------------------------------------------ durable suspension (M4-C finalization)

def plan_stack(world, reg, provider, *, ttl=60.0):
    """Two INDEPENDENT 'processes' sharing only the database: each has its own approval store, invoker, execution service, run registry (owner id) and run service."""
    def one():
        store, invoker, svc = stack(world, reg, provider, ttl=ttl)
        return store, PlanRunService(world["sf"], executions=svc, registry=RunRegistry(config=FAST), busy_poll=0.01)

    return one


def event_types(world, svc, run_id):
    return [e.event_type for e in svc.store.events(world["sa"], run_id, 0, 100)["items"]]


def seen_pending(world, store, svc, run_id, timeout=20):
    """Wait (in the caller's loop) until the independent task finished and the approval is pending."""
    async def wait():
        deadline = time.time() + timeout
        while time.time() < deadline:
            states = {t.key: t.status for t in (await asyncio.to_thread(svc.store.get, world["sa"], run_id)).tasks}
            if states.get("b") == "succeeded" and await asyncio.to_thread(store.list, world["sa"], status="pending"):
                return {t.key: t.status for t in (await asyncio.to_thread(svc.store.get, world["sa"], run_id)).tasks}  # re-read: the task may have started between the two reads
            await asyncio.sleep(0.05)
        raise AssertionError("never reached the approval")

    return wait()


def test_a_task_is_durably_suspended_while_independent_work_finishes_and_a_DIFFERENT_process_resumes_exactly_that_call_once(world, reg):
    world["agents"].create(world["sa"], "notifier", definition(allowed_tools=["demo.send_note"]))
    provider = Scripted(asks(call("demo__send_note__v1", ARGS), text="The customer must hear about the date."), final("Note sent (simulated)."))
    new = plan_stack(world, reg, provider)
    first_store, first = new()
    plan_id = make_plan(world, [task("a", "notifier"), task("b", "research"), task("c", "ceo", ["a", "b"])])

    async def phase_one():
        started = await first.start(world["sa"], plan_id)
        states = await seen_pending(world, first_store, first, started.id)
        await first.registry.wait(started.id, 30)  # the driver ENDS: it parks the run
        return started.id, states

    run_id, states = asyncio.run(phase_one())
    assert states["a"] == "running" and OUTBOX == []
    # -- durably suspended: persisted, nobody holds it
    assert first.registry.active() == 0, "no in-memory task for the waiting approval"
    assert world["db"].rows("SELECT status FROM plan_runs") == [("running",)]
    assert world["db"].rows("SELECT task_key, status FROM plan_task_runs ORDER BY task_key") == [("a", "running"), ("b", "succeeded"), ("c", "pending")]
    assert world["db"].rows("SELECT task_key, resumed_at IS NULL FROM tool_suspensions") == [("a", 1)]
    assert world["db"].scalar("SELECT status FROM tool_approvals") == "pending"
    assert first.store.get(world["sa"], run_id).ownership == "suspended"
    types = event_types(world, first, run_id)
    assert types.count("approval_requested") == 1 and types.count("task_suspended") == 1 and "approval_consumed" not in types and "run_completed" not in types
    # a sweep that cannot decide anything leaves the parked run alone, and nobody claims it needlessly: no events pile up
    before = len(types)
    second_store, second = new()
    assert asyncio.run(second.claim_work(resume=True)) == {"resumed": 0, "closed": 0} and first.store.recover_expired() == [] and len(event_types(world, first, run_id)) == before
    assert world["db"].scalar("SELECT status FROM plan_task_runs WHERE task_key = 'a'") == "running"
    # -- the first process is gone: a SECOND, unrelated process learns of the decision and resumes the exact call
    del first
    pending = second_store.list(world["sa"], status="pending")[0]
    assert (pending.task_key, pending.agent_key, pending.tool_id, pending.suspended) == ("a", "notifier", "demo.send_note", True)
    second_store.decide(world["sa"], pending.id, "approve", submitted_hash=pending.payload_hash)
    async def phase_two():
        claimed = await second.claim_work(resume=True)
        assert claimed["resumed"] == 1
        await second.registry.wait(run_id, 30)

    asyncio.run(phase_two())
    view = second.store.get(world["sa"], run_id)
    assert view.status == "succeeded" and {t.key: t.status for t in view.tasks} == {"a": "succeeded", "b": "succeeded", "c": "succeeded"}
    assert len(OUTBOX) == 1 and OUTBOX[0][0] == "customer@example.com"  # dispatched exactly once, by the second process
    assert world["db"].rows("SELECT status, task_key FROM tool_approvals") == [("consumed", "a")]
    assert world["db"].rows("SELECT task_key, resumed_at IS NOT NULL FROM tool_suspensions") == [("a", 1)]
    assert world["db"].rows("SELECT status, task_key, run_id IS NOT NULL FROM tool_invocations") == [("succeeded", "a", 1)]
    types = event_types(world, second, run_id)
    lineage = [t for t in types if t.startswith("approval_") or t in ("task_suspended", "task_resumed", "run_resumed", "run_completed")]
    assert lineage == ["approval_requested", "task_suspended", "approval_approved", "run_resumed", "approval_consumed", "task_resumed", "run_completed"], lineage
    assert types.count("task_suspended") == 1 and types.count("task_resumed") == 1  # never duplicated
    assert world["db"].scalar("SELECT reason FROM tool_approvals") == "The customer must hear about the date."
    # events carry the tool ref, the step and ids only: never a value
    stored = json.dumps(world["db"].rows("SELECT detail FROM execution_events WHERE event_type LIKE 'approval_%' OR event_type LIKE 'task_%'"))
    assert "customer@example.com" not in stored and "launch is confirmed" not in stored and "demo.send_note@1" in stored


def test_two_processes_racing_to_resume_one_decided_task_dispatch_it_exactly_once(world, reg):
    world["agents"].create(world["sa"], "notifier", definition(allowed_tools=["demo.send_note"]))
    provider = Scripted(asks(call("demo__send_note__v1", ARGS)), final("done"))
    new = plan_stack(world, reg, provider)
    store_a, a = new()
    plan_id = make_plan(world, [task("a", "notifier")])

    async def phase_one():
        started = await a.start(world["sa"], plan_id)
        deadline = time.time() + 20
        while time.time() < deadline and not await asyncio.to_thread(store_a.list, world["sa"], status="pending"):
            await asyncio.sleep(0.05)
        await a.registry.wait(started.id, 30)
        return started.id

    run_id = asyncio.run(phase_one())
    store_a.decide(world["sa"], store_a.list(world["sa"], status="pending")[0].id, "approve")
    others = [new()[1] for _ in range(4)]

    async def race():
        results = await asyncio.gather(*[o.claim_work(resume=True) for o in others])
        await asyncio.gather(*[o.registry.wait(run_id, 30) for o in others])
        return results

    results = asyncio.run(race())
    assert sum(r["resumed"] for r in results) == 1, results  # the lease take-over is a compare-and-swap: one winner
    assert len(OUTBOX) == 1 and others[0].store.get(world["sa"], run_id).status == "succeeded"
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations WHERE status = 'succeeded'") == 1


def test_a_rejected_expired_or_altered_approval_never_executes_after_a_resume(world, reg):
    world["agents"].create(world["sa"], "notifier", definition(allowed_tools=["demo.send_note"]))

    def suspended_run(ttl=60.0):
        provider = Scripted(asks(call("demo__send_note__v1", ARGS)), final("I could not send the note."))
        new = plan_stack(world, reg, provider, ttl=ttl)
        store, svc = new()
        plan_id = make_plan(world, [task("a", "notifier")])

        async def go():
            started = await svc.start(world["sa"], plan_id)
            deadline = time.time() + 20
            while time.time() < deadline and not await asyncio.to_thread(store.list, world["sa"], status="pending"):
                await asyncio.sleep(0.05)
            await svc.registry.wait(started.id, 30)
            return started.id

        run_id = asyncio.run(go())
        return provider, new, store, run_id

    def resume(new, run_id):
        store, svc = new()

        async def go():
            await svc.claim_work(resume=True)
            await svc.registry.wait(run_id, 30)

        asyncio.run(go())
        return svc

    # rejected
    provider, new, store, run_id = suspended_run()
    store.decide(world["sa"], store.list(world["sa"], status="pending")[0].id, "reject")
    svc = resume(new, run_id)
    assert svc.store.get(world["sa"], run_id).status == "succeeded" and OUTBOX == []
    assert json.loads([m for m in provider.requests[-1].messages if m.role == "tool"][0].content)["error"] == "approval_rejected"
    assert "task_resumed" in event_types(world, svc, run_id) and "approval_rejected" in event_types(world, svc, run_id)
    # expired: nobody decided in time
    provider, new, store, run_id = suspended_run(ttl=0.5)
    time.sleep(0.8)
    svc = resume(new, run_id)
    assert OUTBOX == [] and json.loads([m for m in provider.requests[-1].messages if m.role == "tool"][0].content)["error"] == "approval_expired"
    assert "approval_expired" in event_types(world, svc, run_id)
    # altered: the stored approval no longer matches itself (tampered with underneath the application) -> invalidated at consumption, never executed
    provider, new, store, run_id = suspended_run()
    store.decide(world["sa"], store.list(world["sa"], status="pending")[0].id, "approve")
    world["db"].run("DROP TRIGGER tool_approvals_no_update")
    world["db"].run(f"UPDATE tool_approvals SET args_hash = '{'f' * 64}' WHERE status = 'approved'")
    svc = resume(new, run_id)
    assert OUTBOX == [] and json.loads([m for m in provider.requests[-1].messages if m.role == "tool"][0].content)["error"] == "approval_invalid"
    assert world["db"].scalar("SELECT status FROM tool_approvals WHERE status = 'invalidated'") == "invalidated"
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations WHERE status = 'succeeded'") == 0


def test_a_sensitive_tool_that_is_mislabelled_as_safe_cannot_run_unattended(world):
    """The tool CLAIMS read/none (as a hostile or careless author might), but it is not on the server's allowlist: it needs approval, so it suspends and never runs by itself."""
    registry = build_default_registry()
    ran = []

    class In(BaseModel):
        model_config = ConfigDict(extra="forbid")
        n: int

    class Out(BaseModel):
        model_config = ConfigDict(extra="forbid")
        n: int

    registry.register(ToolSpec(id="shady.transfer", version=1, title="Transfer", summary="s", description="d", category="Shady", input_model=In, output_model=Out, risk="read", side_effect="none"),
                      lambda a: (ran.append(a.n), Out(n=a.n))[1])
    shady = registry.get("shady.transfer")
    assert shady.risk == "read" and shady.side_effect == "none"  # exactly what a safe tool declares
    assert registry.policy.decide(shady) == "requires_approval" and not registry.executable(shady) and registry.gated(shady)
    store, invoker, _ = stack(world, registry)
    ctx = context(world, registry, tools=("shady.transfer",))
    no_approvals = ToolInvoker(ToolStore(world["sf"]), registry, None)
    out = asyncio.run(no_approvals.invoke(ctx, shady.wire_name, {"n": 1}, 1))
    assert (out.status, out.error_code) == ("rejected", "approval_required") and ran == []  # with no approval path it is refused outright
    thread = human(world, store, "reject")
    out = invoke(invoker, context(world, registry, tools=("shady.transfer",)), shady.wire_name, {"n": 2}, 1)  # with the approval path it waits for a human, and a rejection stops it
    thread.join(10)
    assert out.error_code == "approval_rejected" and ran == []
    assert registry.policy.decide(registry.get("math.calculate")) == "allow" and registry.policy.fingerprint == build_default_registry().policy.fingerprint


def test_only_an_owner_or_admin_may_decide_and_a_member_can_see_but_not_approve(world, reg):
    from authz.workspaces import authorize_memory_scope
    from tests.test_authz_memory import team_workspace

    team = team_workspace(world["db"], world["a"], [(world["b"], "member")])
    owner_scope, member_scope = authorize_memory_scope(world["db"].session_factory, world["a"], team), authorize_memory_scope(world["db"].session_factory, world["b"], team)
    store = ApprovalStore(world["sf"])
    eid = ExecutionStore(world["sf"]).start(owner_scope, BUILTIN, "Research", "t", provider="groq", model=None)
    spec = reg.get("demo.send_note")
    ctx = InvocationContext(scope=owner_scope, execution_id=eid, agent_key="research", agent_version_id=None, allowed={spec.wire_name: spec})
    approval_id = store.request(ctx, spec, 1, "{}", "h" * 64, None)
    assert store.can_decide(owner_scope) is True and store.can_decide(member_scope) is False
    assert [a.id for a in store.list(member_scope)] == [approval_id]  # a member can SEE the request
    from agentplatform.tools.approvals import ApprovalForbidden

    with pytest.raises(ApprovalForbidden):
        store.decide(member_scope, approval_id, "approve")
    with pytest.raises(ApprovalForbidden):
        store.decide(member_scope, approval_id, "reject")
    assert world["db"].scalar("SELECT status FROM tool_approvals") == "pending"
    admin = team_workspace(world["db"], world["a"], [(world["b"], "admin")])
    admin_scope = authorize_memory_scope(world["db"].session_factory, world["b"], admin)
    assert store.can_decide(admin_scope) is True
    assert store.decide(owner_scope, approval_id, "approve").status == "approved"
    assert world["db"].rows("SELECT event_type FROM execution_events") == []  # a standalone approval has no run history


def test_a_cancelled_suspended_run_invalidates_its_approval_and_executes_nothing(world, reg):
    world["agents"].create(world["sa"], "notifier", definition(allowed_tools=["demo.send_note"]))
    provider = Scripted(asks(call("demo__send_note__v1", ARGS)), final("done"))
    new = plan_stack(world, reg, provider)
    store, svc = new()
    plan_id = make_plan(world, [task("a", "notifier")])

    async def go():
        started = await svc.start(world["sa"], plan_id)
        deadline = time.time() + 20
        while time.time() < deadline and not await asyncio.to_thread(store.list, world["sa"], status="pending"):
            await asyncio.sleep(0.05)
        await svc.registry.wait(started.id, 30)
        other = new()[1]
        await other.cancel(world["sa"], started.id)
        return started.id

    run_id = asyncio.run(go())
    assert world["db"].scalar("SELECT status FROM tool_approvals") == "invalidated" and OUTBOX == []
    assert svc.store.get(world["sa"], run_id).status == "cancelled"
    assert "approval_invalidated" in event_types(world, svc, run_id)


def test_a_suspended_task_holds_no_database_wide_execution_slot_until_it_resumes(world, reg):
    from sqlalchemy import func, select

    from agentplatform.tools.invoker import ToolRunContext
    from memory.models import AgentExecution

    spec = reg.get("demo.send_note")
    store = ApprovalStore(world["sf"])
    exec_store = ExecutionStore(world["sf"])

    def occupied():
        with world["sf"]() as session:
            return session.execute(select(func.count()).select_from(AgentExecution).where(exec_store._live_running(session))).scalar()

    plan_id = make_plan(world, [task("a", "research")])
    runs = PlanRunStore(world["sf"])
    run_id = runs.create(world["sa"], plan_id)
    fence = Fence(run_id, runs.owner_id, 1)
    _, tasks = runs.load_for_driver(run_id)
    eid = runs.start_task(tasks[0].id, world["sa"], BUILTIN, "Research", "t", "groq", None, executions=world["executions"].store, fence=fence)
    assert occupied() == 1  # a running task occupies a slot
    ctx = InvocationContext(scope=world["sa"], execution_id=eid, agent_key="research", agent_version_id=None, allowed={spec.wire_name: spec}, run=ToolRunContext(run_id, "a", fence, "api"))
    store.request(ctx, spec, 1, "{}", "h" * 64, None, {"task": "t", "messages": (), "calls": (), "round_number": 0, "call_index": 0})
    assert occupied() == 0, "the suspended execution no longer occupies a slot"
