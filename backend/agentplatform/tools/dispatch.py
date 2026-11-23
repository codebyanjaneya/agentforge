"""The durable dispatch protocol for side-effecting tools (P0 safety sprint). It answers one question for every external action: was it SENT, and do we KNOW what happened?

STATE MACHINE (``tool_dispatches``, one row per invocation of a tool whose side effect is not ``none``; the database triggers allow exactly these transitions):

    authorized ──► dispatching ──► succeeded            CONFIRMED success
        │               ├─────────► failed               CONFIRMED failure (the tool raised ``DefinitiveFailure``: it proved nothing was done)
        │               └─────────► unknown ──► reconciled_succeeded | reconciled_failed   (only through an audited ``tool_reconciliations`` record)
        └─► cancelled                                    never sent

* ``authorized`` is written in the SAME transaction that consumes the approval and writes the invocation intent: nothing has been sent.
* ``dispatching`` is committed BEFORE the handler runs (and so before any byte leaves). If the process dies from here on, recovery can only say ``unknown``.
* Anything that is not a proven outcome after ``dispatching`` is ``unknown``: a timeout, a cancellation, an unexpected exception, a crash, a lost lease, a failed local write. ``unknown`` is
  never a failure and is NEVER dispatched again automatically.

IDENTITY. ``operation_key`` identifies the LOGICAL operation across attempts and executions: for a plan task it is (workspace, plan, task, tool ref, hash of the canonical validated arguments), so a
retry of the same task asks for the same operation; for a standalone execution it is (execution, step, ...). ``generation`` counts dispatches of that operation; ``idempotency_key`` (what a remote
service receives) is derived from both. A new key is only ever issued after a CONFIRMED-not-performed outcome, never to get around an uncertain one.

THE GATE (``decide``), evaluated under the invocation's own write transaction before a dispatch is authorised, over every dispatch of the same tool in the same task lineage (same plan and task; or the
same execution when there is no plan): an earlier dispatch still ``authorized``/``dispatching`` -> refuse ``operation_in_flight``; any ``unknown`` -> refuse ``outcome_unknown`` (reconcile first, whatever the
arguments); the same operation already CONFIRMED (``succeeded`` or ``reconciled_succeeded``) -> REPLAY the recorded result without dispatching; the same operation ``failed`` -> refuse unless the tool's
``retry_policy`` allows an automatic retry after a confirmed failure; ``reconciled_failed`` / ``cancelled`` -> a new generation may be authorised (a gated tool still needs a fresh exact-action approval).
(operation_key, generation) is UNIQUE, so two concurrent authorisations cannot both win.

NOT CLAIMED: exactly-once effects. For a tool whose remote service honours the idempotency key a repeat is harmless on the remote side; for any other tool the guarantee is AT MOST ONE automatic
dispatch per operation, with unknown outcomes surfaced for reconciliation rather than retried.
"""

import observability
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text, update

from agentplatform import run_events
from memory.models import PlanRun, ToolDispatch, ToolInvocation, ToolReconciliation, User, WorkspaceMembership

logger = logging.getLogger("agentforge.dispatch")

RECONCILER_ROLES = ("owner", "admin")  # who may reconcile: separate from approving (D050) even though the roles coincide today; a future change to one must not change the other
IN_FLIGHT = ("authorized", "dispatching")
CONFIRMED_OK = ("succeeded", "reconciled_succeeded")
MAX_EVIDENCE = 500


