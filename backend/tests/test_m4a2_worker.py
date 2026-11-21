"""M4-A2 Part 3: the durable worker lifecycle.

In-process tests use two registries as two processes against one SQLite database; the REAL-process tests start actual worker processes
(``tests/worker_child.py``, the real ``Worker`` loop) and the real ``python worker.py`` entrypoint, kill them and stop them gracefully.
Single-process tests alone prove nothing about several processes; PostgreSQL repeats the competing-claim cases in test_postgres_integration."""

import asyncio
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

import pytest

from agentplatform.execution import ExecutionStore
from agentplatform.plan_runs import PlanRunStore, RunRegistry, ShuttingDown, UncertainOutcome
from agentplatform.processes import ProcessRegistry
from agentplatform.run_leases import Fence, LeaseConfig
from agentplatform.service import ResolvedAgent
from agentplatform.worker import Worker
from migration_support import BACKEND
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_m4a2_events import history, types
from tests.test_m4a2_retry import calls, failing, retry
from tests.test_plan_runs import CHAIN, make_plan, statuses, task, world  # noqa: F401  (world is a fixture)
from tests.test_run_recovery import FAST, RUNNING_TASKS, expire, until

BUILTIN = ResolvedAgent("builtin", "research", None, None, None)
WORKER = LeaseConfig(ttl_seconds=0.9, heartbeat_seconds=0.3, sweep_seconds=0.3, shutdown_grace_seconds=1.5)


def api_only(world):
    registry = RunRegistry(config=FAST, embedded=False, kind="api")
    return registry, world["service"](registry=registry)


def worker_service(world, name="worker"):
    registry = RunRegistry(owner_id=name, config=WORKER, kind="worker")
    return registry, world["service"](registry=registry)


# ------------------------------------------------------------------------------------------------ recording work for a worker

def test_an_api_only_process_records_the_run_as_unclaimed_and_drives_nothing(world):
    plan_id = make_plan(world, CHAIN)
    registry, api = api_only(world)
    run = asyncio.run(api.start(world["sa"], plan_id))
    assert (run.status, run.ownership, statuses(run)) == ("running", "unclaimed", {"a": "pending", "b": "pending", "c": "pending"})
    assert registry.active() == 0 and world["orch"].calls == [] and world["db"].scalar("SELECT count(*) FROM agent_executions") == 0
    assert types(world, run.id) == ["run_created"]  # nobody has claimed it yet
    # an embedded process's recovery sweep leaves work recorded for a worker alone
    sweeper = PlanRunStore(world["sf"], config=FAST)
    assert sweeper.recover_expired() == [] and sweeper.recover_run(run.id) is None
    assert world["svc"].store.get(world["sa"], run.id).status == "running"


def test_a_user_can_cancel_a_run_no_worker_has_claimed(world):
    plan_id = make_plan(world, CHAIN)
    _registry, api = api_only(world)
    run = asyncio.run(api.start(world["sa"], plan_id))
    cancelled = asyncio.run(api.cancel(world["sa"], run.id))
    assert (cancelled.status, statuses(cancelled)) == ("cancelled", {"a": "cancelled", "b": "cancelled", "c": "cancelled"}) and world["orch"].calls == []
    assert "cancellation_requested" in types(world, run.id) and "cancellation_acknowledged" in types(world, run.id)


def test_two_workers_racing_for_one_run_claim_it_exactly_once_and_every_task_runs_once(world):
    plan_id = make_plan(world, CHAIN)
    _api_registry, api = api_only(world)
    reg_1, svc_1 = worker_service(world, "worker-1")
    reg_2, svc_2 = worker_service(world, "worker-2")

    async def scenario():
        run = await api.start(world["sa"], plan_id)
        passes = await asyncio.gather(svc_1.claim_work(resume=True), svc_2.claim_work(resume=True))
        assert sum(p["resumed"] for p in passes) == 1  # exactly one claimed it
        winner = reg_1 if reg_1.active() else reg_2
        await winner.wait(run.id, 20)
        return run.id, winner

    run_id, winner = asyncio.run(scenario())
    run = world["svc"].store.get(world["sa"], run_id)
    assert (run.status, calls(world)) == ("succeeded", ["research", "sales", "ceo"])  # no duplicate dispatch
    claimed = [e for e in world["svc"].store.events(world["sa"], run_id)["items"] if e.event_type == "run_claimed"]
    assert len(claimed) == 1 and claimed[0].actor_type == "worker" and claimed[0].detail["generation"] == 2
    assert world["db"].rows("SELECT owner_id FROM run_leases") == [(winner.owner_id,)]  # the lease names the one winner


