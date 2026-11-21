"""Durable tool invocation (M4-B): validate, authorise, record, run, record.

THE PIPELINE for one request by an agent (every step is server-side; the model's output is untrusted input at every step):

1. PERMISSION: the requested function name must be one of the tools PINNED in the agent version being executed (``allowed``). A tool the
   registry knows but the agent was not granted is ``not_permitted``; a name nobody registered is ``unknown_tool``; a disabled version, or
   one the policy only allows with a human approval (not available yet), is ``tool_unavailable`` / ``approval_required``. Each refusal is
   recorded as a ``rejected`` invocation (audit) and the model is told the fixed reason so it can carry on.
2. VALIDATION: the arguments must be a JSON object that validates against the tool's strict input model (unknown fields are rejected). A
   failure records the FIELD PATHS only, never the submitted values.
3. IDENTITY: the validated arguments are canonicalised (sorted JSON) and hashed together with the tool id and version; with the execution
   and the step number this yields the unique idempotency key. A step cannot be dispatched twice (database unique constraints).
4. INTENT BEFORE EFFECT: the invocation row is written ``running`` BEFORE the handler is called; for a plan task the write is fenced by the
   run's lease (a stale owner can start nothing). If the process dies now, recovery closes the row ``interrupted`` (``uncertain`` for a tool
   with side effects: it may have taken effect). A tool is never silently re-run after an uncertain outcome.
5. BOUNDED EXECUTION: the handler (trusted code, pure function) runs in a worker thread under the tool's timeout; its result must validate
   against the output model, is redacted for probable secrets and capped in size. A timed-out thread cannot be killed (the tools are pure and
   input-bounded, so this is bounded work); its late result is discarded.
6. OUTCOME: the row is finished once (fenced); the model receives only the validated, redacted output or a fixed error.

NOT claimed: exactly-once external effects. The first tools have no external effects; the ``uncertain`` state and the side-effect class are
the designed-in answer for tools that will.
"""

import observability
import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from agentplatform.run_leases import Fence, LeaseStore
from agentplatform.schema import redact_secrets
from agentplatform.tools import dispatch as dp
from agentplatform.tools.approvals import TaskSuspended, _worker_actor, close_refused, consume_in, invalidate_open
from agentplatform.tools.registry import TOOL_ERRORS, ToolError, ToolInputError, ToolRegistry, ToolSpec
from authz.scope import require_scope
from memory.models import PlanRun, ToolDispatch, ToolInvocation, ToolReconciliation

logger = logging.getLogger("agentforge.tools")

MAX_ARGUMENT_CHARS = 8000
MAX_INPUT_STORED = 4000
MAX_CLAIMED_NAME = 64
PAGE_DEFAULT, PAGE_MAX = 25, 100
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.@-]")


class InvocationNotFound(Exception):
    pass


