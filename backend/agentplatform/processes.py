"""The process registry: one heartbeat row per running application process, API or worker (M4-A2).

Run leases say who owns a RUN; this says whether a PROCESS is alive. It is what gives standalone (REST/WebSocket) executions an owner
(``execution_owners``): an execution is orphaned when the process that runs it has stopped heartbeating, not merely when some other
process starts. It is also what the database-wide concurrency limits count (executions of dead processes do not occupy a slot).

Times follow the lease rule: PostgreSQL uses the database clock, SQLite the injectable clock. Heartbeat and TTL come from the same
``LeaseConfig`` as run leases (30 s / 10 s). The row carries no host, address or credential: only an opaque owner id and a kind.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import insert, update
from sqlalchemy.exc import IntegrityError

from agentplatform.run_leases import LeaseConfig, db_now
from memory.models import WorkerProcess

logger = logging.getLogger("agentforge.processes")


class ProcessRegistry:
    def __init__(self, session_factory, owner_id: str, kind: str, config: LeaseConfig | None = None, clock=datetime.utcnow):
        if kind not in ("api", "worker"):
            raise ValueError("kind must be 'api' or 'worker'")
        self._sessions, self.owner_id, self.kind = session_factory, owner_id, kind
        self.config = config or LeaseConfig()
        self._clock = clock

    def _times(self, session):
        now = db_now(session, self._clock)
        return now, now + timedelta(seconds=self.config.ttl_seconds)

    def heartbeat(self) -> None:
        """Register this process, or renew its row. A process that was declared dead and is in fact alive comes back (what others
        already recovered stays recovered; its new executions are owned again)."""
        with self._sessions() as session, session.begin():
            now, expires = self._times(session)
            done = session.execute(update(WorkerProcess).where(WorkerProcess.owner_id == self.owner_id).values(heartbeat_at=now, expires_at=expires))
            if done.rowcount == 0:
                try:
                    with session.begin_nested():
                        session.execute(insert(WorkerProcess).values(owner_id=self.owner_id, kind=self.kind, started_at=now, heartbeat_at=now, expires_at=expires))
                except IntegrityError:  # registered concurrently by ourselves: renew
                    session.execute(update(WorkerProcess).where(WorkerProcess.owner_id == self.owner_id).values(heartbeat_at=now, expires_at=expires))

    def release(self) -> None:
        """Graceful shutdown: declare this process gone NOW (call it only after its drivers and executions have stopped)."""
        with self._sessions() as session, session.begin():
            now = db_now(session, self._clock)
            session.execute(update(WorkerProcess).where(WorkerProcess.owner_id == self.owner_id).values(expires_at=now, heartbeat_at=now))

    async def run(self) -> None:
        """The heartbeat loop. Never raises: a database failure is logged and retried (the row then simply expires)."""
        while True:
            job = asyncio.ensure_future(asyncio.to_thread(self.heartbeat))
            job.add_done_callback(lambda j: None if j.cancelled() else j.exception())
            try:
                try:
                    await asyncio.shield(job)
                except asyncio.CancelledError:
                    await asyncio.wait({job}, timeout=5.0)  # an in-flight renewal finishes BEFORE ``release``: it can never undo the release
                    raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Process heartbeat failed (%s)", type(exc).__name__)
            await asyncio.sleep(self.config.heartbeat_seconds)