def test_a_run_whose_owner_died_between_tasks_is_continued_not_restarted(world):
    plan_id = make_plan(world, CHAIN)
    store = world["svc"].store
    run_id = store.create(world["sa"], plan_id)
    fence = Fence(run_id, store.owner_id, 1)
    _, tasks = store.load_for_driver(run_id)
    first = tasks[0]
    eid = store.start_task(first.id, world["sa"], BUILTIN, "Research", "t", "groq", None, executions=world["executions"].store, fence=fence)
    store.finish_task_execution(first.id, eid, "succeeded", "succeeded", None, fence=fence, result="the persisted result of a")
    expire(world)  # the owner died after a was finished and before b was dispatched: nothing is in flight
    reg, svc = worker_service(world)

    async def scenario():
        out = await svc.claim_work(resume=True)
        assert out == {"resumed": 1, "closed": 0}
        await reg.wait(run_id, 20)

    asyncio.run(scenario())
    run = store.get(world["sa"], run_id)
    assert (run.status, statuses(run)) == ("succeeded", {"a": "succeeded", "b": "succeeded", "c": "succeeded"})
    assert calls(world) == ["sales", "ceo"]  # research was NOT dispatched again
    assert "the persisted result of a" in world["orch"].calls[0][1]  # b read a's output from the database
    assert [t for t in types(world, run_id) if t in ("run_claimed", "run_resumed")] == ["run_claimed", "run_resumed"]  # created owned by the API process, then resumed
    assert [e for e in store.events(world["sa"], run_id)["items"] if e.event_type == "run_resumed"][0].detail == {"generation": 2, "resumed": True}


def test_a_run_with_a_task_in_flight_when_its_owner_died_is_closed_never_resumed_and_a_retry_needs_acknowledgement(world):
    plan_id = make_plan(world, CHAIN)
    store = world["svc"].store
    run_id = store.create(world["sa"], plan_id)
    fence = Fence(run_id, store.owner_id, 1)
    _, tasks = store.load_for_driver(run_id)
    store.start_task(tasks[0].id, world["sa"], BUILTIN, "Research", "t", "groq", None, executions=world["executions"].store, fence=fence)  # in flight
    expire(world)
    reg, svc = worker_service(world)
    out = asyncio.run(svc.claim_work(resume=True))
    assert out == {"resumed": 0, "closed": 1} and reg.active() == 0 and world["orch"].calls == []  # nothing dispatched: the outcome of the in-flight call is unknown
    run = store.get(world["sa"], run_id)
    assert (run.status, run.error_code, statuses(run)) == ("failed", "interrupted", {"a": "failed", "b": "skipped", "c": "skipped"})
    assert types(world, run_id)[-2:] == ["recovery_performed", "run_completed"] and ("task_interrupted", "a", "recovery", "interrupted") in history(world, run_id)
    with pytest.raises(UncertainOutcome):
        retry(world, run_id)
    assert retry(world, run_id, acknowledge=["a"]).status == "succeeded"  # the explicit, acknowledged retry runs it


def test_a_run_is_not_executed_for_a_user_who_is_no_longer_authorised(world):
    plan_id = make_plan(world, CHAIN)
    _api_registry, api = api_only(world)
    run = asyncio.run(api.start(world["sa"], plan_id))
    world["db"].run(f"UPDATE users SET is_active = 0 WHERE id = '{world['sa'].user_id.hex}'")  # deactivated after the run was recorded
    reg, svc = worker_service(world)
    out = asyncio.run(svc.claim_work(resume=True))
    assert out == {"resumed": 0, "closed": 1} and world["orch"].calls == [] and reg.active() == 0
    closed = world["db"].rows(f"SELECT status, error_code FROM plan_runs WHERE id = '{run.id.hex}'")
    assert closed == [("failed", "interrupted")]
    detail = [e for e in world["svc"].store.events(world["sa"], run.id)["items"] if e.event_type == "recovery_performed"]
    assert detail and detail[0].detail["reason"] == "authorization_unavailable"


# ------------------------------------------------------------------------------------------------ the worker loop

