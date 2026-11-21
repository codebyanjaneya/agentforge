"""M4-A1: run ownership, fencing, lease recovery, persisted cancellation and graceful shutdown.

Service layer on a migrated disposable SQLite database, the real M2-B ExecutionService, a fake provider and a fake built-in
orchestrator (no network). "Another process" is a second ``RunRegistry`` / ``PlanRunStore`` (a different owner id) on the same
database; ``test_a_real_process_killed_mid_run_is_recovered_after_its_lease_expires`` uses a REAL second OS process that is
killed. Single-process tests alone prove nothing about several processes, so the PostgreSQL integration tests repeat the
competing-owner cases with real concurrent connections."""

import asyncio
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime

import pytest
from sqlalchemy.exc import OperationalError

from agentplatform.execution import ExecutionStore
from agentplatform.plan_runs import PlanRunService, PlanRunStore, RunInProgress, RunNotFound, RunRegistry, ShuttingDown
from agentplatform.run_leases import Fence, LeaseConfig, LeaseLost, Renewal
from agentplatform.service import ResolvedAgent
from authz.workspaces import authorize_memory_scope
from tests.test_authz_memory import team_workspace
from migration_support import BACKEND
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_plan_runs import CHAIN, go, make_plan, statuses, task, world  # noqa: F401  (world is a fixture)

FAST = LeaseConfig(ttl_seconds=0.9, heartbeat_seconds=0.3, sweep_seconds=0.3, shutdown_grace_seconds=1.0)
SLOW = LeaseConfig(ttl_seconds=60.0, heartbeat_seconds=30.0, sweep_seconds=1.0, shutdown_grace_seconds=1.0)  # no heartbeat inside a test
LONG_AGO = "2000-01-01 00:00:00.000000"
BUILTIN = ResolvedAgent("builtin", "research", None, None, None)


def expire(world, run_id=None):
    where = f" WHERE run_id = '{run_id.hex}'" if run_id else ""
    world["db"].run(f"UPDATE run_leases SET expires_at = '{LONG_AGO}'{where}")


