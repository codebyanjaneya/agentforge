"""Execution of approved plans (M3-B): persisted runs, a dependency-ordered in-process scheduler and the run service.

ONE runtime: every task is executed by the M2-B ``ExecutionService.run`` (same resolution rules, records, timeouts, limits
and safe errors). This module adds only what a plan needs: the approval gate, the run/task-run state machine, the scheduler
and the link from a task run to its M2-B execution record.

DELIVERY SEMANTICS (honest, not exactly-once). A task is dispatched AT MOST ONCE per run: the ``pending -> running``
transition is a compare-and-set that also links the M2-B execution record, and it happens before the agent is called. Nothing
is retried automatically (agent calls are not idempotent): a failed task stays failed, its dependants are skipped, and the
user must start a NEW run (a new attempt; earlier runs are never overwritten). If the process dies mid-run, the startup sweep
marks running tasks ``failed`` (``interrupted``) and unstarted ones ``skipped``; whether the provider call of an interrupted
task took effect is unknown. Scheduling is in-process (no queue, no workers): limits are those of the shared ``Limits``.

OWNERSHIP (M4-A1). Every running run has a lease (``run_leases``, see ``run_leases.py``): the process that drives it renews
the lease by heartbeat and carries a ``Fence``; every driver write is fenced (checked inside the same transaction), so a stale
owner cannot change the run after a takeover. A process start no longer closes every running run: ``recover_expired`` closes
only runs whose lease is expired or missing, once, after reconciling work that was already persisted. A recovered run NEVER
re-dispatches a task: whatever was in flight ends ``failed``/``interrupted`` (the outcome of the provider call is unknown and
is not claimed to be "did not happen"). Cancellation is a persisted request the owner honours; graceful shutdown stops
dispatching, gives running tasks a bounded time to stop, records ``interrupted`` and only then releases the lease.
"""

import observability
import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError

from agentplatform import run_events
from agentplatform.tools.approvals import TaskSuspended, open_suspensions, ready_suspensions
from agentplatform.tools.invoker import ToolRunContext, close_open as close_tool_calls
from agentplatform.run_leases import UNCLAIMED, Fence, LeaseConfig, LeaseLost, LeaseStore, db_now, new_owner_id

from agentplatform.dag import TaskNode, validate_dag
from agentplatform.execution import (
    MAX_TASK_CHARS,
    AgentUnavailable,
    Busy,
    ExecutionFailed,
    ExecutionRefused,
    ExecutionService,
    ExecutionStore,
)
from agentplatform.plans import PlanNotFound, PlanStore
from authz.scope import require_scope
from memory.models import (
    User,
    AgentExecution,
    ExecutionEvent,
    Plan,
    PlanRevision,
    PlanRun,
    PlanRunRetry,
    PlanTask,
    PlanTaskCarryover,
    PlanTaskDependency,
    PlanTaskRun,
    RunLease,
    ToolSuspension,
    User,
)

logger = logging.getLogger("agentforge.planruns")

MAX_RUNS_PER_PLAN = 20
MAX_ACTIVE_RUNS_PER_USER = 2
MAX_PARALLEL = 3  # tasks of one run in flight at once (the shared Limits still apply on top)
BUSY_POLL_SECONDS = 0.25
BUSY_BUDGET_SECONDS = 120.0  # how long a ready task may wait for a free execution slot before it fails with "busy"
DEPENDENCY_RESULT_CHARS = 1500
OUTPUT_PREVIEW_CHARS = 8000
CANCEL_WAIT_SECONDS = 15.0

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

MESSAGES = {
    "dependency_failed": "A task this one depends on did not succeed, so it was not run.",
    "agent_unavailable": "The agent assigned to this task is no longer available, so it was not run.",
    "busy": "The server was too busy to start this task. Start a new run to try again.",
    "cancelled": "The run was cancelled before this task finished.",
    "interrupted": "The server restarted before this task finished.",
    "internal_error": "The task could not be run.",
    "task_invalid": "The task input could not be prepared.",
}


class RunError(Exception):
    code = "run_error"


class RunNotFound(RunError):
    code = "not_found"


class TaskRunNotFound(RunError):
    code = "task_not_found"


class PlanNotApproved(RunError):
    code = "plan_not_approved"


class PlanCancelledForRun(RunError):
    code = "plan_cancelled"


class PlanStale(RunError):
    code = "plan_stale"


class RunInProgress(RunError):
    code = "run_in_progress"


class AlreadyExecuted(RunError):
    code = "already_executed"


class RunLimit(RunError):
    code = "limit_reached"

    def __init__(self, what: str):
        super().__init__(what)
        self.what = what


class ShuttingDown(RunError):
    code = "shutting_down"


