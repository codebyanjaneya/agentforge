"""Agent execution runtime and version-pinned execution records (M2-B).

ONE runtime serves REST and WebSocket: resolve -> record -> run -> finish.

* RESOLVE: ``AgentService.resolve(scope, key)`` is the only resolution point. Built-in keys resolve to the existing
  built-in modules (executed unchanged through the orchestrator, never shadowed by a custom slug); an active custom slug
  resolves to its CURRENT immutable version. Archived, unknown and foreign keys all resolve to nothing and are therefore
  indistinguishable (``AgentUnavailable``).
* RECORD: before anything runs, a ``running`` row is written that pins the resolved ``version_id`` (custom) or only the
  key (built-in). The definition used for the call is the one resolved at that moment: editing the agent afterwards
  creates a new version and cannot change this execution. The browser never supplies an owner or a version id.
* RUN: a custom agent is sent to the provider selected by its validated ``model.provider``: fixed platform guard, then
  the agent profile and instructions (``prompting``), then recent history and the task as user messages. There are no
  tools and no action channel. Every call is bounded by a deadline; there are no retries and no fallbacks.
* FINISH: exactly one terminal transition (``succeeded`` / ``failed`` / ``cancelled``), also on timeouts, provider
  errors, unexpected exceptions and task cancellation (a closed WebSocket). Only fixed safe text is ever stored or shown
  as an error; provider output, exception text and credentials never are. Rows left ``running`` by a crash are closed as
  ``interrupted`` at startup (``ExecutionStore.fail_orphaned``).

LIMITS (single process, no queue: the app is one process on one VM and executions are I/O-bound LLM calls, so a
distributed queue would add infrastructure without a need today; the trade-off is that limits are per process):
``MAX_CONCURRENT`` running executions in total and ``MAX_PER_USER`` per user; beyond that a request is refused at once
(``Busy``) instead of queueing without bound.
"""

import observability
import asyncio
import base64
import hashlib
import json
import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import and_, exists, func, or_, select, text, update

from agentplatform.prompting import build_system_prompt
from agentplatform.run_leases import db_now
from agentplatform.providers import Message, ProviderError, ProviderRequest, ToolDefinition, provider_error_code
from agentplatform.schema import redact_secrets
from agentplatform.service import AgentService, ResolvedAgent
from agentplatform.tools.approvals import TaskSuspended
from authz.scope import require_scope
from memory.models import AgentExecution, ExecutionOwner, PlanRun, PlanTaskRun, ToolSuspension, WorkerProcess

logger = logging.getLogger("agentforge.execution")

MAX_TASK_CHARS = 4000
MAX_RESULT_CHARS = 20000
MAX_ERROR_CHARS = 300
PROVIDER_TIMEOUT_SECONDS = 60.0  # one provider call
CUSTOM_TIMEOUT_SECONDS = 600.0  # a whole custom execution (M6b: provider retries wait inside it; was 90 s before waits existed)
BUILTIN_TIMEOUT_SECONDS = 180.0  # a built-in execution (the CEO may delegate to several agents)
MAX_CONCURRENT = 8
MAX_PER_USER = 3
MAX_TOOL_ROUNDS = 4  # model <-> tool exchanges per execution; the final answer is then requested WITHOUT tools
MAX_TOOL_CALLS = 8  # tool calls (executed or refused) per execution
MAX_CALLS_PER_ROUND = 4
DB_MAX_CONCURRENT = 16  # database-wide (all processes); M4-A2
DB_MAX_PER_USER = 3  # database-wide, per user, across every process
ADMISSION_LOCK = 4242001  # pg_advisory_xact_lock key that serialises execution admission
PAGE_DEFAULT, PAGE_MAX = 20, 100
HISTORY_TURNS = 20  # the same bound as the short-term memory

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Fixed, user-safe text per error code. ``internal_error`` keeps the pre-M2-B wording.
MESSAGES = {
    "timeout": "The agent took too long to answer.",
    "rate_limited": "The model provider is rate-limiting requests. Please wait a moment and try again.",
    "unavailable": "The model provider is currently unavailable. Please try again shortly.",
    "auth": "The agent runtime is not available right now.",
    "unsupported_model": "The agent's model is not available from the provider. Edit the agent and choose a supported model.",
    "unsupported_provider": "The agent's model provider is not supported.",
    "bad_request": "The model provider rejected the request.",
    "invalid_response": "The model provider returned an unusable response.",
    "malformed_tool_call": "The model produced a malformed tool call and kept doing so. Please try again.",
    "required_action_missing": "The agent did not perform the action this task requires, so the task was not completed.",
    "side_effect_outcome_unknown": "An external action of this task has an unknown outcome, so the task is not reported as done. Reconcile the action first.",
    "side_effect_failed": "An external action of this task failed, so the task is not reported as done.",
    "internal_error": "Agent failed.",
    "cancelled": "The execution was cancelled before it finished.",
    "interrupted": "The server restarted before this execution finished.",
}
# HTTP status for each failure code (REST).
HTTP_STATUS = {"timeout": 504, "rate_limited": 429, "unavailable": 503, "auth": 503, "unsupported_model": 422,
               "unsupported_provider": 422, "bad_request": 502, "invalid_response": 502, "malformed_tool_call": 502, "required_action_missing": 502, "side_effect_outcome_unknown": 409, "side_effect_failed": 502, "internal_error": 500}


