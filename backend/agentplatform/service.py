"""Ownership-scoped custom agents and immutable versions (M2-A).

EVERY operation takes a ``MemoryScope`` (the accepted M1-E scope: internal user id + authorized workspace id, issued only
from a verified Principal and a database membership check) and constrains by BOTH ``user_id`` and ``workspace_id``. No
identifier from a client is ever trusted for ownership; a foreign or unknown agent id is simply "not found".

LIFECYCLE (chosen for simplicity): every save creates a NEW immutable version; there is no mutable draft. Editing needs the
caller's ``base_version`` (the latest version they saw): a stale edit is refused with ``VersionConflict`` instead of
overwriting a newer one (compare-and-set on ``latest_version``; the unique ``(agent_id, version)`` key is the backstop).
Saving a definition identical to the current version creates nothing; the current version is always the latest one
(``current_version`` is kept as its own column so a later release can pin or promote versions without a schema change). Rollback inserts a new version copied from an old
one (``restored_from_version``): history is never changed (the database also rejects UPDATE/DELETE on version rows).
Archiving flips ``status`` only; archived agents keep all versions and cannot be edited until restored.

EXECUTION BOUNDARY: nothing here runs an agent. ``resolve`` is the read-only integration point for the next milestone: it
maps an agent key to either a built-in agent (unchanged, still executed by ``orchestrator``) or the CURRENT version of an
active custom agent, returning the exact ``version_id`` an execution record can pin.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from agentplatform.schema import (
    BUILTIN_AGENT_KEYS,
    AgentDefinition,
    canonical,
    clean_note,
    content_hash,
    parse_definition,
    validate_slug,
)
from authz.scope import require_scope
from memory.models import CustomAgent, CustomAgentVersion

MAX_AGENTS_PER_WORKSPACE = 200  # active + archived, PER USER within a workspace (implementation default; the name is historical)
MAX_VERSIONS_PER_AGENT = 100  # implementation default


class AgentError(Exception):
    code = "agent_error"


class AgentNotFound(AgentError):
    code = "not_found"


class VersionNotFound(AgentError):
    code = "version_not_found"


class SlugTaken(AgentError):
    code = "slug_taken"


class AgentArchived(AgentError):
    code = "agent_archived"


class LimitReached(AgentError):
    code = "limit_reached"

    def __init__(self, what: str):
        super().__init__(what)
        self.what = what


class VersionConflict(AgentError):
    code = "version_conflict"

    def __init__(self, latest_version: int):
        super().__init__(f"latest version is {latest_version}")
        self.latest_version = latest_version


@dataclass(frozen=True)
class VersionInfo:
    number: int
    content_hash: str
    change_note: str | None
    restored_from_version: int | None
    created_at: datetime
    version_id: object  # uuid.UUID


@dataclass(frozen=True)
class AgentView:
    id: object  # uuid.UUID
    slug: str
    status: str
    current_version: int
    latest_version: int
    template_id: str | None
    template_version: int | None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    definition: AgentDefinition
    version: VersionInfo


@dataclass(frozen=True)
class ResolvedAgent:
    source: str  # "builtin" | "custom"
    key: str
    version_id: object | None  # the pin for an execution record (custom agents only)
    version: int | None
    definition: AgentDefinition | None


def _owned(scope):
    return (CustomAgent.user_id == scope.user_id, CustomAgent.workspace_id == scope.workspace_id)


def _version_info(row: CustomAgentVersion) -> VersionInfo:
    return VersionInfo(row.version, row.content_hash, row.change_note, row.restored_from_version, row.created_at, row.id)


def _view(agent: CustomAgent, version: CustomAgentVersion) -> AgentView:
    return AgentView(
        id=agent.id, slug=agent.slug, status=agent.status, current_version=agent.current_version,
        latest_version=agent.latest_version, template_id=agent.template_id, template_version=agent.template_version,
        created_at=agent.created_at, updated_at=agent.updated_at, archived_at=agent.archived_at,
        definition=parse_definition(version.definition),  # re-validated: a corrupted row is never served as valid
        version=_version_info(version),
    )


class AgentService:
    def __init__(self, session_factory):
        self._sessions = session_factory

    # ------------------------------------------------------------------------------------------ helpers

    def _agent(self, session, scope, agent_id) -> CustomAgent:
        agent = session.execute(select(CustomAgent).where(CustomAgent.id == agent_id, *_owned(scope))).scalar_one_or_none()
        if agent is None:
            raise AgentNotFound("not found")  # unknown and foreign ids are indistinguishable
        return agent

    def _version(self, session, agent_id, number: int) -> CustomAgentVersion:
        row = session.execute(
            select(CustomAgentVersion).where(CustomAgentVersion.agent_id == agent_id, CustomAgentVersion.version == number)
        ).scalar_one_or_none()
        if row is None:
            raise VersionNotFound("version not found")
        return row

    # ------------------------------------------------------------------------------------------ create

    def create(self, scope, slug: str, definition: AgentDefinition, *, change_note: str | None = None,
               template_id: str | None = None, template_version: int | None = None) -> AgentView:
        scope = require_scope(scope)
        slug = validate_slug(slug)
        note = clean_note(change_note)
        now = datetime.utcnow()
        try:
            with self._sessions() as session, session.begin():
                total = session.execute(select(func.count()).select_from(CustomAgent).where(*_owned(scope))).scalar()  # per user: one member cannot use up the quota of the others
                if total >= MAX_AGENTS_PER_WORKSPACE:
                    raise LimitReached("agents")
                agent = CustomAgent(
                    user_id=scope.user_id, workspace_id=scope.workspace_id, slug=slug, status="active", latest_version=1,
                    current_version=1, template_id=template_id, template_version=template_version, created_at=now, updated_at=now,
                )
                session.add(agent)
                session.flush()
                version = CustomAgentVersion(
                    agent_id=agent.id, version=1, definition=canonical(definition), content_hash=content_hash(definition),
                    change_note=note, restored_from_version=None, created_by=scope.user_id, created_at=now,
                )
                session.add(version)
                session.flush()
                return _view(agent, version)
        except IntegrityError:
            with self._sessions() as session:  # distinguish a taken slug from any other integrity failure
                taken = session.execute(select(CustomAgent.id).where(*_owned(scope), CustomAgent.slug == slug)).first()
            if taken:
                raise SlugTaken(slug) from None
            raise

    # ------------------------------------------------------------------------------------------ read

    def get(self, scope, agent_id) -> AgentView:
        scope = require_scope(scope)
        with self._sessions() as session:
            agent = self._agent(session, scope, agent_id)
            return _view(agent, self._version(session, agent.id, agent.current_version))

    def list_agents(self, scope, status: str = "active") -> "list[AgentView]":
        scope = require_scope(scope)
        if status not in ("active", "archived", "all"):
            raise ValueError("status must be active, archived or all")
        query = (
            select(CustomAgent, CustomAgentVersion)
            .join(CustomAgentVersion, (CustomAgentVersion.agent_id == CustomAgent.id) & (CustomAgentVersion.version == CustomAgent.current_version))
            .where(*_owned(scope))
            .order_by(CustomAgent.created_at.desc(), CustomAgent.id.desc())
        )
        if status != "all":
            query = query.where(CustomAgent.status == status)
        with self._sessions() as session:
            return [_view(agent, version) for agent, version in session.execute(query).all()]

    def list_versions(self, scope, agent_id) -> list[VersionInfo]:
        scope = require_scope(scope)
        with self._sessions() as session:
            agent = self._agent(session, scope, agent_id)
            rows = session.execute(
                select(CustomAgentVersion).where(CustomAgentVersion.agent_id == agent.id).order_by(CustomAgentVersion.version.desc())
            ).scalars().all()
            return [_version_info(row) for row in rows]

    def get_version(self, scope, agent_id, number: int) -> tuple[VersionInfo, AgentDefinition]:
        scope = require_scope(scope)
        with self._sessions() as session:
            agent = self._agent(session, scope, agent_id)
            row = self._version(session, agent.id, number)
            return _version_info(row), parse_definition(row.definition)

    # ------------------------------------------------------------------------------------------ write

    def _new_version(self, scope, agent_id, base_version: int, definition_data: dict, digest: str, note: str | None,
                     restored_from: int | None) -> AgentView:
        now = datetime.utcnow()
        try:
            with self._sessions() as session, session.begin():
                agent = self._agent(session, scope, agent_id)
                if agent.status == "archived":
                    raise AgentArchived("archived")
                if base_version != agent.latest_version:
                    raise VersionConflict(agent.latest_version)
                current = self._version(session, agent.id, agent.current_version)
                if digest == current.content_hash:
                    return _view(agent, current)  # nothing changed (also a rollback to what is already current): no new version
                if agent.latest_version >= MAX_VERSIONS_PER_AGENT:
                    raise LimitReached("versions")
                number = agent.latest_version + 1
                version = CustomAgentVersion(
                    agent_id=agent.id, version=number, definition=definition_data, content_hash=digest, change_note=note,
                    restored_from_version=restored_from, created_by=scope.user_id, created_at=now,
                )
                session.add(version)
                session.flush()
                swapped = session.execute(
                    update(CustomAgent)
                    .where(CustomAgent.id == agent.id, CustomAgent.latest_version == base_version, *_owned(scope))
                    .values(latest_version=number, current_version=number, updated_at=now)
                )
                if swapped.rowcount != 1:  # someone else saved first
                    raise VersionConflict(base_version + 1)
                session.refresh(agent)
                return _view(agent, version)
        except IntegrityError:
            with self._sessions() as session:
                latest_now = session.execute(select(CustomAgent.latest_version).where(CustomAgent.id == agent_id, *_owned(scope))).scalar()
            if latest_now is None:
                raise AgentNotFound("not found") from None
            raise VersionConflict(latest_now) from None

    def update(self, scope, agent_id, base_version: int, definition: AgentDefinition, *, change_note: str | None = None) -> AgentView:
        scope = require_scope(scope)
        return self._new_version(scope, agent_id, base_version, canonical(definition), content_hash(definition), clean_note(change_note), None)

    def rollback(self, scope, agent_id, to_version: int, base_version: int, *, change_note: str | None = None) -> AgentView:
        scope = require_scope(scope)
        with self._sessions() as session:
            agent = self._agent(session, scope, agent_id)
            target = self._version(session, agent.id, to_version)
            data, digest = dict(target.definition), target.content_hash
        note = clean_note(change_note) or f"Restored version {to_version}"
        return self._new_version(scope, agent_id, base_version, data, digest, note, to_version)

    def _set_status(self, scope, agent_id, status: str) -> AgentView:
        scope = require_scope(scope)
        now = datetime.utcnow()
        with self._sessions() as session, session.begin():
            agent = self._agent(session, scope, agent_id)
            if agent.status != status:
                agent.status = status
                agent.archived_at = now if status == "archived" else None
                agent.updated_at = now
                session.flush()
            return _view(agent, self._version(session, agent.id, agent.current_version))

    def archive(self, scope, agent_id) -> AgentView:
        return self._set_status(scope, agent_id, "archived")

    def unarchive(self, scope, agent_id) -> AgentView:
        return self._set_status(scope, agent_id, "active")

    # ------------------------------------------------------------------------------------------ execution boundary

    def resolve_pinned(self, scope, key: str, version_id) -> ResolvedAgent | None:
        """Like ``resolve`` but for ONE exact immutable version (plan execution, M3-B): the agent must still be active and
        owned by the scope, and ``version_id`` must be one of ITS versions. Built-in keys have no version. Anything else
        (archived, foreign, unknown, a version of another agent, an unreadable definition) resolves to None."""
        scope = require_scope(scope)
        if key in BUILTIN_AGENT_KEYS:
            return ResolvedAgent("builtin", key, None, None, None) if version_id is None else None
        if version_id is None:
            return None
        with self._sessions() as session:
            agent = session.execute(select(CustomAgent).where(CustomAgent.slug == key, CustomAgent.status == "active", *_owned(scope))).scalar_one_or_none()
            if agent is None:
                return None
            row = session.execute(select(CustomAgentVersion).where(CustomAgentVersion.id == version_id, CustomAgentVersion.agent_id == agent.id)).scalar_one_or_none()
            if row is None:
                return None
            try:
                return ResolvedAgent("custom", agent.slug, row.id, row.version, parse_definition(row.definition))
            except Exception:  # noqa: BLE001 - a corrupted stored definition is never executed
                return None

    def resolve(self, scope, key: str) -> ResolvedAgent | None:
        """Read-only lookup for the (future) execution path. Built-in keys always win and are NOT served from the database
        (they cannot be shadowed: custom slugs may not be built-in keys). A custom agent resolves to its CURRENT version
        only while it is active; archived or unknown keys resolve to None."""
        scope = require_scope(scope)
        if key in BUILTIN_AGENT_KEYS:
            return ResolvedAgent("builtin", key, None, None, None)
        with self._sessions() as session:
            agent = session.execute(select(CustomAgent).where(CustomAgent.slug == key, CustomAgent.status == "active", *_owned(scope))).scalar_one_or_none()
            if agent is None:
                return None
            version = self._version(session, agent.id, agent.current_version)
            return ResolvedAgent("custom", agent.slug, version.id, version.version, parse_definition(version.definition))
