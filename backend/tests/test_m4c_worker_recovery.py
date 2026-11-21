"""M4-C finalization: a task suspended on a human approval survives the loss of its worker PROCESS.

REAL worker processes (``tests/worker_child_approvals.py``: the real Worker loop, run service, approval store and invoker, a scripted provider, the SIMULATED tool that
logs every dispatch to a file): one worker is killed (or stopped gracefully) while one task is suspended on an approval and another task is still in flight; a second,
unrelated worker process then recovers the run, a human approves, and the exact suspended call is dispatched once, by the second process. Single-process tests prove
nothing about several processes; PostgreSQL repeats the competing cases in test_postgres_integration."""

import os
import queue
import signal
import subprocess
import sys
import threading
import time

import pytest

from agentplatform.tools.approvals import ApprovalStore
from migration_support import BACKEND
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_m4a2_worker import api_only
from tests.test_plan_runs import definition, make_plan, task, world  # noqa: F401  (world is a fixture)

import asyncio
import importlib


@pytest.fixture
def reg():
    """Registers the SIMULATED fixture tool in the LIVE default registry of this process (other test files reload the app modules, so it is resolved here, at use), so the parent can
    create an agent that pins it; the child processes register it themselves."""
    tools = importlib.import_module("agentplatform.tools")
    fx = importlib.import_module("agentplatform.tools.fixtures")
    registry = tools.default_registry()
    registry.register(fx.DEMO_SEND_NOTE, fx.send_note)
    yield registry
    registry._tools.pop(("demo.send_note", 1), None)


class Child:
    def __init__(self, world, owner, builtin_seconds, log, ttl=2.0, heartbeat=0.5):
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.proc = subprocess.Popen([sys.executable, str(BACKEND / "tests" / "worker_child_approvals.py"), world["db"].db.url, owner, str(builtin_seconds), str(ttl), str(heartbeat), str(log)],
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
        self.proc.wait(timeout=30)

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=20)

    @property
    def calls(self):
        return [line for line in self.lines if line.startswith("CALL ")]


def until(condition, timeout=40.0, what="the condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = condition()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def setup_run(world, log):
    world["agents"].create(world["sa"], "notifier", definition(allowed_tools=["demo.send_note"]))
    plan_id = make_plan(world, [task("a", "notifier"), task("b", "research"), task("c", "ceo", ["a", "b"])])
    _registry, api = api_only(world)
    return plan_id, api


def dispatches(log):
    return [line.split() for line in open(log, encoding="utf-8").read().splitlines()] if os.path.exists(log) else []


def task_states(world):
    return dict(world["db"].rows("SELECT task_key, status FROM plan_task_runs"))


@pytest.mark.parametrize("how", ["kill", "graceful"])
def test_a_suspended_task_survives_the_loss_of_its_worker_and_another_worker_resumes_exactly_that_call_once(world, reg, tmp_path, how):
    log = tmp_path / "dispatch.log"
    plan_id, api = setup_run(world, log)
    store = ApprovalStore(world["sf"])
    first = Child(world, "worker-1", 60, log)  # its built-in task takes a minute: it is still IN FLIGHT when the worker is lost
    second = None
    try:
        first_pid = int(first.wait_line("READY").split()[1])
        run = asyncio.run(api.start(world["sa"], plan_id))
        first.wait_line("CALL research")
        try:
            pending = until(lambda: store.list(world["sa"], status="pending"), timeout=25, what="the approval request")[0]
        except AssertionError:
            raise AssertionError(f"no approval request; worker output: {first.lines[-15:]}; tasks: {world['db'].rows('SELECT task_key, status, error_code FROM plan_task_runs')}") from None
        assert (pending.task_key, pending.tool_id, pending.suspended) == ("a", "demo.send_note", True)
        states = task_states(world)
        assert states["a"] == "running" and states["b"] == "running" and dispatches(log) == []  # suspended + in flight; nothing sensitive ran
        # -- the first worker process is lost
        first.kill() if how == "kill" else first.stop_gracefully()
        # -- a second, unrelated worker starts and recovers the run
        second = Child(world, "worker-2", 0.2, log)
        second_pid = int(second.wait_line("READY").split()[1])
        # a killed worker leaves its in-flight task for the next process to close (and that process then also skips the dependant); a worker that stopped gracefully closes
        # its own in-flight task on the way out and leaves the run parked, so the dependant is skipped only when the run is next driven (after the decision)
        until(lambda: task_states(world).get("b") == "failed" and (how == "graceful" or task_states(world).get("c") == "skipped"), what="closing of the in-flight task")
        assert task_states(world)["a"] == "running", "the suspended task was NOT failed by the loss of its process"
        assert world["db"].scalar("SELECT status FROM tool_approvals") == "pending", "a valid pending approval survives the restart"
        assert world["db"].rows("SELECT task_key, resumed_at IS NULL FROM tool_suspensions") == [("a", 1)]
        assert world["db"].scalar("SELECT status FROM plan_runs") == "running" and dispatches(log) == []
        assert world["db"].rows("SELECT status, error_code FROM plan_task_runs WHERE task_key = 'b'") == [("failed", "interrupted")]  # what was in flight is closed, never re-run
        # -- a human approves the exact pending action; the second worker resumes it
        assert store.decide(world["sa"], pending.id, "approve", submitted_hash=pending.payload_hash).status == "approved"
        until(lambda: world["db"].scalar("SELECT status FROM plan_runs") != "running", what="the run to finish")
    finally:
        first.kill()
        if second is not None:
            second.kill()
    assert world["db"].scalar("SELECT status FROM plan_runs") == "partial"
    assert task_states(world) == {"a": "succeeded", "b": "failed", "c": "skipped"}
    sent = dispatches(log)
    assert len(sent) == 1 and sent[0][0] == "DISPATCH" and sent[0][2] == "customer@example.com", sent  # exactly once
    assert int(sent[0][1]) == second_pid and int(sent[0][1]) != first_pid  # by the SECOND process (real interpreter pids, reported by the children)
    assert (first.calls + second.calls).count("CALL research") == 1  # the interrupted built-in task was never dispatched again
    assert world["db"].rows("SELECT status, task_key FROM tool_approvals") == [("consumed", "a")]
    assert world["db"].rows("SELECT status, task_key FROM tool_invocations") == [("succeeded", "a")]
    assert world["db"].rows("SELECT task_key, resumed_at IS NOT NULL FROM tool_suspensions") == [("a", 1)]
    types = [r[0] for r in world["db"].rows("SELECT event_type FROM execution_events ORDER BY id")]
    for expected in ("approval_requested", "task_suspended", "task_interrupted", "run_resumed", "approval_approved", "approval_consumed", "task_resumed", "run_completed"):
        assert expected in types, (expected, types)
    assert types.count("task_suspended") == 1 and types.count("task_resumed") == 1 and types.count("approval_consumed") == 1  # no duplicates across the recovery
    assert types.index("task_suspended") < types.index("approval_approved") < types.index("approval_consumed") < types.index("task_resumed") < types.index("run_completed")
    stored = " ".join(r[0] or "" for r in world["db"].rows("SELECT detail FROM execution_events"))
    assert "customer@example.com" not in stored and "launch is confirmed" not in stored  # events never carry the values