def test_the_worker_loop_claims_executes_and_shuts_down_gracefully(world):
    plan_id = make_plan(world, CHAIN)
    _api_registry, api = api_only(world)
    registry, svc = worker_service(world, "worker-loop")
    processes = ProcessRegistry(world["sf"], "worker-loop", "worker", WORKER)
    worker = Worker(svc, processes, ExecutionStore(world["sf"]), poll_seconds=0.1)

    async def scenario():
        stop = asyncio.Event()
        task_ = asyncio.ensure_future(worker.run(stop))
        run = await api.start(world["sa"], plan_id)
        await until(world, f"SELECT status FROM plan_runs WHERE id = '{run.id.hex}'", "succeeded", 30)
        assert world["db"].rows("SELECT owner_id, kind FROM worker_processes") == [("worker-loop", "worker")]  # registered while running
        stop.set()
        await asyncio.wait_for(task_, 20)
        return run.id

    run_id = asyncio.run(scenario())
    assert calls(world) == ["research", "sales", "ceo"] and registry.closing is True
    assert world["db"].scalar("SELECT expires_at FROM worker_processes") <= datetime.utcnow().isoformat(sep=" ")  # declared gone only after it stopped


def test_a_worker_shutting_down_mid_task_records_the_interruption_and_releases_its_lease(world):
    plan_id = make_plan(world, CHAIN)
    _api_registry, api = api_only(world)
    registry, svc = worker_service(world, "worker-stop")
    worker = Worker(svc, ProcessRegistry(world["sf"], "worker-stop", "worker", WORKER), ExecutionStore(world["sf"]), poll_seconds=0.1)

    async def scenario():
        async def behaviour(name, text, scope):
            await asyncio.sleep(3600)

        world["orch"].behaviour = behaviour
        stop = asyncio.Event()
        task_ = asyncio.ensure_future(worker.run(stop))
        run = await api.start(world["sa"], plan_id)
        await until(world, RUNNING_TASKS, 1, 30)
        stop.set()
        await asyncio.wait_for(task_, 20)
        return run.id

    run_id = asyncio.run(scenario())
    run = world["svc"].store.get(world["sa"], run_id)
    assert (run.status, run.error_code, statuses(run)) == ("failed", "interrupted", {"a": "failed", "b": "skipped", "c": "skipped"})
    assert world["db"].scalar(f"SELECT expires_at FROM run_leases WHERE run_id = '{run_id.hex}'") <= datetime.utcnow().isoformat(sep=" ")  # released after the driver ended
    assert PlanRunStore(world["sf"], config=FAST).recover_expired() == [] and len(calls(world)) == 1  # nothing left to recover, nothing re-dispatched


def test_an_api_shutting_down_does_not_disturb_a_run_a_worker_is_executing(world):
    plan_id = make_plan(world, CHAIN)
    api_registry, api = api_only(world)
    worker_registry, worker_svc = worker_service(world, "worker-live")

    async def scenario():
        gate = asyncio.Event()

        async def behaviour(name, text, scope):
            await gate.wait()
            return f"result of {name}"

        world["orch"].behaviour = behaviour
        run = await api.start(world["sa"], plan_id)
        assert (await worker_svc.claim_work(resume=True))["resumed"] == 1
        await until(world, RUNNING_TASKS, 1, 30)
        assert await api_registry.shutdown() == []  # the API stops: it owns nothing
        with pytest.raises(ShuttingDown):
            await api.start(world["sa"], plan_id)
        assert world["svc"].store.get(world["sa"], run.id).status == "running"  # the worker's run is untouched
        gate.set()
        await worker_registry.wait(run.id, 20)
        return run.id

    run_id = asyncio.run(scenario())
    assert world["svc"].store.get(world["sa"], run_id).status == "succeeded" and calls(world) == ["research", "sales", "ceo"]


def test_a_worker_shutting_down_does_not_disturb_a_run_the_embedded_api_is_executing(world):
    plan_a = make_plan(world, CHAIN)
    plan_b = make_plan(world, CHAIN, scope=world["sb"])
    api_registry = RunRegistry(config=FAST)  # embedded: it executes its own runs
    api = world["service"](registry=api_registry)
    worker_registry, worker_svc = worker_service(world, "worker-short")
    api_only_registry, recorder = api_only(world)

    async def scenario():
        gate = asyncio.Event()

        async def behaviour(name, text, scope):
            if scope.user_id == world["sb"].user_id:
                await asyncio.sleep(3600)  # the worker's run hangs in flight
            await gate.wait()
            return f"result of {name}"

        world["orch"].behaviour = behaviour
        mine = await api.start(world["sa"], plan_a)  # driven by the embedded API
        theirs = await recorder.start(world["sb"], plan_b)  # recorded for a worker
        assert (await worker_svc.claim_work(resume=True))["resumed"] == 1
        await until(world, "SELECT count(*) FROM plan_task_runs WHERE status = 'running'", 2, 30)
        assert await worker_registry.shutdown() == []  # the worker goes down gracefully
        gate.set()
        await api_registry.wait(mine.id, 20)
        return mine.id, theirs.id

    mine, theirs = asyncio.run(scenario())
    assert world["svc"].store.get(world["sa"], mine).status == "succeeded"  # the API's run finished normally
    other = world["svc"].store.get(world["sb"], theirs)
    assert (other.status, other.error_code) == ("failed", "interrupted")  # the worker's run was interrupted honestly, not re-dispatched


