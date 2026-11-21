"""Child process for the real-worker tests (M4-A2). NOT a test module.

Usage: python tests/worker_child.py <database-url> <owner-id> <task-seconds> [<ttl> <heartbeat>]

It is one real worker process: the real ``Worker`` loop, ``PlanRunService``, ``ProcessRegistry`` and M2-B ``ExecutionService`` against the
given database, with a stub agent that prints ``CALL <agent> <objective-marker>`` and sleeps ``task-seconds``. It prints ``READY`` once
registered and ``STOPPED`` after a graceful shutdown; SIGINT/SIGTERM/SIGBREAK (Ctrl+Break on Windows) request that shutdown. Nothing here
talks to a network or a provider."""

import asyncio
import signal
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from agentplatform.execution import DbLimits, ExecutionService, ExecutionStore, Limits  # noqa: E402
from agentplatform.plan_runs import PlanRunService, RunRegistry  # noqa: E402
from agentplatform.processes import ProcessRegistry  # noqa: E402
from agentplatform.providers import ProviderRegistry  # noqa: E402
from agentplatform.run_leases import LeaseConfig  # noqa: E402
from agentplatform.worker import Worker  # noqa: E402


class StubOrchestrator:
    agent_names = ("ceo", "sales", "research", "marketing", "finance", "hiring", "support")

    def __init__(self, seconds: float):
        self.seconds = seconds

    async def run_agent(self, name, task, scope):
        marker = next((line.split("marker:")[1].strip() for line in task.splitlines() if "marker:" in line), "-")
        print(f"CALL {name} {marker}", flush=True)
        await asyncio.sleep(self.seconds)
        return f"result of {name}"

    run_with_delegation = run_agent


async def main(url, owner_id, seconds, ttl, heartbeat):
    engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False} if url.startswith("sqlite") else {}, pool_pre_ping=True, pool_size=10 if not url.startswith("sqlite") else 5)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    config = LeaseConfig(ttl_seconds=ttl, heartbeat_seconds=heartbeat, sweep_seconds=0.5, shutdown_grace_seconds=3.0)
    registry = RunRegistry(owner_id=owner_id, config=config, kind="worker")
    executions = ExecutionService(sf, orchestrator=StubOrchestrator(seconds), providers=ProviderRegistry({}), short_term=None, long_term=None, limits=Limits(),
                                  owner_id=owner_id, db_limits=DbLimits(total=100, per_user=100))
    service = PlanRunService(sf, executions=executions, registry=registry)
    processes = ProcessRegistry(sf, owner_id, "worker", config)
    worker = Worker(service, processes, ExecutionStore(sf), poll_seconds=0.2)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(*_args):
        loop.call_soon_threadsafe(stop.set)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_stop)
    task = asyncio.ensure_future(worker.run(stop))
    await asyncio.sleep(0.5)
    print("READY", flush=True)
    await task
    print("STOPPED", flush=True)


if __name__ == "__main__":
    url, owner, seconds = sys.argv[1:4]
    ttl = float(sys.argv[4]) if len(sys.argv) > 4 else 2.0
    heartbeat = float(sys.argv[5]) if len(sys.argv) > 5 else 0.5
    asyncio.run(main(url, owner, float(seconds), ttl, heartbeat))
