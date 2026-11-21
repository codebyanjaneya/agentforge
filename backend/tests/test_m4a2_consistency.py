"""M4-A2 Part 1: a task run and its execution record start together and finish together (one fenced transaction each).

Deterministic failure injection at every relevant transaction boundary: the event write inside the transaction (a failure after the
execution row was written), admission refusal, a lost lease, a task that is no longer pending, an execution that was already closed, a
provider failure, a database outage. After every one of them the two records must agree (both written or neither), nothing a stale owner
did may be visible, and a result is only used after it is committed."""

import asyncio
import time

import pytest

from agentplatform.execution import DbLimits, ExecutionStore
from agentplatform.plan_runs import PlanRunStore, RunRegistry
from agentplatform.run_leases import Fence, LeaseConfig, LeaseLost
from agentplatform.service import ResolvedAgent
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_plan_runs import CHAIN, definition, go, make_plan, statuses, task, world  # noqa: F401  (world is a fixture)
from tests.test_run_recovery import FAST, expire

BUILTIN = ResolvedAgent("builtin", "research", None, None, None)


def inject_once(monkeypatch, event_type):
    """Make the FIRST write of ``event_type`` fail: it happens inside the transaction, after the state rows were written."""
    original = PlanRunStore._event  # the plain function (a staticmethod read from the class)
    fired = []

    def event(session, run_id, scope, kind, actor, **kw):
        if kind == event_type and not fired:
            fired.append(kind)
            raise RuntimeError("injected failure inside the transaction")
        return original(session, run_id, scope, kind, actor, **kw)

    monkeypatch.setattr(PlanRunStore, "_event", staticmethod(event))
    return fired


def pair(world):
    """(execution statuses, task statuses): the two views that must never disagree."""
    return world["db"].rows("SELECT status, error_code FROM agent_executions ORDER BY started_at"), world["db"].rows("SELECT task_key, status, error_code FROM plan_task_runs ORDER BY position")


# ------------------------------------------------------------------------------------------------ the start boundary

def test_a_failure_while_a_task_starts_leaves_no_execution_behind_and_the_agent_is_never_called(world, monkeypatch):
    plan_id = make_plan(world, [task("a", "research")])
    fired = inject_once(monkeypatch, "task_started")  # fails AFTER the execution row was written, inside the same transaction
    run = go(world, plan_id)
    assert fired == ["task_started"]
    assert world["db"].scalar("SELECT count(*) FROM agent_executions") == 0 and world["db"].scalar("SELECT count(*) FROM execution_owners") == 0  # rolled back with the task
    assert (run.status, statuses(run), run.tasks[0].error_code, run.tasks[0].execution_id) == ("failed", {"a": "failed"}, "internal_error", None)
    assert world["orch"].calls == []  # the agent was never called


def test_an_admission_refusal_leaves_the_task_pending_and_writes_nothing(world):
    plan_id = make_plan(world, [task("a", "research")])
    world["executions"].store._limits = DbLimits(total=1, per_user=1)
    holder = world["executions"].store.start(world["sa"], BUILTIN, "Research", "an execution that occupies the only slot", provider="groq", model=None)
    run = go(world, plan_id, svc=world["service"](busy_budget=0.1))
    assert (run.status, run.tasks[0].error_code, run.tasks[0].execution_id) == ("failed", "busy", None)  # the honest M3-B outcome: never started
    assert world["db"].rows("SELECT id FROM agent_executions") == [(holder.hex,)] and world["orch"].calls == []  # no extra execution, no agent call


def test_a_stale_owner_cannot_start_a_task_and_nothing_is_written(world):
    plan_id = make_plan(world, CHAIN)
    old = PlanRunStore(world["sf"], owner_id="owner-old", config=FAST)
    run_id = old.create(world["sa"], plan_id)
    _, tasks = old.load_for_driver(run_id)
    first = tasks[0]
    expire(world)
    new = PlanRunStore(world["sf"], owner_id="owner-new", config=FAST)
    assert new.leases.takeover(run_id, "owner-new", 30) is not None
    before = pair(world), world["db"].rows("SELECT * FROM execution_events ORDER BY id")
    with pytest.raises(LeaseLost):
        old.start_task(first.id, world["sa"], BUILTIN, "Research", "text", "groq", None, executions=world["executions"].store, fence=Fence(run_id, "owner-old", 1))
    assert (pair(world), world["db"].rows("SELECT * FROM execution_events ORDER BY id")) == before
    assert world["db"].scalar("SELECT count(*) FROM agent_executions") == 0 and world["db"].scalar("SELECT count(*) FROM execution_owners") == 0


def test_a_task_that_is_not_pending_is_never_started_twice(world):
    plan_id = make_plan(world, [task("a", "research")])
    store = world["svc"].store
    run_id = store.create(world["sa"], plan_id)
    _, tasks = store.load_for_driver(run_id)
    fence = Fence(run_id, store.owner_id, 1)
    store.start_task(tasks[0].id, world["sa"], BUILTIN, "Research", "text", "groq", None, executions=world["executions"].store, fence=fence)
    with pytest.raises(RuntimeError):
        store.start_task(tasks[0].id, world["sa"], BUILTIN, "Research", "text", "groq", None, executions=world["executions"].store, fence=fence)
    assert world["db"].scalar("SELECT count(*) FROM agent_executions") == 1  # the second attempt's execution row was rolled back with it
    assert world["db"].scalar("SELECT count(*) FROM execution_events WHERE event_type = 'task_started'") == 1


# ------------------------------------------------------------------------------------------------ the finish boundary