# ------------------------------------------------------------------------------------------------ REAL worker processes

class Child:
    def __init__(self, world, owner, seconds, ttl=2.0, heartbeat=0.5, url=None):
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.proc = subprocess.Popen([sys.executable, str(BACKEND / "tests" / "worker_child.py"), url or world["db"].db.url, owner, str(seconds), str(ttl), str(heartbeat)],
                                     cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=flags)
        self.lines, self._queue = [], queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.append(line.strip())
            self._queue.put(line.strip())

    def wait_line(self, prefix, timeout=60.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                line = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if line.startswith(prefix):
                return line
        raise AssertionError(f"the worker never printed {prefix!r}; output: {self.lines[-10:]}")

    def stop_gracefully(self):
        if os.name == "nt":
            self.proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            self.proc.terminate()

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=20)

    @property
    def calls(self):
        return [line for line in self.lines if line.startswith("CALL ")]


def test_a_real_worker_process_claims_and_executes_a_run_recorded_by_the_api_and_stops_gracefully(world):
    plan_id = make_plan(world, CHAIN, objective="Launch the product to early customers this month. marker:R1")
    _registry, api = api_only(world)
    child = Child(world, "real-worker-1", 0.3)
    try:
        child.wait_line("READY")
        run = asyncio.run(api.start(world["sa"], plan_id))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and world["svc"].store.get(world["sa"], run.id).status == "running":
            time.sleep(0.2)
        final = world["svc"].store.get(world["sa"], run.id)
        assert (final.status, statuses(final)) == ("succeeded", {"a": "succeeded", "b": "succeeded", "c": "succeeded"})
        assert child.calls == ["CALL research R1", "CALL sales R1", "CALL ceo R1"] and world["orch"].calls == []  # the worker ran it, this process never did
        assert {e.actor_type for e in world["svc"].store.events(world["sa"], run.id)["items"] if e.event_type.startswith("task_")} == {"worker"}
        assert world["db"].rows("SELECT owner_id, kind FROM worker_processes WHERE kind = 'worker'") == [("real-worker-1", "worker")]
        child.stop_gracefully()
        child.wait_line("STOPPED", 30)
        child.proc.wait(timeout=30)
        assert child.proc.returncode == 0
        assert world["db"].scalar("SELECT expires_at FROM worker_processes WHERE owner_id = 'real-worker-1'") <= datetime.utcnow().isoformat(sep=" ")
    finally:
        child.kill()


def test_a_real_worker_killed_mid_task_is_recovered_without_re_dispatch_and_retry_needs_acknowledgement(world):
    plan_id = make_plan(world, CHAIN, objective="Launch the product to early customers this month. marker:R2")
    _registry, api = api_only(world)
    child = Child(world, "real-worker-2", 60)
    try:
        child.wait_line("READY")
        run = asyncio.run(api.start(world["sa"], plan_id))
        child.wait_line("CALL research R2")
        time.sleep(0.5)
        child.kill()  # a hard crash while the provider call is in flight
        sweeper = PlanRunStore(world["sf"], config=LeaseConfig(ttl_seconds=2.0, heartbeat_seconds=0.5))
        deadline, closed = time.monotonic() + 30, []
        while not closed and time.monotonic() < deadline:
            closed = sweeper.recover_expired()
            time.sleep(0.2)
    finally:
        child.kill()
    assert closed == [run.id]
    crashed = world["svc"].store.get(world["sa"], run.id)
    assert (crashed.status, crashed.error_code, statuses(crashed)) == ("failed", "interrupted", {"a": "failed", "b": "skipped", "c": "skipped"})
    assert child.calls == ["CALL research R2"] and world["db"].rows("SELECT status, error_code FROM agent_executions") == [("failed", "interrupted")]
    with pytest.raises(UncertainOutcome):
        retry(world, run.id)
    assert retry(world, run.id, acknowledge=["a"]).status == "succeeded" and calls(world) == ["research", "sales", "ceo"]


