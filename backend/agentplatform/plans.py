"""Persisted plans, immutable revisions and the planning service (M3-A).

OWNERSHIP. Every operation takes a ``MemoryScope`` and constrains by BOTH ``user_id`` and ``workspace_id``; a foreign or
unknown plan is simply "not found". Identifiers from a client are never trusted for ownership.

LIFECYCLE. ``draft`` (a validated plan that can still be revised) -> ``approved`` (one specific revision approved for FUTURE
execution) or ``cancelled``. Creating, revising and approving a plan NEVER starts execution (that is M3-B). Every revision is
immutable (the database rejects UPDATE/DELETE on revisions, tasks and dependencies): editing writes a new revision with the
caller's ``base_revision`` (compare-and-set on ``latest_revision``; the unique ``(plan_id, revision)`` key is the backstop).
Revising an approved plan creates a new draft revision and withdraws the approval (the approved revision stays readable in the
history). Approval re-validates the plan against the agents available right now and approves exactly the revision named.

TRACEABILITY. A custom-agent task pins the exact immutable agent version chosen when its revision was created; a revision keeps
an earlier pin for an unchanged task (with a warning when a newer version exists). Built-in tasks record the key only.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from agentplatform.execution import Busy, Limits, decode_cursor, encode_cursor
from agentplatform.planning import (
    PlanDraft,
    PlanningError,
    Planner,
    PlanRejected,
    Unplannable,
    assemble,
    build_catalog,
    validate_objective,
)
from agentplatform.schema import redact_secrets
from agentplatform.service import AgentService
from authz.scope import require_scope
from memory.models import Plan, PlanRevision, PlanRun, PlanTask, PlanTaskDependency, User

MAX_PLANS_PER_WORKSPACE = 200
MAX_REVISIONS_PER_PLAN = 50
PAGE_DEFAULT, PAGE_MAX = 20, 100


class PlanError(Exception):
    code = "plan_error"


class PlanNotFound(PlanError):
    code = "not_found"


class RevisionNotFound(PlanError):
    code = "revision_not_found"


class PlanConflict(PlanError):
    code = "revision_conflict"

    def __init__(self, latest_revision: int):
        super().__init__(f"latest revision is {latest_revision}")
        self.latest_revision = latest_revision


class PlanCancelled(PlanError):
    code = "plan_cancelled"


class PlanLimitReached(PlanError):
    code = "limit_reached"

    def __init__(self, what: str):
        super().__init__(what)
        self.what = what


class PlanInvalid(PlanError):
    """Approval refused: the plan no longer validates against the agents available now."""

    code = "plan_invalid"

    def __init__(self, errors: list, warnings: list):
        super().__init__("plan invalid")
        self.errors, self.warnings = errors, warnings


@dataclass(frozen=True)
class TaskView:
    key: str
    position: int
    level: int
    title: str
    description: str
    expected_output: str
    rationale: str
    dependency_reason: str | None
    complexity: str
    priority: str
    agent_source: str
    agent_key: str
    agent_name: str
    agent_version_id: object | None
    agent_version: int | None
    depends_on: tuple
    required_tool: str | None = None


@dataclass(frozen=True)
class RevisionView:
    revision: int
    origin: str
    interpretation: str
    warnings: tuple
    unsupported: tuple
    provider: str | None
    model: str | None
    created_at: datetime
    tasks: tuple
    levels: tuple


@dataclass(frozen=True)
class PlanView:
    id: object
    objective: str
    status: str
    latest_revision: int
    approved_revision: int | None
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None
    current: RevisionView
    created_by: str | None = None  # the creator's e-mail (workspace members already see each other's e-mail on the Team page); never an id


def _owned(scope):
    """WRITE access: the plan's creator, inside the workspace."""
    return (Plan.user_id == scope.user_id, Plan.workspace_id == scope.workspace_id)


def _seen(scope):
    """READ access (sprint 5): every member of the workspace sees the workspace's plans; another workspace's plans are indistinguishable from nothing."""
    return (Plan.workspace_id == scope.workspace_id,)


def _levels(tasks) -> tuple:
    out: dict = {}
    for t in sorted(tasks, key=lambda t: (t.level, t.position)):
        out.setdefault(t.level, []).append(t.key)
    return tuple(tuple(out[level]) for level in sorted(out))


