"""Durable run ownership: leases, fencing and persisted cancellation requests (M4-A1).

WHAT THIS IS. A ``running`` plan run is driven by exactly one process at a time. Ownership is a row in ``run_leases``:
``(run_id, owner_id, generation, expires_at, cancel_requested_at)``. The owner keeps the lease alive by renewing it
(heartbeat); if it stops (crash, hang, lost database) the lease expires and ANY process may take the run over, which
increases ``generation`` by one. ``Fence(run_id, owner_id, generation)`` is what a driver carries: every state change of the
run checks it INSIDE the same transaction as the change (``touch``), so a stale owner (older generation, or another owner)
gets ``LeaseLost`` and cannot complete a task, propagate a result, finish or close a run, or overwrite a newer owner's state.

WHAT THIS IS NOT. It is not a queue and not a retry engine: taking a run over never re-dispatches a task. The new owner only
reconciles what was persisted and closes the run (see ``PlanRunStore.recover_expired``).

TIME. On PostgreSQL every expiry is computed and compared with the DATABASE clock (``timezone('UTC', now())``), so the
processes' own clocks do not matter. SQLite (development and tests; one host) uses the injectable ``clock`` of the store.

CONCURRENCY. ``touch`` is an UPDATE of the lease row, so it takes the row lock: a writer holding it blocks a concurrent
takeover until the writer's transaction ends, and a takeover that committed first makes the writer's ``touch`` match no row.
"""

import observability
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import DateTime, exists, func, insert, select, type_coerce, update
from sqlalchemy.exc import IntegrityError

from agentplatform import run_events
from memory.models import PlanRun, RunLease

UNCLAIMED = "unclaimed"  # the owner of a run an API-only process recorded for a worker to claim (its lease is born expired)


class LeaseLost(Exception):
    """The caller no longer owns the run (another owner or generation holds it). Nothing was written."""


@dataclass(frozen=True)
class Fence:
    run_id: object
    owner_id: str
    generation: int


@dataclass(frozen=True)
class LeaseConfig:
    ttl_seconds: float = 30.0
    heartbeat_seconds: float = 10.0
    sweep_seconds: float = 10.0
    shutdown_grace_seconds: float = 10.0

    def __post_init__(self):
        if not (0 < self.heartbeat_seconds and self.heartbeat_seconds * 2 <= self.ttl_seconds):
            raise ValueError("the lease must live at least two heartbeats")
        if self.sweep_seconds <= 0 or self.shutdown_grace_seconds < 0:
            raise ValueError("invalid sweep or shutdown settings")


@dataclass(frozen=True)
class Renewal:
    owned: bool
    cancel_requested: bool = False


def new_owner_id() -> str:
    """Unique per process start (never reused across restarts, so a restarted process is a NEW owner)."""
    return f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def db_now(session, clock=datetime.utcnow):
    """The current time as a SQL expression on PostgreSQL (database clock, naive UTC) or ``clock()`` elsewhere."""
    if session.get_bind().dialect.name == "postgresql":
        return type_coerce(func.timezone("UTC", func.now()), DateTime)
    return clock()


