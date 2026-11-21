"""M4-A2 Part 5: the database is the authority for who is alive and how much work may run.

* every process (API or worker) heartbeats one row in ``worker_processes``;
* every execution records its owner process, so a standalone REST/WebSocket execution is recovered when ITS process is gone, not when some
  other process starts (legacy executions without an owner keep the old rule);
* execution admission is checked against the executions of every live process, atomically (an advisory lock on PostgreSQL, the write lock
  on SQLite), so the global and the per-user limits hold across processes;
* the per-user active-run cap is enforced under concurrency by serialising on the user."""

import asyncio
import threading
import uuid
from datetime import datetime, timedelta

import pytest

from agentplatform.execution import Busy, DbLimits, ExecutionStore
from agentplatform.plan_runs import MAX_ACTIVE_RUNS_PER_USER, PlanRunStore, RunLimit
from agentplatform.processes import ProcessRegistry
from agentplatform.run_leases import LeaseConfig
from agentplatform.service import ResolvedAgent
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_plan_runs import CHAIN, make_plan, task, world  # noqa: F401  (world is a fixture)

BUILTIN = ResolvedAgent("builtin", "research", None, None, None)
CONFIG = LeaseConfig(ttl_seconds=30.0, heartbeat_seconds=10.0)


def process(world, owner, kind="api"):
    registry = ProcessRegistry(world["sf"], owner, kind, CONFIG)
    registry.heartbeat()
    return registry


def kill_process_row(world, owner):
    world["db"].run(f"UPDATE worker_processes SET expires_at = '2000-01-01 00:00:00.000000' WHERE owner_id = '{owner}'")


def start(world, store, scope=None, text="work"):
    return store.start(scope or world["sa"], BUILTIN, "Research", text, provider="groq", model=None)


def status(world, execution_id):
    return world["db"].rows(f"SELECT status, error_code FROM agent_executions WHERE id = '{execution_id.hex}'")[0]


# ------------------------------------------------------------------------------------------------ the process registry

def test_the_process_registry_registers_renews_and_releases_one_row_without_infrastructure_details(world):
    registry = process(world, "proc-1", "worker")
    row = world["db"].rows("SELECT owner_id, kind FROM worker_processes")
    assert row == [("proc-1", "worker")]
    assert [c[1] for c in world["db"].rows("PRAGMA table_info(worker_processes)")] == ["owner_id", "kind", "started_at", "heartbeat_at", "expires_at"]  # no host, pid or address
    first = world["db"].scalar("SELECT expires_at FROM worker_processes")
    registry._clock = lambda: datetime.utcnow() + timedelta(seconds=5)
    registry.heartbeat()
    assert world["db"].scalar("SELECT expires_at FROM worker_processes") > first and world["db"].scalar("SELECT count(*) FROM worker_processes") == 1  # renewed, not duplicated
    registry._clock = datetime.utcnow
    registry.release()
    assert world["db"].scalar("SELECT expires_at FROM worker_processes") <= datetime.utcnow().isoformat(sep=" ")  # a graceful stop declares the process gone now
    registry.heartbeat()  # a process declared dead that is in fact alive comes back
    assert world["db"].scalar("SELECT expires_at FROM worker_processes") > datetime.utcnow().isoformat(sep=" ")
    with pytest.raises(ValueError):
        ProcessRegistry(world["sf"], "x", "daemon")


# ------------------------------------------------------------------------------------------------ standalone ownership

def test_a_second_process_starting_up_never_closes_a_live_processes_standalone_execution(world):
    process(world, "proc-a")
    a = ExecutionStore(world["sf"], owner_id="proc-a")
    running = start(world, a)
    assert world["db"].rows("SELECT owner_id FROM execution_owners") == [("proc-a",)]
    b = ExecutionStore(world["sf"], owner_id="proc-b")  # process B starts up and sweeps
    process(world, "proc-b")
    assert b.fail_orphaned() == 0 and status(world, running) == ("running", None)  # A is alive: its execution is untouched
    kill_process_row(world, "proc-a")  # A crashes (stops heartbeating)
    assert b.fail_orphaned() == 1 and status(world, running) == ("failed", "interrupted")  # now, and only now, it is recovered
    assert b.fail_orphaned() == 0  # idempotent


def test_a_gracefully_stopped_process_has_its_executions_recovered_at_once_and_legacy_ones_keep_the_old_rule(world):
    registry = process(world, "proc-a")
    owned = start(world, ExecutionStore(world["sf"], owner_id="proc-a"))
    legacy = start(world, ExecutionStore(world["sf"]))  # created before M4-A2 / without an owner row
    assert ExecutionStore(world["sf"], owner_id="proc-b").fail_orphaned() == 1 and status(world, legacy) == ("failed", "interrupted") and status(world, owned)[0] == "running"
    registry.release()
    assert ExecutionStore(world["sf"], owner_id="proc-b").fail_orphaned() == 1 and status(world, owned) == ("failed", "interrupted")


def test_an_execution_whose_owner_process_row_is_missing_is_not_alive(world):
    owned = start(world, ExecutionStore(world["sf"], owner_id="never-registered"))
    assert ExecutionStore(world["sf"], owner_id="proc-b").fail_orphaned() == 1 and status(world, owned) == ("failed", "interrupted")