async def until(world, sql, expected, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if await asyncio.to_thread(world["db"].scalar, sql) == expected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {sql} == {expected!r}")


RUNNING_TASKS = "SELECT count(*) FROM plan_task_runs WHERE status = 'running'"


def table_state(world):
    return {t: world["db"].rows(f"SELECT * FROM {t} ORDER BY 1") for t in ("plan_runs", "plan_task_runs", "agent_executions", "run_leases")}


def stuck_agent(world, cancelled):
    """A built-in agent that never finishes by itself; it records that its call was cancelled."""

    async def behaviour(name, text, scope):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(name)
            raise

    world["orch"].behaviour = behaviour


# ------------------------------------------------------------------------------------------------ ownership

def test_two_instances_competing_for_one_plan_leave_exactly_one_owner(world):
    plan_id = make_plan(world, CHAIN)
    reg_a, reg_b = RunRegistry(config=FAST), RunRegistry(config=FAST)
    svc_a, svc_b = world["service"](registry=reg_a), world["service"](registry=reg_b)

    async def scenario():
        gate = asyncio.Event()

        async def behaviour(name, text, scope):
            await gate.wait()
            return f"result of {name}"

        world["orch"].behaviour = behaviour
        results = await asyncio.gather(svc_a.start(world["sa"], plan_id), svc_b.start(world["sa"], plan_id), return_exceptions=True)
        started = [r for r in results if not isinstance(r, BaseException)]
        refused = [r for r in results if isinstance(r, BaseException)]
        assert len(started) == 1 and len(refused) == 1 and isinstance(refused[0], RunInProgress)
        run_id = started[0].id
        winner, loser = (reg_a, svc_b) if reg_a.get(run_id) else (reg_b, svc_a)
        snap = svc_a.store.leases.snapshot(run_id)
        assert (snap["owner_id"], snap["generation"]) == (winner.owner_id, 1) and world["db"].scalar("SELECT count(*) FROM run_leases") == 1
        assert loser.registry.active() == 0 and loser.store.recover_expired() == []  # the loser neither drives nor takes over a live lease
        gate.set()
        await winner.wait(run_id, 20)
        return run_id

    run_id = asyncio.run(scenario())
    assert world["svc"].store.get(world["sa"], run_id).status == "succeeded"
    assert world["db"].scalar("SELECT count(*) FROM plan_runs") == 1 and len(world["orch"].calls) == 3  # each task dispatched once


def test_a_second_process_starting_up_cannot_interrupt_a_live_owners_run(world):
    plan_id = make_plan(world, CHAIN)
    reg_a = RunRegistry(config=FAST)
    svc_a = world["service"](registry=reg_a)

    async def scenario():
        gate = asyncio.Event()

        async def behaviour(name, text, scope):
            await gate.wait()
            return f"result of {name}"

        world["orch"].behaviour = behaviour
        run = await svc_a.start(world["sa"], plan_id)
        await until(world, RUNNING_TASKS, 1)
        standalone = world["executions"].store.start(world["sa"], BUILTIN, "Research", "a REST execution", provider="groq", model=None)
        before = table_state(world)
        # "process B" starts: the standalone-execution sweep (unchanged semantics) and the lease recovery
        assert ExecutionStore(world["sf"]).fail_orphaned() == 1  # ONLY the standalone execution; the run's execution is not touched
        assert PlanRunStore(world["sf"], config=FAST).recover_expired() == []
        after = table_state(world)
        assert [r for r in after["plan_runs"] if r[0] == run.id.hex] == [r for r in before["plan_runs"] if r[0] == run.id.hex]
        assert after["plan_task_runs"] == before["plan_task_runs"] and after["run_leases"] == before["run_leases"]
        assert world["db"].scalar(f"SELECT status FROM agent_executions WHERE id = '{standalone.hex}'") == "failed"
        assert world["db"].scalar("SELECT count(*) FROM agent_executions WHERE status = 'running'") == 1  # the live task's execution
        gate.set()
        await reg_a.wait(run.id, 20)
        return run.id

    run_id = asyncio.run(scenario())
    assert world["svc"].store.get(world["sa"], run_id).status == "succeeded"


def test_the_heartbeat_extends_a_valid_lease_beyond_its_first_term_and_the_end_releases_it(world):
    plan_id = make_plan(world, [task("a", "research")])
    reg = RunRegistry(config=FAST)
    svc = world["service"](registry=reg)

    async def scenario():
        async def behaviour(name, text, scope):
            await asyncio.sleep(2.0)
            return "done"

        world["orch"].behaviour = behaviour
        run = await svc.start(world["sa"], plan_id)
        first = svc.store.leases.snapshot(run.id)["expires_at"]
        await asyncio.sleep(1.4)  # longer than the first term (0.9 s)
        mid = svc.store.leases.snapshot(run.id)
        assert mid["expires_at"] > first and mid["owner_id"] == reg.owner_id
        assert PlanRunStore(world["sf"], config=FAST).recover_expired() == []  # alive only because the heartbeat renewed it
        await reg.wait(run.id, 20)
        return run.id

    run_id = asyncio.run(scenario())
    assert world["svc"].store.get(world["sa"], run_id).status == "succeeded"
    assert world["svc"].store.leases.snapshot(run_id)["expires_at"] <= datetime.utcnow()  # released once the driver could no longer write


def test_an_expired_lease_is_recovered_exactly_once_even_when_sweepers_race_and_recovery_is_idempotent(world):
    plan_id = make_plan(world, CHAIN)
    run_id = world["svc"].store.create(world["sa"], plan_id)
    expire(world)
    sweepers = [PlanRunStore(world["sf"], config=FAST) for _ in range(4)]
    barrier, outcomes, errors = threading.Barrier(4), [], []

    def sweep(store):
        try:
            barrier.wait()
            outcomes.append(store.recover_run(run_id))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=sweep, args=(s,)) for s in sweepers]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == [] and sorted(o for o in outcomes if o is not None) == ["failed"] and outcomes.count(None) == 3  # exactly one winner
    run = world["svc"].store.get(world["sa"], run_id)
    assert (run.status, run.error_code, statuses(run)) == ("failed", "interrupted", {"a": "skipped", "b": "skipped", "c": "skipped"})
    state = table_state(world)
    for store in sweepers:
        assert store.recover_expired() == []  # repeated recovery changes nothing
    assert table_state(world) == state and world["svc"].store.leases.snapshot(run_id)["generation"] == 2
    assert len(world["orch"].calls) == 0  # recovery never dispatches anything