class LeaseStore:
    def __init__(self, session_factory, *, clock=datetime.utcnow):
        self._sessions = session_factory
        self._clock = clock

    # ---------------------------------------------------------------------------------------------- time

    @staticmethod
    def _is_pg(session) -> bool:
        return session.get_bind().dialect.name == "postgresql"

    def _now(self, session):
        """Database time on PostgreSQL (naive UTC, like every timestamp column here); the store's clock elsewhere."""
        return db_now(session, self._clock)

    def _after(self, session, seconds: float):
        return self._now(session) + timedelta(seconds=seconds)

    # ---------------------------------------------------------------------------------------------- lease lifecycle

    def create(self, session, run_id, owner_id: str, ttl: float) -> None:
        """Inside the transaction that creates the run: a run never exists without an owner (generation 1)."""
        now = self._now(session)
        expires = now if owner_id == UNCLAIMED else self._after(session, ttl)  # unclaimed work is born claimable
        session.execute(insert(RunLease).values(run_id=run_id, owner_id=owner_id, generation=1, expires_at=expires, created_at=now, updated_at=now))

    def touch(self, session, fence: Fence) -> None:
        """The fencing check. Call it first inside every transaction that changes the run or its tasks."""
        done = session.execute(update(RunLease).where(RunLease.run_id == fence.run_id, RunLease.owner_id == fence.owner_id,
                                                      RunLease.generation == fence.generation).values(updated_at=self._now(session)))
        if done.rowcount != 1:
            raise LeaseLost("ownership of the run was lost")

    def renew(self, fence: Fence, ttl: float) -> Renewal:
        """Heartbeat: extend the lease IF the caller still owns this generation AND the lease has not expired yet (an expired or
        released lease is never resurrected, so a late or in-flight renewal cannot undo a release or outlive a takeover); also
        reports a persisted cancel request. ``owned=False`` means the lease is lost."""
        with self._sessions() as session, session.begin():
            done = session.execute(update(RunLease).where(RunLease.run_id == fence.run_id, RunLease.owner_id == fence.owner_id,
                                                          RunLease.generation == fence.generation, RunLease.expires_at > self._now(session))
                                   .values(expires_at=self._after(session, ttl), updated_at=self._now(session)))
            if done.rowcount != 1:
                return Renewal(False)
            requested = session.execute(select(RunLease.cancel_requested_at).where(RunLease.run_id == fence.run_id)).scalar_one()
            return Renewal(True, requested is not None)

    def release(self, fence: Fence) -> bool:
        """Expire the caller's own lease NOW. Only call it when the driver can no longer write (see ``PlanRunService._drive``)."""
        with self._sessions() as session, session.begin():
            now = self._now(session)
            done = session.execute(update(RunLease).where(RunLease.run_id == fence.run_id, RunLease.owner_id == fence.owner_id,
                                                          RunLease.generation == fence.generation).values(expires_at=now, updated_at=now))
            return done.rowcount == 1

    def takeover(self, run_id, owner_id: str, ttl: float) -> Fence | None:
        """Acquire an EXPIRED (or missing) lease; None if it is live or another process won the race. Exactly one caller
        succeeds per expiry: the UPDATE only matches an expired row, and a missing row is created under the primary key."""
        try:
            with self._sessions() as session, session.begin():
                now = self._now(session)
                done = session.execute(update(RunLease).where(RunLease.run_id == run_id, RunLease.expires_at <= now,
                                                              exists().where(PlanRun.id == RunLease.run_id, PlanRun.status == "running"))  # a finished run has no owner to replace
                                       .values(owner_id=owner_id, generation=RunLease.generation + 1, expires_at=self._after(session, ttl), updated_at=now))
                if done.rowcount == 1:
                    generation = session.execute(select(RunLease.generation).where(RunLease.run_id == run_id, RunLease.owner_id == owner_id)).scalar_one()
                    observability.inc("lease_takeovers_total")
                    return Fence(run_id, owner_id, generation)
                has_lease = session.execute(select(RunLease.run_id).where(RunLease.run_id == run_id)).first() is not None
                if has_lease:
                    return None  # live, or the run is no longer running
                running = session.execute(select(PlanRun.id).where(PlanRun.id == run_id, PlanRun.status == "running")).first() is not None
                if not running:
                    return None
                self.create(session, run_id, owner_id, ttl)  # a run that predates leases, or lost its lease row
                return Fence(run_id, owner_id, 1)
        except IntegrityError:
            return None  # another process created it first

    # ---------------------------------------------------------------------------------------------- cancellation

    def request_cancel(self, run_id, actor_user_id=None, source: str = "api") -> bool:
        """Persist a cancellation request (any process may; idempotent: the first request time is kept). The first request also writes
        the ``cancellation_requested`` event in the same transaction. False if the run has no lease row."""
        with self._sessions() as session, session.begin():
            now = self._now(session)
            done = session.execute(update(RunLease).where(RunLease.run_id == run_id, RunLease.cancel_requested_at.is_(None))
                                   .values(cancel_requested_at=now, updated_at=now))
            if done.rowcount == 1:
                run_events.add(session, run_id, "cancellation_requested", actor_type="user" if actor_user_id else source,
                               actor_id=str(actor_user_id) if actor_user_id else None)
                return True
            return session.execute(select(RunLease.run_id).where(RunLease.run_id == run_id)).first() is not None

    def cancel_requested(self, run_id) -> bool:
        with self._sessions() as session:
            return session.execute(select(RunLease.cancel_requested_at).where(RunLease.run_id == run_id)).scalar_one_or_none() is not None

    # ---------------------------------------------------------------------------------------------- recovery

    def expired_run_ids(self, limit: int = 50, run_id=None, include_unclaimed: bool = True) -> list:
        """Running runs whose lease is expired or missing, oldest first (read-only; ``run_id`` narrows it to one run).
        ``include_unclaimed=False`` leaves out runs recorded for a worker that no worker has claimed yet."""
        with self._sessions() as session:
            now = self._now(session)
            query = (select(PlanRun.id).outerjoin(RunLease, RunLease.run_id == PlanRun.id)
                     .where(PlanRun.status == "running", (RunLease.run_id.is_(None)) | (RunLease.expires_at <= now)))
            if not include_unclaimed:
                query = query.where((RunLease.run_id.is_(None)) | (RunLease.owner_id != UNCLAIMED))
            if run_id is not None:
                query = query.where(PlanRun.id == run_id)
            return list(session.execute(query.order_by(PlanRun.created_at).limit(limit)).scalars())

    def snapshot(self, run_id) -> dict | None:
        with self._sessions() as session:
            row = session.execute(select(RunLease).where(RunLease.run_id == run_id)).scalar_one_or_none()
            if row is None:
                return None
            return {"owner_id": row.owner_id, "generation": row.generation, "expires_at": row.expires_at, "cancel_requested_at": row.cancel_requested_at}