class PlanStore:
    def __init__(self, session_factory):
        self._sessions = session_factory

    # ------------------------------------------------------------------------------------------ reading

    def _plan(self, session, scope, plan_id, write: bool = False) -> Plan:
        plan = session.execute(select(Plan).where(Plan.id == plan_id, *(_owned(scope) if write else _seen(scope)))).scalar_one_or_none()
        if plan is None:
            raise PlanNotFound("not found")  # unknown and foreign ids are indistinguishable
        return plan

    def _revision_view(self, session, plan_id, number: int) -> RevisionView:
        row = session.execute(select(PlanRevision).where(PlanRevision.plan_id == plan_id, PlanRevision.revision == number)).scalar_one_or_none()
        if row is None:
            raise RevisionNotFound("revision not found")
        rows = session.execute(select(PlanTask).where(PlanTask.revision_id == row.id).order_by(PlanTask.position)).scalars().all()
        edges: dict = {}
        for dep in session.execute(select(PlanTaskDependency).where(PlanTaskDependency.revision_id == row.id)
                                   .order_by(PlanTaskDependency.task_key, PlanTaskDependency.depends_on_key)).scalars():
            edges.setdefault(dep.task_key, []).append(dep.depends_on_key)
        tasks = tuple(TaskView(t.task_key, t.position, t.level, t.title, t.description, t.expected_output, t.rationale, t.dependency_reason,
                               t.complexity, t.priority, t.agent_source, t.agent_key, t.agent_name, t.agent_version_id, t.agent_version,
                               tuple(edges.get(t.task_key, ())), t.required_tool) for t in rows)
        return RevisionView(row.revision, row.origin, row.interpretation, tuple(row.warnings or ()), tuple(row.unsupported or ()),
                            row.provider, row.model, row.created_at, tasks, _levels(tasks))

    def _view(self, session, plan: Plan, revision: int | None = None) -> PlanView:
        return PlanView(plan.id, plan.objective, plan.status, plan.latest_revision, plan.approved_revision, plan.created_at, plan.updated_at,
                        plan.approved_at, self._revision_view(session, plan.id, revision or plan.latest_revision),
                        session.execute(select(User.email).where(User.id == plan.user_id)).scalar_one_or_none())

    def get(self, scope, plan_id) -> PlanView:
        scope = require_scope(scope)
        with self._sessions() as session:
            return self._view(session, self._plan(session, scope, plan_id))

    def list_plans(self, scope, *, status: str = "active", limit: int = PAGE_DEFAULT, cursor: str | None = None) -> dict:
        scope = require_scope(scope)
        if status not in ("active", "draft", "approved", "cancelled", "all"):
            raise ValueError("invalid_status")
        if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
            raise ValueError("invalid_limit")
        query = select(Plan).where(*_seen(scope))
        if status == "active":
            query = query.where(Plan.status != "cancelled")
        elif status != "all":
            query = query.where(Plan.status == status)
        if cursor is not None:
            c_time, c_id = decode_cursor(cursor)
            query = query.where((Plan.created_at < c_time) | ((Plan.created_at == c_time) & (Plan.id < c_id)))
        with self._sessions() as session:
            rows = session.execute(query.order_by(Plan.created_at.desc(), Plan.id.desc()).limit(limit + 1)).scalars().all()
            page = rows[:limit]
            counts = {}
            for plan in page:
                counts[plan.id] = session.execute(
                    select(func.count()).select_from(PlanTask).join(PlanRevision, PlanRevision.id == PlanTask.revision_id)
                    .where(PlanRevision.plan_id == plan.id, PlanRevision.revision == plan.latest_revision)).scalar()
            creators = dict(session.execute(select(User.id, User.email).where(User.id.in_({p.user_id for p in page}))).all()) if page else {}
            return {"items": [(plan, counts[plan.id]) for plan in page], "creators": creators,
                    "next_cursor": encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None}

    def list_revisions(self, scope, plan_id) -> list:
        scope = require_scope(scope)
        with self._sessions() as session:
            plan = self._plan(session, scope, plan_id)
            rows = session.execute(select(PlanRevision).where(PlanRevision.plan_id == plan.id).order_by(PlanRevision.revision.desc())).scalars().all()
            return [(r.revision, r.origin, r.created_at, r.provider, r.model) for r in rows]

    def get_revision(self, scope, plan_id, number: int) -> RevisionView:
        scope = require_scope(scope)
        with self._sessions() as session:
            plan = self._plan(session, scope, plan_id)
            return self._revision_view(session, plan.id, number)

    # ------------------------------------------------------------------------------------------ writing

    @staticmethod
    def _insert_revision(session, plan_id, number: int, origin: str, draft: PlanDraft, provider, model, now) -> None:
        revision = PlanRevision(plan_id=plan_id, revision=number, origin=origin, interpretation=draft.interpretation,
                                warnings=list(draft.warnings), unsupported=list(draft.unsupported), provider=provider, model=model, created_at=now)
        session.add(revision)
        session.flush()
        for t in draft.tasks:
            session.add(PlanTask(revision_id=revision.id, task_key=t.key, position=t.position, level=t.level, title=t.title, description=t.description,
                                 expected_output=t.expected_output, rationale=t.rationale, dependency_reason=t.dependency_reason,
                                 complexity=t.complexity, priority=t.priority, agent_source=t.agent_source, agent_key=t.agent_key,
                                 agent_name=t.agent_name, agent_version_id=t.agent_version_id, agent_version=t.agent_version, required_tool=t.required_tool))
        session.flush()
        for t in draft.tasks:
            for dep in t.depends_on:
                session.add(PlanTaskDependency(revision_id=revision.id, task_key=t.key, depends_on_key=dep))
        session.flush()

    def create(self, scope, objective: str, draft: PlanDraft, *, provider: str | None, model: str | None) -> PlanView:
        scope = require_scope(scope)
        now = datetime.utcnow()
        with self._sessions() as session, session.begin():
            total = session.execute(select(func.count()).select_from(Plan).where(Plan.workspace_id == scope.workspace_id)).scalar()
            if total >= MAX_PLANS_PER_WORKSPACE:
                raise PlanLimitReached("plans")
            plan = Plan(user_id=scope.user_id, workspace_id=scope.workspace_id, objective=objective, status="draft", latest_revision=1,
                        created_at=now, updated_at=now)
            session.add(plan)
            session.flush()
            self._insert_revision(session, plan.id, 1, "model", draft, provider, model, now)
            return self._view(session, plan)

    def add_revision(self, scope, plan_id, base_revision: int, draft: PlanDraft) -> PlanView:
        scope = require_scope(scope)
        now = datetime.utcnow()
        try:
            with self._sessions() as session, session.begin():
                plan = self._plan(session, scope, plan_id, write=True)
                if plan.status == "cancelled":
                    raise PlanCancelled("cancelled")
                if base_revision != plan.latest_revision:
                    raise PlanConflict(plan.latest_revision)
                if plan.latest_revision >= MAX_REVISIONS_PER_PLAN:
                    raise PlanLimitReached("revisions")
                number = plan.latest_revision + 1
                self._insert_revision(session, plan.id, number, "revised", draft, None, None, now)
                swapped = session.execute(
                    update(Plan).where(Plan.id == plan.id, Plan.latest_revision == base_revision, Plan.status != "cancelled", *_owned(scope))
                    .values(latest_revision=number, status="draft", approved_revision=None, approved_at=None, updated_at=now))
                if swapped.rowcount != 1:
                    raise PlanConflict(base_revision + 1)
                session.refresh(plan)
                return self._view(session, plan)
        except IntegrityError:
            with self._sessions() as session:
                latest = session.execute(select(Plan.latest_revision).where(Plan.id == plan_id, *_owned(scope))).scalar()
            if latest is None:
                raise PlanNotFound("not found") from None
            raise PlanConflict(latest) from None

    def approve(self, scope, plan_id, revision: int) -> PlanView:
        scope = require_scope(scope)
        now = datetime.utcnow()
        with self._sessions() as session, session.begin():
            plan = self._plan(session, scope, plan_id, write=True)
            if plan.status == "cancelled":
                raise PlanCancelled("cancelled")
            if revision != plan.latest_revision:
                raise PlanConflict(plan.latest_revision)
            if plan.status != "approved":
                done = session.execute(update(Plan).where(Plan.id == plan.id, Plan.latest_revision == revision, Plan.status == "draft", *_owned(scope))
                                       .values(status="approved", approved_revision=revision, approved_at=now, updated_at=now))
                if done.rowcount != 1:
                    raise PlanConflict(plan.latest_revision)
                session.refresh(plan)
            return self._view(session, plan)

    def latest_run(self, scope, plan_id) -> dict | None:
        """The newest run of a plan (M3-B) as a small summary, or None. Read-only and scope-constrained; never changes a plan."""
        scope = require_scope(scope)
        with self._sessions() as session:
            run = session.execute(select(PlanRun).where(PlanRun.plan_id == plan_id, PlanRun.workspace_id == scope.workspace_id)
                                  .order_by(PlanRun.attempt.desc()).limit(1)).scalar_one_or_none()
            if run is None:
                return None
            return {"id": run.id, "status": run.status, "attempt": run.attempt, "created_at": run.created_at, "completed_at": run.completed_at}

    def cancel(self, scope, plan_id) -> PlanView:
        scope = require_scope(scope)
        now = datetime.utcnow()
        with self._sessions() as session, session.begin():
            plan = self._plan(session, scope, plan_id, write=True)
            if plan.status != "cancelled":
                session.execute(update(Plan).where(Plan.id == plan.id, *_owned(scope)).values(status="cancelled", approved_revision=None, approved_at=None, updated_at=now))
                session.refresh(plan)
            return self._view(session, plan)