def test_a_stale_owner_cannot_change_the_run_after_a_takeover(world):
    plan_id = make_plan(world, CHAIN)
    old = PlanRunStore(world["sf"], owner_id="owner-old", config=FAST)
    run_id = old.create(world["sa"], plan_id)
    fence_old = Fence(run_id, "owner-old", 1)
    _, tasks = old.load_for_driver(run_id)
    by = {t.key: t for t in tasks}
    executions = ExecutionStore(world["sf"])
    first = executions.start(world["sa"], BUILTIN, "Research", "t", provider="groq", model=None)
    old.mark_running(by["a"].id, first, fence=fence_old)  # the owner can write
    expire(world)
    new = PlanRunStore(world["sf"], owner_id="owner-new", config=FAST)
    fence_new = new.leases.takeover(run_id, "owner-new", 30)
    assert fence_new == Fence(run_id, "owner-new", 2)
    second = executions.start(world["sa"], BUILTIN, "Research", "t", provider="groq", model=None)
    before = table_state(world)
    stale_writes = (
        lambda: old.mark_running(by["b"].id, second, fence=fence_old),
        lambda: old.finish_task(by["a"].id, "succeeded", fence=fence_old),  # cannot complete a task
        lambda: old.finish_task(by["b"].id, "skipped", "dependency_failed", fence=fence_old),
        lambda: old.finish_run(run_id, "succeeded", fence=fence_old),  # cannot modify the run
        lambda: old.close_unfinished(run_id, "cancelled", "cancelled", "cancelled", fence=fence_old),  # cannot overwrite state
    )
    for write in stale_writes:
        with pytest.raises(LeaseLost):
            write()
    assert table_state(world) == before  # nothing changed, not even the lease row
    assert old.leases.renew(fence_old, 30) == Renewal(False) and old.leases.release(fence_old) is False
    assert new.leases.snapshot(run_id)["owner_id"] == "owner-new"  # a stale release did not touch the new owner's lease
    new.finish_task(by["a"].id, "succeeded", fence=fence_new)  # the new owner can
    assert new.close_unfinished(run_id, "failed", "interrupted", "interrupted", fence=fence_new) == "partial"


def test_a_stale_drivers_late_result_is_not_adopted_and_nothing_is_dispatched_again(world):
    plan_id = make_plan(world, CHAIN)
    reg_a = RunRegistry(config=SLOW)
    svc_a = world["service"](registry=reg_a)
    errors = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: errors.append(context))
        gate = asyncio.Event()

        async def behaviour(name, text, scope):
            await gate.wait()
            return f"result of {name}"

        world["orch"].behaviour = behaviour
        run = await svc_a.start(world["sa"], plan_id)
        await until(world, RUNNING_TASKS, 1)
        expire(world)  # the owner "hung" for a whole term ...
        recovered = await asyncio.to_thread(PlanRunStore(world["sf"], config=FAST).recover_expired)
        assert recovered == [run.id]  # ... and another process took the run over and closed it
        gate.set()  # the stale driver's agent now answers
        await reg_a.wait(run.id, 20)
        return run.id

    run_id = asyncio.run(scenario())
    run = world["svc"].store.get(world["sa"], run_id)
    # the unknown outcome of the in-flight task is reported as interrupted; the late answer is NOT propagated and nothing re-ran
    assert (run.status, run.error_code, statuses(run)) == ("failed", "interrupted", {"a": "failed", "b": "skipped", "c": "skipped"})
    assert [t.error_code for t in run.tasks] == ["interrupted"] * 3
    assert [name for name, _ in world["orch"].calls] == ["research"] and world["db"].scalar("SELECT count(*) FROM agent_executions") == 1
    assert world["svc"].registry.active() == 0 and reg_a.active() == 0 and errors == []


