"""Human approval of ONE exact tool invocation (M4-C), built on the M4-B invocation layer, with DURABLE suspension of the waiting task.

WHAT AN APPROVAL IS. A durable row (``tool_approvals``) that binds a human decision to exactly one action: the workspace and user, the execution
(and the plan run and task), the agent and its exact version, the exact tool id and version, the VALIDATED canonical arguments and a ``payload_hash``
(sha256 of the canonical envelope of all of them). It authorises that invocation and nothing else: not another argument, another tool version,
another step, another run, and never a later call of the same tool. Plan approval is a different thing and never substitutes for it.

TRUST. The server decides everything. A hash sent by the browser is only a STALENESS check ("approve what I was shown"): the server recomputes the
hash from the stored binding and compares it with the stored one, and when the action is finally consumed it recomputes the hash from the
parameters that are ABOUT TO RUN and compares again. Any difference invalidates the approval. Only a workspace OWNER or ADMIN may decide (D050).

LIFECYCLE (triggers enforce it, migration 0012). pending -> approved | rejected | expired | invalidated; approved -> consumed | expired | invalidated;
everything else is final. ``consumed`` is written in the SAME transaction as the invocation's intent row (``consume_in``). Transitions are
conditional UPDATEs (compare-and-swap): of N concurrent attempts exactly one wins. Every transition writes its event into the run history in the same
transaction (``approval_requested`` .. ``approval_consumed``, ``task_suspended``, ``task_resumed``), only when the transition really happened.

DURABLE SUSPENSION (migration 0013). A plan task that reaches a gated call does not wait in memory: ``request(..., suspension=...)`` persists the approval AND the
redacted conversation so far (``tool_suspensions``) in one transaction, the task's process lets go of it, and ANY process can resume the exact call after the
human decides (``claim``/resume in ``plan_runs``). ``resumed_at`` is written in the same transaction as the consumption (or the recorded refusal), so a suspension is
open until its outcome is durable. A process that dies while the run is parked, or even mid-flight, does NOT invalidate a valid pending approval. What stays
fail-closed: an execution that is really closed (cancelled, failed, interrupted) invalidates its open approvals (``invalidate_open``); and an approved action
is dispatched at most once because consumption is atomic.

NOT CLAIMED: exactly-once external effects. The guarantee is that an approved action is dispatched at most once by this system; a tool that takes effect and
then loses the process before recording is closed ``uncertain`` (M4-B) and is never re-run."""

import observability
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, select, update

from agentplatform import run_events
from agentplatform.schema import redact_secrets
from agentplatform.tools import transcript as tx
from memory.models import Notification, ToolApproval, ToolSuspension, WorkspaceMembership

DEFAULT_TTL_SECONDS = 900.0  # PROPOSED default (D046): how long a human has to decide
MAX_REASON_CHARS = 500
PAGE_MAX = 100
OPEN = ("pending", "approved")
APPROVER_ROLES = ("owner", "admin")  # D050: who may decide; a plain member can see requests but not decide them


class ApprovalNotFound(Exception):
    pass


class ApprovalConflict(Exception):
    """The approval is not in a state that allows this transition (already decided, expired, invalidated or consumed)."""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


class ApprovalStale(Exception):
    """The caller's hash is not the approval's hash: they were shown something else."""


class ApprovalForbidden(Exception):
    """The caller is a member of the workspace but not an owner or admin."""


class TaskSuspended(Exception):
    """Raised by the tool loop when a plan task has been durably suspended on an approval: it is NOT a failure. The task and its execution stay ``running``;
    the process lets go of the task and any process resumes it after the decision."""

    def __init__(self, approval_id):
        super().__init__(str(approval_id))
        self.approval_id = approval_id