class DispatchRefused(Exception):
    """The gate refused to authorise a dispatch; ``code`` is one of the fixed tool error codes."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class DefinitiveFailure(Exception):
    """Raised BY A TOOL when it can PROVE the action was not performed (for example the remote service refused the request before accepting it). Anything else a handler raises after dispatch
    started is an unknown outcome."""


class DispatchNotAuthorized(Exception):
    """The dispatch row is no longer ``authorized`` (recovery or another process closed it): nothing may be sent."""


class ReconcileError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Gate:
    action: str  # "proceed" | "replay" | "refuse"
    generation: int = 1
    code: str | None = None
    replay_of: object | None = None  # the invocation id whose recorded result is replayed


@dataclass(frozen=True)
class DispatchInfo:
    """What a handler that cares (``takes_dispatch``) receives: the key to send to the remote service."""

    dispatch_id: object
    idempotency_key: str
    operation_key: str
    generation: int


def operation_key(workspace_id, plan_id, task_key, execution_id, seq: int, ref: str, args_hash: str) -> str:
    base = f"op|{workspace_id}|plan:{plan_id}|{task_key}|{ref}|{args_hash}" if plan_id is not None else f"op|{workspace_id}|exec:{execution_id}|{seq}|{ref}|{args_hash}"
    return hashlib.sha256(base.encode()).hexdigest()


def remote_key(op_key: str, generation: int) -> str:
    return f"af-{op_key[:48]}-g{generation}"


def _lineage(session, ctx_run_id, ctx_task, execution_id, spec):
    """Every dispatch of this tool in the same task lineage (the plan and task, or the execution when there is no plan), oldest first."""
    query = select(ToolDispatch).where(ToolDispatch.tool_id == spec.id, ToolDispatch.tool_version == spec.version)
    if ctx_run_id is not None:
        plan_id = session.execute(select(PlanRun.plan_id).where(PlanRun.id == ctx_run_id)).scalar_one()
        query = query.join(PlanRun, PlanRun.id == ToolDispatch.run_id).where(PlanRun.plan_id == plan_id, ToolDispatch.task_key == ctx_task)
    else:
        query = query.where(ToolDispatch.execution_id == execution_id)
    return list(session.execute(query.order_by(ToolDispatch.created_at, ToolDispatch.generation)).scalars())


def plan_of(session, run_id):
    return session.execute(select(PlanRun.plan_id).where(PlanRun.id == run_id)).scalar_one() if run_id is not None else None


def lock_lineage(session, *, run_id, task_key, execution_id, spec) -> None:
    """Serialise authorisations of the same tool in the same task lineage. PostgreSQL (READ COMMITTED) would otherwise let two transactions both read "nothing in flight" and both authorise (the unique
    ``(operation_key, generation)`` only catches the SAME arguments): a transaction-scoped advisory lock closes that. SQLite already serialises writers (the intent row is inserted first)."""
    if session.get_bind().dialect.name != "postgresql":
        return
    plan_id = plan_of(session, run_id)
    lineage = f"lin|plan:{plan_id}|{task_key}|{spec.ref}" if plan_id is not None else f"lin|exec:{execution_id}|{spec.ref}"
    key = int.from_bytes(hashlib.sha256(lineage.encode()).digest()[:8], "big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def decide(session, *, run_id, task_key, execution_id, spec, op_key: str) -> Gate:
    """The duplicate-prevention gate (see the module docstring). Read-only; the caller holds the write transaction that makes the answer authoritative."""
    rows = _lineage(session, run_id, task_key, execution_id, spec)
    if any(r.state in IN_FLIGHT for r in rows):
        return Gate("refuse", code="operation_in_flight")
    if any(r.state == "unknown" for r in rows):
        return Gate("refuse", code="outcome_unknown")
    same = [r for r in rows if r.operation_key == op_key]
    done = [r for r in same if r.state in CONFIRMED_OK]
    if done:
        return Gate("replay", replay_of=done[-1].invocation_id, generation=max(r.generation for r in same))
    if any(r.state == "failed" for r in same) and getattr(spec, "retry_policy", "never") != "after_confirmed_failure":
        return Gate("refuse", code="retry_not_permitted")
    return Gate("proceed", generation=(max((r.generation for r in same), default=0) + 1))


def emit(session, row: ToolDispatch, event_type: str, actor: tuple, **detail) -> None:
    """One run-history event per transition, in the transaction of the transition (skipped for a standalone execution: it has no run history). Never carries values."""
    if row.run_id is None:
        return
    run_events.add(session, row.run_id, event_type, actor_type=actor[0], actor_id=actor[1], task_key=row.task_key, execution_id=row.execution_id,
                   tool=f"{row.tool_id}@{row.tool_version}", generation=row.generation, **detail)


def authorize_in(session, *, invocation: ToolInvocation, op_key: str, generation: int, actor: tuple, now: datetime) -> ToolDispatch:
    row = ToolDispatch(invocation_id=invocation.id, user_id=invocation.user_id, workspace_id=invocation.workspace_id, operation_key=op_key, generation=generation,
                       idempotency_key=remote_key(op_key, generation), execution_id=invocation.execution_id, run_id=invocation.run_id, task_key=invocation.task_key,
                       tool_id=invocation.tool_id, tool_version=invocation.tool_version, args_hash=invocation.args_hash, state="authorized", created_at=now)
    session.add(row)
    session.flush()
    emit(session, row, "tool_dispatch_authorized", actor, seq=invocation.seq)
    return row


def start_in(session, dispatch_id, actor: tuple, now: datetime) -> ToolDispatch:
    """authorized -> dispatching, committed by the caller BEFORE the handler runs. Raises ``DispatchNotAuthorized`` when the row is not ``authorized`` any more."""
    done = session.execute(update(ToolDispatch).where(ToolDispatch.id == dispatch_id, ToolDispatch.state == "authorized").values(state="dispatching", dispatch_started_at=now))
    if done.rowcount != 1:
        raise DispatchNotAuthorized("not authorized")
    row = session.execute(select(ToolDispatch).where(ToolDispatch.id == dispatch_id)).scalar_one()
    emit(session, row, "tool_dispatch_started", actor)
    return row


def finish_in(session, dispatch_id, state: str, actor: tuple, now: datetime, *, external_ref: str | None = None, error_code: str | None = None) -> bool:
    """dispatching -> succeeded | failed | unknown (compare-and-swap). False when the row had already left ``dispatching`` (recovery got there first): the caller must not overwrite it."""
    assert state in ("succeeded", "failed", "unknown")
    done = session.execute(update(ToolDispatch).where(ToolDispatch.id == dispatch_id, ToolDispatch.state == "dispatching")
                           .values(state=state, completed_at=now, external_ref=(external_ref or None) and str(external_ref)[:100], error_code=error_code))
    if done.rowcount != 1:
        return False
    row = session.execute(select(ToolDispatch).where(ToolDispatch.id == dispatch_id)).scalar_one()
    emit(session, row, f"tool_dispatch_{state}", actor, code=error_code)
    return True


def close_open_in(session, execution_ids, now: datetime) -> tuple:
    """An execution is being closed (a crash, a shutdown, recovery). ``authorized`` dispatches become ``cancelled`` (never sent; their invocation ends ``cancelled``/``not_dispatched``),
    ``dispatching`` ones become ``unknown`` (their invocation ends ``uncertain``). Returns (cancelled, unknown). Runs INSIDE the caller's transaction."""
    if not execution_ids:
        return 0, 0
    actor, cancelled, unknown = ("recovery", None), 0, 0
    rows = session.execute(select(ToolDispatch).where(ToolDispatch.execution_id.in_(list(execution_ids)), ToolDispatch.state.in_(IN_FLIGHT))).scalars().all()
    for row in rows:
        target, status, code = ("cancelled", "cancelled", "not_dispatched") if row.state == "authorized" else ("unknown", "uncertain", "uncertain")
        from agentplatform.tools.registry import TOOL_ERRORS

        # LOCK ORDER: invocation first, then dispatch: the same order as ``ToolStore.finish`` (found by the PostgreSQL race test as a deadlock when it was the other way round). If a finisher
        # holds the invocation we wait for it; it then no longer matches ``running`` and the dispatch compare-and-swap below fails too, so nothing is double-closed.
        closed = session.execute(update(ToolInvocation).where(ToolInvocation.id == row.invocation_id, ToolInvocation.status == "running")
                                 .values(status=status, error_code=code, error_message=TOOL_ERRORS[code], completed_at=now)).rowcount
        done = session.execute(update(ToolDispatch).where(ToolDispatch.id == row.id, ToolDispatch.state == row.state).values(state=target, completed_at=now, error_code=code))
        if done.rowcount != 1:
            if closed:
                raise RuntimeError("dispatch and invocation disagree during close: rolled back")  # cannot happen under the lock order above; never leave half a close
            continue
        session.refresh(row)
        emit(session, row, f"tool_dispatch_{target}", actor, code=code)
        cancelled, unknown = cancelled + (target == "cancelled"), unknown + (target == "unknown")
    return cancelled, unknown


