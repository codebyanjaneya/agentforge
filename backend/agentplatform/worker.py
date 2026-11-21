"""The durable worker lifecycle (M4-A2): a process that claims and executes plan runs independently of the API.

ONE PROTOCOL. A worker, an embedded API process and a recovery sweep all use the same database-backed ownership protocol of M4-A1: a run
is owned through its lease (``run_leases``), every driver write is fenced by ``(owner, generation)``, and takeover is a compare-and-set.
Nothing in a worker is different in kind from the embedded driver; what differs is WHO claims:

* an API process in ``api-only`` mode records a run as UNCLAIMED (a lease born expired, owner ``unclaimed``) and returns at once;
* a worker claims unclaimed runs, and runs whose owner died WITHOUT a task in flight, and continues them: finished tasks are kept (their
  results are read from the persisted execution records), pending tasks are dispatched for the first time. A run that had a task in flight
  is closed by recovery (that task ``failed``/``interrupted``, never re-dispatched): its outcome is unknown, so only an explicit retry with
  an acknowledgement can run it again.

Claiming is atomic (two workers racing for one run: exactly one wins), tasks are dispatched through the same compare-and-set as before (no
duplicate dispatch), and the worker registers itself in ``worker_processes`` with its own heartbeat, so executions it runs have an owner and
are recovered if it dies. Graceful shutdown (SIGTERM/SIGINT): stop claiming, stop dispatching, cancel running tasks with a bounded wait and
record ``failed``/``interrupted`` for them, release leases only after the drivers ended, then mark the process gone. No queue, no Redis, no
second service: the worker is the same code as the API's embedded mode, started on its own.
"""

import asyncio
import logging

from agentplatform.execution import ExecutionStore
from agentplatform.plan_runs import PlanRunService
from agentplatform.processes import ProcessRegistry

logger = logging.getLogger("agentforge.worker")


class Worker:
    def __init__(self, service: PlanRunService, processes: ProcessRegistry, executions: ExecutionStore, *, poll_seconds: float = 2.0):
        self.service, self.processes, self.executions, self.poll_seconds = service, processes, executions, poll_seconds

    async def run(self, stop: asyncio.Event) -> None:
        """Run until ``stop`` is set, then shut down gracefully (never raises for a database failure of one pass)."""
        await asyncio.to_thread(self.processes.heartbeat)  # registered before any work is claimed
        beat = asyncio.ensure_future(self.processes.run())
        logger.info("Worker %s started", self.processes.kind)
        try:
            while not stop.is_set():
                try:
                    claimed = await self.service.claim_work(resume=True)
                    if claimed["resumed"] or claimed["closed"]:
                        logger.info("Worker pass: %d run(s) claimed or continued, %d closed", claimed["resumed"], claimed["closed"])
                    await asyncio.to_thread(self.executions.fail_orphaned)  # standalone executions whose process is gone
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - e.g. the database is unreachable: the next pass retries
                    logger.error("Worker pass failed (%s)", type(exc).__name__)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            stuck = await self.service.registry.shutdown()  # stop dispatching; drivers record "interrupted" and release their leases
            if stuck:
                logger.warning("%d run driver(s) did not stop in time; their leases will expire and be recovered", len(stuck))
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
            try:
                await asyncio.to_thread(self.processes.release)  # only now: nothing of this process can write any more
            except Exception as exc:  # noqa: BLE001 - the heartbeat row simply expires
                logger.warning("Could not release the worker process row (%s)", type(exc).__name__)
            logger.info("Worker stopped")