def canonical(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(*, workspace_id, user_id, execution_id, seq: int, run_id, task_key, agent_key: str, agent_version_id, tool_id: str, tool_version: int, args_hash: str) -> str:
    """Deterministic SHA-256 of the canonical envelope of everything an approval authorises. Pure; the server recomputes it, never trusts a client's."""
    envelope = {"v": 1, "workspace": str(workspace_id), "user": str(user_id), "execution": str(execution_id), "seq": seq, "run": str(run_id) if run_id else None,
                "task": task_key, "agent": agent_key, "agent_version": str(agent_version_id) if agent_version_id else None, "tool": tool_id, "tool_version": tool_version,
                "args_hash": args_hash}
    return hashlib.sha256(canonical(envelope).encode()).hexdigest()


def _row_hash(row: ToolApproval) -> str:
    return payload_hash(workspace_id=row.workspace_id, user_id=row.user_id, execution_id=row.execution_id, seq=row.seq, run_id=row.run_id, task_key=row.task_key,
                        agent_key=row.agent_key, agent_version_id=row.agent_version_id, tool_id=row.tool_id, tool_version=row.tool_version, args_hash=row.args_hash)


@dataclass(frozen=True)
class ApprovalView:
    id: object
    execution_id: object
    run_id: object | None
    task_key: str | None
    agent_key: str
    agent_version_id: object | None
    tool_id: str
    tool_version: int
    seq: int
    risk: str
    side_effect: str
    args: dict | None
    args_hash: str
    payload_hash: str
    reason: str | None
    status: str
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    decided_by: object | None
    consumed_at: datetime | None
    invocation_id: object | None
    suspended: bool = False  # the task is durably suspended on this approval right now


@dataclass(frozen=True)
class SuspensionView:
    approval_id: object
    execution_id: object
    run_id: object
    task_key: str
    seq: int
    round_number: int
    call_index: int
    task: str
    messages: tuple
    calls: tuple


def _loads(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _view(row: ToolApproval, suspended: bool = False) -> ApprovalView:
    return ApprovalView(row.id, row.execution_id, row.run_id, row.task_key, row.agent_key, row.agent_version_id, row.tool_id, row.tool_version, row.seq, row.risk, row.side_effect,
                        _loads(row.args_json), row.args_hash, row.payload_hash, row.reason, row.status, row.created_at, row.expires_at, row.decided_at, row.decided_by, row.consumed_at,
                        row.invocation_id, suspended)


# ---------------------------------------------------------------------------------------------- events (same transaction as the transition)

def _emit(session, row, event_type: str, actor: tuple, *, policy: str | None = None, **detail) -> None:
    """The approval lifecycle in the run history. Skipped for approvals without a run (a standalone execution has no run history). Values are never included:
    the event carries the exact tool ref, the step, the approval id and the policy fingerprint only."""
    if row.run_id is None:
        return
    run_events.add(session, row.run_id, event_type, actor_type=actor[0], actor_id=actor[1], task_key=row.task_key, execution_id=row.execution_id,
                   tool=f"{row.tool_id}@{row.tool_version}", seq=row.seq, approval=str(row.id), policy=policy, **detail)


def _worker_actor(run) -> tuple:
    """(actor type, actor id) of the process driving a task: its kind (api|worker) and its owner id (never exposed by the API)."""
    if run is None:
        return ("api", None)
    return (getattr(run, "kind", "worker"), run.fence.owner_id if run.fence is not None else None)


def _expire_due(session, where, now) -> int:
    """Close the open approvals matching ``where`` whose window has passed. One ``approval_expired`` event per approval that REALLY changed."""
    rows = session.execute(select(ToolApproval).where(where, ToolApproval.status.in_(OPEN), ToolApproval.expires_at <= now)).scalars().all()
    n = 0
    for row in rows:
        done = session.execute(update(ToolApproval).where(ToolApproval.id == row.id, ToolApproval.status.in_(OPEN), ToolApproval.expires_at <= now).values(status="expired", decided_at=now))
        if done.rowcount == 1:
            _emit(session, row, "approval_expired", ("api", None))
            n += 1
    return n


def _close_suspension(session, approval_id, now, actor: tuple, outcome: str) -> bool:
    """Mark the suspension of this approval resumed (exactly once) and record ``task_resumed`` in the same transaction. False when none was open."""
    susp = session.execute(select(ToolSuspension).where(ToolSuspension.approval_id == approval_id, ToolSuspension.resumed_at.is_(None))).scalar_one_or_none()
    if susp is None:
        return False
    done = session.execute(update(ToolSuspension).where(ToolSuspension.approval_id == approval_id, ToolSuspension.resumed_at.is_(None)).values(resumed_at=now))
    if done.rowcount != 1:
        return False
    row = session.execute(select(ToolApproval).where(ToolApproval.id == approval_id)).scalar_one()
    _emit(session, row, "task_resumed", actor, status=outcome)
    return True


def close_refused(session, approval_id, now, run, outcome: str) -> None:
    """A refusal after a suspension (rejected, expired, invalid): the suspension is closed in the transaction that records the refusal."""
    _close_suspension(session, approval_id, now, _worker_actor(run), outcome)


def consume_in(session, approval_id, *, scope, execution_id, seq: int, run_id, task_key, agent_key: str, agent_version_id, tool_id: str, tool_version: int,
               args_hash: str, invocation_id, now: datetime, run=None) -> str | None:
    """Inside the caller's transaction: authorise exactly this invocation. Returns None when consumed, else the reason it cannot be (the approval is
    invalidated when its binding does not match what is about to run). Recomputes the hash from the LIVE parameters, never from the approval."""
    row = session.execute(select(ToolApproval).where(ToolApproval.id == approval_id, ToolApproval.workspace_id == scope.workspace_id)).scalar_one_or_none()
    if row is None:
        return "invalid"
    live = payload_hash(workspace_id=scope.workspace_id, user_id=scope.user_id, execution_id=execution_id, seq=seq, run_id=run_id, task_key=task_key, agent_key=agent_key,
                        agent_version_id=agent_version_id, tool_id=tool_id, tool_version=tool_version, args_hash=args_hash)
    actor = _worker_actor(run)
    if live != row.payload_hash or live != _row_hash(row):
        done = session.execute(update(ToolApproval).where(ToolApproval.id == approval_id, ToolApproval.status.in_(OPEN)).values(status="invalidated", decided_at=now))
        if done.rowcount == 1:
            _emit(session, row, "approval_invalidated", actor, reason="mismatch")
        return "invalid"
    done = session.execute(update(ToolApproval).where(ToolApproval.id == approval_id, ToolApproval.status == "approved", ToolApproval.expires_at > now)
                           .values(status="consumed", consumed_at=now, invocation_id=invocation_id))
    if done.rowcount == 1:
        _emit(session, row, "approval_consumed", actor)
        _close_suspension(session, approval_id, now, actor, "approved")
        return None
    _expire_due(session, ToolApproval.id == approval_id, now)
    state = session.execute(select(ToolApproval.status).where(ToolApproval.id == approval_id)).scalar_one()
    return "expired" if state == "expired" else "invalid" if state in ("invalidated", "consumed") else state


def invalidate_open(session, execution_ids, now: datetime) -> int:
    """An execution is really being closed (cancelled, failed, interrupted): the approvals nobody can act on any more are closed ``invalidated``."""
    ids = list(execution_ids)
    if not ids:
        return 0
    n = 0
    for row in session.execute(select(ToolApproval).where(ToolApproval.execution_id.in_(ids), ToolApproval.status.in_(OPEN))).scalars().all():
        if session.execute(update(ToolApproval).where(ToolApproval.id == row.id, ToolApproval.status.in_(OPEN)).values(status="invalidated", decided_at=now)).rowcount == 1:
            _emit(session, row, "approval_invalidated", ("recovery", None), reason="closed")
            n += 1
    return n


# ---------------------------------------------------------------------------------------------- suspension queries (used by the run driver and recovery)

def open_suspensions(session, run_id) -> list:
    """[(approval_id, execution_id, task_key, approval status, expires_at)] of the run's OPEN suspensions (the task is waiting for this approval)."""
    rows = session.execute(select(ToolSuspension.approval_id, ToolSuspension.execution_id, ToolSuspension.task_key, ToolApproval.status, ToolApproval.expires_at)
                           .join(ToolApproval, ToolApproval.id == ToolSuspension.approval_id)
                           .where(ToolSuspension.run_id == run_id, ToolSuspension.resumed_at.is_(None))).all()
    return [tuple(r) for r in rows]


def ready_suspensions(session, run_id, now: datetime) -> dict:
    """task_key -> approval_id of the open suspensions whose approval has an outcome that lets the task proceed (approved, or refused/expired/invalidated, which the
    resumed task is told). A still-pending approval inside its window stays suspended."""
    ready = {}
    for approval_id, _execution, key, status, expires_at in open_suspensions(session, run_id):
        if status != "pending" or expires_at <= now:
            ready[key] = approval_id
    return ready


class ApprovalStore:
    def __init__(self, session_factory, *, ttl_seconds: float = DEFAULT_TTL_SECONDS, clock=datetime.utcnow, policy_fingerprint: str | None = None):
        self._sessions, self.ttl_seconds, self._now = session_factory, ttl_seconds, clock
        self.policy_fingerprint = policy_fingerprint  # recorded in the ``approval_requested`` event: a change of the allowlist is visible in the history

    def request(self, ctx, spec, seq: int, args_json: str, args_hash: str, reason: str | None, suspension: dict | None = None) -> uuid.UUID:
        """Create the pending approval for this exact step. ``args_json`` is the canonical VALIDATED arguments (redacted for storage). With ``suspension``
        (``task``, ``messages``, ``calls``, ``round_number``, ``call_index``) the task is durably suspended in the SAME transaction (plan tasks only)."""
        scope, now = ctx.scope, self._now()
        digest = payload_hash(workspace_id=scope.workspace_id, user_id=scope.user_id, execution_id=ctx.execution_id, seq=seq, run_id=ctx.run.run_id if ctx.run else None,
                              task_key=ctx.run.task_key if ctx.run else None, agent_key=ctx.agent_key, agent_version_id=ctx.agent_version_id, tool_id=spec.id,
                              tool_version=spec.version, args_hash=args_hash)
        row = ToolApproval(user_id=scope.user_id, workspace_id=scope.workspace_id, execution_id=ctx.execution_id, run_id=ctx.run.run_id if ctx.run else None,
                           task_key=ctx.run.task_key if ctx.run else None, agent_key=ctx.agent_key, agent_version_id=ctx.agent_version_id, tool_id=spec.id,
                           tool_version=spec.version, seq=seq, risk=spec.risk, side_effect=spec.side_effect, args_json=redact_secrets(args_json), args_hash=args_hash,
                           payload_hash=digest, reason=redact_secrets(reason.strip())[:MAX_REASON_CHARS] if reason and reason.strip() else None, status="pending",
                           created_at=now, expires_at=now + timedelta(seconds=self.ttl_seconds))
        if suspension is not None and ctx.run is None:
            raise ValueError("only a plan task can be suspended")
        with self._sessions() as session, session.begin():
            if ctx.run is not None and ctx.run.fence is not None:
                from agentplatform.run_leases import LeaseStore

                LeaseStore(self._sessions).touch(session, ctx.run.fence)  # a stale owner cannot open an approval request
            session.add(row)
            session.flush()
            actor = _worker_actor(ctx.run)
            _emit(session, row, "approval_requested", actor, policy=self.policy_fingerprint)
            # M6: the workspace's current owners and admins are told, in THIS transaction (the notification exists iff the request does); recipients are chosen here, server-side,
            # and UNIQUE (user_id, kind, ref_id) means a retry or a recovery can never notify the same person twice about the same approval.
            for user_id in session.execute(select(WorkspaceMembership.user_id).where(WorkspaceMembership.workspace_id == scope.workspace_id, WorkspaceMembership.role.in_(APPROVER_ROLES))).scalars().all():
                session.add(Notification(workspace_id=scope.workspace_id, user_id=user_id, kind="approval_requested", ref_id=row.id, run_id=row.run_id, created_at=now))
            if suspension is not None:
                session.add(ToolSuspension(approval_id=row.id, execution_id=ctx.execution_id, run_id=ctx.run.run_id, task_key=ctx.run.task_key, seq=seq,
                                           round_number=suspension["round_number"], call_index=suspension["call_index"],
                                           transcript=tx.dump(suspension["task"], suspension["messages"]), calls=tx.dump_calls(suspension["calls"]), created_at=now))
                session.flush()
                _emit(session, row, "task_suspended", actor)
            return row.id

    def load_suspension(self, scope, approval_id) -> SuspensionView:
        """The persisted conversation of an OPEN suspension of the caller's workspace (strictly parsed; a corrupt one raises ``TranscriptError``)."""
        with self._sessions() as session:
            susp = session.execute(select(ToolSuspension).join(ToolApproval, ToolApproval.id == ToolSuspension.approval_id)
                                   .where(ToolSuspension.approval_id == approval_id, ToolApproval.workspace_id == scope.workspace_id)).scalar_one_or_none()
            if susp is None:
                raise ApprovalNotFound("not found")
            task, messages = tx.load(susp.transcript)
            return SuspensionView(susp.approval_id, susp.execution_id, susp.run_id, susp.task_key, susp.seq, susp.round_number, susp.call_index, task, messages, tx.load_calls(susp.calls))

    def status(self, approval_id) -> str:
        """The current state (lazily expiring). Used by the waiting task; cheap."""
        now = self._now()
        with self._sessions() as session, session.begin():
            _expire_due(session, ToolApproval.id == approval_id, now)
            return session.execute(select(ToolApproval.status).where(ToolApproval.id == approval_id)).scalar_one()

    def invalidate(self, approval_id, reason: str = "cancelled") -> bool:
        """The waiting task went away (cancelled): nothing is waiting for this approval any more."""
        with self._sessions() as session, session.begin():
            row = session.execute(select(ToolApproval).where(ToolApproval.id == approval_id, ToolApproval.status.in_(OPEN))).scalar_one_or_none()
            if row is None:
                return False
            done = session.execute(update(ToolApproval).where(ToolApproval.id == approval_id, ToolApproval.status.in_(OPEN)).values(status="invalidated", decided_at=self._now())).rowcount == 1
            if done:
                _emit(session, row, "approval_invalidated", ("api", None), reason=reason)
            return done

    def can_decide(self, scope) -> bool:
        """May this member decide approvals of the workspace? (D050: owners and admins.)"""
        with self._sessions() as session:
            role = session.execute(select(WorkspaceMembership.role).where(WorkspaceMembership.workspace_id == scope.workspace_id, WorkspaceMembership.user_id == scope.user_id)).scalar_one_or_none()
            return role in APPROVER_ROLES

    def decide(self, scope, approval_id, decision: str, submitted_hash: str | None = None) -> ApprovalView:
        """A human decision on ONE approval of the caller's workspace, by an owner or admin. Compare-and-swap: of concurrent decisions exactly one wins."""
        if decision not in ("approve", "reject"):
            raise ValueError("invalid_decision")
        now, target = self._now(), "approved" if decision == "approve" else "rejected"
        observability.set_context(workspace=scope.workspace_id, approval=approval_id)
        with self._sessions() as session, session.begin():
            row = session.execute(select(ToolApproval).where(ToolApproval.id == approval_id, ToolApproval.workspace_id == scope.workspace_id)).scalar_one_or_none()
            if row is None:
                raise ApprovalNotFound("not found")
            role = session.execute(select(WorkspaceMembership.role).where(WorkspaceMembership.workspace_id == scope.workspace_id, WorkspaceMembership.user_id == scope.user_id)).scalar_one_or_none()
            if role not in APPROVER_ROLES:
                raise ApprovalForbidden("approver_role_required")  # authorisation is checked BEFORE the state, so a member learns nothing about it
            _expire_due(session, ToolApproval.id == approval_id, now)
            session.refresh(row)
            if row.status != "pending":
                raise ApprovalConflict(row.status)
            if _row_hash(row) != row.payload_hash:  # the stored binding no longer matches its own hash: never authorise it
                if session.execute(update(ToolApproval).where(ToolApproval.id == approval_id, ToolApproval.status == "pending").values(status="invalidated", decided_at=now)).rowcount == 1:
                    _emit(session, row, "approval_invalidated", ("api", None), reason="mismatch")
                raise ApprovalConflict("invalidated")
            if submitted_hash is not None and submitted_hash != row.payload_hash:
                raise ApprovalStale("stale")
            done = session.execute(update(ToolApproval).where(ToolApproval.id == approval_id, ToolApproval.status == "pending", ToolApproval.expires_at > now)
                                   .values(status=target, decided_at=now, decided_by=scope.user_id))
            if done.rowcount != 1:
                raise ApprovalConflict(session.execute(select(ToolApproval.status).where(ToolApproval.id == approval_id)).scalar_one())
            _emit(session, row, "approval_approved" if target == "approved" else "approval_rejected", ("user", str(scope.user_id)))
            session.refresh(row)
            observability.inc("approval_decisions_total", decision=target)
            observability.observe("approval_wait_seconds", max(0.0, (now - row.created_at).total_seconds()))
            return _view(row)

    def _suspended_ids(self, session, ids) -> set:
        if not ids:
            return set()
        return set(session.execute(select(ToolSuspension.approval_id).where(ToolSuspension.approval_id.in_(ids), ToolSuspension.resumed_at.is_(None))).scalars())

    def get(self, scope, approval_id) -> ApprovalView:
        now = self._now()
        with self._sessions() as session, session.begin():
            _expire_due(session, and_(ToolApproval.id == approval_id, ToolApproval.workspace_id == scope.workspace_id), now)
            row = session.execute(select(ToolApproval).where(ToolApproval.id == approval_id, ToolApproval.workspace_id == scope.workspace_id)).scalar_one_or_none()
            if row is None:
                raise ApprovalNotFound("not found")
            return _view(row, row.id in self._suspended_ids(session, [row.id]))

    def list(self, scope, *, status: str | None = None, run_id=None, limit: int = 50) -> list:
        if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
            raise ValueError("invalid_limit")
        now = self._now()
        with self._sessions() as session, session.begin():
            _expire_due(session, ToolApproval.workspace_id == scope.workspace_id, now)
            query = select(ToolApproval).where(ToolApproval.workspace_id == scope.workspace_id)
            if status is not None:
                query = query.where(ToolApproval.status == status)
            if run_id is not None:
                query = query.where(ToolApproval.run_id == run_id)
            rows = session.execute(query.order_by(ToolApproval.created_at.desc(), ToolApproval.id.desc()).limit(limit)).scalars().all()
            suspended = self._suspended_ids(session, [r.id for r in rows])
            return [_view(r, r.id in suspended) for r in rows]

    def pending_count(self, scope) -> int:
        from sqlalchemy import func

        now = self._now()
        with self._sessions() as session, session.begin():
            _expire_due(session, ToolApproval.workspace_id == scope.workspace_id, now)
            return session.execute(select(func.count()).select_from(ToolApproval).where(ToolApproval.workspace_id == scope.workspace_id, ToolApproval.status == "pending")).scalar_one()


__all__ = ["ApprovalStore", "ApprovalView", "SuspensionView", "ApprovalNotFound", "ApprovalConflict", "ApprovalStale", "ApprovalForbidden", "TaskSuspended", "consume_in",
           "close_refused", "invalidate_open", "open_suspensions", "ready_suspensions", "payload_hash", "DEFAULT_TTL_SECONDS", "APPROVER_ROLES"]