def test_a_live_lease_cannot_be_taken_and_an_expired_one_cannot_be_renewed(world):
    plan_id = make_plan(world, CHAIN)
    owner = PlanRunStore(world["sf"], owner_id="owner-live", config=FAST)
    run_id = owner.create(world["sa"], plan_id)
    fence = Fence(run_id, "owner-live", 1)
    thief = PlanRunStore(world["sf"], owner_id="owner-thief", config=FAST)
    assert thief.leases.takeover(run_id, "owner-thief", 30) is None and thief.recover_run(run_id) is None  # a live lease cannot be taken
    assert owner.leases.renew(fence, 30) == Renewal(True) and thief.leases.takeover(run_id, "owner-thief", 30) is None  # renewing keeps it live
    expire(world)
    assert owner.leases.renew(fence, 30) == Renewal(False)  # an expired lease is never resurrected: the owner has lost it
    assert owner.leases.snapshot(run_id)["generation"] == 1 and owner.get(world["sa"], run_id).status == "running"  # (nothing changed by the failed renewal)
    taken = thief.leases.takeover(run_id, "owner-thief", 30)
    assert taken == Fence(run_id, "owner-thief", 2)
    assert thief.close_unfinished(run_id, "failed", "interrupted", "interrupted", fence=taken) == "failed"
    expire(world)  # even with an expired lease, a FINISHED run has no owner to replace
    assert thief.leases.takeover(run_id, "owner-other", 30) is None and thief.recover_run(run_id) is None and thief.recover_expired() == []


def test_when_the_supervisor_is_cancelled_but_the_driver_ignores_the_stop_the_lease_is_kept_until_the_driver_ends(world):
    """The event loop is going down (the supervising task is cancelled) while an agent call ignores the stop request: the driver can
    still write, so the lease must stay owned and renewed; only after the driver ended may it expire and be recovered."""
    plan_id = make_plan(world, CHAIN)
    reg = RunRegistry(config=FAST)
    svc = world["service"](registry=reg)

    async def scenario():
        release = asyncio.Event()

        async def stubborn(name, text, scope):
            while not release.is_set():
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    pass
            return "late"

        world["orch"].behaviour = stubborn
        run = await svc.start(world["sa"], plan_id)
        await until(world, RUNNING_TASKS, 1)
        context = reg._contexts[run.id]
        supervisor = reg.get(run.id)
        supervisor.cancel()  # the supervising task is cancelled (loop teardown) ...
        await asyncio.wait({supervisor}, timeout=FAST.shutdown_grace_seconds + 2)
        assert supervisor.done() and not context.driver.done()  # ... but the driver is still alive
        await asyncio.sleep(FAST.ttl_seconds * 1.5)
        snap = svc.store.leases.snapshot(run.id)
        assert snap["owner_id"] == reg.owner_id and snap["expires_at"] > datetime.utcnow()  # NOT released, still renewed
        assert PlanRunStore(world["sf"], config=FAST).recover_expired() == []  # nobody may take over a driver that can still write
        release.set()
        await asyncio.wait({context.driver}, timeout=10)
        await asyncio.sleep(FAST.ttl_seconds + 0.4)  # the heartbeat ended with the driver: the lease expires
        assert PlanRunStore(world["sf"], config=FAST).recover_expired() == [run.id]
        return run.id

    run_id = asyncio.run(scenario())
    run = world["svc"].store.get(world["sa"], run_id)
    assert (run.status, run.error_code) == ("failed", "interrupted") and len(world["orch"].calls) == 1


# ------------------------------------------------------------------------------------------------ recovery semantics

def test_a_task_whose_execution_already_succeeded_is_reconciled_and_never_run_again(world):
    plan_id = make_plan(world, CHAIN)
    store, executions = world["svc"].store, ExecutionStore(world["sf"])
    run_id = store.create(world["sa"], plan_id)
    _, tasks = store.load_for_driver(run_id)
    by = {t.key: t for t in tasks}
    done = executions.start(world["sa"], BUILTIN, "Research", "t", provider="groq", model=None)
    store.mark_running(by["a"].id, done)
    executions.finish(done, "succeeded", result="the persisted result")  # the process died BEFORE the task run was updated
    expire(world)
    assert store.recover_expired() == [run_id]
    run = store.get(world["sa"], run_id)
    assert (run.status, run.error_code, statuses(run)) == ("partial", "interrupted", {"a": "succeeded", "b": "skipped", "c": "skipped"})
    assert run.tasks[0].error_code is None and run.tasks[0].execution_id == done
    assert store.task_result(world["sa"], done)["result"] == "the persisted result"  # the result is kept and traceable
    assert world["orch"].calls == [] and world["db"].scalar("SELECT count(*) FROM agent_executions") == 1  # never dispatched again