class RetryNotAllowed(RunError):
    """An explicit retry was refused: ``code`` is one of run_not_retryable, not_latest_attempt, already_retried, nothing_to_retry, unknown_task."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ReconciliationRequired(RunError):
    """P0: the retry would re-run tasks that have a side-effecting tool call whose outcome is UNKNOWN (``tasks``). An acknowledgement cannot override this: the unknown outcome must first be
    reconciled (against the remote service, or by an authorised operator's recorded decision)."""

    code = "reconciliation_required"

    def __init__(self, tasks: list):
        super().__init__("reconciliation required")
        self.tasks = tasks


class UncertainOutcome(RunError):
    """The retry would re-run tasks whose previous outcome is unknown and the user did not acknowledge them (``tasks``)."""

    code = "uncertain_outcome"

    def __init__(self, tasks: list):
        super().__init__("uncertain outcome")
        self.tasks = tasks


class RunInvalid(RunError):
    code = "plan_invalid"

    def __init__(self, errors: list):
        super().__init__("plan invalid")
        self.errors = errors


@dataclass(frozen=True)
class TaskRunView:
    key: str
    position: int
    level: int
    title: str
    description: str
    expected_output: str
    depends_on: tuple
    blocked_by: tuple
    agent_source: str
    agent_key: str
    agent_name: str
    agent_version_id: object | None
    agent_version: int | None
    status: str
    execution_id: object | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    reused: bool = False  # M4-A2: carried over from an earlier attempt (not executed again); ``execution_id`` is the one that really ran it
    reused_run_id: object | None = None
    reused_attempt: int | None = None
    retry_class: str | None = None  # M4-A2: "safe" | "uncertain" for a task that did not succeed (see ``retry_class``)


@dataclass(frozen=True)
class RunView:
    id: object
    plan_id: object
    revision: int
    attempt: int
    status: str
    error_code: str | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None
    tasks: tuple
    counts: dict = field(default_factory=dict)
    retry_of: dict | None = None  # M4-A2: {"id", "attempt"} of the attempt this one retries
    retried_by: dict | None = None  # M4-A2: {"id", "attempt"} of the attempt that retried this one
    ownership: str | None = None  # M4-A2, running runs only: "live" | "expired" | "unclaimed" | "none" (no process identity is exposed)
    retry: dict | None = None  # M4-A2, runs that ended without success: {"allowed", "reason", "uncertain"}
    started_by: str | None = None  # sprint 5: the e-mail of the member who started the run (workspace members already see each other's e-mail); never an id


@dataclass(frozen=True)
class RunSummary:
    id: object
    revision: int
    attempt: int
    status: str
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None
    counts: dict


@dataclass(frozen=True)
class DriverTask:
    id: object
    key: str
    position: int
    level: int
    title: str
    description: str
    expected_output: str
    depends_on: tuple
    agent_key: str
    agent_version_id: object | None
    status: str
    carried: bool = False  # M4-A2: a task reused from an earlier attempt (already succeeded, never dispatched again)
    required_tool: str | None = None  # M6b: the exact tool this task must really invoke to count as done


def _owned(scope):
    """WRITE access: the user who started the run, inside the workspace."""
    return (PlanRun.user_id == scope.user_id, PlanRun.workspace_id == scope.workspace_id)


def _seen(scope):
    """READ access (sprint 5): every member of the workspace sees the workspace's runs."""
    return (PlanRun.workspace_id == scope.workspace_id,)


def _counts(statuses) -> dict:
    out = {s: 0 for s in ("pending", "running", "succeeded", "failed", "skipped", "cancelled")}
    for s in statuses:
        out[s] += 1
    out["total"] = sum(out.values())
    return out


def final_status(statuses, cancelled: bool = False) -> str:
    """The run status implied by its task statuses: all succeeded -> succeeded; cancelled; some succeeded but not all ->
    partial; none succeeded -> failed."""
    done = sum(1 for s in statuses if s == "succeeded")
    if cancelled:
        return "cancelled"
    if done == len(statuses):
        return "succeeded"
    return "partial" if done else "failed"


# A task that was DISPATCHED and then failed with one of these codes was rejected by the provider before it did any work, so running it
# again cannot duplicate anything. Every other outcome of a dispatched task (a timeout, an interruption, an internal error, an unusable
# response, a cancellation in flight) leaves it unknown whether the call took effect: that is ``uncertain``.
SAFE_AFTER_DISPATCH = frozenset({"rate_limited", "unavailable", "auth", "unsupported_model", "unsupported_provider", "bad_request"})
RETRYABLE_RUN_STATUSES = ("partial", "failed", "cancelled")


def retry_class(status: str, error_code: str | None, execution_id) -> str | None:
    """None for a task that needs no retry (succeeded, pending, running). ``safe``: the task was never dispatched (skipped, busy,
    agent unavailable, a dependency failed, cancelled before it started) or the provider rejected the request: an explicit retry
    cannot duplicate any effect. ``uncertain``: the outcome of the dispatched call is unknown: it is retried only when the user
    acknowledges that for this task, and the acknowledgement is recorded. (Today agents have no tools, so the only effect of a re-run is
    cost; the rule exists so that no future capability inherits a silent retry.)"""
    if status in ("succeeded", "pending", "running"):
        return None
    if execution_id is None:
        return "safe"
    if status == "failed" and error_code in SAFE_AFTER_DISPATCH:
        return "safe"
    return "uncertain"


_DIALECT_PG = "postgresql"


# ------------------------------------------------------------------------------------------------ store

class PlanRunStore:
    """Persistence for runs and task runs. Every scope-facing read is constrained by the scope's user AND workspace; the
    methods the driver uses are keyed by ids it created itself. The database triggers (migration 0008) enforce the legal
    state transitions, so a bug here cannot rewrite history.

    M4-A2: every state change writes its ``execution_events`` row in the SAME transaction; a task starts and finishes together with
    its execution record (``start_task`` / ``finish_task_execution``); a retry attempt reuses the persisted results of the tasks that
    already succeeded (``plan_task_carryovers``) and is linked to the attempt it retries (``plan_run_retries``)."""

    def __init__(self, session_factory, *, owner_id: str | None = None, config: LeaseConfig | None = None, clock=datetime.utcnow, kind: str = "api"):
        self._sessions = session_factory
        self.owner_id = owner_id or new_owner_id()  # one per process: ``PlanRunService`` takes it from the shared ``RunRegistry``
        self.config = config or LeaseConfig()
        self.kind = kind  # "api" | "worker": the source recorded in events
        self.leases = LeaseStore(session_factory, clock=clock)

    # ---------------------------------------------------------------------------------------------- events

    def _actor(self, fence: Fence | None = None, recovery: bool = False) -> tuple:
        if recovery:
            return ("recovery", self.owner_id)
        return (self.kind, fence.owner_id if fence is not None else self.owner_id)

    @staticmethod
    def _event(session, run_id, scope, event_type, actor, *, task_key=None, execution_id=None, **detail) -> None:
        run_events.add(session, run_id, event_type, actor_type=actor[0], actor_id=actor[1], task_key=task_key, execution_id=execution_id, scope=scope, **detail)

    def events(self, scope, run_id, after: int = 0, limit: int = 100) -> dict:
        """The run's history in order (scope-checked: unknown and foreign runs are both ``RunNotFound``). ``after`` is the id of the last
        event seen; ``next_after`` is None when the page is the end of the history."""
        scope = require_scope(scope)
        if type(limit) is not int or not 1 <= limit <= 200 or type(after) is not int or after < 0:
            raise ValueError("invalid_paging")
        with self._sessions() as session:
            if session.execute(select(PlanRun.id).where(PlanRun.id == run_id, *_seen(scope))).first() is None:
                raise RunNotFound("not found")
            rows = session.execute(select(ExecutionEvent).where(ExecutionEvent.run_id == run_id, ExecutionEvent.id > after)
                                   .order_by(ExecutionEvent.id).limit(limit + 1)).scalars().all()
            page = rows[:limit]
            return {"items": [run_events.view(r) for r in page], "next_after": page[-1].id if len(rows) > limit else None}

    # ---------------------------------------------------------------------------------------------- start gate

    def create(self, scope, plan_id, *, retry_of=None, acknowledged=(), claim: bool = True) -> uuid.UUID:
        """The approval gate, atomically: the plan must belong to the scope and be APPROVED at its latest revision; the
        exact approved revision is the one executed (never client supplied). Refuses a second active run and a revision
        that already succeeded (database partial unique indexes are the backstop for races).

        ``retry_of`` makes this an explicit RETRY attempt of that run (see ``_retry_source``): the tasks that already succeeded are
        carried over (their persisted results are reused, they are never dispatched again), every other task is pending again, and
        the lineage, the acknowledged uncertain tasks and the authorisation are recorded with the attempt. ``claim=False`` records the
        run as UNCLAIMED (for a worker) instead of owned by this process."""
        scope = require_scope(scope)
        now = datetime.utcnow()
        try:
            with self._sessions() as session, session.begin():
                plan = session.execute(select(Plan).where(Plan.id == plan_id, Plan.user_id == scope.user_id, Plan.workspace_id == scope.workspace_id)).scalar_one_or_none()
                if plan is None:
                    raise PlanNotFound("not found")
                if plan.status == "cancelled":
                    raise PlanCancelledForRun("cancelled")
                if plan.status != "approved" or plan.approved_revision is None:
                    raise PlanNotApproved("not approved")
                if plan.approved_revision != plan.latest_revision:
                    raise PlanStale("stale")
                # lock/compare: nothing may change the approval between this check and the insert (row lock on PostgreSQL)
                held = session.execute(update(Plan).where(Plan.id == plan.id, Plan.status == "approved", Plan.approved_revision == plan.approved_revision,
                                                          Plan.user_id == scope.user_id, Plan.workspace_id == scope.workspace_id).values(updated_at=Plan.updated_at))
                if held.rowcount != 1:
                    raise PlanNotApproved("not approved")
                if session.get_bind().dialect.name == _DIALECT_PG:  # the per-user active-run cap is check-then-insert: serialise it per user
                    session.execute(select(User.id).where(User.id == scope.user_id).with_for_update())
                revision = session.execute(select(PlanRevision).where(PlanRevision.plan_id == plan.id, PlanRevision.revision == plan.approved_revision)).scalar_one()
                runs = session.execute(select(PlanRun.status, PlanRun.attempt, PlanRun.revision_id).where(PlanRun.plan_id == plan.id)).all()
                if any(r.status == "running" for r in runs):
                    raise RunInProgress("running")
                if any(r.status == "succeeded" and r.revision_id == revision.id for r in runs):
                    raise AlreadyExecuted("executed")
                if len(runs) >= MAX_RUNS_PER_PLAN:
                    raise RunLimit("runs")
                active = session.execute(select(func.count()).select_from(PlanRun).where(PlanRun.user_id == scope.user_id, PlanRun.status == "running")).scalar()
                if active >= MAX_ACTIVE_RUNS_PER_USER:
                    raise RunLimit("active_runs")
                source, carried, acknowledged_effective = None, {}, []
                if retry_of is not None:
                    source, carried, acknowledged_effective = self._retry_source(session, scope, revision, runs, retry_of, acknowledged)
                attempt = max((r.attempt for r in runs), default=0) + 1
                run = PlanRun(plan_id=plan.id, revision_id=revision.id, user_id=scope.user_id, workspace_id=scope.workspace_id, attempt=attempt, status="running", created_at=now)
                session.add(run)
                session.flush()
                self.leases.create(session, run.id, self.owner_id if claim else UNCLAIMED, self.config.ttl_seconds)  # the run never exists without an owner record
                tasks = session.execute(select(PlanTask).where(PlanTask.revision_id == revision.id).order_by(PlanTask.position)).scalars().all()
                for t in tasks:
                    if t.task_key in carried:
                        session.add(PlanTaskCarryover(run_id=run.id, task_key=t.task_key, source_task_run_id=carried[t.task_key], created_at=now))
                        continue
                    session.add(PlanTaskRun(run_id=run.id, revision_id=revision.id, task_key=t.task_key, position=t.position, level=t.level,
                                            agent_source=t.agent_source, agent_key=t.agent_key, agent_name=t.agent_name, agent_version_id=t.agent_version_id,
                                            agent_version=t.agent_version, status="pending", created_at=now))
                if source is not None:
                    session.add(PlanRunRetry(run_id=run.id, retry_of_run_id=source.id, authorized_by=scope.user_id, acknowledged=json.dumps(sorted(acknowledged_effective)),
                                             carried_count=len(carried), created_at=now))
                session.flush()
                ids = (scope.user_id, scope.workspace_id)
                user_actor = ("user", str(scope.user_id))
                if source is not None:
                    retried = [t.task_key for t in tasks if t.task_key not in carried]
                    self._event(session, run.id, ids, "retry_authorized", user_actor, attempt=attempt, retry_of_attempt=source.attempt,
                                acknowledged=acknowledged_effective, carried=sorted(carried), retried=retried)
                    for key in sorted(carried):
                        self._event(session, run.id, ids, "task_reused", user_actor, task_key=key)
                self._event(session, run.id, ids, "run_created", user_actor, attempt=attempt, count=len(tasks))
                if claim:
                    self._event(session, run.id, ids, "run_claimed", self._actor(), generation=1)
                return run.id
        except IntegrityError:  # the partial unique indexes / the one-retry-per-run constraint: a concurrent start or retry won
            with self._sessions() as session:
                running = session.execute(select(func.count()).select_from(PlanRun).where(PlanRun.plan_id == plan_id, PlanRun.status == "running")).scalar()
                retried = retry_of is not None and session.execute(select(PlanRunRetry.run_id).where(PlanRunRetry.retry_of_run_id == retry_of)).first() is not None
            if retried:
                raise RetryNotAllowed("already_retried") from None
            raise (RunInProgress("running") if running else AlreadyExecuted("executed")) from None

    # ---------------------------------------------------------------------------------------------- retry

    def _effective_states(self, session, run: PlanRun) -> list:
        """Every task of a run as (key, status, error_code, execution_id, original task run id): the run's own task runs plus the tasks it
        carried over (reported as succeeded, with the task run that really executed them)."""
        out = []
        for tr in session.execute(select(PlanTaskRun).where(PlanTaskRun.run_id == run.id)).scalars():
            out.append((tr.task_key, tr.status, tr.error_code, tr.execution_id, tr.id))
        for co, source in session.execute(select(PlanTaskCarryover, PlanTaskRun).join(PlanTaskRun, PlanTaskRun.id == PlanTaskCarryover.source_task_run_id)
                                          .where(PlanTaskCarryover.run_id == run.id)).all():
            out.append((co.task_key, "succeeded", None, source.execution_id, source.id))
        return out

    def _retry_source(self, session, scope, revision, runs, retry_of, acknowledged) -> tuple:
        """Validate a retry INSIDE ``create``'s transaction; returns (source run, {task key: original task run id}, acknowledged keys)."""
        source = session.execute(select(PlanRun).where(PlanRun.id == retry_of, PlanRun.plan_id == revision.plan_id, *_owned(scope))).scalar_one_or_none()
        if source is None:
            raise RunNotFound("not found")  # unknown and foreign runs are indistinguishable
        if source.revision_id != revision.id:
            raise PlanStale("stale")
        if source.status not in RETRYABLE_RUN_STATUSES:
            raise RetryNotAllowed("run_not_retryable")
        if source.attempt != max(r.attempt for r in runs):
            raise RetryNotAllowed("not_latest_attempt")
        if session.execute(select(PlanRunRetry.run_id).where(PlanRunRetry.retry_of_run_id == source.id)).first() is not None:
            raise RetryNotAllowed("already_retried")
        states = self._effective_states(session, source)
        keys = {t for t in session.execute(select(PlanTask.task_key).where(PlanTask.revision_id == revision.id)).scalars()}
        carried = {key: original for key, status, _code, _exe, original in states if status == "succeeded"}
        uncertain = {key for key, status, code, execution_id, _o in states if retry_class(status, code, execution_id) == "uncertain"}
        from agentplatform.tools import dispatch as dp

        blocked = dp.unresolved_for_plan(session, revision.plan_id) & (keys - set(carried))
        if blocked:
            raise ReconciliationRequired(sorted(blocked))  # before any acknowledgement is looked at: an unknown side-effect outcome is never overridden by "acknowledge"
        ack = set(acknowledged)
        if not ack <= keys:
            raise RetryNotAllowed("unknown_task")
        if uncertain - ack:
            raise UncertainOutcome(sorted(uncertain - ack))
        if keys <= set(carried):
            raise RetryNotAllowed("nothing_to_retry")
        return source, carried, sorted(ack & uncertain)

    def retry_preview(self, scope, run_id, acknowledged=()) -> dict:
        """What a retry of ``run_id`` would do, with the same checks as ``create`` (raises the same errors): the plan, the tasks that would
        run again (key, agent, pinned version) and the tasks that would be reused. Read-only."""
        scope = require_scope(scope)
        with self._sessions() as session:
            source = session.execute(select(PlanRun).where(PlanRun.id == run_id, *_owned(scope))).scalar_one_or_none()
            if source is None:
                raise RunNotFound("not found")
            plan = session.execute(select(Plan).where(Plan.id == source.plan_id, Plan.user_id == scope.user_id, Plan.workspace_id == scope.workspace_id)).scalar_one()
            if plan.status == "cancelled":
                raise PlanCancelledForRun("cancelled")
            if plan.status != "approved" or plan.approved_revision is None:
                raise PlanNotApproved("not approved")
            if plan.approved_revision != plan.latest_revision:
                raise PlanStale("stale")
            revision = session.execute(select(PlanRevision).where(PlanRevision.plan_id == plan.id, PlanRevision.revision == plan.approved_revision)).scalar_one()
            runs = session.execute(select(PlanRun.status, PlanRun.attempt, PlanRun.revision_id).where(PlanRun.plan_id == plan.id)).all()
            _source, carried, ack = self._retry_source(session, scope, revision, runs, run_id, acknowledged)
            tasks = session.execute(select(PlanTask).where(PlanTask.revision_id == revision.id).order_by(PlanTask.position)).scalars().all()
            return {"plan_id": plan.id, "carried": sorted(carried), "acknowledged": ack,
                    "retried": [(t.task_key, t.agent_key, t.agent_version_id) for t in tasks if t.task_key not in carried]}

    def _retry_info(self, session, run: PlanRun, tasks: tuple) -> dict | None:
        """The ``retry`` summary shown with a run that ended without success (read-only, no authorisation is implied)."""
        if run.status not in RETRYABLE_RUN_STATUSES:
            return None
        uncertain = sorted(t.key for t in tasks if t.retry_class == "uncertain")
        reconcile = sorted(t.key for t in tasks if t.retry_class == "reconcile")
        reason = None
        latest = session.execute(select(func.max(PlanRun.attempt)).where(PlanRun.plan_id == run.plan_id)).scalar()
        plan = session.execute(select(Plan).where(Plan.id == run.plan_id)).scalar_one()
        number = session.execute(select(PlanRevision.revision).where(PlanRevision.id == run.revision_id)).scalar_one()
        if run.attempt != latest:  # the same order of checks as ``_retry_source``: a retried run is never the latest attempt
            reason = "not_latest_attempt"
        elif session.execute(select(PlanRunRetry.run_id).where(PlanRunRetry.retry_of_run_id == run.id)).first() is not None:
            reason = "already_retried"
        elif plan.status != "approved" or plan.approved_revision != number or plan.approved_revision != plan.latest_revision:
            reason = "plan_not_approved"
        elif reconcile:
            reason = "reconciliation_required"
        return {"allowed": reason is None, "reason": reason, "uncertain": uncertain, "reconcile": reconcile}

    # ---------------------------------------------------------------------------------------------- reading

    def _tasks(self, session, run: PlanRun) -> tuple:
        rows = session.execute(
            select(PlanTaskRun, PlanTask).join(PlanTask, (PlanTask.revision_id == PlanTaskRun.revision_id) & (PlanTask.task_key == PlanTaskRun.task_key))
            .where(PlanTaskRun.run_id == run.id).order_by(PlanTaskRun.position)).all()
        carried = session.execute(
            select(PlanTaskCarryover, PlanTaskRun, PlanTask, PlanRun.attempt)
            .join(PlanTaskRun, PlanTaskRun.id == PlanTaskCarryover.source_task_run_id)
            .join(PlanTask, (PlanTask.revision_id == PlanTaskRun.revision_id) & (PlanTask.task_key == PlanTaskRun.task_key))
            .join(PlanRun, PlanRun.id == PlanTaskRun.run_id).where(PlanTaskCarryover.run_id == run.id)).all()
        deps: dict = {}
        for d in session.execute(select(PlanTaskDependency).where(PlanTaskDependency.revision_id == run.revision_id).order_by(PlanTaskDependency.task_key, PlanTaskDependency.depends_on_key)).scalars():
            deps.setdefault(d.task_key, []).append(d.depends_on_key)
        from agentplatform.tools import dispatch as dp

        unresolved = dp.unresolved_tasks(session, run.id)  # P0: tasks with a side-effect outcome that is UNKNOWN
        state = {tr.task_key: tr.status for tr, _ in rows}
        state.update({co.task_key: "succeeded" for co, _tr, _t, _a in carried})
        out = []
        for tr, t in rows:
            d = tuple(deps.get(tr.task_key, ()))
            blocked = tuple(k for k in d if state.get(k) != "succeeded") if tr.status == "pending" else ()
            out.append(TaskRunView(tr.task_key, tr.position, tr.level, t.title, t.description, t.expected_output, d, blocked, tr.agent_source, tr.agent_key,
                                   tr.agent_name, tr.agent_version_id, tr.agent_version, tr.status, tr.execution_id, tr.error_code, tr.error_message,
                                   tr.created_at, tr.started_at, tr.completed_at, retry_class="reconcile" if tr.task_key in unresolved and tr.status != "succeeded" else retry_class(tr.status, tr.error_code, tr.execution_id)))
        for co, tr, t, attempt in carried:
            out.append(TaskRunView(tr.task_key, tr.position, tr.level, t.title, t.description, t.expected_output, tuple(deps.get(tr.task_key, ())), (),
                                   tr.agent_source, tr.agent_key, tr.agent_name, tr.agent_version_id, tr.agent_version, "succeeded", tr.execution_id, None, None,
                                   co.created_at, tr.started_at, tr.completed_at, reused=True, reused_run_id=tr.run_id, reused_attempt=attempt))
        return tuple(sorted(out, key=lambda v: v.position))

    def _ownership(self, session, run: PlanRun) -> str | None:
        if run.status != "running":
            return None
        lease = session.execute(select(RunLease.owner_id, RunLease.expires_at).where(RunLease.run_id == run.id)).first()
        if lease is None:
            return "none"
        now = db_now(session, self.leases._clock)
        now = now if isinstance(now, datetime) else session.execute(select(now)).scalar()
        if lease.owner_id == UNCLAIMED or lease.expires_at <= now:
            if any(True for _r in open_suspensions(session, run.id)):
                return "suspended"  # M4-C: no process holds it because a task is waiting for a human approval; any process resumes it
            return "unclaimed" if lease.owner_id == UNCLAIMED else "expired"
        return "live"

    def require_owner(self, scope, run_id) -> None:
        """WRITE authorisation (sprint 5): only the member who started a run may change it (cancel). ``get`` is a READ path open to every member of the workspace and must never be used as a permission check."""
        scope = require_scope(scope)
        with self._sessions() as session:
            if session.execute(select(PlanRun.id).where(PlanRun.id == run_id, *_owned(scope))).first() is None:
                raise RunNotFound("not found")

    def get(self, scope, run_id) -> RunView:
        scope = require_scope(scope)
        with self._sessions() as session:
            run = session.execute(select(PlanRun).where(PlanRun.id == run_id, *_seen(scope))).scalar_one_or_none()
            if run is None:
                raise RunNotFound("not found")  # unknown and foreign ids are indistinguishable
            number = session.execute(select(PlanRevision.revision).where(PlanRevision.id == run.revision_id)).scalar_one()
            tasks = self._tasks(session, run)
            origin = session.execute(select(PlanRun.id, PlanRun.attempt).join(PlanRunRetry, PlanRunRetry.retry_of_run_id == PlanRun.id).where(PlanRunRetry.run_id == run.id)).first()
            later = session.execute(select(PlanRun.id, PlanRun.attempt).join(PlanRunRetry, PlanRunRetry.run_id == PlanRun.id).where(PlanRunRetry.retry_of_run_id == run.id)).first()
            return RunView(run.id, run.plan_id, number, run.attempt, run.status, run.error_code, run.error_message, run.created_at, run.completed_at,
                           tasks, _counts(t.status for t in tasks), {"id": origin[0], "attempt": origin[1]} if origin else None,
                           {"id": later[0], "attempt": later[1]} if later else None, self._ownership(session, run), self._retry_info(session, run, tasks),
                           session.execute(select(User.email).where(User.id == run.user_id)).scalar_one_or_none())

    def list_for_plan(self, scope, plan_id) -> list:
        scope = require_scope(scope)
        with self._sessions() as session:
            plan = session.execute(select(Plan.id).where(Plan.id == plan_id, Plan.workspace_id == scope.workspace_id)).scalar_one_or_none()
            if plan is None:
                raise PlanNotFound("not found")
            runs = session.execute(select(PlanRun, PlanRevision.revision).join(PlanRevision, PlanRevision.id == PlanRun.revision_id)
                                   .where(PlanRun.plan_id == plan_id, *_seen(scope)).order_by(PlanRun.attempt.desc())).all()
            out = []
            for run, number in runs:
                statuses = list(session.execute(select(PlanTaskRun.status).where(PlanTaskRun.run_id == run.id)).scalars())
                statuses += ["succeeded"] * session.execute(select(func.count()).select_from(PlanTaskCarryover).where(PlanTaskCarryover.run_id == run.id)).scalar()
                out.append(RunSummary(run.id, number, run.attempt, run.status, run.error_code, run.created_at, run.completed_at, _counts(statuses)))
            return out

    def task_result(self, scope, execution_id) -> dict | None:
        """The M2-B record behind a task run (result and provenance), read under the same scope."""
        with self._sessions() as session:
            row = session.execute(select(AgentExecution).where(AgentExecution.id == execution_id, AgentExecution.workspace_id == scope.workspace_id)).scalar_one_or_none()  # read: any member of the workspace
            if row is None:
                return None
            return {"execution_id": row.id, "input": row.task, "status": row.status, "result": row.result, "provider": row.provider, "model": row.model,
                    "started_at": row.started_at, "completed_at": row.completed_at}

    # ---------------------------------------------------------------------------------------------- driver

    def load_for_driver(self, run_id) -> tuple:
        """(objective, [DriverTask]) of a run the driver itself created or claimed. Carried-over tasks are included as already
        ``succeeded`` (``carried=True``): they are never dispatched, and their dependants read their persisted results."""
        with self._sessions() as session:
            run = session.execute(select(PlanRun).where(PlanRun.id == run_id)).scalar_one()
            objective = session.execute(select(Plan.objective).where(Plan.id == run.plan_id)).scalar_one()
            deps: dict = {}
            for d in session.execute(select(PlanTaskDependency).where(PlanTaskDependency.revision_id == run.revision_id).order_by(PlanTaskDependency.depends_on_key)).scalars():
                deps.setdefault(d.task_key, []).append(d.depends_on_key)
            rows = session.execute(
                select(PlanTaskRun, PlanTask).join(PlanTask, (PlanTask.revision_id == PlanTaskRun.revision_id) & (PlanTask.task_key == PlanTaskRun.task_key))
                .where(PlanTaskRun.run_id == run_id)).all()
            out = [DriverTask(tr.id, tr.task_key, tr.position, tr.level, t.title, t.description, t.expected_output, tuple(deps.get(tr.task_key, ())),
                              tr.agent_key, tr.agent_version_id, tr.status, required_tool=t.required_tool) for tr, t in rows]
            for co, tr, t in session.execute(
                    select(PlanTaskCarryover, PlanTaskRun, PlanTask).join(PlanTaskRun, PlanTaskRun.id == PlanTaskCarryover.source_task_run_id)
                    .join(PlanTask, (PlanTask.revision_id == PlanTaskRun.revision_id) & (PlanTask.task_key == PlanTaskRun.task_key))
                    .where(PlanTaskCarryover.run_id == run_id)).all():
                out.append(DriverTask(None, tr.task_key, tr.position, tr.level, t.title, t.description, t.expected_output, tuple(deps.get(tr.task_key, ())),
                                      tr.agent_key, tr.agent_version_id, "succeeded", carried=True, required_tool=t.required_tool))
            return objective, sorted(out, key=lambda d: (d.level, d.position))

    def load_results(self, run_id) -> dict:
        """key -> persisted result text of every task of the run that already succeeded (its own, and the carried-over ones): what a
        claimed or retried run feeds to the tasks that depend on them. Read from the execution records, never from memory."""
        with self._sessions() as session:
            own = session.execute(select(PlanTaskRun.task_key, AgentExecution.result).join(AgentExecution, AgentExecution.id == PlanTaskRun.execution_id)
                                  .where(PlanTaskRun.run_id == run_id, PlanTaskRun.status == "succeeded", AgentExecution.status == "succeeded")).all()
            carried = session.execute(select(PlanTaskCarryover.task_key, AgentExecution.result).join(PlanTaskRun, PlanTaskRun.id == PlanTaskCarryover.source_task_run_id)
                                      .join(AgentExecution, AgentExecution.id == PlanTaskRun.execution_id).where(PlanTaskCarryover.run_id == run_id)).all()
            return {key: result or "" for key, result in [*own, *carried]}

    def has_running_task(self, run_id) -> bool:
        """A task that is really IN FLIGHT (its process may have died mid-call). A task durably suspended on a human approval is not in flight."""
        suspended = exists().where(ToolSuspension.execution_id == PlanTaskRun.execution_id, ToolSuspension.resumed_at.is_(None))
        with self._sessions() as session:
            return session.execute(select(PlanTaskRun.id).where(PlanTaskRun.run_id == run_id, PlanTaskRun.status == "running", ~suspended).limit(1)).first() is not None

    # ---------------------------------------------------------------------------------------------- M4-C: durable suspension

    def open_suspension_map(self, run_id) -> dict:
        """task_key -> (approval_id, execution_id) of the tasks of this run that are SUSPENDED on an approval right now."""
        with self._sessions() as session:
            live = {r.task_key for r in session.execute(select(PlanTaskRun.task_key).where(PlanTaskRun.run_id == run_id, PlanTaskRun.status == "running")).all()}
            return {key: (approval_id, execution_id) for approval_id, execution_id, key, _status, _exp in open_suspensions(session, run_id) if key in live}

    def suspension_ready(self, run_id) -> dict:
        """task_key -> (approval_id, execution_id) of the suspended tasks whose approval has an outcome (approved, or refused/expired/invalidated)."""
        with self._sessions() as session:
            now = db_now(session, self.leases._clock)
            now = now if isinstance(now, datetime) else session.execute(select(now)).scalar()
            live = {r.task_key for r in session.execute(select(PlanTaskRun.task_key).where(PlanTaskRun.run_id == run_id, PlanTaskRun.status == "running")).all()}
            ready = ready_suspensions(session, run_id, now)
            by_key = {key: (approval_id, execution_id) for approval_id, execution_id, key, _s, _e in open_suspensions(session, run_id)}
            return {key: by_key[key] for key in ready if key in live}

    def has_open_suspension(self, run_id) -> bool:
        return bool(self.open_suspension_map(run_id))

    def parked_idle(self, run_id) -> bool:
        """True when the run is PARKED and there is nothing to do: some task is suspended on a still-pending approval, no approval has an outcome yet, no task is in
        flight and no pending task could start. Such a run is left alone (no process owns it, none needs to)."""
        if not self.open_suspension_map(run_id):
            return False
        if self.suspension_ready(run_id) or self.has_running_task(run_id):
            return False
        _objective, tasks = self.load_for_driver(run_id)
        status = {t.key: t.status for t in tasks}
        return not any(t.status == "pending" and all(status.get(d) == "succeeded" for d in t.depends_on) for t in tasks)

    def close_inflight(self, run_id, *, fence: Fence | None = None, recovery: bool = False, reason: str | None = None) -> int:
        """Close ONLY the tasks that were really in flight when their process stopped (``failed`` / ``interrupted``; their in-flight tool calls end with them, ``uncertain`` for a
        tool with side effects) and leave everything else alone: tasks suspended on a human approval stay suspended, pending tasks stay pending, and the run stays
        ``running`` for a process to resume. Used when a run that has a valid suspension loses its process (crash or graceful shutdown). Persisted results of finished
        executions are reconciled first. One transaction; events in it."""
        from agentplatform.execution import MESSAGES as EXECUTION_MESSAGES

        with self._sessions() as session, session.begin():
            if fence is not None:
                self.leases.touch(session, fence)
            now = datetime.utcnow()
            scope = run_events.run_scope(session, run_id)
            actor = self._actor(fence, recovery)
            message = lambda code: MESSAGES.get(code) or EXECUTION_MESSAGES.get(code)  # noqa: E731
            self._reconcile(session, run_id, scope, actor, now, message)
            suspended = exists().where(ToolSuspension.execution_id == PlanTaskRun.execution_id, ToolSuspension.resumed_at.is_(None))
            open_tasks = session.execute(select(PlanTaskRun.id, PlanTaskRun.task_key, PlanTaskRun.execution_id).where(PlanTaskRun.run_id == run_id, PlanTaskRun.status == "running", ~suspended)).all()
            for task_id, key, execution_id in open_tasks:
                session.execute(update(PlanTaskRun).where(PlanTaskRun.id == task_id, PlanTaskRun.status == "running")
                                .values(status="failed", completed_at=now, error_code="interrupted", error_message=message("interrupted")))
                if execution_id is not None:
                    close_tool_calls(session, [execution_id], now)
                    session.execute(update(AgentExecution).where(AgentExecution.id == execution_id, AgentExecution.status == "running")
                                    .values(status="failed", error_code="interrupted", completed_at=now, error_message=EXECUTION_MESSAGES["interrupted"]))
                self._event(session, run_id, scope, "task_interrupted", actor, task_key=key, execution_id=execution_id, code="interrupted")
            if recovery and open_tasks:
                self._event(session, run_id, scope, "recovery_performed", actor, code="interrupted", reason=reason or "lease_expired", count=len(open_tasks))
            return len(open_tasks)

    def start_task(self, task_run_id, scope, resolved, name: str, task: str, provider: str | None, model: str | None, *, executions, fence: Fence) -> uuid.UUID:
        """The ATOMIC task start: in ONE fenced transaction create the ``running`` execution record (database-wide admission, owner row) and
        move the task run ``pending -> running`` linked to it. Either both exist or neither does, so a task and its execution record can never
        disagree because of the order of two commits. Raises ``Busy`` (no slot), ``LeaseLost`` (ownership lost) or ``RuntimeError`` (the task
        is not pending): nothing is written then and the agent is never called."""
        now = datetime.utcnow()
        with self._sessions() as session, session.begin():
            self.leases.touch(session, fence)
            execution_id = executions.insert(session, scope, resolved, name, task, provider=provider, model=model)
            done = session.execute(update(PlanTaskRun).where(PlanTaskRun.id == task_run_id, PlanTaskRun.status == "pending")
                                   .values(status="running", execution_id=execution_id, started_at=now))
            if done.rowcount != 1:
                raise RuntimeError("task run is not pending")
            key = session.execute(select(PlanTaskRun.task_key).where(PlanTaskRun.id == task_run_id)).scalar_one()
            self._event(session, fence.run_id, (scope.user_id, scope.workspace_id), "task_started", self._actor(fence), task_key=key, execution_id=execution_id)
            return execution_id

    def finish_task_execution(self, task_run_id, execution_id, exec_status: str, task_status: str, task_code: str | None, *, fence: Fence,
                              result: str | None = None, error_code: str | None = None, provider: str | None = None, model: str | None = None) -> bool:
        """The ATOMIC task finish: in ONE fenced transaction finish the execution record (``running -> exec_status``) AND the task run
        (``running -> task_status``). Nothing is written (and ``LeaseLost`` is raised) when ownership was lost, so a stale owner can neither
        complete a task nor leave the two disagreeing; both then stay ``running`` for lease recovery. False when the execution was already final."""
        from agentplatform.execution import MESSAGES as EXECUTION_MESSAGES, final_values

        with self._sessions() as session, session.begin():
            self.leases.touch(session, fence)
            done = session.execute(update(AgentExecution).where(AgentExecution.id == execution_id, AgentExecution.status == "running")
                                   .values(**final_values(exec_status, result=result, error_code=error_code, provider=provider, model=model)))
            if done.rowcount != 1:
                return False
            message = (MESSAGES.get(task_code) or EXECUTION_MESSAGES.get(task_code) or MESSAGES["internal_error"]) if task_code else None
            moved = session.execute(update(PlanTaskRun).where(PlanTaskRun.id == task_run_id, PlanTaskRun.status == "running")
                                    .values(status=task_status, completed_at=datetime.utcnow(), error_code=task_code, error_message=message))
            if moved.rowcount != 1:
                raise RuntimeError("task run is not running")  # rolls the execution update back too: never half done
            tr = session.execute(select(PlanTaskRun.run_id, PlanTaskRun.task_key).where(PlanTaskRun.id == task_run_id)).one()
            scope = run_events.run_scope(session, tr.run_id)
            self._event(session, tr.run_id, scope, _event_for(task_status, task_code), self._actor(fence), task_key=tr.task_key, execution_id=execution_id, code=task_code)
            return True

    def mark_running(self, task_run_id, execution_id, *, fence: Fence | None = None) -> None:
        """pending -> running, linking the M2-B execution record, as ONE compare-and-set: a second dispatch of the same task
        run matches no row and raises (so a task can never be started twice). With a ``fence`` the caller must still own the run
        (``LeaseLost`` otherwise, and the agent is then never called). (The runtime uses ``start_task``, which also creates the
        execution in the same transaction; this stays for callers that already hold an execution.)"""
        now = datetime.utcnow()
        with self._sessions() as session, session.begin():
            if fence is not None:
                self.leases.touch(session, fence)
            done = session.execute(update(PlanTaskRun).where(PlanTaskRun.id == task_run_id, PlanTaskRun.status == "pending")
                                   .values(status="running", execution_id=execution_id, started_at=now))
            if done.rowcount != 1:
                raise RuntimeError("task run is not pending")
            tr = session.execute(select(PlanTaskRun.run_id, PlanTaskRun.task_key).where(PlanTaskRun.id == task_run_id)).one()
            self._event(session, tr.run_id, run_events.run_scope(session, tr.run_id), "task_started", self._actor(fence), task_key=tr.task_key, execution_id=execution_id)

    def finish_task(self, task_run_id, status: str, error_code: str | None = None, *, allowed=("pending", "running"), fence: Fence | None = None) -> bool:
        """A single transition out of pending/running. False if the row had already moved on (never overwritten)."""
        now = datetime.utcnow()
        values = {"status": status, "completed_at": now, "error_code": error_code, "error_message": MESSAGES.get(error_code) if error_code else None}
        if error_code and error_code not in MESSAGES:
            from agentplatform.execution import MESSAGES as EXECUTION_MESSAGES

            values["error_message"] = EXECUTION_MESSAGES.get(error_code, MESSAGES["internal_error"])
        legal = {"succeeded": ("running",), "failed": ("pending", "running"), "skipped": ("pending",), "cancelled": ("pending", "running")}[status]
        with self._sessions() as session, session.begin():
            if fence is not None:
                self.leases.touch(session, fence)
            done = session.execute(update(PlanTaskRun).where(PlanTaskRun.id == task_run_id, PlanTaskRun.status.in_(tuple(s for s in allowed if s in legal))).values(**values))
            if done.rowcount == 1:
                tr = session.execute(select(PlanTaskRun.run_id, PlanTaskRun.task_key, PlanTaskRun.execution_id).where(PlanTaskRun.id == task_run_id)).one()
                self._event(session, tr.run_id, run_events.run_scope(session, tr.run_id), _event_for(status, error_code), self._actor(fence), task_key=tr.task_key,
                            execution_id=tr.execution_id, code=error_code)
            return done.rowcount == 1

    def finish_run(self, run_id, status: str, error_code: str | None = None, *, fence: Fence | None = None) -> bool:
        values = {"status": status, "completed_at": datetime.utcnow(), "error_code": error_code, "error_message": MESSAGES.get(error_code) if error_code else None}
        with self._sessions() as session, session.begin():
            if fence is not None:
                self.leases.touch(session, fence)
            done = session.execute(update(PlanRun).where(PlanRun.id == run_id, PlanRun.status == "running").values(**values))
            if done.rowcount == 1:
                self._event(session, run_id, run_events.run_scope(session, run_id), "run_completed", self._actor(fence), status=status, code=error_code)
                observability.inc("runs_finished_total", status=status, code=error_code or "none")
            return done.rowcount == 1

    def task_statuses(self, run_id) -> list:
        with self._sessions() as session:
            statuses = list(session.execute(select(PlanTaskRun.status).where(PlanTaskRun.run_id == run_id)).scalars())
            return statuses + ["succeeded"] * session.execute(select(func.count()).select_from(PlanTaskCarryover).where(PlanTaskCarryover.run_id == run_id)).scalar()

    def close_unfinished(self, run_id, task_status_running: str, running_code: str, pending_code: str, *, fence: Fence | None = None,
                         reconcile: bool = False, recovery: bool = False, reason: str | None = None) -> str:
        """Close everything still open in a run (cancellation, shutdown, recovery, unexpected error) in ONE transaction and
        return the run's final status: running tasks -> ``task_status_running`` (failed|cancelled), pending tasks -> skipped
        (``pending_code``) or cancelled. The M2-B execution records of the tasks it closes are closed with them (they would
        otherwise stay ``running`` forever). With a ``fence`` the caller must still own the run. ``reconcile=True`` (recovery)
        first turns a running task whose linked execution already ENDED into that outcome (succeeded -> succeeded, failed ->
        failed with its code): persisted work is kept, never re-run and never reported as interrupted. Every task and run change
        writes its event in this transaction (``recovery=True`` records the actor as the recovery process and adds ``recovery_performed``)."""
        from agentplatform.execution import MESSAGES as EXECUTION_MESSAGES

        def message(code):
            return MESSAGES.get(code) or EXECUTION_MESSAGES.get(code)

        with self._sessions() as session, session.begin():
            if fence is not None:
                self.leases.touch(session, fence)
            now = datetime.utcnow()
            scope = run_events.run_scope(session, run_id)
            actor = self._actor(fence, recovery)
            reconciled = 0
            if reconcile:
                reconciled = self._reconcile(session, run_id, scope, actor, now, message)
            open_tasks = session.execute(select(PlanTaskRun.task_key, PlanTaskRun.execution_id).where(PlanTaskRun.run_id == run_id, PlanTaskRun.status == "running")).all()
            pending_keys = list(session.execute(select(PlanTaskRun.task_key).where(PlanTaskRun.run_id == run_id, PlanTaskRun.status == "pending")).scalars())
            session.execute(update(PlanTaskRun).where(PlanTaskRun.run_id == run_id, PlanTaskRun.status == "running")
                            .values(status=task_status_running, completed_at=now, error_code=running_code, error_message=message(running_code)))
            open_executions = [execution_id for _key, execution_id in open_tasks if execution_id is not None]
            if open_executions:
                close_tool_calls(session, open_executions, now)  # an in-flight tool call ends with its execution
                cancelled = running_code == "cancelled"
                session.execute(update(AgentExecution).where(AgentExecution.id.in_(open_executions), AgentExecution.status == "running").values(
                    status="cancelled" if cancelled else "failed", error_code="cancelled" if cancelled else "interrupted", completed_at=now,
                    error_message=EXECUTION_MESSAGES["cancelled" if cancelled else "interrupted"]))
            for key, execution_id in open_tasks:
                self._event(session, run_id, scope, _event_for(task_status_running, running_code), actor, task_key=key, execution_id=execution_id, code=running_code)
            if pending_code == "cancelled":
                session.execute(update(PlanTaskRun).where(PlanTaskRun.run_id == run_id, PlanTaskRun.status == "pending")
                                .values(status="cancelled", completed_at=now, error_code="cancelled", error_message=message("cancelled")))
            else:
                session.execute(update(PlanTaskRun).where(PlanTaskRun.run_id == run_id, PlanTaskRun.status == "pending")
                                .values(status="skipped", completed_at=now, error_code=pending_code, error_message=message(pending_code)))
            for key in pending_keys:
                self._event(session, run_id, scope, "task_cancelled" if pending_code == "cancelled" else "task_skipped", actor, task_key=key, code=pending_code)
            statuses = list(session.execute(select(PlanTaskRun.status).where(PlanTaskRun.run_id == run_id)).scalars())
            statuses += ["succeeded"] * session.execute(select(func.count()).select_from(PlanTaskCarryover).where(PlanTaskCarryover.run_id == run_id)).scalar()
            status = final_status(statuses, cancelled=running_code == "cancelled")
            closed = session.execute(update(PlanRun).where(PlanRun.id == run_id, PlanRun.status == "running")
                                     .values(status=status, completed_at=now, error_code=running_code, error_message=message(running_code)))
            if closed.rowcount == 1:
                if recovery:
                    self._event(session, run_id, scope, "recovery_performed", actor, status=status, code=running_code, reconciled=reconciled, reason=reason)
                if running_code == "cancelled":
                    self._event(session, run_id, scope, "cancellation_acknowledged", actor)
                self._event(session, run_id, scope, "run_completed", actor, status=status, code=running_code)
            return status

    def _reconcile(self, session, run_id, scope, actor, now, message) -> int:
        rows = session.execute(
            select(PlanTaskRun.id, PlanTaskRun.task_key, PlanTaskRun.execution_id, AgentExecution.status, AgentExecution.error_code, AgentExecution.completed_at)
            .join(AgentExecution, AgentExecution.id == PlanTaskRun.execution_id)
            .where(PlanTaskRun.run_id == run_id, PlanTaskRun.status == "running", AgentExecution.status.in_(("succeeded", "failed")))).all()
        for task_id, key, execution_id, status, code, completed in rows:
            if status == "succeeded":
                values = {"status": "succeeded", "completed_at": completed or now, "error_code": None, "error_message": None}
            else:
                code = code or "internal_error"
                values = {"status": "failed", "completed_at": completed or now, "error_code": code, "error_message": message(code) or MESSAGES["internal_error"]}
            session.execute(update(PlanTaskRun).where(PlanTaskRun.id == task_id, PlanTaskRun.status == "running").values(**values))
            self._event(session, run_id, scope, _event_for(values["status"], values["error_code"]), actor, task_key=key, execution_id=execution_id, code=values["error_code"], reason="reconciled")
        return len(rows)

    # ---------------------------------------------------------------------------------------------- recovery and claiming

    def claim_or_recover(self, run_id, *, resume: bool = False, cancel: bool = False, include_unclaimed: bool | None = None):
        """Take over ONE run whose lease is expired or missing (a compare-and-set: concurrent callers get it exactly once) and decide, from
        persisted state only:

        * ``("resume", fence)``: ``resume=True``, no task is in flight and no cancellation was requested. Nothing was dispatched that
          could be uncertain, so the caller (a process that executes runs) continues the run: pending tasks are dispatched for the FIRST
          time, finished tasks are kept. The lease stays with the caller.
        * ``("closed", status)``: otherwise the run is closed exactly as lease recovery always did (reconcile finished executions, running
          tasks ``failed``/``interrupted``, unstarted tasks ``skipped``, or ``cancelled`` for a cancellation) and the lease is released.
        * ``None``: the lease is live, the run is not running, or another process won the race.

        A run is never re-dispatched here: a task that was ``running`` is closed, never started again."""
        include = resume if include_unclaimed is None else include_unclaimed
        if not self.leases.expired_run_ids(1, run_id, include_unclaimed=include):
            return None  # read-only first: a live owner is never even written to
        if not cancel and not self.leases.cancel_requested(run_id):
            if self.parked_idle(run_id):
                return None  # PARKED on a human approval and nothing to do: leave it (no process needs to own it)
            if not resume and self.has_open_suspension(run_id):
                return None  # a valid suspension is only resumed by a process that executes runs; a sweep that cannot execute must not fail the task
        before = self.leases.snapshot(run_id)
        fence = self.leases.takeover(run_id, self.owner_id, self.config.ttl_seconds)
        if fence is None:
            return None
        try:
            cancelled = cancel or self.leases.cancel_requested(run_id)
            if resume and not cancelled and self.has_open_suspension(run_id):
                # M4-C: a valid human-approval suspension survives the loss of its process. Only what was really in flight is closed (never re-dispatched);
                # the suspended task is resumed from its persisted conversation by this process.
                self.close_inflight(run_id, fence=fence, recovery=True, reason="lease_expired")
                with self._sessions() as session, session.begin():
                    self.leases.touch(session, fence)
                    unclaimed = before is not None and before["owner_id"] == UNCLAIMED
                    self._event(session, run_id, run_events.run_scope(session, run_id), "run_claimed" if unclaimed else "run_resumed", self._actor(fence),
                                generation=fence.generation, resumed=not unclaimed)
                return ("resume", fence)
            if resume and not cancelled and not self.has_running_task(run_id):
                with self._sessions() as session, session.begin():
                    self.leases.touch(session, fence)
                    unclaimed = before is not None and before["owner_id"] == UNCLAIMED
                    self._event(session, run_id, run_events.run_scope(session, run_id), "run_claimed" if unclaimed else "run_resumed", self._actor(fence),
                                generation=fence.generation, resumed=not unclaimed)
                return ("resume", fence)
            code = "cancelled" if cancelled else "interrupted"
            status = self.close_unfinished(run_id, "cancelled" if cancelled else "failed", code, code, fence=fence, reconcile=True, recovery=True,
                                           reason="cancel_requested" if cancelled else "lease_expired")
            self._release_quietly(fence)  # nothing of ours can write any more
            return ("closed", status)
        except LeaseLost:
            return None
        except Exception:
            self._release_quietly(fence)
            raise

    def _release_quietly(self, fence) -> None:
        try:
            self.leases.release(fence)
        except Exception as exc:  # noqa: BLE001 - the lease simply expires
            logger.warning("Could not release the lease of run %s (%s)", fence.run_id, type(exc).__name__)

    def recover_run(self, run_id, cancel: bool = False) -> str | None:
        """Take over ONE run whose lease is expired or missing and close it (None: the lease is live or another process won).
        The take-over is a compare-and-set, so concurrent sweepers recover a run exactly once. Nothing is dispatched: running
        tasks whose execution ended are reconciled, the rest end ``failed``/``interrupted``, unstarted tasks ``skipped``; a
        persisted cancellation request makes it ``cancelled`` instead. (``cancel=True``: the caller is a user who asked for it;
        it also closes a run that was recorded for a worker and never claimed.)"""
        outcome = self.claim_or_recover(run_id, resume=False, cancel=cancel, include_unclaimed=cancel)
        return None if outcome is None else outcome[1]

    def recover_expired(self, limit: int = 50, include_unclaimed: bool = False) -> list:
        """The recovery sweep (startup and periodic): every running run whose lease is expired or missing (runs recorded for a worker
        and not yet claimed are left alone unless ``include_unclaimed``). Idempotent; a failure on one run (e.g. a database error) is
        logged and does not stop the others. Returns the ids it closed."""
        closed = []
        for run_id in self.leases.expired_run_ids(limit, include_unclaimed=include_unclaimed):
            try:
                if self.recover_run(run_id, cancel=False) is not None:
                    closed.append(run_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("Recovery of plan run %s failed (%s)", run_id, type(exc).__name__)
        return closed


def _event_for(task_status: str, code: str | None) -> str:
    """The event type that describes a task reaching ``task_status`` with ``code``."""
    if task_status == "succeeded":
        return "task_succeeded"
    if task_status == "failed":
        return "task_interrupted" if code == "interrupted" else "task_failed"
    if task_status == "skipped":
        return "task_skipped"
    return "task_cancelled"


# ------------------------------------------------------------------------------------------------ registry

class DriveContext:
    """The state a live driver shares with the registry and its heartbeat: its fence, WHY it is being stopped
    (``cancel`` = a persisted cancellation request or an in-process cancel, ``shutdown`` = graceful stop of this process,
    ``lost`` = ownership was lost or could not be kept: write nothing) and its scheduler task."""

    def __init__(self, fence: Fence):
        self.fence = fence
        self.reason: str | None = None
        self.driver: asyncio.Future | None = None

    @property
    def stopping(self) -> bool:
        return self.reason is not None

    def stop(self, reason: str) -> None:
        if self.reason is None:  # the first reason wins
            self.reason = reason
        if self.driver is not None and not self.driver.done():
            self.driver.cancel()


class RunRegistry:
    """The live drivers of this process (the in-process scheduler) and its identity: ``owner_id`` is what this process writes
    into the leases it holds. One per app, shared by all requests; a second ``RunRegistry`` (another process, or a test) is a
    different owner."""

    def __init__(self, owner_id: str | None = None, config: LeaseConfig | None = None, clock=datetime.utcnow, *, embedded: bool = True, kind: str = "api"):
        self.owner_id = owner_id or new_owner_id()
        self.config = config or LeaseConfig()
        self.clock = clock
        self.embedded = embedded  # True: this process drives the runs it creates (M3-B/M4-A1). False (api-only): it only records them for a worker
        self.kind = kind  # "api" | "worker": the source recorded in events
        self.closing = False  # set by ``shutdown``: no new run is accepted
        self._tasks: dict = {}
        self._contexts: dict = {}

    def launch(self, run_id, coroutine, context: DriveContext | None = None) -> asyncio.Task:
        task = asyncio.ensure_future(coroutine)
        self._tasks[run_id] = task
        if context is not None:
            self._contexts[run_id] = context
        task.add_done_callback(lambda t, rid=run_id: self._forget(rid, t))
        return task

    def _forget(self, run_id, task) -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)
            self._contexts.pop(run_id, None)

    def get(self, run_id):
        return self._tasks.get(run_id)

    def active(self) -> int:
        return len(self._tasks)

    async def cancel(self, run_id, wait: float = CANCEL_WAIT_SECONDS) -> bool:
        """Cancel a run THIS process drives (False if it does not drive it)."""
        task = self._tasks.get(run_id)
        if task is None:
            return False
        context = self._contexts.get(run_id)
        if context is not None:
            context.stop("cancel")
        else:
            task.cancel()
        await asyncio.wait({task}, timeout=wait)
        return True

    async def wait(self, run_id, timeout: float | None = None) -> None:
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.wait({task}, timeout=timeout)

    async def shutdown(self, grace: float | None = None) -> list:
        """Graceful stop: accept no new run, stop every driver (no further dispatch, running tasks are cancelled and recorded
        ``interrupted``) and wait up to ``grace`` seconds. Returns the runs whose driver did NOT stop in time: their leases are
        deliberately NOT released (a driver that can still write must keep owning the run); they expire and are recovered."""
        self.closing = True
        tasks = dict(self._tasks)
        for run_id, task in tasks.items():
            context = self._contexts.get(run_id)
            if context is not None:
                context.stop("shutdown")
            else:
                task.cancel()
        if tasks:
            await asyncio.wait(set(tasks.values()), timeout=self.config.shutdown_grace_seconds if grace is None else grace)
        return [run_id for run_id, task in tasks.items() if not task.done()]


async def run_recovery_loop(store: PlanRunStore, config: LeaseConfig, *, executions=None, resumer=None) -> None:
    """Periodic recovery of this process: every ``sweep_seconds`` close the running runs whose lease expired and (when ``executions`` is given)
    the standalone executions whose process is gone. The caller does the first pass before serving. Never raises (a database error is
    logged and the next pass retries)."""
    while True:
        await asyncio.sleep(config.sweep_seconds)
        try:
            closed = await asyncio.to_thread(store.recover_expired)
            if closed:
                logger.warning("Recovered %d plan run(s) whose owner stopped renewing its lease", len(closed))
            if resumer is not None:  # M4-C: a process that executes runs resumes tasks whose human approval has an outcome (a worker does the same on its own poll)
                await resumer.claim_work(resume=True)
            if executions is not None:
                orphans = await asyncio.to_thread(executions.fail_orphaned)
                if orphans:
                    logger.warning("Closed %d execution(s) whose process stopped", orphans)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Plan-run recovery pass failed (%s)", type(exc).__name__)


def compose_input(objective: str, task: DriverTask, titles: dict, results: dict) -> str:
    """What the agent receives: the (redacted) objective, its own task, and the results of the tasks it depends on. Only text
    from this run is used; results are bounded and stripped of control characters; the total never exceeds the task limit."""
    head = [f"Overall objective: {objective}", f"Your task: {task.title}", task.description, f"Expected output: {task.expected_output}"]
    if task.required_tool:
        head.append(f"Required action: you must call the tool {task.required_tool} yourself; describing it is not doing it. If it needs a person's approval the system pauses it for you, so do not ask for permission in your answer.")
    base = "\n\n".join(head)
    parts = [base]
    if task.depends_on:
        parts.append("Results of the tasks you depend on:")
        room = max(200, min(DEPENDENCY_RESULT_CHARS, (MAX_TASK_CHARS - len(base) - 120) // len(task.depends_on)))
        for key in task.depends_on:
            text = _CONTROL.sub("", results.get(key, ""))
            parts.append(f"[{titles[key]}]\n{text if len(text) <= room else text[: room - 1] + '…'}")
    text = "\n\n".join(parts)
    return text if len(text) <= MAX_TASK_CHARS else text[: MAX_TASK_CHARS - 1] + "…"


class PlanRunService:
    def __init__(self, session_factory, *, executions: ExecutionService, registry: RunRegistry, max_parallel: int = MAX_PARALLEL,
                 busy_poll: float = BUSY_POLL_SECONDS, busy_budget: float = BUSY_BUDGET_SECONDS):
        self.store = PlanRunStore(session_factory, owner_id=registry.owner_id, config=registry.config, clock=registry.clock, kind=registry.kind)
        self._sessions = session_factory
        self.plans = PlanStore(session_factory)
        self.executions = executions
        self.registry = registry
        self.max_parallel, self.busy_poll, self.busy_budget = max_parallel, busy_poll, busy_budget

    async def detail(self, scope, run_id) -> tuple:
        """(RunView, outputs): the final outputs of a finished run are the results of its succeeded LEAF tasks (tasks nothing
        else depends on), each read from its M2-B execution record, so every output is traceable to an execution."""
        scope = require_scope(scope)
        view = await asyncio.to_thread(self.store.get, scope, run_id)
        outputs = []
        if view.status in ("succeeded", "partial"):
            needed = {d for t in view.tasks for d in t.depends_on}
            for t in view.tasks:
                if t.key not in needed and t.status == "succeeded" and t.execution_id is not None:
                    record = await asyncio.to_thread(self.store.task_result, scope, t.execution_id)
                    if record and record["result"] is not None:
                        text = record["result"]
                        outputs.append({"task": t.key, "title": t.title, "execution_id": t.execution_id,
                                        "result": text if len(text) <= OUTPUT_PREVIEW_CHARS else text[: OUTPUT_PREVIEW_CHARS - 1] + "…"})
        return view, outputs

    async def task_detail(self, scope, run_id, key: str) -> tuple:
        scope = require_scope(scope)
        view = await asyncio.to_thread(self.store.get, scope, run_id)
        task = next((t for t in view.tasks if t.key == key), None)
        if task is None:
            raise TaskRunNotFound("not found")
        record = await asyncio.to_thread(self.store.task_result, scope, task.execution_id) if task.execution_id is not None else None
        return view, task, record

    # ---------------------------------------------------------------------------------------------- start / cancel

    async def start(self, scope, plan_id) -> RunView:
        """Start the APPROVED revision of a plan. The revision, owner and agent versions are never taken from the client."""
        scope = require_scope(scope)
        if self.registry.closing:
            raise ShuttingDown("shutting down")  # a stopping process accepts no new run work
        await asyncio.to_thread(self._validate_gate, scope, plan_id)
        run_id = await asyncio.to_thread(self.store.create, scope, plan_id, claim=self.registry.embedded)
        self._launch(run_id, scope)
        return await asyncio.to_thread(self.store.get, scope, run_id)

    async def retry(self, scope, run_id, acknowledge=()) -> RunView:
        """An EXPLICIT retry attempt of a run that ended without success (partial, failed or cancelled). The tasks that already succeeded are
        reused (their persisted results feed their dependants; they are never dispatched again); every other task runs again, pinned to the
        same approved revision and agent versions. A task whose previous outcome is uncertain (see ``retry_class``) is retried only if it is
        listed in ``acknowledge``; otherwise ``UncertainOutcome`` names it. The authorisation, the acknowledgements and the link to the
        attempt being retried are recorded with the new attempt. At most one retry of a run exists, and concurrent retries create one."""
        scope = require_scope(scope)
        if self.registry.closing:
            raise ShuttingDown("shutting down")
        preview = await asyncio.to_thread(self.store.retry_preview, scope, run_id, tuple(acknowledge))
        await asyncio.to_thread(self._validate_gate, scope, preview["plan_id"], {key for key, _agent, _pin in preview["retried"]})
        new_id = await asyncio.to_thread(self.store.create, scope, preview["plan_id"], retry_of=run_id, acknowledged=tuple(acknowledge), claim=self.registry.embedded)
        self._launch(new_id, scope)
        return await asyncio.to_thread(self.store.get, scope, new_id)

    def _launch(self, run_id, scope) -> None:
        """Embedded mode: drive the run this process just created (its lease, generation 1, is ours). API-only mode: the run was recorded as
        UNCLAIMED and a worker claims it."""
        if not self.registry.embedded:
            return
        fence = Fence(run_id, self.store.owner_id, 1)
        context = DriveContext(fence)
        self.registry.launch(run_id, self._drive(run_id, scope, fence, context), context)

    async def claim_work(self, *, resume: bool, limit: int = 20) -> dict:
        """One claiming/recovery pass of THIS process. Expired or unclaimed runs are taken over exactly once (compare-and-set on the lease).
        With ``resume=True`` (a worker) a run with no task in flight is continued from its persisted state: finished tasks are kept and the
        pending ones are dispatched for the first time. Everything else is closed by recovery (an in-flight task ends ``failed`` /
        ``interrupted``, never re-dispatched). Returns {"resumed": n, "closed": n}. Never raises for one run's failure."""
        out = {"resumed": 0, "closed": 0}
        if self.registry.closing:
            return out
        ids = await asyncio.to_thread(self.store.leases.expired_run_ids, limit, None, resume)
        for run_id in ids:
            try:
                claim = await asyncio.to_thread(self.store.claim_or_recover, run_id, resume=resume)
                if claim is None:
                    continue
                if claim[0] == "closed":
                    out["closed"] += 1
                    continue
                fence = claim[1]
                scope = await asyncio.to_thread(self._run_scope, run_id)
                if scope is None:  # the owner of the run is no longer authorised: close it instead of executing for them
                    await asyncio.to_thread(self.store.close_unfinished, run_id, "failed", "interrupted", "interrupted", fence=fence, recovery=True, reason="authorization_unavailable")
                    await asyncio.to_thread(self.store.leases.release, fence)
                    out["closed"] += 1
                    continue
                context = DriveContext(fence)
                self.registry.launch(run_id, self._drive(run_id, scope, fence, context), context)
                out["resumed"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("Claiming plan run %s failed (%s)", run_id, type(exc).__name__)
        return out

    def _run_scope(self, run_id):
        """The scope a claimed run executes under, re-authorised NOW from the database (membership may have been revoked since the run
        was created); None if it cannot be issued."""
        from sqlalchemy import select as _select

        from authz.errors import AuthorizationError, ScopeError
        from authz.workspaces import authorize_run_scope

        with self._sessions() as session:
            row = session.execute(_select(PlanRun.user_id, PlanRun.workspace_id).where(PlanRun.id == run_id)).one()
        try:
            return authorize_run_scope(self._sessions, row.user_id, row.workspace_id)
        except (AuthorizationError, ScopeError):
            return None

    def _validate_gate(self, scope, plan_id, only: set | None = None) -> None:
        """Pre-checks with precise errors (the transaction in ``store.create`` re-checks the approval itself): status, DAG,
        and that every assigned agent is still available AT ITS APPROVED VERSION (for a retry: every task that runs again)."""
        view = self.plans.get(scope, plan_id)  # PlanNotFound for unknown and foreign plans
        if view.status == "cancelled":
            raise PlanCancelledForRun("cancelled")
        if view.status != "approved" or view.approved_revision is None:
            raise PlanNotApproved("not approved")
        if view.approved_revision != view.latest_revision:
            raise PlanStale("stale")
        revision = self.plans.get_revision(scope, plan_id, view.approved_revision)
        errors = [{"code": e.code, "task": e.task, "message": e.message} for e in validate_dag([TaskNode(t.key, tuple(t.depends_on)) for t in revision.tasks]).errors]
        for t in revision.tasks:
            if only is not None and t.key not in only:
                continue
            if self.executions.agents.resolve_pinned(scope, t.agent_key, t.agent_version_id) is None:
                errors.append({"code": "agent_unavailable", "task": t.key,
                               "message": "The agent assigned to this task, or the exact version approved in the plan, is no longer available."})
        if errors:
            raise RunInvalid(sorted(errors, key=lambda e: (e["code"], e["task"] or "")))

    async def cancel(self, scope, run_id) -> RunView:
        """Cancel a run. The request is PERSISTED first (any process may issue it); the process that owns the run honours it
        within one heartbeat, and the process that serves this call cancels at once when it is the owner. If nobody owns the run
        any more (the lease expired) it is taken over and closed as cancelled."""
        scope = require_scope(scope)
        await asyncio.to_thread(self.store.require_owner, scope, run_id)  # not `get`: reading is open to the workspace, cancelling is not
        view = await asyncio.to_thread(self.store.get, scope, run_id)
        if view.status == "running":
            await asyncio.to_thread(self.store.leases.request_cancel, run_id, scope.user_id, self.registry.kind)
            if not await self.registry.cancel(run_id):
                await self._await_remote_cancel(scope, run_id)
            view = await asyncio.to_thread(self.store.get, scope, run_id)
        return view

    async def _await_remote_cancel(self, scope, run_id) -> None:
        """Another process (or nobody) drives the run: give its owner one heartbeat (plus margin) to honour the persisted
        request, taking the run over if the lease is, or becomes, expired."""
        deadline = time.monotonic() + self.registry.config.heartbeat_seconds * 1.5 + 1
        while True:
            await asyncio.to_thread(self.store.recover_run, run_id, True)
            if (await asyncio.to_thread(self.store.get, scope, run_id)).status != "running" or time.monotonic() >= deadline:
                return
            await asyncio.sleep(min(0.25, self.registry.config.heartbeat_seconds / 4))

    # ---------------------------------------------------------------------------------------------- scheduler

    async def _drive(self, run_id, scope, fence: Fence, context: DriveContext) -> None:
        """Supervisor of one run: it starts the scheduler (``_schedule``) and the lease heartbeat, waits for the scheduler,
        then settles the run according to WHY it ended and releases the lease. The lease is released only after the
        scheduler has finished, i.e. when this driver can no longer write."""
        observability.set_context(workspace=scope.workspace_id, run=run_id)
        context.driver = asyncio.ensure_future(self._schedule(run_id, scope, fence, context))
        beat = asyncio.ensure_future(self._heartbeat(fence, context))
        if context.stopping:  # stopped before it even started
            context.driver.cancel()
        outer_cancelled = False
        try:
            await asyncio.wait({context.driver})
        except asyncio.CancelledError:  # this task itself was cancelled (the event loop is going down): treat it as a shutdown
            outer_cancelled = True
            context.stop("shutdown")
            await asyncio.wait({context.driver}, timeout=self.registry.config.shutdown_grace_seconds)
        if context.driver.done():
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
            self._settle(run_id, fence, context)
            self._release(fence)
        # else (this task was cancelled and the driver ignored the stop): the heartbeat is NOT cancelled, so the lease stays ours
        # for as long as the driver can still write; the heartbeat ends by itself when the driver does, and the lease then expires
        if outer_cancelled:
            raise asyncio.CancelledError

    def _settle(self, run_id, fence: Fence, context: DriveContext) -> None:
        """Write the run's final state after the scheduler has ended. Synchronous on purpose: this may run while the event loop
        is shutting down. Ownership lost -> write nothing."""
        driver = context.driver
        try:
            if driver.cancelled():
                reason = context.reason or "shutdown"
                if reason == "cancel":
                    self.store.close_unfinished(run_id, "cancelled", "cancelled", "cancelled", fence=fence)
                elif reason == "shutdown":
                    if self.store.has_open_suspension(run_id):
                        self.store.close_inflight(run_id, fence=fence, reason="shutdown")  # M4-C: suspended tasks stay suspended; another process resumes the run
                    else:
                        self.store.close_unfinished(run_id, "failed", "interrupted", "interrupted", fence=fence)
                return
            exc = driver.exception()
            if exc is None or isinstance(exc, LeaseLost):
                return  # finished normally (the scheduler recorded the final status) or lost ownership (never write)
            logger.error("Plan run %s failed in the scheduler (%s)", run_id, type(exc).__name__)
            self.store.close_unfinished(run_id, "failed", "internal_error", "internal_error", fence=fence)
        except LeaseLost:
            logger.warning("Plan run %s was taken over before its final state could be written", run_id)
        except Exception as exc:  # noqa: BLE001 - e.g. the database is down: the lease expires and recovery closes the run
            logger.error("Could not record the final state of plan run %s (%s); recovery will close it", run_id, type(exc).__name__)

    def _release(self, fence: Fence) -> None:
        try:
            self.store.leases.release(fence)
        except Exception as exc:  # noqa: BLE001 - the lease simply expires
            logger.warning("Could not release the lease of run %s (%s)", fence.run_id, type(exc).__name__)

    async def _heartbeat(self, fence: Fence, context: DriveContext) -> None:
        """Keep the lease alive and observe persisted cancellation. If ownership is lost, or cannot be renewed for a whole
        lease term (the database is unreachable), the scheduler is stopped WITHOUT writing: another process may take over."""
        config = self.registry.config
        last_ok = time.monotonic()
        while True:
            await asyncio.sleep(config.heartbeat_seconds)
            if context.driver is not None and context.driver.done():
                return  # nothing can write any more; the lease is released by the supervisor, or simply expires
            sent = time.monotonic()
            job = asyncio.ensure_future(asyncio.to_thread(self.store.leases.renew, fence, config.ttl_seconds))
            job.add_done_callback(lambda j: None if j.cancelled() else j.exception())  # a failure after a cancel is not an unretrieved error
            try:
                try:
                    renewal = await asyncio.shield(job)
                except asyncio.CancelledError:
                    # Cancelled while a renewal is already running in its thread: let it finish (bounded) BEFORE the supervisor releases the
                    # lease, so a renewal that was in flight can never commit after the release and extend a released lease.
                    await asyncio.wait({job}, timeout=5.0)
                    raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Lease renewal failed for run %s (%s)", fence.run_id, type(exc).__name__)
                if time.monotonic() - last_ok >= config.ttl_seconds:
                    context.stop("lost")
                    return
                continue
            if not renewal.owned:
                context.stop("lost")
                return
            last_ok = sent  # the lease runs at least until ``sent + ttl``
            if renewal.cancel_requested:
                context.stop("cancel")
                return

    async def _schedule(self, run_id, scope, fence: Fence, context: DriveContext) -> None:
        """The scheduler for one run: skip tasks whose prerequisites did not succeed, start the ready ones in
        (level, position) order up to ``max_parallel`` (and the shared Limits), and finish the run when nothing is left.
        This is the only writer of the run's task states (fenced), so decisions are made from its own in-memory view."""
        running: dict = {}
        try:
            objective, tasks = await asyncio.to_thread(self.store.load_for_driver, run_id)
            by_key = {t.key: t for t in tasks}
            titles = {t.key: t.title for t in tasks}
            state = {t.key: t.status for t in tasks}  # carried-over tasks are already "succeeded"
            for key in await asyncio.to_thread(self.store.open_suspension_map, run_id):  # M4-C: tasks suspended on a human approval
                if state.get(key) == "running":
                    state[key] = "suspended"
            results: dict = await asyncio.to_thread(self.store.load_results, run_id)  # persisted results of finished and carried-over tasks
            busy_since: dict = {}
            retry_at: dict = {}
            while True:
                if context.stopping:
                    raise asyncio.CancelledError  # a stop was observed: nothing new starts
                for t in tasks:  # a failed, skipped or cancelled prerequisite means the task is never launched
                    if state[t.key] == "pending" and any(state[d] in ("failed", "skipped", "cancelled") for d in t.depends_on):
                        await self._record(self.store.finish_task, t.id, "skipped", "dependency_failed", fence=fence)
                        state[t.key] = "skipped"
                if any(v == "suspended" for v in state.values()):  # a decided (or expired) approval lets exactly ITS task resume from the persisted conversation
                    ready = await asyncio.to_thread(self.store.suspension_ready, run_id)
                    for t in tasks:
                        if len(running) >= self.max_parallel or context.stopping:
                            break
                        if state[t.key] == "suspended" and t.key in ready:
                            state[t.key] = "resuming"
                            running[asyncio.ensure_future(self._resume_task(scope, run_id, objective, t, titles, results, fence, context, *ready[t.key]))] = t
                now = time.monotonic()
                for t in tasks:
                    if len(running) >= self.max_parallel or context.stopping:
                        break
                    if state[t.key] != "pending" or retry_at.get(t.key, 0) > now or any(state[d] != "succeeded" for d in t.depends_on):
                        continue
                    state[t.key] = "starting"
                    job = asyncio.ensure_future(self._run_task(scope, run_id, objective, t, titles, results, fence, context))
                    running[job] = t
                if not running:
                    runnable = any(state[t.key] == "pending" and all(state[d] == "succeeded" for d in t.depends_on) for t in tasks)
                    if not runnable and any(v == "suspended" for v in state.values()):
                        return  # PARKED (M4-C): nothing is running and nothing can start; the only open work waits for a human. The run stays ``running`` with no owner and any
                        # process resumes it after the decision. The lease is released by the supervisor; nothing here is held in memory.
                    if not any(s == "pending" for s in state.values()):
                        break
                    await asyncio.sleep(self.busy_poll)  # everything pending is waiting for a free execution slot
                    continue
                wait_for = self.busy_poll if any(state[k] in ("pending", "suspended") for k in state) else None
                done, _ = await asyncio.wait(set(running), timeout=wait_for, return_when=asyncio.FIRST_COMPLETED)
                for job in done:
                    t = running.pop(job)
                    status, code, text = job.result()
                    if status == "busy":
                        busy_since.setdefault(t.key, time.monotonic())
                        if time.monotonic() - busy_since[t.key] > self.busy_budget:
                            await self._record(self.store.finish_task, t.id, "failed", "busy", fence=fence)
                            state[t.key] = "failed"
                        else:
                            state[t.key] = "pending"
                            retry_at[t.key] = time.monotonic() + self.busy_poll
                    else:
                        state[t.key] = status
                        if status == "succeeded":
                            results[t.key] = text
            statuses = await asyncio.to_thread(self.store.task_statuses, run_id)
            await self._record(self.store.finish_run, run_id, final_status(statuses), fence=fence)
        except BaseException:
            for job in running:  # whatever ended the scheduler (a stop, a lost lease, an error): its tasks end with it
                job.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            raise

    async def _record(self, fn, *args, **kwargs):
        """A state write that completes even if a cancellation arrives while it is in flight."""
        job = asyncio.ensure_future(asyncio.to_thread(lambda: fn(*args, **kwargs)))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            await job
            raise

    def _finalizer(self, t: DriverTask, fence: Fence, context: DriveContext):
        """The ``finalize`` hook for one task: finish its execution record AND the task run in one fenced transaction. A cancelled call
        becomes ``cancelled`` for a cancellation, ``failed``/``interrupted`` for a shutdown, and for a lost lease NOTHING is written (both
        records stay ``running`` and recovery closes them together)."""

        def finalize(execution_id, status, *, result=None, error_code=None, provider=None, model=None) -> bool:
            if status == "succeeded":
                task_status, task_code = "succeeded", None
            elif status == "failed":
                task_status, task_code = "failed", error_code
            else:
                if context.reason == "lost":
                    raise LeaseLost("ownership of the run was lost")
                task_status, task_code = ("failed", "interrupted") if context.reason == "shutdown" else ("cancelled", "cancelled")
            done = self.store.finish_task_execution(t.id, execution_id, status, task_status, task_code, fence=fence, result=result, error_code=error_code,
                                                    provider=provider, model=model)
            if not done and status == "succeeded":
                raise RuntimeError("the execution record was already closed: its result is not adopted")
            if done:
                observability.inc("tasks_finished_total", status=task_status, code=task_code or "none")
            return done

        return finalize

    async def _run_task(self, scope, run_id, objective: str, t: DriverTask, titles: dict, results: dict, fence: Fence, context: DriveContext) -> tuple:
        """Run one task through the M2-B runtime. Returns (status, error_code, result_text); status ``busy`` means no slot
        was free and nothing was recorded (the task stays pending). The task run and its execution record start together and finish
        together (``start_task`` / ``finish_task_execution``: one fenced transaction each), so they can never disagree, a stale owner's
        result is never propagated, and a result is only used after it is committed."""
        observability.set_context(workspace=scope.workspace_id, run=run_id, task=t.key)
        text = compose_input(objective, t, titles, results)
        started: dict = {}

        def starter(resolved, name, task_text, provider, model):
            started["id"] = self.store.start_task(t.id, scope, resolved, name, task_text, provider, model, executions=self.executions.store, fence=fence)
            return started["id"]

        try:
            outcome = await self.executions.run(scope, t.agent_key, text, pin=t.agent_version_id, starter=starter, finalize=self._finalizer(t, fence, context),
                                                tool_run=ToolRunContext(run_id, t.key, fence, self.registry.kind, t.required_tool))
        except Busy:
            return "busy", None, ""
        except TaskSuspended:
            return "suspended", None, ""  # M4-C: durably suspended on a human approval; the task and its execution stay ``running``, nothing is held in memory
        except asyncio.CancelledError:
            raise  # the execution and the task were finished together by the runtime (or neither was started); the supervisor closes the rest
        except ExecutionFailed as exc:
            return "failed", exc.code, ""  # already recorded together with its execution
        except AgentUnavailable:
            await self._record(self.store.finish_task, t.id, "failed", "agent_unavailable", fence=fence)
            return "failed", "agent_unavailable", ""
        except ExecutionRefused:
            await self._record(self.store.finish_task, t.id, "failed", "task_invalid", fence=fence)
            return "failed", "task_invalid", ""
        except LeaseLost:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Task %s of run %s failed (%s)", t.key, run_id, type(exc).__name__)
            if "id" in started:  # the execution exists: finish both together
                closed = await self._record(self.store.finish_task_execution, t.id, started["id"], "failed", "failed", "internal_error", fence=fence, error_code="internal_error")
                if not closed:  # its execution was already final: still end the task honestly
                    await self._record(self.store.finish_task, t.id, "failed", "internal_error", fence=fence)
            else:
                await self._record(self.store.finish_task, t.id, "failed", "internal_error", fence=fence)
            return "failed", "internal_error", ""
        return "succeeded", None, outcome.result

    async def _resume_task(self, scope, run_id, objective: str, t: DriverTask, titles: dict, results: dict, fence: Fence, context: DriveContext, approval_id, execution_id) -> tuple:
        """Resume ONE suspended task after its approval has an outcome (M4-C), in this process. Returns (status, error_code, result_text) like ``_run_task``; ``suspended`` means it
        stays (or is again) suspended. The execution was started by the process that suspended the task: here it is only continued and finished, in the same fenced
        transaction as the task run."""
        try:
            outcome = await self.executions.resume(scope, t.agent_key, pin=t.agent_version_id, execution_id=execution_id, approval_id=approval_id, finalize=self._finalizer(t, fence, context),
                                                   tool_run=ToolRunContext(run_id, t.key, fence, self.registry.kind, t.required_tool))
        except Busy:
            await asyncio.sleep(self.busy_poll)  # no slot now: it stays suspended and is tried again
            return "suspended", None, ""
        except TaskSuspended:
            return "suspended", None, ""  # it asked for another sensitive action: suspended again on a NEW approval
        except asyncio.CancelledError:
            raise
        except ExecutionFailed as exc:
            return "failed", exc.code, ""  # already recorded together with its execution
        except AgentUnavailable:
            await self._record(self.store.finish_task_execution, t.id, execution_id, "failed", "failed", "agent_unavailable", fence=fence, error_code="agent_unavailable")
            return "failed", "agent_unavailable", ""
        except LeaseLost:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Resuming task %s of run %s failed (%s)", t.key, run_id, type(exc).__name__)
            closed = await self._record(self.store.finish_task_execution, t.id, execution_id, "failed", "failed", "internal_error", fence=fence, error_code="internal_error")
            if not closed:
                await self._record(self.store.finish_task, t.id, "failed", "internal_error", fence=fence)
            return "failed", "internal_error", ""
        return "succeeded", None, outcome.result
