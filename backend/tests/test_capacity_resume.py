"""M6 / Priority 6: capacity accounting for RESUMED tasks. A suspended task holds no slot; a resumed one must acquire one atomically with the consumption of its approval, BEFORE
its action is dispatched; and a resumed task occupies a slot until it ends whichever process resumed it. Real SQLite database, two independent 'processes' sharing only it."""

import asyncio
import time
from datetime import datetime

from sqlalchemy import func, select

from agentplatform.execution import DbLimits, ExecutionService, ExecutionStore, Limits
from agentplatform.plan_runs import PlanRunService, RunRegistry
from agentplatform.providers import ProviderRegistry
from agentplatform.tools.invoker import ToolInvoker, ToolStore
from agentplatform.tools.approvals import ApprovalStore
from agentplatform.tools.fixtures import OUTBOX
from memory.models import AgentExecution
from memory.repository import MemoryRepository
from memory.short_term import ShortTermMemory
from tests.test_approvals import ARGS, FAST, reg  # noqa: F401  (reg is a fixture)
from tests.api_support import FakeRedis
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_plan_runs import definition, make_plan, task, world  # noqa: F401  (world is a fixture)
from tests.test_tools import BUILTIN, Scripted, asks, call, final


def process(world, reg, provider, limits):
    registry = RunRegistry(config=FAST)
    store = ApprovalStore(world["sf"], ttl_seconds=60.0)
    invoker = ToolInvoker(ToolStore(world["sf"]), reg, store, poll_seconds=0.02)
    svc = ExecutionService(world["sf"], orchestrator=world["orch"], providers=ProviderRegistry({"groq": lambda: provider}), short_term=ShortTermMemory(client=FakeRedis()),
                           long_term=MemoryRepository(world["sf"]), limits=Limits(total=8, per_user=3), tools=invoker, owner_id=registry.owner_id, db_limits=limits)
    return store, PlanRunService(world["sf"], executions=svc, registry=registry, busy_poll=0.01)


def occupied(world) -> int:
    store = ExecutionStore(world["sf"], limits=DbLimits(total=1, per_user=1))
    with world["sf"]() as session:
        return session.execute(select(func.count()).select_from(AgentExecution).where(store._live_running(session))).scalar()


def test_a_resume_acquires_capacity_before_dispatch_waits_when_full_and_then_proceeds_exactly_once(world, reg):
    from agentplatform.tools.fixtures import OUTBOX

    limits = DbLimits(total=1, per_user=1)  # ONE slot in the whole database
    world["agents"].create(world["sa"], "notifier", definition(allowed_tools=["demo.send_note"]))
    provider = Scripted(asks(call("demo__send_note__v1", ARGS), text="The customer must hear about the date."), final("Note sent (simulated)."))
    first_store, first = process(world, reg, provider, limits)
    plan_id = make_plan(world, [task("a", "notifier")])

    async def phase_one():
        started = await first.start(world["sa"], plan_id)
        deadline = time.time() + 20
        while time.time() < deadline and not first_store.list(world["sa"], status="pending"):
            await asyncio.sleep(0.05)
        await first.registry.wait(started.id, 30)
        return started.id

    run_id = asyncio.run(phase_one())
    assert world["db"].rows("SELECT task_key, resumed_at IS NULL FROM tool_suspensions") == [("a", 1)]
    assert occupied(world) == 0, "a task suspended on a human holds no slot"
    del first
    # the only slot is taken by someone else (a standalone execution of the same user), which a suspended task never blocked
    blocker = ExecutionStore(world["sf"], limits=limits).start(world["sa"], BUILTIN, "Research", "t", provider="groq", model=None)
    assert occupied(world) == 1
    second_store, second = process(world, reg, provider, limits)
    pending = second_store.list(world["sa"], status="pending")[0]
    second_store.decide(world["sa"], pending.id, "approve", submitted_hash=pending.payload_hash)

    async def both_phases():
        # ONE event loop for the whole resume (cancelling a driver that is mid-resume would close the task, which is not what is being tested)
        end = time.time() + 1.5
        while time.time() < end:
            await second.claim_work(resume=True)
            await asyncio.sleep(0.1)
        # FULL: the resume was refused atomically: nothing was consumed, dispatched or recorded, and the task is still suspended and recoverable
        assert OUTBOX == []
        assert world["db"].scalar("SELECT status FROM tool_approvals") == "approved"
        assert world["db"].rows("SELECT task_key, resumed_at IS NULL FROM tool_suspensions") == [("a", 1)]
        assert world["db"].scalar("SELECT count(*) FROM tool_invocations") == 0
        assert world["db"].scalar("SELECT status FROM plan_task_runs WHERE task_key = 'a'") == "running"
        # a slot frees up: the resume proceeds and the action is dispatched exactly once
        assert ExecutionStore(world["sf"]).finish(blocker, "succeeded", result="done")
        end = time.time() + 40
        while time.time() < end and world["db"].scalar("SELECT status FROM plan_runs") == "running":
            await second.claim_work(resume=True)
            await asyncio.sleep(0.1)
        if second.registry.active():
            await second.registry.wait(run_id, 30)

    asyncio.run(both_phases())
    assert world["db"].scalar("SELECT status FROM plan_runs") == "succeeded"
    assert len(OUTBOX) == 1 and world["db"].scalar("SELECT status FROM tool_approvals") == "consumed"
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations") == 1
    assert occupied(world) == 0


def test_a_resumed_task_keeps_its_slot_even_when_the_process_that_suspended_it_is_gone(world, reg):
    """The execution's owner row names the process that SUSPENDED it. Once resumed (suspension closed) and still running, it counts as occupied regardless: otherwise a dead
    owner would make a really-running resumed task invisible to the limits."""
    world["agents"].create(world["sa"], "notifier", definition(allowed_tools=["demo.send_note"]))
    provider = Scripted(asks(call("demo__send_note__v1", ARGS), text="Needs sending."), final("Done."))
    store, svc = process(world, reg, provider, DbLimits(total=1, per_user=1))
    plan_id = make_plan(world, [task("a", "notifier")])

    async def go():
        started = await svc.start(world["sa"], plan_id)
        while not store.list(world["sa"], status="pending"):
            await asyncio.sleep(0.05)
        await svc.registry.wait(started.id, 30)

    asyncio.run(go())
    assert world["db"].rows("SELECT count(*) FROM execution_owners WHERE execution_id = (SELECT execution_id FROM plan_task_runs WHERE task_key = 'a')") == [(1,)]
    assert world["db"].rows("SELECT count(*) FROM worker_processes WHERE expires_at > CURRENT_TIMESTAMP") == [(0,)]  # the suspending process is not alive
    assert occupied(world) == 0  # suspended: no slot, whoever owns it
    world["db"].run("UPDATE tool_suspensions SET resumed_at = '2026-01-01 00:00:00.000000'")  # the suspension is closed: the task was resumed (by a process that is not the owner)
    assert occupied(world) == 1  # resumed and still running: it holds a slot although its owner row names a dead process