def test_a_task_whose_execution_already_failed_keeps_that_failure_code(world):
    plan_id = make_plan(world, [task("a", "research"), task("b", "sales", ["a"])])
    store, executions = world["svc"].store, ExecutionStore(world["sf"])
    run_id = store.create(world["sa"], plan_id)
    _, tasks = store.load_for_driver(run_id)
    done = executions.start(world["sa"], BUILTIN, "Research", "t", provider="groq", model=None)
    store.mark_running(tasks[0].id, done)
    executions.finish(done, "failed", error_code="timeout")
    expire(world)
    assert store.recover_expired() == [run_id]
    run = store.get(world["sa"], run_id)
    assert (run.status, statuses(run), run.tasks[0].error_code) == ("failed", {"a": "failed", "b": "skipped"}, "timeout")


def test_an_in_flight_task_with_an_unknown_outcome_ends_interrupted_and_its_execution_is_closed_with_it(world):
    plan_id = make_plan(world, CHAIN)
    store, executions = world["svc"].store, ExecutionStore(world["sf"])
    run_id = store.create(world["sa"], plan_id)
    _, tasks = store.load_for_driver(run_id)
    live = executions.start(world["sa"], BUILTIN, "Research", "t", provider="groq", model=None)
    store.mark_running(tasks[0].id, live)  # the provider call was in flight when the process died
    expire(world)
    assert store.recover_expired() == [run_id]
    run = store.get(world["sa"], run_id)
    assert (run.status, run.error_code, run.error_message) == ("failed", "interrupted", "The server restarted before this task finished.")
    assert world["db"].rows(f"SELECT status, error_code FROM agent_executions WHERE id = '{live.hex}'") == [("failed", "interrupted")]
    assert world["orch"].calls == [] and world["svc"].registry.active() == 0  # no automatic redispatch, no driver
    assert asyncio.run(world["svc"].start(world["sa"], plan_id)).attempt == 2  # only an explicit new attempt re-runs anything


def test_a_cancel_request_on_a_run_nobody_owns_any_more_closes_it_as_cancelled(world):
    plan_id = make_plan(world, CHAIN)
    run_id = world["svc"].store.create(world["sa"], plan_id)  # created, but its process is gone
    expire(world)
    view = asyncio.run(world["svc"].cancel(world["sa"], run_id))
    assert (view.status, view.error_code, statuses(view)) == ("cancelled", "cancelled", {"a": "cancelled", "b": "cancelled", "c": "cancelled"})


# ------------------------------------------------------------------------------------------------ cancellation

def test_cancellation_requested_by_another_process_reaches_the_owner_and_nothing_else_starts(world):
    plan_id = make_plan(world, CHAIN)
    reg_a, reg_b = RunRegistry(config=FAST), RunRegistry(config=FAST)
    svc_a, svc_b = world["service"](registry=reg_a), world["service"](registry=reg_b)
    cancelled = []

    async def scenario():
        stuck_agent(world, cancelled)
        run = await svc_a.start(world["sa"], plan_id)
        await until(world, RUNNING_TASKS, 1)
        started = time.monotonic()
        closes = []
        original = svc_b.store.close_unfinished
        svc_b.store.close_unfinished = lambda *a, **k: (closes.append(a), original(*a, **k))[1]  # a spy: B must never write run state
        view = await svc_b.cancel(world["sa"], run.id)  # process B does not drive this run
        elapsed = time.monotonic() - started
        assert closes == []  # a non-owner only REQUESTS the cancellation; the owner (fenced) records it
        assert svc_a.store.leases.snapshot(run.id)["cancel_requested_at"] is not None  # the request was persisted
        await reg_a.wait(run.id, 20)
        return run.id, view, elapsed

    run_id, view, elapsed = asyncio.run(scenario())
    assert (view.status, view.error_code) == ("cancelled", "cancelled") and elapsed < FAST.heartbeat_seconds * 1.5 + 1
    final = world["svc"].store.get(world["sa"], run_id)
    assert statuses(final) == {"a": "cancelled", "b": "cancelled", "c": "cancelled"}
    assert cancelled == ["research"] and [name for name, _ in world["orch"].calls] == ["research"]  # the provider call was cancelled; b and c never started
    assert world["db"].rows("SELECT status, error_code FROM agent_executions") == [("cancelled", "cancelled")]
    assert reg_a.active() == 0 and world["svc"].store.leases.snapshot(run_id)["expires_at"] <= datetime.utcnow()