def test_a_real_worker_asked_to_stop_mid_task_records_interrupted_and_releases_everything(world):
    plan_id = make_plan(world, CHAIN, objective="Launch the product to early customers this month. marker:R3")
    _registry, api = api_only(world)
    child = Child(world, "real-worker-3", 60)
    try:
        child.wait_line("READY")
        run = asyncio.run(api.start(world["sa"], plan_id))
        child.wait_line("CALL research R3")
        time.sleep(0.3)
        child.stop_gracefully()
        child.wait_line("STOPPED", 30)
        child.proc.wait(timeout=30)
    finally:
        child.kill()
    final = world["svc"].store.get(world["sa"], run.id)
    assert (final.status, final.error_code, statuses(final)) == ("failed", "interrupted", {"a": "failed", "b": "skipped", "c": "skipped"})
    assert world["db"].scalar(f"SELECT expires_at FROM run_leases WHERE run_id = '{run.id.hex}'") <= datetime.utcnow().isoformat(sep=" ")
    assert world["db"].scalar("SELECT expires_at FROM worker_processes WHERE owner_id = 'real-worker-3'") <= datetime.utcnow().isoformat(sep=" ")
    assert PlanRunStore(world["sf"], config=FAST).recover_expired() == [] and child.proc.returncode == 0  # a clean stop leaves nothing to recover


def test_two_real_workers_racing_for_several_runs_execute_every_task_exactly_once(world):
    plans = [(world["sa"], make_plan(world, CHAIN, objective=f"Launch the product to early customers this month. marker:A{i}")) for i in range(2)]
    plans += [(world["sb"], make_plan(world, CHAIN, scope=world["sb"], objective=f"Launch the product to early customers this month. marker:B{i}")) for i in range(2)]
    _registry, api = api_only(world)
    runs = [asyncio.run(api.start(scope, plan_id)) for scope, plan_id in plans]  # four runs recorded, none claimed
    children = [Child(world, f"real-worker-race-{i}", 0.3) for i in range(2)]
    try:
        for child in children:
            child.wait_line("READY")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and world["db"].scalar("SELECT count(*) FROM plan_runs WHERE status = 'running'") > 0:
            time.sleep(0.3)
        assert world["db"].rows("SELECT status, count(*) FROM plan_runs GROUP BY status") == [("succeeded", 4)]
        every_call = [line for child in children for line in child.calls]
        assert len(every_call) == 12 and len(set(every_call)) == 12  # 4 runs x 3 tasks, each dispatched once across BOTH processes
        assert world["db"].scalar("SELECT count(*) FROM agent_executions") == 12 and world["db"].scalar("SELECT count(*) FROM plan_task_runs WHERE status = 'succeeded'") == 12
        for run in runs:
            assert world["db"].scalar(f"SELECT count(*) FROM execution_events WHERE run_id = '{run.id.hex}' AND event_type = 'run_claimed'") == 1  # each run claimed exactly once
        for child in children:
            child.stop_gracefully()
        for child in children:
            child.wait_line("STOPPED", 30)
    finally:
        for child in children:
            child.kill()


def test_the_python_worker_entrypoint_starts_registers_and_stops_gracefully(world):
    env = {**os.environ, "APP_ENV": "test", "GROQ_API_KEY": "scripted-not-a-real-key", "DATABASE_URL": world["db"].db.url, "PYTHON_DOTENV_DISABLED": "1", "PYTHONPATH": str(BACKEND)}
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen([sys.executable, str(BACKEND / "worker.py")], cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=flags)
    lines = []
    threading.Thread(target=lambda: [lines.append(line.strip()) for line in proc.stdout], daemon=True).start()
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not any("Worker worker started" in line for line in lines):
            assert proc.poll() is None, lines[-15:]
            time.sleep(0.3)
        assert any("Worker worker started" in line for line in lines), lines[-15:]
        assert world["db"].rows("SELECT kind FROM worker_processes") == [("worker",)]  # registered with its own heartbeat row
        proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
        proc.wait(timeout=30)
        assert proc.returncode == 0 and any("Worker stopped" in line for line in lines), lines[-15:]
        assert world["db"].scalar("SELECT expires_at FROM worker_processes") <= datetime.utcnow().isoformat(sep=" ")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=20)