class ExecutionRefused(Exception):
    """The request was refused BEFORE anything was recorded or run."""

    code = "refused"
    message = "The request was refused."


class TaskInvalid(ExecutionRefused):
    code = "task_invalid"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class AgentUnavailable(ExecutionRefused):
    code = "agent_unavailable"
    message = "Unknown agent."  # unknown, archived and foreign agents are indistinguishable


class Busy(ExecutionRefused):
    code = "busy"
    message = "The server is busy running other agents. Please try again in a moment."


class ExecutionFailed(Exception):
    """The execution was recorded and ended in failure; ``message`` is fixed safe text."""

    def __init__(self, execution_id, code: str):
        self.execution_id, self.code = execution_id, code
        self.message = MESSAGES.get(code, MESSAGES["internal_error"])
        super().__init__(self.message)


class ExecutionNotFound(Exception):
    pass


class CursorError(ValueError):
    pass


@dataclass(frozen=True)
class ExecutionView:
    id: uuid.UUID
    agent_source: str
    agent_key: str
    agent_name: str
    agent_version_id: uuid.UUID | None
    agent_version: int | None
    status: str
    task: str
    result: str | None
    error_code: str | None
    error_message: str | None
    provider: str | None
    model: str | None
    created_at: datetime
    started_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class Outcome:
    execution_id: uuid.UUID
    status: str
    agent_source: str
    agent_key: str
    agent_name: str
    agent_version: int | None
    result: str
    provider: str | None = None
    model: str | None = None
    extra: dict = field(default_factory=dict)  # built-in CEO delegation data (existing protocol)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def validate_task(task) -> str:
    if not isinstance(task, str):
        raise TaskInvalid("The task must be text.")
    task = task.strip()
    if not task:
        raise TaskInvalid("The task must not be empty.")
    if len(task) > MAX_TASK_CHARS:
        raise TaskInvalid(f"The task is too long (at most {MAX_TASK_CHARS} characters).")
    if _CONTROL.search(task):
        raise TaskInvalid("The task contains unsupported control characters.")
    return task


# ------------------------------------------------------------------------------------------------ cursor