def test_a_cancel_in_the_owning_process_is_immediate_and_persisted(world):
    plan_id = make_plan(world, CHAIN)
    cancelled = []

    async def scenario():
        stuck_agent(world, cancelled)
        run = await world["svc"].start(world["sa"], plan_id)
        await until(world, RUNNING_TASKS, 1)
        view = await world["svc"].cancel(world["sa"], run.id)
        again = await world["svc"].cancel(world["sa"], run.id)  # idempotent
        return run.id, view, again

    run_id, view, again = asyncio.run(scenario())
    assert view.status == "cancelled" and again.status == "cancelled" and cancelled == ["research"]
    assert world["svc"].store.leases.snapshot(run_id)["cancel_requested_at"] is not None


def test_a_foreign_scope_cannot_cancel_a_run_or_leave_a_request_behind(world):
    plan_id = make_plan(world, CHAIN)
    team = team_workspace(world["db"], world["a"], [(world["b"], "member")])
    member = authorize_memory_scope(world["sf"], world["b"], team)

    async def scenario():
        stuck_agent(world, [])
        run = await world["svc"].start(world["sa"], plan_id)
        await until(world, RUNNING_TASKS, 1)
        for scope in (world["sb"], member):  # another user, and the same user in another workspace
            with pytest.raises(RunNotFound):
                await world["svc"].cancel(scope, run.id)
        assert world["svc"].store.leases.snapshot(run.id)["cancel_requested_at"] is None and world["svc"].store.get(world["sa"], run.id).status == "running"
        await world["svc"].cancel(world["sa"], run.id)

    asyncio.run(scenario())


# ------------------------------------------------------------------------------------------------ graceful shutdown

def test_graceful_shutdown_stops_dispatch_records_interrupted_and_only_then_releases_the_lease(world):
    plan_id = make_plan(world, CHAIN)
    reg = RunRegistry(config=FAST)
    svc = world["service"](registry=reg)
    cancelled = []

    async def scenario():
        stuck_agent(world, cancelled)
        run = await svc.start(world["sa"], plan_id)
        await until(world, RUNNING_TASKS, 1)
        assert await reg.shutdown() == []  # everything stopped in time
        with pytest.raises(ShuttingDown):
            await svc.start(world["sa"], plan_id)  # a stopping process accepts no new run
        return run.id

    run_id = asyncio.run(scenario())
    run = world["svc"].store.get(world["sa"], run_id)
    assert (run.status, run.error_code, statuses(run)) == ("failed", "interrupted", {"a": "failed", "b": "skipped", "c": "skipped"})
    assert [t.error_code for t in run.tasks] == ["interrupted"] * 3 and cancelled == ["research"] and len(world["orch"].calls) == 1
    assert world["db"].rows("SELECT status FROM agent_executions") == [("cancelled",)]  # the provider call itself was cancelled (recorded by the M2-B runtime)
    assert world["svc"].store.leases.snapshot(run_id)["expires_at"] <= datetime.utcnow()  # released after the driver stopped


def test_shutdown_does_not_release_the_lease_while_a_driver_can_still_write(world):
    plan_id = make_plan(world, CHAIN)
    reg = RunRegistry(config=FAST)
    svc = world["service"](registry=reg)

    async def scenario():
        release, ignored = asyncio.Event(), []

        async def stubborn(name, text, scope):  # an agent call that ignores the stop request
            while not release.is_set():
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    ignored.append(name)
            return f"late result of {name}"

        world["orch"].behaviour = stubborn
        run = await svc.start(world["sa"], plan_id)
        await until(world, RUNNING_TASKS, 1)
        assert await reg.shutdown(grace=0.4) == [run.id]  # the driver did NOT stop within the grace period
        await asyncio.sleep(FAST.ttl_seconds * 1.5)  # longer than a whole lease term
        snap = svc.store.leases.snapshot(run.id)
        assert snap["owner_id"] == reg.owner_id and snap["expires_at"] > datetime.utcnow()  # still owned and still renewed
        assert PlanRunStore(world["sf"], config=FAST).recover_expired() == []  # nobody can take over a driver that can still write
        release.set()
        await reg.wait(run.id, 20)
        return run.id, ignored

    run_id, ignored = asyncio.run(scenario())
    run = world["svc"].store.get(world["sa"], run_id)
    # the stop reached the task late; the late answer of a cancelled call is dropped (Python's wait_for re-raises the cancellation)
    assert ignored and (run.status, run.error_code, statuses(run)) == ("failed", "interrupted", {"a": "failed", "b": "skipped", "c": "skipped"})
    assert len(world["orch"].calls) == 1 and world["svc"].store.leases.snapshot(run_id)["expires_at"] <= datetime.utcnow()  # released only after it ended


