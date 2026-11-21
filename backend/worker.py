"""Start a durable AgentForge worker: ``python worker.py`` (M4-A2).

The worker claims and executes plan runs independently of the API process, with the SAME code and database-backed ownership protocol as
the API's embedded mode (see ``agentplatform/worker.py``). Use it with the API in ``RUN_EXECUTION=api-only`` mode: the API records runs,
workers execute them. It needs the same environment as the API (``DATABASE_URL``, ``GROQ_API_KEY``, ``REDIS_URL`` ...); it opens no HTTP
port and needs no new infrastructure. Stop it with SIGTERM or Ctrl+C (graceful: running tasks are recorded ``interrupted``, leases are
released only after the drivers ended); a hard kill is recovered by lease expiry like any crashed process.
"""

import asyncio
import logging
import signal

logger = logging.getLogger("agentforge.worker")


async def _run() -> None:
    import main as app  # builds the same services as the API (orchestrator, providers, memory); starts no server and no lifespan
    from agentplatform.plan_runs import PlanRunService, RunRegistry
    from agentplatform.processes import ProcessRegistry
    from agentplatform.worker import Worker
    from agentplatform.execution import ExecutionStore
    from memory.long_term import SessionLocal
    from migrations.schema_guard import verify_schema_version

    await asyncio.to_thread(verify_schema_version, app.settings.database_url)  # fails closed exactly like the API
    registry = RunRegistry(kind="worker")
    executions = app.build_execution_service(SessionLocal, owner_id=registry.owner_id)
    service = PlanRunService(SessionLocal, executions=executions, registry=registry)
    processes = ProcessRegistry(SessionLocal, registry.owner_id, "worker", registry.config)
    worker = Worker(service, processes, ExecutionStore(SessionLocal), poll_seconds=2.0)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(*_args) -> None:
        loop.call_soon_threadsafe(stop.set)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK: Ctrl+Break on Windows
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_stop)
    await worker.run(stop)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