def test_a_provider_failure_finishes_the_task_and_its_execution_together_with_the_same_code(world):
    plan_id = make_plan(world, CHAIN)

    async def boom(name, text, scope):
        raise RuntimeError("provider exploded")

    world["orch"].behaviour = boom
    run = go(world, plan_id)
    assert statuses(run) == {"a": "failed", "b": "skipped", "c": "skipped"}
    executions, tasks = pair(world)
    assert executions == [("failed", "internal_error")] and tasks[0] == ("a", "failed", "internal_error")


def test_a_failure_at_the_success_commit_rolls_both_records_back_and_they_end_failed_together(world, monkeypatch):
    plan_id = make_plan(world, CHAIN)
    fired = inject_once(monkeypatch, "task_succeeded")  # after the execution row was updated, before the commit
    run = go(world, plan_id)
    assert fired == ["task_succeeded"]
    executions, tasks = pair(world)
    # never "execution succeeded, task not": the rollback took both back, the honest fallback then ended both as failed
    assert executions == [("failed", "internal_error")] and tasks == [("a", "failed", "internal_error"), ("b", "skipped", "dependency_failed"), ("c", "skipped", "dependency_failed")]
    assert world["db"].scalar("SELECT result FROM agent_executions") is None  # the unadopted answer was not stored
    assert (run.status, len(world["orch"].calls)) == ("failed", 1)  # and nothing downstream used it


def test_memory_never_holds_a_reply_that_its_execution_record_does_not(world, monkeypatch):
    world["agents"].create(world["sa"], "writer", definition())
    plan_id = make_plan(world, [task("a", "writer")])
    inject_once(monkeypatch, "task_succeeded")
    run = go(world, plan_id)
    assert statuses(run) == {"a": "failed"}
    assert world["executions"]._short_term.get(world["sa"], "writer") == []  # the reply was never "remembered": the commit did not happen
    clean = go(world, plan_id)  # a new attempt (explicit) works and remembers the committed reply
    assert statuses(clean) == {"a": "succeeded"} and len(world["executions"]._short_term.get(world["sa"], "writer")) == 2


def test_a_stale_owner_cannot_finish_a_task_and_both_records_stay_running_for_recovery(world):
    plan_id = make_plan(world, [task("a", "research"), task("b", "sales", ["a"])])
    old = PlanRunStore(world["sf"], owner_id="owner-old", config=FAST)
    run_id = old.create(world["sa"], plan_id)
    _, tasks = old.load_for_driver(run_id)
    fence_old = Fence(run_id, "owner-old", 1)
    eid = old.start_task(tasks[0].id, world["sa"], BUILTIN, "Research", "text", "groq", None, executions=world["executions"].store, fence=fence_old)
    expire(world)
    new = PlanRunStore(world["sf"], owner_id="owner-new", config=FAST)
    assert new.leases.takeover(run_id, "owner-new", 30) is not None
    before = pair(world)
    with pytest.raises(LeaseLost):
        old.finish_task_execution(tasks[0].id, eid, "succeeded", "succeeded", None, fence=fence_old, result="a late answer")
    assert pair(world) == before and world["db"].scalar("SELECT result FROM agent_executions") is None  # the stale answer is not stored anywhere
    expire(world)  # the new owner dies too
    assert PlanRunStore(world["sf"], owner_id="owner-recovery", config=FAST).recover_run(run_id) == "failed"  # recovery closes both together
    assert pair(world) == ([("failed", "interrupted")], [("a", "failed", "interrupted"), ("b", "skipped", "interrupted")])


def test_an_execution_that_was_closed_elsewhere_is_never_adopted_as_a_success(world):
    plan_id = make_plan(world, [task("a", "research"), task("b", "sales", ["a"])])
    seen = []

    async def behaviour(name, text, scope):
        # while the agent "works", something else closes its execution record (a race the fence alone does not cover)
        eid = world["db"].scalar("SELECT id FROM agent_executions")
        ExecutionStore(world["sf"]).finish(__import__("uuid").UUID(eid), "failed", error_code="interrupted")
        seen.append(name)
        return "an answer that must not be adopted"

    world["orch"].behaviour = behaviour
    run = go(world, plan_id)
    assert seen == ["research"] and statuses(run) == {"a": "failed", "b": "skipped"}
    assert world["db"].rows("SELECT status FROM agent_executions") == [("failed",)] and world["orch"].calls[0][0] == "research" and len(world["orch"].calls) == 1


def test_a_database_outage_at_the_start_leaves_no_half_started_task(world):
    from tests.test_run_recovery import Flaky, flaky_service

    plan_id = make_plan(world, [task("a", "research")])
    flaky, reg = Flaky(world["sf"]), RunRegistry(config=FAST)
    svc = flaky_service(world, reg, flaky)
    errors = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: errors.append(context))
        run = await svc.start(world["sa"], plan_id)
        flaky.down = True  # the database disappears before the scheduler dispatches (or while it does)
        await reg.wait(run.id, 15)
        flaky.down = False
        return run.id

    run_id = asyncio.run(scenario())
    assert errors == []
    executions, tasks = pair(world)
    assert executions == [] or tasks[0][1] != "pending"  # either nothing was dispatched, or both records exist together
    time.sleep(FAST.ttl_seconds + 0.3)
    PlanRunStore(world["sf"], config=FAST).recover_expired()
    run = world["svc"].store.get(world["sa"], run_id)
    assert run.status in ("failed", "partial", "succeeded")  # observable, terminal
    executions, tasks = pair(world)
    assert all(status != "running" for status, _code in executions) and all(t[1] not in ("running", "pending") for t in tasks)