# ------------------------------------------------------------------------------------------------ database outage

class Flaky:
    """A session factory whose database can be switched off (the run store of one process loses its database)."""

    def __init__(self, sf):
        self.sf, self.down = sf, False

    def __call__(self):
        if self.down:
            raise OperationalError("SELECT 1", {}, Exception("the database is down"))
        return self.sf()


def flaky_service(world, registry, flaky):
    return PlanRunService(flaky, executions=world["executions"], registry=registry, busy_poll=0.01, busy_budget=0.6)


def test_a_driver_that_loses_its_database_stops_without_unhandled_errors_and_the_run_is_recovered_later(world, caplog):
    plan_id = make_plan(world, CHAIN)
    flaky, reg = Flaky(world["sf"]), RunRegistry(config=FAST)
    svc = flaky_service(world, reg, flaky)
    errors, cancelled = [], []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: errors.append(context))
        stuck_agent(world, cancelled)
        run = await svc.start(world["sa"], plan_id)
        await until(world, RUNNING_TASKS, 1)
        flaky.down = True
        await reg.wait(run.id, 15)  # the heartbeat cannot renew for a whole term: the driver fences itself and stops
        assert reg.active() == 0 and cancelled == ["research"]
        flaky.down = False
        return run.id

    run_id = asyncio.run(scenario())
    assert errors == [] and "Traceback" not in caplog.text
    assert world["svc"].store.get(world["sa"], run_id).status == "running"  # nothing could be written while the database was down ...
    assert world["svc"].store.leases.snapshot(run_id)["expires_at"] <= datetime.utcnow()  # ... and the lease is expired
    assert PlanRunStore(world["sf"], config=FAST).recover_expired() == [run_id]  # observable, recoverable outcome
    run = world["svc"].store.get(world["sa"], run_id)
    assert (run.status, run.error_code, statuses(run)) == ("failed", "interrupted", {"a": "failed", "b": "skipped", "c": "skipped"})
    assert world["db"].scalar("SELECT count(*) FROM agent_executions") == 1 and len(world["orch"].calls) == 1  # never re-dispatched
    world["orch"].behaviour = None
    assert go(world, plan_id).attempt == 2  # the plan is not blocked


def test_a_database_failure_while_a_task_completes_commits_nothing_and_recovery_closes_both_records_together(world, caplog):
    """M4-A2: the task run and its execution record finish in ONE transaction, so a database failure at that moment leaves BOTH ``running``
    (never one succeeded and one not); the unadopted answer is not used; after the lease expired recovery closes both as interrupted."""
    plan_id = make_plan(world, CHAIN)
    flaky, reg = Flaky(world["sf"]), RunRegistry(config=FAST)
    svc = flaky_service(world, reg, flaky)
    errors = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: errors.append(context))
        gate = asyncio.Event()

        async def behaviour(name, text, scope):
            await gate.wait()
            return f"result of {name}"

        world["orch"].behaviour = behaviour
        run = await svc.start(world["sa"], plan_id)
        await until(world, RUNNING_TASKS, 1)
        flaky.down = True
        gate.set()  # the agent answers while the run store has no database
        await reg.wait(run.id, 15)
        flaky.down = False
        return run.id

    run_id = asyncio.run(scenario())
    assert errors == [] and "Traceback" not in caplog.text and "recovery will close it" in caplog.text
    assert world["db"].rows("SELECT status FROM agent_executions") == [("running",)]  # the execution did NOT succeed on its own
    assert world["db"].rows("SELECT status FROM plan_task_runs WHERE task_key = 'a'") == [("running",)]
    assert world["svc"].store.get(world["sa"], run_id).status == "running" and reg.active() == 0
    time.sleep(FAST.ttl_seconds + 0.2)  # the lease expires (the heartbeat stopped with the driver)
    assert PlanRunStore(world["sf"], config=FAST).recover_expired() == [run_id]
    run = world["svc"].store.get(world["sa"], run_id)
    assert (run.status, run.error_code, statuses(run)) == ("failed", "interrupted", {"a": "failed", "b": "skipped", "c": "skipped"})
    assert world["db"].rows("SELECT status, error_code FROM agent_executions") == [("failed", "interrupted")]  # both closed together, consistently
    assert len(world["orch"].calls) == 1  # never dispatched again