class ApprovalUnusable(Exception):
    """The approval cannot authorise this invocation (expired, invalidated, consumed or not matching); nothing was recorded."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ToolRunContext:
    """What links a tool call to a plan task: the run, the task and the lease fence of the process that executes it."""

    run_id: object
    task_key: str
    fence: Fence | None = None
    kind: str = "worker"  # "api" | "worker": the process kind recorded as the actor of approval events
    required_tool: str | None = None  # M6b: the exact tool ref ("id@version") the task must really invoke to count as done


@dataclass(frozen=True)
class InvocationContext:
    scope: object
    execution_id: object
    agent_key: str
    agent_version_id: object | None
    allowed: dict  # wire name -> pinned ToolSpec (exactly the tools of this agent version)
    run: ToolRunContext | None = None


@dataclass(frozen=True)
class ToolOutcome:
    invocation_id: object
    status: str
    ok: bool
    content: str  # exactly what the model is shown (validated output, or a fixed error)
    tool_ref: str | None
    error_code: str | None = None


@dataclass(frozen=True)
class InvocationView:
    id: object
    execution_id: object
    run_id: object | None
    task_key: str | None
    agent_key: str
    tool_id: str
    tool_version: int | None
    seq: int
    status: str
    side_effect: str
    args_hash: str | None
    input: dict | None
    output: dict | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime
    completed_at: datetime | None


def canonical(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _loads(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _view(row: ToolInvocation) -> InvocationView:
    return InvocationView(row.id, row.execution_id, row.run_id, row.task_key, row.agent_key, row.tool_id, row.tool_version, row.seq, row.status, row.side_effect, row.args_hash,
                          _loads(row.input), _loads(row.output), row.error_code, row.error_message, row.created_at, row.started_at, row.completed_at)


def close_open(session, execution_ids, now: datetime | None = None) -> int:
    """Close the still-``running`` invocations of executions that are being closed (a crash, a shutdown, recovery). In-flight calls of a tool
    without side effects end ``interrupted``; a tool with side effects ends ``uncertain`` (it may have taken effect). Runs INSIDE the caller's
    transaction, so a closed execution and its closed invocations always agree."""
    if not execution_ids:
        return 0
    now = now or datetime.utcnow()
    closed = sum(dp.close_open_in(session, execution_ids, now))  # P0: a dispatch that never started is ``cancelled``; one that started is ``unknown`` (their invocations end with it)
    for status, condition in (("uncertain", ToolInvocation.side_effect != "none"), ("interrupted", ToolInvocation.side_effect == "none")):
        done = session.execute(update(ToolInvocation).where(ToolInvocation.execution_id.in_(list(execution_ids)), ToolInvocation.status == "running", condition)
                               .values(status=status, error_code=status, error_message=TOOL_ERRORS[status], completed_at=now))
        closed += done.rowcount
    invalidate_open(session, execution_ids, now)  # M4-C: an approval nobody is waiting for any more is never executed later
    return closed


class ToolStore:
    """Persistence of invocations. Every read is constrained by the scope's user AND workspace (foreign == unknown)."""

    def __init__(self, session_factory):
        self._sessions = session_factory
        self.leases = LeaseStore(session_factory)
        self.admit = None  # M6: set by the ExecutionService: (session, user_id) -> raises Busy when no slot is free; run when an approval of a suspended task is consumed

    def _touch(self, session, run: ToolRunContext | None) -> None:
        if run is not None and run.fence is not None:
            self.leases.touch(session, run.fence)  # LeaseLost: a stale owner can neither start nor finish a tool call

    def action_state(self, execution_id, tool_id: str, tool_version: int) -> str:
        """``succeeded`` (the tool really ran to success in this execution), ``attempted`` (it was requested but refused, rejected, failed or is unfinished) or ``none``. Read from the
        invocation records: a model's prose never counts."""
        with self._sessions() as session:
            statuses = set(session.execute(select(ToolInvocation.status).where(ToolInvocation.execution_id == execution_id, ToolInvocation.tool_id == tool_id,
                                                                             ToolInvocation.tool_version == tool_version)).scalars())
        return "succeeded" if "succeeded" in statuses else "attempted" if statuses else "none"

    def side_effect_state(self, execution_id, run=None) -> str:
        """Server-side execution truth for ANY side-effecting call this execution made, whether or not the task required it: ``unknown`` (an outcome that is unproven or still in flight: the
        task must not be reported as done), ``failed`` (a confirmed failure of a tool that never succeeded afterwards) or ``ok``. Read from the dispatch ledger, never from prose. With ``run`` (a plan task)
        the whole task LINEAGE counts (same plan and task, every attempt): a retry whose send was refused by the tool policy cannot complete the task over an earlier confirmed failure."""
        from memory.models import ToolDispatch

        with self._sessions() as session:
            query = select(ToolDispatch.tool_id, ToolDispatch.tool_version, ToolDispatch.state)
            if run is not None:
                query = query.join(PlanRun, PlanRun.id == ToolDispatch.run_id).where(PlanRun.plan_id == dp.plan_of(session, run.run_id), ToolDispatch.task_key == run.task_key)
            else:
                query = query.where(ToolDispatch.execution_id == execution_id)
            rows = session.execute(query).all()
        if any(state in ("unknown", "authorized", "dispatching") for _, _, state in rows):
            return "unknown"
        done = {(t, v) for t, v, state in rows if state in ("succeeded", "reconciled_succeeded")}
        return "failed" if any(state == "failed" and (t, v) not in done for t, v, state in rows) else "ok"

    def begin(self, ctx: InvocationContext, spec: ToolSpec, seq: int, args_json: str, args_hash: str, key: str, approval_id=None, resuming: bool = False) -> uuid.UUID:
        scope, now = require_scope(ctx.scope), datetime.utcnow()
        row = ToolInvocation(user_id=scope.user_id, workspace_id=scope.workspace_id, execution_id=ctx.execution_id, run_id=ctx.run.run_id if ctx.run else None,
                             task_key=ctx.run.task_key if ctx.run else None, agent_key=ctx.agent_key, agent_version_id=ctx.agent_version_id, tool_id=spec.id,
                             tool_version=spec.version, seq=seq, idempotency_key=key, args_hash=args_hash, side_effect=spec.side_effect, status="running",
                             input=redact_secrets(args_json)[:MAX_INPUT_STORED], created_at=now, started_at=now)
        with self._sessions() as session, session.begin():
            self._touch(session, ctx.run)
            session.add(row)
            session.flush()
            if resuming and self.admit is not None:
                self.admit(session, scope.user_id)  # M6: capacity is acquired atomically with the consumption; Busy rolls everything back (nothing dispatched, still suspended)
            if approval_id is not None:  # M4-C: the approval is consumed in the SAME transaction as the intent row, against the LIVE parameters
                why = consume_in(session, approval_id, scope=scope, execution_id=ctx.execution_id, seq=seq, run_id=ctx.run.run_id if ctx.run else None,
                                 task_key=ctx.run.task_key if ctx.run else None, agent_key=ctx.agent_key, agent_version_id=ctx.agent_version_id, tool_id=spec.id,
                                 tool_version=spec.version, args_hash=args_hash, invocation_id=row.id, now=now, run=ctx.run)
                if why is not None:
                    raise ApprovalUnusable(why)  # rolls the intent row back: nothing runs
            if spec.side_effect != "none":
                # P0: the duplicate-prevention gate, authoritative because this transaction already holds the write lock (the intent row is inserted); then the durable dispatch authorisation,
                # committed WITH the approval consumption: either both exist or neither does
                op_key = dp.operation_key(scope.workspace_id, dp.plan_of(session, row.run_id), row.task_key, ctx.execution_id, seq, spec.ref, args_hash)
                dp.lock_lineage(session, run_id=row.run_id, task_key=row.task_key, execution_id=ctx.execution_id, spec=spec)
                gate = dp.decide(session, run_id=row.run_id, task_key=row.task_key, execution_id=ctx.execution_id, spec=spec, op_key=op_key)
                if gate.action != "proceed":
                    raise dp.DispatchRefused(gate.code or "operation_in_flight")  # a replay appearing after the pre-check is a race: refuse, never dispatch
                dp.authorize_in(session, invocation=row, op_key=op_key, generation=gate.generation, actor=_worker_actor(ctx.run), now=now)
            return row.id

    def reject(self, ctx: InvocationContext, claimed: str, spec: ToolSpec | None, seq: int, code: str, args_json: str | None, args_hash: str | None = None,
               detail: str | None = None, approval_id=None) -> uuid.UUID:
        """A refused request, recorded already final (``rejected``) so the audit trail shows what was asked and why it was refused."""
        scope, now = require_scope(ctx.scope), datetime.utcnow()
        name = _SAFE_NAME.sub("", claimed)[:MAX_CLAIMED_NAME] or "unknown"
        key = hashlib.sha256(f"{ctx.execution_id}|{seq}|rejected|{name}".encode()).hexdigest()
        row = ToolInvocation(user_id=scope.user_id, workspace_id=scope.workspace_id, execution_id=ctx.execution_id, run_id=ctx.run.run_id if ctx.run else None,
                             task_key=ctx.run.task_key if ctx.run else None, agent_key=ctx.agent_key, agent_version_id=ctx.agent_version_id, tool_id=spec.id if spec else name,
                             tool_version=spec.version if spec else None, seq=seq, idempotency_key=key, args_hash=args_hash, side_effect=spec.side_effect if spec else "none",
                             status="rejected", input=redact_secrets(args_json)[:MAX_INPUT_STORED] if args_json else None, output=canonical({"detail": detail}) if detail else None,
                             error_code=code, error_message=TOOL_ERRORS[code],
                             created_at=now, started_at=now, completed_at=now)
        with self._sessions() as session, session.begin():
            self._touch(session, ctx.run)
            session.add(row)
            session.flush()
            if approval_id is not None:  # a refusal AFTER a suspension (rejected, expired, invalid): the suspension closes in the transaction that records the refusal
                close_refused(session, approval_id, now, ctx.run, code)
            if code in ("operation_in_flight", "outcome_unknown", "retry_not_permitted") and ctx.run is not None:
                from agentplatform import run_events

                actor = _worker_actor(ctx.run)
                run_events.add(session, ctx.run.run_id, "tool_dispatch_refused", actor_type=actor[0], actor_id=actor[1], task_key=ctx.run.task_key, execution_id=ctx.execution_id,
                               tool=spec.ref if spec else None, seq=seq, code=code)
            return row.id

    def precheck(self, ctx: InvocationContext, spec: ToolSpec, seq: int, args_hash: str) -> dp.Gate:
        """The gate, read-only, BEFORE a human is asked: a refusal or a replay needs no approval. The authoritative check is repeated inside ``begin``."""
        scope = require_scope(ctx.scope)
        with self._sessions() as session:
            run_id = ctx.run.run_id if ctx.run else None
            op_key = dp.operation_key(scope.workspace_id, dp.plan_of(session, run_id), ctx.run.task_key if ctx.run else None, ctx.execution_id, seq, spec.ref, args_hash)
            return dp.decide(session, run_id=run_id, task_key=ctx.run.task_key if ctx.run else None, execution_id=ctx.execution_id, spec=spec, op_key=op_key)

    def record_replay(self, ctx: InvocationContext, spec: ToolSpec, seq: int, args_json: str, args_hash: str, key: str, replay_of) -> tuple:
        """An operation that was already CONFIRMED performed is not dispatched again: a ``succeeded`` invocation records the replay of the earlier result (no dispatch row, no approval)."""
        scope, now = require_scope(ctx.scope), datetime.utcnow()
        with self._sessions() as session, session.begin():
            self._touch(session, ctx.run)
            earlier = session.execute(select(ToolInvocation).where(ToolInvocation.id == replay_of)).scalar_one()
            payload = canonical({"already_performed": True, "performed_at": (earlier.completed_at or earlier.created_at).isoformat(), "result": _loads(earlier.output)})
            row = ToolInvocation(user_id=scope.user_id, workspace_id=scope.workspace_id, execution_id=ctx.execution_id, run_id=ctx.run.run_id if ctx.run else None,
                                 task_key=ctx.run.task_key if ctx.run else None, agent_key=ctx.agent_key, agent_version_id=ctx.agent_version_id, tool_id=spec.id, tool_version=spec.version,
                                 seq=seq, idempotency_key=hashlib.sha256(f"{key}|replay".encode()).hexdigest(), args_hash=args_hash, side_effect=spec.side_effect, status="succeeded",
                                 input=redact_secrets(args_json)[:MAX_INPUT_STORED], output=payload[:MAX_INPUT_STORED * 2], created_at=now, started_at=now, completed_at=now)
            session.add(row)
            session.flush()
            if ctx.run is not None:
                from agentplatform import run_events

                actor = _worker_actor(ctx.run)
                run_events.add(session, ctx.run.run_id, "tool_dispatch_replayed", actor_type=actor[0], actor_id=actor[1], task_key=ctx.run.task_key, execution_id=ctx.execution_id,
                               tool=spec.ref, seq=seq)
            return row.id, payload

    def start_dispatch(self, ctx: InvocationContext, invocation_id) -> dp.DispatchInfo:
        """authorized -> dispatching, in its OWN committed, fenced transaction, BEFORE the handler is called (so before any byte can leave). A stale owner cannot start a dispatch."""
        with self._sessions() as session, session.begin():
            self._touch(session, ctx.run)
            row = session.execute(select(ToolDispatch).where(ToolDispatch.invocation_id == invocation_id)).scalar_one()
            dp.start_in(session, row.id, _worker_actor(ctx.run), datetime.utcnow())
            return dp.DispatchInfo(row.id, row.idempotency_key, row.operation_key, row.generation)

    def finish(self, invocation_id, status: str, *, output: str | None = None, error_code: str | None = None, run: ToolRunContext | None = None,
               dispatch: tuple | None = None) -> bool:
        """The single running -> final transition of the invocation. ``dispatch`` = (dispatch id, state, external_ref, error code): the dispatch row moves in the SAME transaction (CAS), so the two can
        never disagree; if the lease is lost both writes roll back and the dispatch stays ``dispatching`` for recovery to close as ``unknown``."""
        now = datetime.utcnow()
        values = {"status": status, "completed_at": now, "output": output, "error_code": error_code, "error_message": TOOL_ERRORS[error_code] if error_code else None}
        with self._sessions() as session, session.begin():
            self._touch(session, run)
            done = session.execute(update(ToolInvocation).where(ToolInvocation.id == invocation_id, ToolInvocation.status == "running").values(**values))
            if done.rowcount == 1 and dispatch is not None:
                dp.finish_in(session, dispatch[0], dispatch[1], _worker_actor(run), now, external_ref=dispatch[2], error_code=dispatch[3])
            if done.rowcount == 1:
                observability.inc("tool_invocations_total", status=status, code=error_code or "none")
                if dispatch is not None and dispatch[1] == "unknown":
                    observability.inc("dispatch_unknown_total", source="finish")
            return done.rowcount == 1

    def dispatches_for(self, scope, invocation_ids) -> dict:
        """invocation id -> the dispatch view (state, honest outcome class, reconciliation) for the caller's workspace."""
        scope = require_scope(scope)
        if not invocation_ids:
            return {}
        with self._sessions() as session:
            rows = session.execute(select(ToolDispatch).where(ToolDispatch.invocation_id.in_(list(invocation_ids)), ToolDispatch.workspace_id == scope.workspace_id)).scalars().all()
            recs: dict = {}
            for r in session.execute(select(ToolReconciliation).where(ToolReconciliation.dispatch_id.in_([d.id for d in rows])).order_by(ToolReconciliation.created_at)).scalars():
                recs.setdefault(r.dispatch_id, []).append(r)
            actors = dp.actor_emails(session, [r for lst in recs.values() for r in lst])
            return {d.invocation_id: dp.dispatch_view(d, recs.get(d.id, ()), actors) for d in rows}

    def get(self, scope, invocation_id) -> InvocationView:
        scope = require_scope(scope)
        with self._sessions() as session:
            row = session.execute(select(ToolInvocation).where(ToolInvocation.id == invocation_id, ToolInvocation.user_id == scope.user_id,
                                                               ToolInvocation.workspace_id == scope.workspace_id)).scalar_one_or_none()
            if row is None:
                raise InvocationNotFound("not found")
            return _view(row)

    def list(self, scope, *, run_id=None, execution_id=None, tool_id: str | None = None, agent_key: str | None = None, status: str | None = None,
             limit: int = PAGE_DEFAULT, before: tuple | None = None) -> dict:
        """Newest first. ``before`` = (created_at, id) of the last item of the previous page."""
        scope = require_scope(scope)
        if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
            raise ValueError("invalid_limit")
        query = select(ToolInvocation).where(ToolInvocation.user_id == scope.user_id, ToolInvocation.workspace_id == scope.workspace_id)
        for column, value in ((ToolInvocation.run_id, run_id), (ToolInvocation.execution_id, execution_id), (ToolInvocation.tool_id, tool_id),
                              (ToolInvocation.agent_key, agent_key), (ToolInvocation.status, status)):
            if value is not None:
                query = query.where(column == value)
        if before is not None:
            query = query.where(or_(ToolInvocation.created_at < before[0], and_(ToolInvocation.created_at == before[0], ToolInvocation.id < before[1])))
        with self._sessions() as session:
            rows = session.execute(query.order_by(ToolInvocation.created_at.desc(), ToolInvocation.id.desc()).limit(limit + 1)).scalars().all()
            page = rows[:limit]
            return {"items": [_view(r) for r in page], "next": (page[-1].created_at, page[-1].id) if len(rows) > limit else None}

    def usage(self, scope) -> dict:
        """(tool_id, version) -> {calls, last_used} for the scope: real usage numbers for the catalog."""
        from sqlalchemy import func

        scope = require_scope(scope)
        with self._sessions() as session:
            rows = session.execute(select(ToolInvocation.tool_id, ToolInvocation.tool_version, func.count(), func.max(ToolInvocation.created_at))
                                   .where(ToolInvocation.user_id == scope.user_id, ToolInvocation.workspace_id == scope.workspace_id, ToolInvocation.status != "rejected")
                                   .group_by(ToolInvocation.tool_id, ToolInvocation.tool_version)).all()
            return {(tool, version): {"calls": n, "last_used": last} for tool, version, n, last in rows}