# ------------------------------------------------------------------------------------------------ service

class PlanningService:
    def __init__(self, session_factory, *, providers, limits: Limits, planner: Planner | None = None, usage=None):
        self.agents = AgentService(session_factory)
        self.store = PlanStore(session_factory)
        self._planner = planner or Planner(providers)
        self._limits = limits
        self._usage = usage  # M6: a UsageRecorder (or None): what the planning requests really used

    async def create_plan(self, scope, objective) -> PlanView:
        """Plan an objective. Raises ValueError (bad objective), ``Busy`` (the shared concurrency limits, as for executions),
        ``PlanningError`` (provider or output problem), ``Unplannable`` or ``PlanRejected``. Nothing is stored in any of those."""
        scope = require_scope(scope)
        objective = validate_objective(objective)
        if not self._limits.acquire(scope.user_id):
            raise Busy()
        seen, plan_id = [], None
        from agentplatform.resilience import CONTEXT, make_sink

        CONTEXT.set({"scope": scope, "sink": make_sink(self.store._sessions), "provider": "groq"})  # M6b: planner retries are recorded too (before any plan exists)
        try:
            catalog = await asyncio.to_thread(build_catalog, self.agents, scope)
            proposal, provider, model = await self._planner.propose(objective, catalog, lambda name, response: seen.append((name, response)))
            unsupported = [u.model_dump() for u in proposal.unsupported]
            if not proposal.feasible or not proposal.tasks:
                raise Unplannable(proposal.interpretation, unsupported)
            resolver = lambda key: self.agents.resolve(scope, key)  # noqa: E731 - the authoritative resolution point
            draft = await asyncio.to_thread(assemble, list(proposal.tasks), resolver, interpretation=proposal.interpretation, unsupported=unsupported)
            created = await asyncio.to_thread(self.store.create, scope, redact_secrets(objective), draft, provider=provider, model=model)
            plan_id = created.id
            return created
        finally:
            self._limits.release(scope.user_id)
            if self._usage is not None:
                for name, response in seen:  # the model was really called whether or not a plan came out of it
                    self._usage.record(scope, purpose="plan", provider=name, model=response.model, usage=response.usage, plan_id=plan_id)

    def revise(self, scope, plan_id, base_revision: int, interpretation: str | None, items: list) -> PlanView:
        """A user revision: the same validation as a model proposal, and a NEW immutable revision. Unchanged tasks keep
        their earlier agent version pin."""
        scope = require_scope(scope)
        current = self.store.get(scope, plan_id)  # also the ownership check
        if current.status == "cancelled":
            raise PlanCancelled("cancelled")
        previous = self.store.get_revision(scope, plan_id, current.latest_revision)
        keep = {(t.key, t.agent_key): (t.agent_version_id, t.agent_version) for t in previous.tasks if t.agent_source == "custom"}
        draft = assemble(items, lambda key: self.agents.resolve(scope, key), interpretation=interpretation or previous.interpretation,
                         unsupported=[dict(u) for u in previous.unsupported], keep_pins=keep)
        return self.store.add_revision(scope, plan_id, base_revision, draft)

    def validate(self, scope, plan_id) -> dict:
        """Read-only: re-check the CURRENT revision against the agents available now. Persists nothing."""
        scope = require_scope(scope)
        view = self.store.get(scope, plan_id)
        errors, warnings = [], [dict(w) for w in view.current.warnings]
        unavailable: set = set()
        for t in view.current.tasks:
            if self.agents.resolve(scope, t.agent_key) is None:
                unavailable.add(t.key)
                errors.append({"code": "agent_unavailable", "task": t.key,
                               "message": f"Task '{t.key}' is assigned to '{t.agent_key}', which is no longer available (archived or removed)."})
        for t in view.current.tasks:
            blocked = sorted(set(t.depends_on) & unavailable)
            if blocked and t.key not in unavailable:
                errors.append({"code": "dependency_unsatisfiable", "task": t.key,
                               "message": f"Task '{t.key}' depends on {', '.join(repr(b) for b in blocked)}, which cannot run because its agent is not available."})
        if view.status == "cancelled":
            errors.append({"code": "plan_cancelled", "task": None, "message": "The plan was cancelled."})
        return {"valid": not errors, "revision": view.latest_revision, "status": view.status, "errors": sorted(errors, key=lambda e: (e["code"], e["task"] or "")),
                "warnings": warnings, "levels": [list(level) for level in view.current.levels]}

    def approve(self, scope, plan_id, revision: int) -> PlanView:
        report = self.validate(scope, plan_id)
        if report["revision"] == revision and report["errors"] and report["status"] != "cancelled":
            raise PlanInvalid(report["errors"], report["warnings"])
        return self.store.approve(scope, plan_id, revision)