# ------------------------------------------------------------------------------------------------ reading

def actor_emails(session, reconciliations) -> dict:
    """user id -> sign-in email of the people who reconciled (workspace members can already see each other's emails on the Team page; an audit trail names who decided)."""
    ids = {r.actor_user_id for r in reconciliations}
    return {u.id: u.email for u in session.execute(select(User).where(User.id.in_(ids))).scalars()} if ids else {}


def dispatch_view(row: ToolDispatch, reconciliations=(), actors=None) -> dict:
    """The facts a person needs, with the honest classification: ``confirmed`` (we know), ``unknown`` (we do not), ``reconciled`` (decided through a record) and whether an automatic retry is permitted."""
    unknown = row.state == "unknown"
    reconciled = row.state.startswith("reconciled")
    last = reconciliations[-1] if reconciliations else None
    # D075: the idempotency key is internal protocol detail and is NOT part of any shared view (the server uses it itself when it asks the remote)
    return {
        "id": str(row.id), "state": row.state, "operation_key": row.operation_key[:16], "generation": row.generation,
        "external_ref": row.external_ref, "dispatch_started_at": row.dispatch_started_at.isoformat() if row.dispatch_started_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "outcome": "unknown" if unknown else "confirmed" if row.state in ("succeeded", "failed") else "reconciled" if reconciled else "not_sent" if row.state == "cancelled" else "in_flight",
        "performed": True if row.state in CONFIRMED_OK else False if row.state in ("failed", "reconciled_failed", "cancelled") else None,
        "requires_reconciliation": unknown,
        "reconciliation": None if last is None else {"decision": last.decision, "evidence_kind": last.evidence_kind, "evidence": last.evidence, "reason": last.reason,
                                                      "at": last.created_at.isoformat(), "attestation_only": last.evidence_kind == "operator_attestation",
                                                      "by": (actors or {}).get(last.actor_user_id), "attempts": len(reconciliations)},
    }