# ------------------------------------------------------------------------------------------------ guards

def _lines_using(source: str, name: str):
    """Every physical line that calls or passes ``self.store.<name>`` (a driver write may be handed to ``_record``)."""
    return [line.strip() for line in source.splitlines() if f"self.store.{name}" in line]


def test_every_state_change_of_the_driver_carries_its_fence():
    text = (BACKEND / "agentplatform" / "plan_runs.py").read_text(encoding="utf-8")  # the file, not the module (other tests purge and re-import modules)
    source = text[text.index("class PlanRunService"):]
    for name in ("start_task", "finish_task_execution", "finish_task", "finish_run", "close_unfinished"):
        found = _lines_using(source, name)
        assert found, name
        for line in found:
            assert "fence=fence" in line, f"unfenced driver write: {line}"
    assert "def fail_orphaned" not in text  # the unconditional plan-run sweep is gone (the standalone-execution sweep lives in execution.py)


def test_a_run_never_exists_without_an_owner(world):
    plan_id = make_plan(world, CHAIN)
    run_id = world["svc"].store.create(world["sa"], plan_id)
    assert world["db"].rows("SELECT count(*) FROM run_leases") == [(1,)] and world["svc"].store.leases.snapshot(run_id)["generation"] == 1


# ------------------------------------------------------------------------------------------------ a real process, killed

def _read_lines(stream, lines):
    for line in stream:
        lines.put(line.strip())


def _wait_line(lines, prefix, timeout=60.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        if line.startswith(prefix):
            return line
    raise AssertionError(f"the child process never printed {prefix!r}")


def test_a_real_process_killed_mid_run_is_recovered_after_its_lease_expires(world):
    plan_id = make_plan(world, CHAIN)
    url = world["db"].db.url
    child = subprocess.Popen([sys.executable, str(BACKEND / "tests" / "crash_child.py"), url, str(world["sa"].user_id), str(plan_id), "2.0", "0.5", "child-process-1"],
                             cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = queue.Queue()
    threading.Thread(target=_read_lines, args=(child.stdout, lines), daemon=True).start()
    try:
        run_id = _wait_line(lines, "RUN ").split()[1]
        _wait_line(lines, "CALL research")  # the provider call is in flight in the child
        other = PlanRunStore(world["sf"], config=LeaseConfig(ttl_seconds=2.0, heartbeat_seconds=0.5))
        time.sleep(2.6)  # longer than the lease term: only the child's heartbeat keeps the run owned
        assert other.recover_expired() == [] and other.leases.snapshot(uuid.UUID(run_id))["owner_id"] == "child-process-1"
        assert child.poll() is None and ExecutionStore(world["sf"]).fail_orphaned() == 0  # a live process's run is untouched by a second process
        child.kill()  # a hard crash: no cleanup, no release
        child.wait(timeout=20)
        assert world["svc"].store.get(world["sa"], uuid.UUID(run_id)).status == "running"  # nothing closed it yet
        deadline, closed = time.monotonic() + 15, []
        while not closed and time.monotonic() < deadline:  # recovery waits for the lease to expire
            closed = other.recover_expired()
            time.sleep(0.2)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=20)
    assert [str(r) for r in closed] == [run_id]
    run = world["svc"].store.get(world["sa"], uuid.UUID(run_id))
    assert (run.status, run.error_code, statuses(run)) == ("failed", "interrupted", {"a": "failed", "b": "skipped", "c": "skipped"})
    assert world["db"].rows("SELECT status, error_code FROM agent_executions") == [("failed", "interrupted")]
    assert world["orch"].calls == [] and world["db"].scalar("SELECT count(*) FROM agent_executions") == 1  # nothing was dispatched again
    assert other.recover_expired() == []