def _checksum(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()[:8]


def encode_cursor(created_at: datetime, execution_id: uuid.UUID) -> str:
    payload = json.dumps({"t": created_at.isoformat(), "i": str(execution_id)}, separators=(",", ":")).encode()
    return f"v1.{base64.urlsafe_b64encode(payload).decode().rstrip('=')}.{_checksum(payload)}"


def decode_cursor(value) -> tuple[datetime, uuid.UUID]:
    try:
        version, body, check = value.split(".")
        if version != "v1" or len(body) > 200:
            raise ValueError
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        if check != _checksum(payload):
            raise ValueError
        data = json.loads(payload)
        if not isinstance(data, dict) or set(data) != {"t", "i"} or not isinstance(data["i"], str):
            raise ValueError
        created_at = datetime.fromisoformat(data["t"])
        if created_at.tzinfo is not None:
            raise ValueError
        return created_at, uuid.UUID(data["i"])
    except Exception:  # noqa: BLE001 - one fixed error for every malformed cursor
        raise CursorError("invalid_cursor") from None


# ------------------------------------------------------------------------------------------------ store

def _owned(scope):
    return (AgentExecution.user_id == scope.user_id, AgentExecution.workspace_id == scope.workspace_id)


def _view(row: AgentExecution) -> ExecutionView:
    return ExecutionView(row.id, row.agent_source, row.agent_key, row.agent_name, row.agent_version_id, row.agent_version, row.status,
                         row.task, row.result, row.error_code, row.error_message, row.provider, row.model, row.created_at,
                         row.started_at, row.completed_at)


@dataclass(frozen=True)
class DbLimits:
    """Database-wide admission limits (every process together). ``per_user`` is across all processes, which the in-process
    ``Limits`` cannot know."""

    total: int = DB_MAX_CONCURRENT
    per_user: int = DB_MAX_PER_USER


def final_values(status: str, *, result: str | None = None, error_code: str | None = None, provider: str | None = None, model: str | None = None) -> dict:
    """The column values of the single running -> final transition (shared by the plain and the atomic plan-task finish)."""
    values = {"status": status, "completed_at": datetime.utcnow(), "result": _truncate(result, MAX_RESULT_CHARS) if result is not None else None,
              "error_code": error_code, "error_message": _truncate(MESSAGES[error_code], MAX_ERROR_CHARS) if error_code else None}
    if provider is not None:
        values["provider"] = provider
    if model is not None:
        values["model"] = model[:100]
    return values


class ExecutionStore:
    """Persistence for execution records. Every read is constrained by the scope's user AND workspace.

    M4-A2: ``owner_id`` (the id of the process that runs executions, see ``processes.py``) is written to ``execution_owners`` in the
    same transaction as the execution, and ``limits`` (``DbLimits``) are enforced at admission, in that same transaction, against the
    executions of every LIVE process, so the limits hold across processes."""

    def __init__(self, session_factory, *, owner_id: str | None = None, limits: DbLimits | None = None, clock=datetime.utcnow):
        self._sessions = session_factory
        self.owner_id, self._limits, self._clock = owner_id, limits, clock

    # ---------------------------------------------------------------------------------------------- admission

    def _live_running(self, session):
        """Running executions that occupy a slot: those of a live process, and legacy ones (no owner row) that were never swept."""
        now = db_now(session, self._clock)
        owned = exists().where(ExecutionOwner.execution_id == AgentExecution.id)
        alive = exists().where(ExecutionOwner.execution_id == AgentExecution.id, WorkerProcess.owner_id == ExecutionOwner.owner_id, WorkerProcess.expires_at > now)
        suspended = exists().where(ToolSuspension.execution_id == AgentExecution.id, ToolSuspension.resumed_at.is_(None))  # M4-C: a task waiting for a human holds no slot
        # M6: a RESUMED task (its suspension is closed) occupies a slot until it ends, whichever process resumed it: its owner row names the process that SUSPENDED it, which may be gone
        resumed = exists().where(ToolSuspension.execution_id == AgentExecution.id, ToolSuspension.resumed_at.is_not(None))
        return and_(AgentExecution.status == "running", ~suspended, or_(~owned, alive, resumed))

    def _admit(self, session, user_id) -> None:
        """Refuse (``Busy``) when the database-wide limits are reached. Serialised: PostgreSQL takes an advisory transaction lock, SQLite
        takes its write lock first, so two concurrent admissions can never both see a free slot."""
        if self._limits is None:
            return
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADMISSION_LOCK})
        else:
            session.execute(update(WorkerProcess).where(WorkerProcess.owner_id == "").values(kind="api"))  # a write statement: takes the write lock
        occupied = self._live_running(session)
        total = session.execute(select(func.count()).select_from(AgentExecution).where(occupied)).scalar()
        mine = session.execute(select(func.count()).select_from(AgentExecution).where(occupied, AgentExecution.user_id == user_id)).scalar()
        if total >= self._limits.total or mine >= self._limits.per_user:
            raise Busy()

    def admit_resumed(self, session, user_id) -> None:
        """M6: the same atomic admission as a start, run by the transaction that CONSUMES an approval and so un-suspends a task (a suspended task holds no slot, so a resumed one must
        acquire one BEFORE its action is dispatched). Raises ``Busy``: the transaction rolls back, the approval stays approved and the task stays suspended, tried again later."""
        self._admit(session, user_id)

    def insert(self, session, scope, resolved: ResolvedAgent, agent_name: str, task: str, *, provider: str | None, model: str | None) -> uuid.UUID:
        """Admit and create the ``running`` record (and its owner row) in the CALLER's transaction."""
        self._admit(session, scope.user_id)
        now = datetime.utcnow()
        custom = resolved.source == "custom"
        row = AgentExecution(
            user_id=scope.user_id, workspace_id=scope.workspace_id, agent_source=resolved.source, agent_key=resolved.key,
            agent_name=agent_name[:80], agent_version_id=resolved.version_id if custom else None,
            agent_version=resolved.version if custom else None, status="running", task=redact_secrets(task),
            provider=provider, model=model, created_at=now, started_at=now,
        )
        session.add(row)
        session.flush()
        if self.owner_id is not None:
            session.add(ExecutionOwner(execution_id=row.id, owner_id=self.owner_id, created_at=now))
            session.flush()
        return row.id

    def start(self, scope, resolved: ResolvedAgent, agent_name: str, task: str, *, provider: str | None, model: str | None) -> uuid.UUID:
        with self._sessions() as session, session.begin():
            return self.insert(session, scope, resolved, agent_name, task, provider=provider, model=model)

    def finish(self, execution_id, status: str, *, result: str | None = None, error_code: str | None = None,
               provider: str | None = None, model: str | None = None) -> bool:
        """The single running -> final transition. False if the row was already final (so it is never overwritten)."""
        values = final_values(status, result=result, error_code=error_code, provider=provider, model=model)
        with self._sessions() as session, session.begin():
            done = session.execute(update(AgentExecution).where(AgentExecution.id == execution_id, AgentExecution.status == "running").values(**values))
            return done.rowcount == 1

    def fail_orphaned(self) -> int:
        """Close STANDALONE executions whose process is gone (startup and periodic sweep). An execution is orphaned when it is
        ``running``, does not belong to a task of a still-``running`` plan run (their lease owner, or lease recovery, closes those
        with the run), and its owner process is not alive: no owner row (created before M4-A2, so the old single-process rule
        applies: closed at startup), or an owner whose ``worker_processes`` row is missing or expired. An execution of a live process
        is never touched, so a second process starting up cannot interrupt another process's live REST/WebSocket execution."""
        live = select(PlanTaskRun.execution_id).join(PlanRun, PlanRun.id == PlanTaskRun.run_id).where(PlanRun.status == "running", PlanTaskRun.execution_id.is_not(None))
        with self._sessions() as session, session.begin():
            now = db_now(session, self._clock)
            owner_alive = exists().where(ExecutionOwner.execution_id == AgentExecution.id, WorkerProcess.owner_id == ExecutionOwner.owner_id, WorkerProcess.expires_at > now)
            orphans = list(session.execute(select(AgentExecution.id).where(AgentExecution.status == "running", AgentExecution.id.not_in(live), ~owner_alive)).scalars())
            from agentplatform.tools.invoker import close_open

            close_open(session, orphans)  # in-flight tool calls end with their execution (interrupted, or uncertain for a tool with side effects)
            done = session.execute(update(AgentExecution).where(AgentExecution.id.in_(orphans) if orphans else AgentExecution.id.is_(None)).values(
                status="failed", error_code="interrupted", error_message=MESSAGES["interrupted"], completed_at=datetime.utcnow()))
            return done.rowcount

    def get(self, scope, execution_id) -> ExecutionView:
        scope = require_scope(scope)
        with self._sessions() as session:
            row = session.execute(select(AgentExecution).where(AgentExecution.id == execution_id, *_owned(scope))).scalar_one_or_none()
            if row is None:
                raise ExecutionNotFound("not found")  # unknown and foreign ids are indistinguishable
            return _view(row)

    def list(self, scope, *, limit: int = PAGE_DEFAULT, cursor: str | None = None, agent_key: str | None = None) -> dict:
        scope = require_scope(scope)
        if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
            raise ValueError("invalid_limit")
        query = select(AgentExecution).where(*_owned(scope))
        if agent_key is not None:
            query = query.where(AgentExecution.agent_key == agent_key)
        if cursor is not None:
            c_time, c_id = decode_cursor(cursor)
            query = query.where(or_(AgentExecution.created_at < c_time, and_(AgentExecution.created_at == c_time, AgentExecution.id < c_id)))
        with self._sessions() as session:
            rows = session.execute(query.order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()).limit(limit + 1)).scalars().all()
            page = rows[:limit]
            return {"items": [_view(row) for row in page],
                    "next_cursor": encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None}

    def count_running(self) -> int:
        with self._sessions() as session:
            return session.execute(select(func.count()).select_from(AgentExecution).where(AgentExecution.status == "running")).scalar()