def test_the_owner_row_is_written_in_the_same_transaction_as_the_execution(world):
    process(world, "proc-a")
    store = ExecutionStore(world["sf"], owner_id="proc-a", limits=DbLimits(total=0, per_user=0))
    with pytest.raises(Busy):
        start(world, store)  # refused at admission: nothing was written
    assert world["db"].scalar("SELECT count(*) FROM agent_executions") == 0 and world["db"].scalar("SELECT count(*) FROM execution_owners") == 0


# ------------------------------------------------------------------------------------------------ database-wide admission

def test_admission_limits_hold_across_processes_per_user_and_in_total(world):
    process(world, "proc-a")
    process(world, "proc-b")
    limits = DbLimits(total=3, per_user=2)
    a, b = ExecutionStore(world["sf"], owner_id="proc-a", limits=limits), ExecutionStore(world["sf"], owner_id="proc-b", limits=limits)
    first, _ = start(world, a), start(world, b)  # user A: one on each process
    with pytest.raises(Busy):
        start(world, a)  # user A's third execution: the per-user limit counts BOTH processes
    with pytest.raises(Busy):
        start(world, b)
    other = start(world, b, world["sb"])  # another user is unaffected until the global total is reached
    with pytest.raises(Busy):
        start(world, a, world["sb"])  # the total (3) counts both processes
    a.finish(first, "succeeded", result="done")  # a finished execution frees its slot at once
    assert start(world, a) is not None


def test_the_executions_of_a_dead_process_do_not_occupy_slots_but_legacy_ones_do(world):
    process(world, "proc-a")
    process(world, "proc-b")
    limits = DbLimits(total=2, per_user=2)
    a, b = ExecutionStore(world["sf"], owner_id="proc-a", limits=limits), ExecutionStore(world["sf"], owner_id="proc-b", limits=limits)
    start(world, a), start(world, a)
    with pytest.raises(Busy):
        start(world, b)
    kill_process_row(world, "proc-a")  # ghosts of a dead process (not yet swept) must not block live work
    assert start(world, b) is not None
    legacy = ExecutionStore(world["sf"])
    start(world, legacy)  # an un-swept legacy row has no owner: it counts until it is swept
    with pytest.raises(Busy):
        start(world, b)


def test_concurrent_admissions_never_exceed_the_limits(world):
    process(world, "proc-a")
    process(world, "proc-b")
    limits = DbLimits(total=5, per_user=3)
    stores = [ExecutionStore(world["sf"], owner_id=o, limits=limits) for o in ("proc-a", "proc-b") for _ in range(6)]
    barrier, outcomes = threading.Barrier(len(stores)), []

    def admit(store):
        barrier.wait()
        try:
            start(world, store)
            outcomes.append("ok")
        except Busy:
            outcomes.append("busy")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(exc)

    threads = [threading.Thread(target=admit, args=(s,)) for s in stores]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert outcomes.count("ok") == 3 and outcomes.count("busy") == len(stores) - 3  # per-user 3 < total 5, whatever the interleaving
    assert world["db"].scalar("SELECT count(*) FROM agent_executions WHERE status = 'running'") == 3


def test_the_service_refuses_a_standalone_execution_when_the_database_wide_limit_is_reached(world):
    process(world, "proc-x")
    world["executions"].store.owner_id, world["executions"].store._limits = "proc-x", DbLimits(total=1, per_user=1)

    async def scenario():
        gate = asyncio.Event()

        async def behaviour(name, text, scope):
            await gate.wait()
            return "ok"

        world["orch"].behaviour = behaviour
        first = asyncio.ensure_future(world["executions"].run(world["sa"], "research", "first"))
        for _ in range(100):
            if world["db"].scalar("SELECT count(*) FROM agent_executions WHERE status = 'running'") == 1:
                break
            await asyncio.sleep(0.02)
        with pytest.raises(Busy):
            await world["executions"].run(world["sa"], "sales", "second")  # the local Limits would have allowed it (3 per user)
        gate.set()
        return await first

    assert asyncio.run(scenario()).status == "succeeded"
    assert world["db"].scalar("SELECT count(*) FROM agent_executions") == 1  # the refused one wrote nothing


# ------------------------------------------------------------------------------------------------ the per-user active-run cap

def test_the_per_user_active_run_cap_holds_under_concurrency(world):
    plans = [make_plan(world, [task("a", "research")], objective=f"Launch product number {i} to early customers within a month.") for i in range(8)]
    stores = [PlanRunStore(world["sf"], config=LeaseConfig(ttl_seconds=30, heartbeat_seconds=10)) for _ in plans]
    barrier, outcomes = threading.Barrier(len(plans)), []

    def create(store, plan_id):
        barrier.wait()
        try:
            outcomes.append(store.create(world["sa"], plan_id, claim=False))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(exc)

    threads = [threading.Thread(target=create, args=(s, p)) for s, p in zip(stores, plans)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    created = [o for o in outcomes if isinstance(o, uuid.UUID)]
    assert len(created) == MAX_ACTIVE_RUNS_PER_USER and all(isinstance(o, RunLimit) and o.what == "active_runs" for o in outcomes if o not in created)
    assert world["db"].scalar("SELECT count(*) FROM plan_runs WHERE status = 'running'") == MAX_ACTIVE_RUNS_PER_USER