class ToolInvoker:
    def __init__(self, store: ToolStore, registry: ToolRegistry, approvals=None, *, poll_seconds: float = 0.5):
        self.store, self.registry = store, registry
        self.approvals = approvals  # M4-C: None = no approval path exists, so a tool that needs one is refused (fail closed)
        self.poll_seconds = poll_seconds

    async def invoke(self, ctx: InvocationContext, wire_name: str, raw_arguments, seq: int, reason: str | None = None, *, suspend=None, resume_approval=None) -> ToolOutcome:
        """Run one requested call. Never raises for a refusal or a tool failure (they are recorded and returned to the model as fixed errors);
        ``LeaseLost`` (a stale owner) and ``CancelledError`` propagate."""
        def refuse(*a, **k):
            return self._refuse(*a, approval_id=resume_approval, **k)  # when this call RESUMES a suspension, a refusal closes it (the task is never stuck)

        raw = raw_arguments if isinstance(raw_arguments, str) else canonical(raw_arguments) if isinstance(raw_arguments, dict) else ""
        spec = ctx.allowed.get(wire_name)
        if spec is None:
            known = next((s for s in self.registry.list_specs() if s.wire_name == wire_name), None)
            return await refuse(ctx, wire_name, known, seq, "not_permitted" if known else "unknown_tool", raw)
        if spec.lifecycle == "disabled":
            return await refuse(ctx, wire_name, spec, seq, "tool_unavailable", raw)
        gated = False
        if resume_approval is not None and self.approvals is None:
            return await refuse(ctx, wire_name, spec, seq, "approval_required", raw)
        if not self.registry.executable(spec) or resume_approval is not None:
            if self.approvals is None or not (self.registry.gated(spec) or resume_approval is not None):
                return await refuse(ctx, wire_name, spec, seq, "approval_required", raw)  # no approval path here: fail closed
            gated = True  # M4-C: runs only with a human approval bound to THIS exact call
        if len(raw) > MAX_ARGUMENT_CHARS:
            return await refuse(ctx, wire_name, spec, seq, "invalid_input", raw[:MAX_INPUT_STORED], detail="The arguments are too large.")
        try:
            data = json.loads(raw or "{}")
            if not isinstance(data, dict):
                raise ValueError
            validated = spec.input_model.model_validate(data)
        except ValidationError as exc:
            paths = ", ".join(sorted({".".join(str(p) for p in e["loc"]) or "(root)" for e in exc.errors()}))[:200]
            return await refuse(ctx, wire_name, spec, seq, "invalid_input", raw[:MAX_INPUT_STORED], detail=f"Invalid fields: {paths}")
        except (ValueError, RecursionError):
            return await refuse(ctx, wire_name, spec, seq, "invalid_input", raw[:MAX_INPUT_STORED], detail="The arguments must be a JSON object.")
        canon = canonical(validated.model_dump(mode="json"))
        args_hash = hashlib.sha256(f"{spec.ref}|{canon}".encode()).hexdigest()
        key = hashlib.sha256(f"{ctx.execution_id}|{seq}|{spec.ref}|{args_hash}".encode()).hexdigest()
        if spec.side_effect != "none":
            # P0: the duplicate-prevention gate BEFORE a person is asked to approve anything. A confirmed earlier performance is REPLAYED (nothing is sent, no approval is needed); an earlier attempt that is in
            # flight, of unknown outcome, or a confirmed failure the tool does not allow retrying is REFUSED. The same gate is repeated authoritatively inside ``begin``.
            gate = await asyncio.to_thread(self.store.precheck, ctx, spec, seq, args_hash)
            if gate.action == "refuse":
                return await refuse(ctx, wire_name, spec, seq, gate.code, canon)
            if gate.action == "replay":
                if resume_approval is not None:
                    await asyncio.to_thread(self.approvals.invalidate, resume_approval)  # nothing will use this approval: the operation was already performed
                invocation_id, payload = await asyncio.to_thread(self.store.record_replay, ctx, spec, seq, canon, args_hash, key, gate.replay_of)
                return ToolOutcome(invocation_id, "succeeded", True, payload, spec.ref)
        approval_id = None
        if gated and resume_approval is not None:
            # RESUME of a durably suspended task: the approval already exists; everything is re-validated here and again, atomically, when it is consumed below
            approval_id = resume_approval
            state = await asyncio.to_thread(self.approvals.status, approval_id)
            if state != "approved":
                code = {"rejected": "approval_rejected", "expired": "approval_expired"}.get(state, "approval_invalid")
                return await refuse(ctx, wire_name, spec, seq, code, canon)
        elif gated and suspend is not None and ctx.run is not None:
            # a plan task: do NOT wait in memory. Persist the approval and the conversation together and let go of the task; any process resumes it after the decision
            try:
                suspended_on = await asyncio.to_thread(self.approvals.request, ctx, spec, seq, canon, args_hash, reason, suspend())
            except IntegrityError:
                raise ToolError("duplicate_call") from None
            raise TaskSuspended(suspended_on)
        elif gated:
            refused, approval_id = await self._await_approval(ctx, wire_name, spec, seq, canon, args_hash, reason)
            if refused is not None:
                return refused
        try:
            invocation_id = await asyncio.to_thread(self.store.begin, ctx, spec, seq, canon, args_hash, key, approval_id, resume_approval is not None)
            observability.set_context(invocation=invocation_id, approval=approval_id)  # every later log line of this call carries both ids
        except IntegrityError:
            raise ToolError("duplicate_call") from None
        except ApprovalUnusable as unusable:
            await asyncio.to_thread(self.approvals.invalidate, approval_id)  # no-op when already terminal; closes a mismatching approval
            return await refuse(ctx, wire_name, spec, seq, "approval_expired" if unusable.reason == "expired" else "approval_invalid", canon)  # the same step is never dispatched twice
        except dp.DispatchRefused as refused_dispatch:  # the authoritative gate said no (a race with another attempt): the approval is not used
            if approval_id is not None:
                await asyncio.to_thread(self.approvals.invalidate, approval_id)
            return await refuse(ctx, wire_name, spec, seq, refused_dispatch.code, canon)
        run = ctx.run
        effect = spec.side_effect != "none"
        info = None
        if effect:
            try:
                info = await asyncio.to_thread(self.store.start_dispatch, ctx, invocation_id)  # committed BEFORE the handler runs, so before any byte can leave
            except dp.DispatchNotAuthorized:
                return await self._conclude(invocation_id, spec, "cancelled", "cancelled", run)  # recovery closed it first: nothing was sent
        handler = self.registry.handler(spec)
        call = (lambda: handler(validated, info)) if effect and self.registry.takes_dispatch(spec) else (lambda: handler(validated))
        dispatch = lambda state, ref=None, code=None: (info.dispatch_id, state, ref, code) if effect else None  # noqa: E731
        try:
            result = await asyncio.wait_for(asyncio.to_thread(call), spec.timeout_seconds)
        except asyncio.CancelledError:
            # a cancelled call that may already have been sent is UNKNOWN, never "cancelled"
            self._finish_sync(invocation_id, "uncertain" if effect else "cancelled", run=run, dispatch=dispatch("unknown", None, "uncertain"))
            raise
        except asyncio.TimeoutError:
            if effect:  # the thread cannot be stopped and may still deliver: the outcome is unknown, not a failure
                return await self._conclude(invocation_id, spec, "uncertain", "uncertain", run, dispatch=dispatch("unknown", None, "uncertain"))
            return await self._conclude(invocation_id, spec, "timed_out", "timeout", run)
        except ToolInputError as exc:  # the tool refused the arguments before acting: a PROVEN non-performance
            return await self._conclude(invocation_id, spec, "failed", "invalid_input", run, detail=str(exc)[:200], dispatch=dispatch("failed", None, "invalid_input"))
        except dp.DefinitiveFailure:  # the tool proved nothing was done
            return await self._conclude(invocation_id, spec, "failed", "tool_error", run, dispatch=dispatch("failed", None, "tool_error"))
        except Exception as exc:  # noqa: BLE001 - nothing but a fixed code leaves this block
            logger.error("Tool %s failed (%s)", spec.ref, type(exc).__name__)
            if effect:  # an unexpected error after dispatch started proves nothing about the remote side
                return await self._conclude(invocation_id, spec, "uncertain", "uncertain", run, dispatch=dispatch("unknown", None, "uncertain"))
            return await self._conclude(invocation_id, spec, "failed", "tool_error", run)
        ref = str(getattr(result, spec.external_ref_field, "") or "")[:100] if effect and spec.external_ref_field else None
        # the handler RETURNED: the tool says it acted, so the dispatch is a confirmed success even if its output is unusable to us
        if not isinstance(result, spec.output_model):
            return await self._conclude(invocation_id, spec, "failed", "output_invalid", run, dispatch=dispatch("succeeded", ref))
        text = canonical(result.model_dump(mode="json"))
        if len(text.encode()) > spec.max_output_bytes:
            return await self._conclude(invocation_id, spec, "failed", "output_too_large", run, dispatch=dispatch("succeeded", ref))
        text = redact_secrets(text)
        await asyncio.shield(self._finish(invocation_id, "succeeded", output=text, run=run, dispatch=dispatch("succeeded", ref)))
        return ToolOutcome(invocation_id, "succeeded", True, text, spec.ref)

    async def _await_approval(self, ctx, wire_name, spec, seq, canon, args_hash, reason):
        """Open a pending approval for this exact step and wait (bounded by its expiry) for a human decision. Returns (refusal-or-None, approval id).
        ``reason`` is the model's own words (untrusted, shown to the human as the agent's stated reason; it grants nothing)."""
        try:
            approval_id = await asyncio.to_thread(self.approvals.request, ctx, spec, seq, canon, args_hash, reason)
        except IntegrityError:
            raise ToolError("duplicate_call") from None
        try:
            while True:
                state = await asyncio.to_thread(self.approvals.status, approval_id)
                if state == "approved":
                    return None, approval_id
                if state != "pending":
                    break
                await asyncio.sleep(self.poll_seconds)
        except asyncio.CancelledError:
            self.approvals.invalidate(approval_id)  # nobody is waiting any more
            raise
        code = {"rejected": "approval_rejected", "expired": "approval_expired"}.get(state, "approval_invalid")
        return await self._refuse(ctx, wire_name, spec, seq, code, canon), approval_id

    async def refuse(self, ctx: InvocationContext, wire_name: str, seq: int, code: str) -> ToolOutcome:
        """Record and return a refusal decided by the RUNTIME (for example the per-execution call limit), not by the call's content."""
        return await self._refuse(ctx, wire_name, ctx.allowed.get(wire_name), seq, code, "")

    async def _finish(self, invocation_id, status, **kw):
        return await asyncio.to_thread(lambda: self.store.finish(invocation_id, status, **kw))

    def _finish_sync(self, invocation_id, status, *, run, dispatch=None) -> None:
        """Synchronous on purpose: runs while the task is being cancelled (see ``ExecutionService._finish_cancelled``)."""
        try:
            self.store.finish(invocation_id, status, error_code=status, run=run, dispatch=dispatch)
        except Exception as exc:  # noqa: BLE001 - recovery closes whatever slips through
            logger.error("Could not record the %s of a tool call (%s)", status, type(exc).__name__)

    async def _conclude(self, invocation_id, spec, status, code, run, detail: str | None = None, dispatch=None) -> ToolOutcome:
        output = canonical({"detail": detail}) if detail else None
        await asyncio.shield(self._finish(invocation_id, status, output=output, error_code=code, run=run, dispatch=dispatch))
        return ToolOutcome(invocation_id, status, False, _error_content(code, detail), spec.ref, code)

    async def _refuse(self, ctx, claimed, spec, seq, code, raw, detail: str | None = None, approval_id=None) -> ToolOutcome:
        invocation_id = await asyncio.to_thread(self.store.reject, ctx, claimed, spec, seq, code, raw[:MAX_INPUT_STORED] if raw else None, None, detail, approval_id)
        return ToolOutcome(invocation_id, "rejected", False, _error_content(code, detail), spec.ref if spec else None, code)


def _error_content(code: str, detail: str | None = None) -> str:
    body = {"error": code, "message": TOOL_ERRORS[code]}
    if detail:
        body["detail"] = detail
    return canonical(body)