# ------------------------------------------------------------------------------------------------ limits

class Limits:
    """In-process concurrency limits. ``acquire`` has no await in it, so it is atomic on the event loop."""

    def __init__(self, total: int = MAX_CONCURRENT, per_user: int = MAX_PER_USER):
        self.total, self.per_user = total, per_user
        self._active = 0
        self._by_user = defaultdict(int)

    def acquire(self, user_id) -> bool:
        if self._active >= self.total or self._by_user[user_id] >= self.per_user:
            return False
        self._active += 1
        self._by_user[user_id] += 1
        return True

    def release(self, user_id) -> None:
        self._active = max(0, self._active - 1)
        self._by_user[user_id] = max(0, self._by_user[user_id] - 1)
        if not self._by_user[user_id]:
            del self._by_user[user_id]


# ------------------------------------------------------------------------------------------------ service

class ExecutionService:
    def __init__(self, session_factory, *, orchestrator, providers, short_term, long_term, limits: Limits,
                 provider_timeout: float = PROVIDER_TIMEOUT_SECONDS, custom_timeout: float = CUSTOM_TIMEOUT_SECONDS,
                 builtin_timeout: float = BUILTIN_TIMEOUT_SECONDS, owner_id: str | None = None, db_limits: DbLimits | None = None, tools=None, usage=None):
        from agentplatform.resilience import make_sink

        self._provider_sink = make_sink(session_factory)  # M6b: where a ResilientProvider reports waits, retries and the final outcome (persisted with the run)
        self._usage = usage  # M6: a UsageRecorder (or None): what each provider request really used
        self.tools = tools  # M4-B: a ``ToolInvoker`` (None: agents run without tools, exactly as before)
        self.agents = AgentService(session_factory)
        self.store = ExecutionStore(session_factory, owner_id=owner_id, limits=db_limits)
        if tools is not None:
            tools.store.admit = self.store.admit_resumed  # M6: consuming an approval of a suspended task is an admission
        self._orchestrator, self._providers = orchestrator, providers
        self._short_term, self._long_term, self._limits = short_term, long_term, limits
        self._provider_timeout, self._custom_timeout, self._builtin_timeout = provider_timeout, custom_timeout, builtin_timeout

    async def resolve(self, scope, key) -> ResolvedAgent | None:
        scope = require_scope(scope)
        if not isinstance(key, str):
            return None
        return await asyncio.to_thread(self.agents.resolve, scope, key)

    async def run(self, scope, key: str, task, *, delegate: bool = False, pin=None, on_started=None, starter=None, finalize=None, tool_run=None) -> Outcome:
        """Run one agent and return its outcome. Raises ``ExecutionRefused`` (nothing recorded) or ``ExecutionFailed``
        (recorded). ``delegate`` keeps the WebSocket's existing behaviour for the CEO (it may delegate to other agents).

        Plan execution uses optional hooks and changes nothing for other callers. ``pin`` is the exact ``custom_agent_versions`` id
        a plan approved (the agent runs THAT version, not the current one; unavailable -> ``AgentUnavailable``). ``on_started`` (M3-B,
        kept for compatibility) is a synchronous callback run after the ``running`` record exists. M4-A2 adds the two ATOMIC hooks plan
        tasks use so a task and its execution record can never disagree:

        * ``starter(resolved, name, task, provider, model) -> execution_id`` REPLACES the plain record creation: it creates the
          ``running`` execution (with the database-wide admission check and the owner row) AND marks the task run ``running`` with the
          link, in ONE fenced transaction (it raises ``Busy`` when no slot is free, ``LeaseLost`` when ownership is lost; nothing is
          written then, and the agent is never called);
        * ``finalize(execution_id, status, **values) -> bool`` REPLACES the plain ``running -> final`` write: it finishes the execution
          AND the task run in ONE fenced transaction (on ``LeaseLost`` nothing is written and both stay ``running`` for recovery).

        ``tool_run`` (M4-B, ``ToolRunContext``) links the tool calls of a plan task to its run and task and carries the lease fence, so a stale
        owner can start or finish no tool call."""
        scope = require_scope(scope)
        task = validate_task(task)
        if pin is None:
            resolved = await self.resolve(scope, key)
        else:
            resolved = await asyncio.to_thread(self.agents.resolve_pinned, scope, key, pin) if isinstance(key, str) else None
        if resolved is None:
            raise AgentUnavailable()
        name, provider_name, model_name = key, None, None
        if resolved.source == "custom":
            name, provider_name, model_name = resolved.definition.name, resolved.definition.model.provider, resolved.definition.model.name
        else:
            name, provider_name = key.upper() if len(key) <= 3 else key.capitalize(), "groq"
        if not self._limits.acquire(scope.user_id):
            raise Busy()
        try:
            if starter is not None:
                execution_id = await asyncio.to_thread(starter, resolved, name, task, provider_name, model_name)
            else:
                execution_id = await asyncio.to_thread(self.store.start, scope, resolved, name, task, provider=provider_name, model=model_name)
            finish = finalize or self.store.finish
            try:
                if on_started is not None:
                    await asyncio.to_thread(on_started, execution_id)
                if resolved.source == "custom":
                    text, extra, provider_name, model_name = await self._run_custom(scope, resolved, task, execution_id, tool_run)
                else:
                    text, extra = await self._run_builtin(scope, key, task, delegate)
            except TaskSuspended:
                raise  # M4-C: durably suspended on a human approval. Not a failure and not a finish: the task and its execution stay ``running``
            except asyncio.CancelledError:
                self._finish_cancelled(execution_id, finish)
                raise
            except Exception as exc:  # noqa: BLE001 - classified below; nothing but a fixed code leaves this block
                code = self._code_for(exc)
                logger.error("Execution %s failed (%s: %s)", execution_id, code, type(exc).__name__)
                await asyncio.to_thread(finish, execution_id, "failed", error_code=code)
                raise ExecutionFailed(execution_id, code) from None
            await self._finish_shielded(execution_id, "succeeded", finish, result=text, provider=provider_name, model=model_name)
            if resolved.source == "custom":
                await asyncio.to_thread(self._remember, scope, resolved.key, task, _truncate(text, MAX_RESULT_CHARS))  # after the commit: memory never holds a reply the record does not
            return Outcome(execution_id, "succeeded", resolved.source, key, name, resolved.version if resolved.source == "custom" else None,
                           _truncate(text, MAX_RESULT_CHARS), provider_name, model_name, extra)
        finally:
            self._limits.release(scope.user_id)

    # ---------------------------------------------------------------------------------------------- helpers

    @staticmethod
    def _code_for(exc: BaseException) -> str:
        if isinstance(exc, ProviderError):
            return exc.code
        if isinstance(exc, asyncio.TimeoutError):
            return "timeout"
        return provider_error_code(exc) or "internal_error"

    def _finish_cancelled(self, execution_id, finish) -> None:
        """Synchronous on purpose: this runs while the task is being cancelled (a closed socket, a stopping server), when
        awaiting a worker thread can be cut short and would leave a ``running`` row; a single-row UPDATE is cheap."""
        try:
            finish(execution_id, "cancelled", error_code="cancelled")
        except Exception as exc:  # noqa: BLE001 - the startup sweep closes anything that still slips through
            logger.error("Could not record the cancellation of execution %s (%s)", execution_id, type(exc).__name__)

    async def _finish_shielded(self, execution_id, status: str, finish, **values) -> None:
        """Finish a succeeded record even if a cancellation arrives while it is being written."""
        job = asyncio.ensure_future(asyncio.to_thread(finish, execution_id, status, **values))
        try:
            await asyncio.shield(job)
        except asyncio.CancelledError:
            await job  # the cancellation is re-raised by the caller; the write itself completes first
            raise

    async def _run_builtin(self, scope, key: str, task: str, delegate: bool):
        if delegate and key == "ceo":
            async def call():
                result = await self._orchestrator.run_with_delegation(key, task, scope)
                return result["primary_response"], {"primary_agent": result["primary_agent"], "primary_response": result["primary_response"],
                                                   "delegations": result["delegations"]}
        else:
            async def call():
                return await self._orchestrator.run_agent(key, task, scope), {}
        text, extra = await asyncio.wait_for(call(), self._builtin_timeout)
        if not isinstance(text, str):
            raise ProviderError("invalid_response")
        return text, extra

    def _provider_context(self, scope, resolved, execution_id, tool_run) -> None:
        from agentplatform.resilience import CONTEXT

        CONTEXT.set({"scope": scope, "sink": self._provider_sink, "provider": resolved.definition.model.provider, "execution_id": execution_id,
                     "run_id": tool_run.run_id if tool_run else None, "task_key": tool_run.task_key if tool_run else None})

    async def _run_custom(self, scope, resolved: ResolvedAgent, task: str, execution_id=None, tool_run=None):
        definition = resolved.definition  # the version pinned at start; later edits cannot reach this call
        self._provider_context(scope, resolved, execution_id, tool_run)
        provider = self._providers.get(definition.model.provider)
        history = await asyncio.to_thread(self._history, scope, resolved.key)
        granted, offered = self._granted_tools(definition)
        messages = [*history, Message("user", task)]

        def request(tools=(), force=None):
            return ProviderRequest(
                model=definition.model.name, system=build_system_prompt(definition, offered), messages=tuple(messages), temperature=definition.model.temperature,
                max_tokens=definition.model.max_tokens, timeout_seconds=self._provider_timeout,
                tools=tuple(ToolDefinition(t.wire_name, t.summary, t.input_schema()) for t in tools), tool_choice=force,
            )

        if tool_run is not None and tool_run.required_tool:  # M6b: a task that requires an action cannot even start without the means to perform it
            wanted = self.tools.registry.resolve_pin(tool_run.required_tool) if self.tools is not None else None
            if wanted is None or wanted.wire_name not in granted or wanted not in offered:
                raise ProviderError("required_action_missing")
        if not offered:  # no tool granted (or no tool layer): the single text call, exactly as before M4-B
            response = await asyncio.wait_for(provider.complete(request()), self._custom_timeout)
            await self._note_usage(scope, resolved, execution_id, tool_run, response)
        else:
            # M4-C: a task that may ask a human for approval gets the approval window on top of its working time (the wait itself is bounded by the approval's expiry)
            window = self.tools.approvals.ttl_seconds if self.tools.approvals is not None and tool_run is None and any(self.tools.registry.gated(s) for s in offered) else 0.0
            response = await asyncio.wait_for(self._tool_loop(provider, request, messages, granted, offered, scope, resolved, execution_id, tool_run, task=task), self._custom_timeout + window)
        if not isinstance(response.text, str) or not response.text.strip():
            raise ProviderError("invalid_response")
        text = _truncate(response.text, MAX_RESULT_CHARS)
        return text, {}, definition.model.provider, response.model or definition.model.name

    def _granted_tools(self, definition) -> tuple:
        """(granted, offered): the tools PINNED in this agent version (wire name -> spec) and the subset that may actually run now. Only the
        offered ones are shown to the model; the invoker re-checks every call against ``granted`` regardless of what the model sends."""
        if self.tools is None or not definition.allowed_tools:
            return {}, ()
        granted = {}
        for pinned in definition.allowed_tools:
            spec = self.tools.registry.resolve_pin(pinned)
            if spec is not None:
                granted[spec.wire_name] = spec
        approvals = getattr(self.tools, "approvals", None) is not None
        return granted, tuple(s for s in granted.values() if self.tools.registry.executable(s) or (approvals and self.tools.registry.gated(s)))

    async def _tool_loop(self, provider, request, messages, granted, offered, scope, resolved, execution_id, tool_run, resume=None, task: str = ""):
        """A bounded exchange with the model: it may request tools; each request is validated, authorised, recorded and run server-side; the
        results go back as data. After ``MAX_TOOL_ROUNDS`` rounds, or ``MAX_TOOL_CALLS`` calls, the final answer is requested without tools.

        M4-C: for a PLAN TASK a call that needs a human approval does not wait in memory: the loop persists its state (the conversation, the calls of the
        round and which one waits) together with the approval and raises ``TaskSuspended``. ``resume`` (a ``SuspensionView``) re-enters the loop at exactly that call:
        the existing approval is validated and consumed atomically, and the exchange continues from the persisted conversation."""
        from agentplatform.tools.invoker import InvocationContext

        ctx = InvocationContext(scope=scope, execution_id=execution_id, agent_key=resolved.key, agent_version_id=resolved.version_id, allowed=granted, run=tool_run)
        durable = tool_run is not None and getattr(self.tools, "approvals", None) is not None
        observability.set_context(workspace=scope.workspace_id, execution=execution_id, run=tool_run.run_id if tool_run else None, task=tool_run.task_key if tool_run else None)
        seq = resume.seq - 1 if resume is not None else 0  # the resumed call keeps ITS step number
        pending = resume
        required = self.tools.registry.resolve_pin(tool_run.required_tool) if tool_run is not None and tool_run.required_tool else None
        corrected, force = False, None
        for round_number in range(resume.round_number if resume is not None else 0, MAX_TOOL_ROUNDS + 1):
            if pending is not None:
                calls, first, approval_to_use = pending.calls, pending.call_index, pending.approval_id
                said = next((m.content for m in reversed(messages) if m.role == "assistant"), "")
                pending = None
            else:
                final_round = round_number == MAX_TOOL_ROUNDS or seq >= MAX_TOOL_CALLS
                response = await provider.complete(request(() if final_round else offered, force))
                await self._note_usage(scope, resolved, execution_id, tool_run, response)
                if not response.tool_calls:
                    # Execution truth (enforced here, not in the prompt): a side effect whose outcome is unproven, or that confirmedly failed, can never end as a successful task, required or not.
                    if tool_run is not None:
                        truth = await asyncio.to_thread(self.tools.store.side_effect_state, execution_id, tool_run)
                        if truth != "ok":
                            raise ProviderError("side_effect_outcome_unknown" if truth == "unknown" else "side_effect_failed")
                    if required is None:
                        return response
                    # M6b: the model produced prose. Prose is not an action: only a recorded, successful invocation of the required tool completes the task.
                    state = await asyncio.to_thread(self.tools.store.action_state, execution_id, required.id, required.version)
                    if state == "succeeded":
                        return response
                    if state == "none" and not corrected and not final_round:
                        corrected, force = True, required.wire_name  # one server-side correction: ask again, this time requiring the call itself
                        messages.append(Message("assistant", response.text or "(no answer)"))
                        messages.append(Message("user", f"The required action has not been performed. Do not describe it: call the tool {required.wire_name} now, with correct arguments."))
                        continue
                    raise ProviderError("required_action_missing")  # never performed (or refused, rejected, failed): the task is not complete
                if final_round:
                    raise ProviderError("invalid_response")  # tools were not offered but the model asked for one anyway
                calls, first, approval_to_use, said = response.tool_calls[:MAX_CALLS_PER_ROUND], 0, None, response.text
                messages.append(Message("assistant", response.text or "", tool_calls=calls))
            for index in range(first, len(calls)):
                call = calls[index]
                seq += 1
                if seq > MAX_TOOL_CALLS:
                    outcome = await self.tools.refuse(ctx, call.name, seq, "limit_exceeded")
                else:
                    suspend = (lambda idx=index, cs=calls, rn=round_number: {"task": task, "messages": messages, "calls": cs, "round_number": rn, "call_index": idx}) if durable else None
                    outcome = await self.tools.invoke(ctx, call.name, call.arguments, seq, reason=said, suspend=suspend, resume_approval=approval_to_use)
                    approval_to_use = None  # only the first call of a resumed round carries the approval
                messages.append(Message("tool", outcome.content, tool_call_id=call.id))
            force = None
        raise ProviderError("invalid_response")  # unreachable: the last round never returns tool calls

    async def _note_usage(self, scope, resolved, execution_id, tool_run, response) -> None:
        """Record what ONE provider request used (best effort: accounting never breaks an execution)."""
        if self._usage is None:
            return
        await asyncio.to_thread(self._usage.record, scope, purpose="agent", provider=resolved.definition.model.provider, model=response.model, usage=response.usage, run_id=tool_run.run_id if tool_run else None,
                                task_key=tool_run.task_key if tool_run else None, execution_id=execution_id, agent_key=resolved.key)

    async def resume(self, scope, key: str, *, pin, execution_id, approval_id, finalize, tool_run) -> "Outcome":
        """Resume a task that was durably suspended on a human approval (M4-C), in THIS process, whichever process suspended it. The agent runs the exact pinned
        version it was suspended with; the persisted conversation is re-entered at the waiting call; the approval is validated and consumed atomically with the
        invocation's intent row (so the action is dispatched at most once, and never if the approval is rejected, expired, invalid or already used). Raises the same
        exceptions as ``run`` (``Busy``: no slot now, nothing was written; ``TaskSuspended``: it asked again and was suspended again)."""
        scope = require_scope(scope)
        resolved = await asyncio.to_thread(self.agents.resolve_pinned, scope, key, pin) if isinstance(key, str) else None
        if resolved is None or resolved.source != "custom" or self.tools is None or self.tools.approvals is None:
            raise AgentUnavailable()
        susp = await asyncio.to_thread(self.tools.approvals.load_suspension, scope, approval_id)
        if susp.execution_id != execution_id:
            raise AgentUnavailable()  # the suspension is not this task's: never resume on a mismatch
        if not self._limits.acquire(scope.user_id):
            raise Busy()
        try:
            try:
                text, provider_name, model_name = await self._resume_custom(scope, resolved, execution_id, tool_run, susp)
            except (TaskSuspended, Busy):
                raise  # M6: Busy here = the database-wide admission refused the resume (nothing was written; it stays suspended)
            except asyncio.CancelledError:
                self._finish_cancelled(execution_id, finalize)
                raise
            except Exception as exc:  # noqa: BLE001 - classified like ``run``; nothing but a fixed code leaves this block
                code = self._code_for(exc)
                logger.error("Execution %s failed on resume (%s: %s)", execution_id, code, type(exc).__name__)
                await asyncio.to_thread(finalize, execution_id, "failed", error_code=code)
                raise ExecutionFailed(execution_id, code) from None
            await self._finish_shielded(execution_id, "succeeded", finalize, result=text, provider=provider_name, model=model_name)
            await asyncio.to_thread(self._remember, scope, resolved.key, susp.task, _truncate(text, MAX_RESULT_CHARS))
            return Outcome(execution_id, "succeeded", resolved.source, key, resolved.definition.name, resolved.version, _truncate(text, MAX_RESULT_CHARS), provider_name, model_name, {})
        finally:
            self._limits.release(scope.user_id)

    async def _resume_custom(self, scope, resolved: ResolvedAgent, execution_id, tool_run, susp):
        definition = resolved.definition
        self._provider_context(scope, resolved, execution_id, tool_run)
        provider = self._providers.get(definition.model.provider)
        granted, offered = self._granted_tools(definition)
        messages = list(susp.messages)

        def request(tools=(), force=None):
            return ProviderRequest(
                model=definition.model.name, system=build_system_prompt(definition, offered), messages=tuple(messages), temperature=definition.model.temperature,
                max_tokens=definition.model.max_tokens, timeout_seconds=self._provider_timeout,
                tools=tuple(ToolDefinition(t.wire_name, t.summary, t.input_schema()) for t in tools), tool_choice=force,
            )

        response = await asyncio.wait_for(self._tool_loop(provider, request, messages, granted, offered, scope, resolved, execution_id, tool_run, resume=susp, task=susp.task), self._custom_timeout)
        if not isinstance(response.text, str) or not response.text.strip():
            raise ProviderError("invalid_response")
        return _truncate(response.text, MAX_RESULT_CHARS), definition.model.provider, response.model or definition.model.name

    def _history(self, scope, key: str) -> tuple:
        """Recent turns for this agent (the same short-term memory the built-ins use). Best effort: no history is better
        than a failed execution."""
        try:
            turns = self._short_term.get(scope, key)[-HISTORY_TURNS:]
            return tuple(Message(t["role"], t["content"]) for t in turns if t.get("role") in ("user", "assistant") and isinstance(t.get("content"), str))
        except Exception as exc:  # noqa: BLE001
            logger.warning("History unavailable for a custom agent (%s)", type(exc).__name__)
            return ()

    def _remember(self, scope, key: str, task: str, reply: str) -> None:
        """Same memory writes as the built-ins; best effort once the answer exists."""
        try:
            self._short_term.save(scope, key, "user", task)
            self._short_term.save(scope, key, "assistant", reply)
            self._long_term.save(scope, key, reply)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Memory write failed for a custom agent (%s)", type(exc).__name__)