def unresolved_tasks(session, run_id) -> set:
    """Task keys of this run whose side-effect outcome is ``unknown`` (they block a retry until reconciled)."""
    return set(session.execute(select(ToolDispatch.task_key).where(ToolDispatch.run_id == run_id, ToolDispatch.state == "unknown")).scalars())


def unresolved_for_plan(session, plan_id) -> set:
    """Task keys of ANY attempt of this plan with an ``unknown`` outcome: a retry of any later attempt is blocked for them until reconciled."""
    return set(session.execute(select(ToolDispatch.task_key).join(PlanRun, PlanRun.id == ToolDispatch.run_id).where(PlanRun.plan_id == plan_id, ToolDispatch.state == "unknown")).scalars())


# ------------------------------------------------------------------------------------------------ reconciliation

def reconcile(session_factory, scope, invocation_id, *, decision: str, evidence_kind: str, evidence: str, reason: str, external_ref: str | None = None, now: datetime | None = None) -> dict:
    """Record ONE reconciliation attempt of an ``unknown`` dispatch of the caller's workspace and, unless ``unresolved``, move it to ``reconciled_*`` in the same transaction by compare-and-swap
    (of concurrent attempts exactly one wins; the others get ``already_resolved`` and write nothing). The actor must be a workspace owner or admin."""
    now = now or datetime.utcnow()
    observability.set_context(invocation=invocation_id)
    if decision not in ("confirmed_succeeded", "confirmed_failed", "unresolved") or evidence_kind not in ("remote_query", "operator_attestation"):
        raise ReconcileError("invalid_decision")
    evidence, reason = (evidence or "").strip()[:MAX_EVIDENCE], (reason or "").strip()[:MAX_EVIDENCE]
    if len(evidence) < 3 or len(reason) < 3:
        raise ReconcileError("evidence_and_reason_required")
    with session_factory() as session, session.begin():
        role = session.execute(select(WorkspaceMembership.role).where(WorkspaceMembership.workspace_id == scope.workspace_id, WorkspaceMembership.user_id == scope.user_id)).scalar_one_or_none()
        row = session.execute(select(ToolDispatch).where(ToolDispatch.invocation_id == invocation_id, ToolDispatch.workspace_id == scope.workspace_id)).scalar_one_or_none()
        if row is None:
            raise ReconcileError("not_found")
        if role not in RECONCILER_ROLES:  # authorisation before state: a non-reconciler learns nothing about the dispatch
            raise ReconcileError("reconciler_role_required")
        if row.state != "unknown":
            raise ReconcileError("already_resolved" if row.state.startswith("reconciled") else "not_unknown")
        if decision != "unresolved":
            target = "reconciled_succeeded" if decision == "confirmed_succeeded" else "reconciled_failed"
            done = session.execute(update(ToolDispatch).where(ToolDispatch.id == row.id, ToolDispatch.state == "unknown")
                                   .values(state=target, external_ref=(external_ref or row.external_ref)))
            if done.rowcount != 1:
                raise ReconcileError("already_resolved")
        session.add(ToolReconciliation(dispatch_id=row.id, invocation_id=row.invocation_id, user_id=row.user_id, workspace_id=row.workspace_id, actor_user_id=scope.user_id, decision=decision,
                                       evidence_kind=evidence_kind, evidence=evidence, reason=reason, external_ref=(external_ref or None) and str(external_ref)[:100], created_at=now))
        session.flush()
        session.refresh(row)
        emit(session, row, "tool_dispatch_reconciled", ("user", str(scope.user_id)), reason=decision if decision == "unresolved" else row.state)
        recs = session.execute(select(ToolReconciliation).where(ToolReconciliation.dispatch_id == row.id).order_by(ToolReconciliation.created_at)).scalars().all()
        return dispatch_view(row, recs, actor_emails(session, recs))
