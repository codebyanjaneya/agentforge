"""Child process for the real-worker APPROVAL recovery tests (M4-C). NOT a test module.

Usage: python tests/worker_child_approvals.py <database-url> <owner-id> <builtin-seconds> <ttl> <heartbeat> <dispatch-log>

One real worker process with the REAL approval stack: ``Worker`` loop, ``PlanRunService``, ``ExecutionService`` with a ``ToolInvoker`` + ``ApprovalStore``, against the given
database. A scripted provider stands in for a model (no credential, no network): a custom agent holding ``demo.send_note`` asks for it once, then answers. The SIMULATED
tool appends ``DISPATCH <pid> <recipient>`` to the dispatch log, so a test can prove how many times, and from which process, it really ran. Built-in agents are stubs
that print ``CALL <agent>`` and sleep ``builtin-seconds``. Prints ``READY`` once registered and ``STOPPED`` after a graceful shutdown."""

import asyncio
import json
import os
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
from agentplatform.providers import ProviderRegistry, ProviderResponse, ToolCall  # noqa: E402
from agentplatform.run_leases import LeaseConfig  # noqa: E402
from agentplatform.tools.approvals import ApprovalStore  # noqa: E402
from agentplatform.tools import default_registry  # noqa: E402
from agentplatform.tools.fixtures import DEMO_SEND_NOTE, SendNoteOut  # noqa: E402
from agentplatform.tools.invoker import ToolInvoker, ToolStore  # noqa: E402
from agentplatform.worker import Worker  # noqa: E402

ARGS = {"recipient": "customer@example.com", "subject": "Launch date", "body": "The launch is confirmed for next week."}


class StubOrchestrator:
    agent_names = ("ceo", "sales", "research", "marketing", "finance", "hiring", "support")

    def __init__(self, seconds: float):
        self.seconds = seconds

    async def run_agent(self, name, task, scope):
        print(f"CALL {name}", flush=True)
        await asyncio.sleep(self.seconds)
        return f"result of {name}"

    run_with_delegation = run_agent


class ScriptedProvider:
    name = "groq"

    async def complete(self, request):
        results = [m for m in request.messages if m.role == "tool"]
        if request.tools and not results:
            return ProviderResponse(text="The customer must be told.", model="m", tool_calls=(ToolCall("c1", "demo__send_note__v1", json.dumps(ARGS)),))
        return ProviderResponse(text="Done: the note was handled." if results else "no tools", model="m")


async def main(url, owner_id, seconds, ttl, heartbeat, dispatch_log):
    engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False} if url.startswith("sqlite") else {}, pool_pre_ping=True, pool_size=10 if not url.startswith("sqlite") else 5)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    config = LeaseConfig(ttl_seconds=ttl, heartbeat_seconds=heartbeat, sweep_seconds=0.5, shutdown_grace_seconds=3.0)
    registry = RunRegistry(owner_id=owner_id, config=config, kind="worker")

    def logged_send(args):
        with open(dispatch_log, "a", encoding="utf-8") as log:
            log.write(f"DISPATCH {os.getpid()} {args.recipient}\n")
        return SendNoteOut(simulated=True, delivered=False, receipt="r")

    tools = default_registry()  # the process-wide registry: agent definitions pin their tools through it
    tools.register(DEMO_SEND_NOTE, logged_send)  # the SIMULATED fixture tool, registered explicitly
    invoker = ToolInvoker(ToolStore(sf), tools, ApprovalStore(sf, policy_fingerprint=tools.policy.fingerprint), poll_seconds=0.1)
    executions = ExecutionService(sf, orchestrator=StubOrchestrator(seconds), providers=ProviderRegistry({"groq": lambda: ScriptedProvider()}), short_term=None, long_term=None,
                                  limits=Limits(), owner_id=owner_id, db_limits=DbLimits(total=100, per_user=100), tools=invoker)
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
    print("READY", os.getpid(), flush=True)
    await task
    print("STOPPED", flush=True)


if __name__ == "__main__":
    url, owner, seconds, ttl, heartbeat, log = sys.argv[1:7]
    asyncio.run(main(url, owner, float(seconds), float(ttl), float(heartbeat), log))
