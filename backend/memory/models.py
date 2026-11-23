"""SQLAlchemy declarative models (metadata only).

Deliberately free of engines, sessions, settings and environment access, so the
metadata can be imported without any credentials or database (e.g. by Alembic in T1-D).
Every declarative model must be defined in this module (guarded by
tests/test_models_metadata.py). ``memory.long_term`` re-exports these names.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
)


class Base(DeclarativeBase):
    pass


class AgentMemory(Base):
    """Agent memory rows. ``user_id`` / ``workspace_id`` (M1-C) are ownership references. Both are
    NULLABLE and are NEVER backfilled: every legacy row (keyed only by the client-asserted
    ``user_email``) keeps NULL ownership and is quarantined, i.e. not reachable through ownership-scoped
    queries. Nothing in this module assigns or infers ownership."""

    __tablename__ = "agent_memories"
    __table_args__ = (
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_agent_memories_user_id_users"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_agent_memories_workspace_id_workspaces"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_name: Mapped[str] = mapped_column(String(100), index=True)
    user_email: Mapped[str | None] = mapped_column(
        String(255), index=True, nullable=True
    )
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


# M1-C: composite index for stable, newest-first history queries scoped to one user. Defined after the
# class so that the DESC columns can reference the mapped columns.
Index(
    "ix_agent_memories_user_id_created_at_id",
    AgentMemory.user_id,
    AgentMemory.created_at.desc(),
    AgentMemory.id.desc(),
)


class TelegramLink(Base):
    """Persists each Telegram chat's linked email and active agent so they
    survive backend restarts/deploys (otherwise users must /link again)."""

    __tablename__ = "telegram_links"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active_agent: Mapped[str] = mapped_column(String(100), default="ceo")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class User(Base):
    """Internal application identity (M1-A). The UUID is the identity; email is only an attribute.

    ``email`` is never unique and never a lookup key for identity: two external accounts with the
    same email are two users (no automatic linking). ``email_normalized`` (casefolded, set by
    application code) exists only as a non-unique lookup aid. UUIDs are generated in Python: no
    PostgreSQL extension and no server default, so the same code works on every supported dialect.
    """

    __tablename__ = "users"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="users_pkey"),
        Index("ix_users_email_normalized", "email_normalized"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320))
    email_normalized: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuthIdentity(Base):
    """An external account (provider + provider subject) linked to exactly one internal user."""

    __tablename__ = "auth_identities"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="auth_identities_pkey"),
        UniqueConstraint("provider", "provider_subject", name="uq_auth_identities_provider_provider_subject"),
        Index("ix_auth_identities_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", name="fk_auth_identities_user_id_users")
    )
    provider: Mapped[str] = mapped_column(String(32))
    provider_subject: Mapped[str] = mapped_column(String(255))
    email_at_link: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# Row-level CHECK expressions (M1-B). Kept as module constants so that migration 0003_workspaces,
# which must be self-contained, can be compared with them in tests. Both are valid, with the same
# meaning, on SQLite and PostgreSQL.
WORKSPACE_PERSONAL_CHECK = (
    "(is_personal AND personal_for_user_id IS NOT NULL) OR (NOT is_personal AND personal_for_user_id IS NULL)"
)
MEMBERSHIP_ROLE_CHECK = "role IN ('owner', 'admin', 'member')"


class Workspace(Base):
    """A workspace (M1-B). A personal workspace is tied to exactly one user through
    ``personal_for_user_id`` (unique: one personal workspace per user; NULL for team workspaces).

    The database enforces only row-level consistency (``is_personal`` iff ``personal_for_user_id`` is
    set). That a workspace always has an owner membership is a SERVICE-LEVEL invariant, enforced in
    application code in a later task; no CHECK can express it. Nothing here authorizes access.
    """

    __tablename__ = "workspaces"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="workspaces_pkey"),
        UniqueConstraint("personal_for_user_id", name="uq_workspaces_personal_for_user_id"),
        ForeignKeyConstraint(["personal_for_user_id"], ["users.id"], name="fk_workspaces_personal_for_user_id_users"),
        CheckConstraint(WORKSPACE_PERSONAL_CHECK, name="ck_workspaces_personal_consistency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))
    is_personal: Mapped[bool] = mapped_column(Boolean, default=False)
    personal_for_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkspaceMembership(Base):
    """A user's membership in a workspace with one of the roles owner, admin or member (no viewer).

    Possessing a workspace id is never authorization; membership rows are what a later authorization
    layer will consult. The composite primary key makes a duplicate membership impossible.
    """

    __tablename__ = "workspace_memberships"
    __table_args__ = (
        PrimaryKeyConstraint("workspace_id", "user_id", name="workspace_memberships_pkey"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_workspace_memberships_workspace_id_workspaces"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_workspace_memberships_user_id_users"),
        CheckConstraint(MEMBERSHIP_ROLE_CHECK, name="ck_workspace_memberships_role"),
        Index("ix_workspace_memberships_user_id", "user_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# Row-level CHECK expressions for the agent platform (M2-A, migration 0005_custom_agents). Kept as module constants
# so the self-contained migration can be compared with them in tests; valid on SQLite and PostgreSQL.
AGENT_STATUS_CHECK = "status IN ('active', 'archived')"
AGENT_LATEST_VERSION_CHECK = "latest_version >= 1"
AGENT_CURRENT_VERSION_CHECK = "current_version >= 1 AND current_version <= latest_version"
AGENT_TEMPLATE_PAIR_CHECK = (
    "(template_id IS NULL AND template_version IS NULL) OR (template_id IS NOT NULL AND template_version IS NOT NULL)"
)
AGENT_VERSION_NUMBER_CHECK = "version >= 1"
AGENT_VERSION_RESTORED_CHECK = "restored_from_version IS NULL OR (restored_from_version >= 1 AND restored_from_version < version)"


class CustomAgent(Base):
    """A user-defined agent (M2-A). Owned by the (user_id, workspace_id) scope that created it; the pair always comes
    from a trusted MemoryScope, never from a request. ``slug`` is the stable key within one (workspace, user): two members may each own an agent with the same slug (migration 0018). The definition itself
    lives in immutable ``custom_agent_versions`` rows: this row only tracks which versions exist (``latest_version``)
    and which one is current, plus the lifecycle status. Archiving flips ``status``; nothing is ever deleted."""

    __tablename__ = "custom_agents"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="custom_agents_pkey"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_custom_agents_user_id_users"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_custom_agents_workspace_id_workspaces"),
        UniqueConstraint("workspace_id", "user_id", "slug", name="uq_custom_agents_workspace_id_user_id_slug"),
        CheckConstraint(AGENT_STATUS_CHECK, name="ck_custom_agents_status"),
        CheckConstraint(AGENT_LATEST_VERSION_CHECK, name="ck_custom_agents_latest_version_positive"),
        CheckConstraint(AGENT_CURRENT_VERSION_CHECK, name="ck_custom_agents_current_version_range"),
        CheckConstraint(AGENT_TEMPLATE_PAIR_CHECK, name="ck_custom_agents_template_pair"),
        Index("ix_custom_agents_workspace_id_user_id", "workspace_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    slug: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), default="active")
    latest_version: Mapped[int] = mapped_column(Integer)
    current_version: Mapped[int] = mapped_column(Integer)
    template_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CustomAgentVersion(Base):
    """One IMMUTABLE version of a custom agent's definition (M2-A). Rows are only ever inserted: the migration installs
    triggers that reject UPDATE and DELETE on SQLite and PostgreSQL. Rollback therefore inserts a new version copied
    from an old one (``restored_from_version``) instead of changing history. A future execution record can reference
    ``id`` to pin the exact definition it ran."""

    __tablename__ = "custom_agent_versions"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="custom_agent_versions_pkey"),
        ForeignKeyConstraint(["agent_id"], ["custom_agents.id"], name="fk_custom_agent_versions_agent_id_custom_agents"),
        ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_custom_agent_versions_created_by_users"),
        UniqueConstraint("agent_id", "version", name="uq_custom_agent_versions_agent_id_version"),
        CheckConstraint(AGENT_VERSION_NUMBER_CHECK, name="ck_custom_agent_versions_version_positive"),
        CheckConstraint(AGENT_VERSION_RESTORED_CHECK, name="ck_custom_agent_versions_restored_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    change_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    restored_from_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# Row-level CHECK expressions for agent executions (M2-B, migration 0006_agent_executions); valid on SQLite and PostgreSQL.
EXECUTION_SOURCE_CHECK = "agent_source IN ('builtin', 'custom')"
EXECUTION_STATUS_CHECK = "status IN ('running', 'succeeded', 'failed', 'cancelled')"
EXECUTION_VERSION_PIN_CHECK = (
    "(agent_source = 'custom' AND agent_version_id IS NOT NULL AND agent_version IS NOT NULL AND agent_version >= 1) "
    "OR (agent_source = 'builtin' AND agent_version_id IS NULL AND agent_version IS NULL)"
)
EXECUTION_COMPLETION_CHECK = "(status = 'running' AND completed_at IS NULL) OR (status <> 'running' AND completed_at IS NOT NULL)"
EXECUTION_OUTCOME_CHECK = "(status <> 'succeeded' OR error_code IS NULL) AND (status <> 'failed' OR error_code IS NOT NULL)"


class AgentExecution(Base):
    """One execution of a built-in or custom agent (M2-B), owned by the (user_id, workspace_id) scope that started it.

    A CUSTOM execution pins the exact immutable ``custom_agent_versions`` row it resolved at start (``agent_version_id``;
    ``agent_version`` is the display number). A BUILT-IN execution records only its key: built-in prompts are code, not
    versioned rows, so there is deliberately no fake version id. A row is created ``running`` and finished exactly once;
    triggers (migration 0006) then freeze it, and fix its identity and pin from the start. Nothing is deleted or cascaded.
    ``task`` holds a redacted copy of the input; ``error_message`` only ever holds fixed safe text, never provider output."""

    __tablename__ = "agent_executions"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="agent_executions_pkey"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_agent_executions_user_id_users"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_agent_executions_workspace_id_workspaces"),
        ForeignKeyConstraint(["agent_version_id"], ["custom_agent_versions.id"], name="fk_agent_executions_agent_version_id_custom_agent_versions"),
        CheckConstraint(EXECUTION_SOURCE_CHECK, name="ck_agent_executions_source"),
        CheckConstraint(EXECUTION_STATUS_CHECK, name="ck_agent_executions_status"),
        CheckConstraint(EXECUTION_VERSION_PIN_CHECK, name="ck_agent_executions_version_pin"),
        CheckConstraint(EXECUTION_COMPLETION_CHECK, name="ck_agent_executions_completion"),
        CheckConstraint(EXECUTION_OUTCOME_CHECK, name="ck_agent_executions_outcome"),
        Index("ix_agent_executions_agent_version_id", "agent_version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    agent_source: Mapped[str] = mapped_column(String(16))
    agent_key: Mapped[str] = mapped_column(String(64))
    agent_name: Mapped[str] = mapped_column(String(80))
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    agent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    task: Mapped[str] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(300), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# Newest-first history for one scope; ``id DESC`` is the tie-breaker (same shape as the memory history index).
Index(
    "ix_agent_executions_workspace_id_user_id_created_at_id",
    AgentExecution.workspace_id,
    AgentExecution.user_id,
    AgentExecution.created_at.desc(),
    AgentExecution.id.desc(),
)


# Row-level CHECK expressions for plans (M3-A, migration 0007_plans); valid on SQLite and PostgreSQL.
PLAN_STATUS_CHECK = "status IN ('draft', 'approved', 'cancelled')"
PLAN_REVISION_POSITIVE_CHECK = "latest_revision >= 1"
PLAN_APPROVAL_CHECK = (
    "(status = 'approved' AND approved_revision IS NOT NULL AND approved_at IS NOT NULL AND approved_revision >= 1 "
    "AND approved_revision <= latest_revision) "
    "OR (status <> 'approved' AND approved_revision IS NULL AND approved_at IS NULL)"
)
PLAN_REVISION_NUMBER_CHECK = "revision >= 1"
PLAN_ORIGIN_CHECK = "origin IN ('model', 'revised')"
PLAN_TASK_SOURCE_CHECK = "agent_source IN ('builtin', 'custom')"
PLAN_TASK_COMPLEXITY_CHECK = "complexity IN ('low', 'medium', 'high')"
PLAN_TASK_PRIORITY_CHECK = "priority IN ('low', 'normal', 'high')"
PLAN_TASK_POSITION_CHECK = "position >= 0 AND level >= 0"
PLAN_TASK_PIN_CHECK = (
    "(agent_source = 'custom' AND agent_version_id IS NOT NULL AND agent_version IS NOT NULL AND agent_version >= 1) "
    "OR (agent_source = 'builtin' AND agent_version_id IS NULL AND agent_version IS NULL)"
)
PLAN_DEPENDENCY_SELF_CHECK = "task_key <> depends_on_key"


class Plan(Base):
    """A plan header (M3-A), owned by the (user_id, workspace_id) scope that created it. The content lives in immutable
    ``plan_revisions`` / ``plan_tasks`` / ``plan_task_dependencies`` rows: this row only tracks the latest revision, the
    lifecycle status (draft -> approved, or cancelled) and which revision was approved. Planning and approval never start
    execution."""

    __tablename__ = "plans"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="plans_pkey"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_plans_user_id_users"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_plans_workspace_id_workspaces"),
        CheckConstraint(PLAN_STATUS_CHECK, name="ck_plans_status"),
        CheckConstraint(PLAN_REVISION_POSITIVE_CHECK, name="ck_plans_latest_revision_positive"),
        CheckConstraint(PLAN_APPROVAL_CHECK, name="ck_plans_approval"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    objective: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    latest_revision: Mapped[int] = mapped_column(Integer)
    approved_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


Index(
    "ix_plans_workspace_id_user_id_created_at_id",
    Plan.workspace_id,
    Plan.user_id,
    Plan.created_at.desc(),
    Plan.id.desc(),
)


class PlanRevision(Base):
    """One IMMUTABLE revision of a plan (insert-only: triggers reject UPDATE and DELETE). ``origin`` says whether the model
    proposed it ('model', after validation) or a user revised it ('revised'). ``warnings`` and ``unsupported`` keep the
    validation findings that were true when the revision was created."""

    __tablename__ = "plan_revisions"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="plan_revisions_pkey"),
        ForeignKeyConstraint(["plan_id"], ["plans.id"], name="fk_plan_revisions_plan_id_plans"),
        UniqueConstraint("plan_id", "revision", name="uq_plan_revisions_plan_id_revision"),
        CheckConstraint(PLAN_REVISION_NUMBER_CHECK, name="ck_plan_revisions_revision_positive"),
        CheckConstraint(PLAN_ORIGIN_CHECK, name="ck_plan_revisions_origin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(String(16))
    interpretation: Mapped[str] = mapped_column(Text)
    warnings: Mapped[list] = mapped_column(JSON)
    unsupported: Mapped[list] = mapped_column(JSON)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PlanTask(Base):
    """One task of one plan revision (insert-only). A custom-agent task pins the exact immutable
    ``custom_agent_versions`` row chosen when the revision was created (same pin rule as M2-B executions); a built-in task
    records only its key (built-in prompts are code, not versioned rows)."""

    __tablename__ = "plan_tasks"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="plan_tasks_pkey"),
        ForeignKeyConstraint(["revision_id"], ["plan_revisions.id"], name="fk_plan_tasks_revision_id_plan_revisions"),
        ForeignKeyConstraint(["agent_version_id"], ["custom_agent_versions.id"], name="fk_plan_tasks_agent_version_id_custom_agent_versions"),
        UniqueConstraint("revision_id", "task_key", name="uq_plan_tasks_revision_id_task_key"),
        UniqueConstraint("revision_id", "position", name="uq_plan_tasks_revision_id_position"),
        CheckConstraint(PLAN_TASK_SOURCE_CHECK, name="ck_plan_tasks_source"),
        CheckConstraint(PLAN_TASK_COMPLEXITY_CHECK, name="ck_plan_tasks_complexity"),
        CheckConstraint(PLAN_TASK_PRIORITY_CHECK, name="ck_plan_tasks_priority"),
        CheckConstraint(PLAN_TASK_POSITION_CHECK, name="ck_plan_tasks_position"),
        CheckConstraint(PLAN_TASK_PIN_CHECK, name="ck_plan_tasks_version_pin"),
        Index("ix_plan_tasks_agent_version_id", "agent_version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    task_key: Mapped[str] = mapped_column(String(32))
    position: Mapped[int] = mapped_column(Integer)
    level: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    expected_output: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(String(400))
    dependency_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    complexity: Mapped[str] = mapped_column(String(8))
    priority: Mapped[str] = mapped_column(String(8))
    agent_source: Mapped[str] = mapped_column(String(16))
    agent_key: Mapped[str] = mapped_column(String(64))
    agent_name: Mapped[str] = mapped_column(String(80))
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    agent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_tool: Mapped[str | None] = mapped_column(String(100), nullable=True)  # M6b (migration 0015): the exact tool ref this task must invoke to count as done, resolved server-side


class PlanTaskDependency(Base):
    """A dependency edge ``task_key`` -> ``depends_on_key`` inside one revision (insert-only). Acyclicity cannot be a CHECK:
    it is guaranteed by the DAG validator before any revision is written, and revisions are immutable afterwards."""

    __tablename__ = "plan_task_dependencies"
    __table_args__ = (
        PrimaryKeyConstraint("revision_id", "task_key", "depends_on_key", name="plan_task_dependencies_pkey"),
        ForeignKeyConstraint(["revision_id", "task_key"], ["plan_tasks.revision_id", "plan_tasks.task_key"], name="fk_plan_task_dependencies_task"),
        ForeignKeyConstraint(["revision_id", "depends_on_key"], ["plan_tasks.revision_id", "plan_tasks.task_key"], name="fk_plan_task_dependencies_depends_on"),
        CheckConstraint(PLAN_DEPENDENCY_SELF_CHECK, name="ck_plan_task_dependencies_not_self"),
    )

    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    task_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    depends_on_key: Mapped[str] = mapped_column(String(32), primary_key=True)


# ------------------------------------------------------------------------------------------------ M3-B: plan runs

PLAN_RUN_STATUS_CHECK = "status IN ('running', 'succeeded', 'partial', 'failed', 'cancelled')"
PLAN_RUN_COMPLETION_CHECK = "(status = 'running' AND completed_at IS NULL) OR (status <> 'running' AND completed_at IS NOT NULL)"
PLAN_RUN_ATTEMPT_CHECK = "attempt >= 1"
PLAN_TASK_RUN_STATUS_CHECK = "status IN ('pending', 'running', 'succeeded', 'failed', 'skipped', 'cancelled')"
PLAN_TASK_RUN_PIN_CHECK = (
    "(agent_source = 'custom' AND agent_version_id IS NOT NULL AND agent_version IS NOT NULL AND agent_version >= 1) "
    "OR (agent_source = 'builtin' AND agent_version_id IS NULL AND agent_version IS NULL)"
)
PLAN_TASK_RUN_SOURCE_CHECK = "agent_source IN ('builtin', 'custom')"
PLAN_TASK_RUN_EXECUTION_CHECK = (
    "(status IN ('running', 'succeeded') AND execution_id IS NOT NULL) "
    "OR (status IN ('pending', 'skipped') AND execution_id IS NULL) OR status IN ('failed', 'cancelled')"
)
PLAN_TASK_RUN_COMPLETION_CHECK = (
    "(status IN ('pending', 'running') AND completed_at IS NULL) OR (status NOT IN ('pending', 'running') AND completed_at IS NOT NULL)"
)
PLAN_TASK_RUN_OUTCOME_CHECK = "(status <> 'succeeded' OR error_code IS NULL) AND (status NOT IN ('failed', 'skipped') OR error_code IS NOT NULL)"


class PlanRun(Base):
    """One execution attempt of one APPROVED, immutable plan revision (M3-B), owned by the (user_id, workspace_id) scope that
    started it. A run references the immutable ``plan_revisions`` row it executes, never the mutable plan header. ``attempt``
    numbers the runs of a plan; earlier runs are never overwritten. A partial unique index allows only one ``running`` run per
    plan and another allows only one ``succeeded`` run per revision (no duplicate execution). Only the running -> final
    transition may change a run (triggers in migration 0008); nothing is deleted or cascaded."""

    __tablename__ = "plan_runs"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="plan_runs_pkey"),
        ForeignKeyConstraint(["plan_id"], ["plans.id"], name="fk_plan_runs_plan_id_plans"),
        ForeignKeyConstraint(["revision_id"], ["plan_revisions.id"], name="fk_plan_runs_revision_id_plan_revisions"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_plan_runs_user_id_users"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_plan_runs_workspace_id_workspaces"),
        UniqueConstraint("plan_id", "attempt", name="uq_plan_runs_plan_id_attempt"),
        UniqueConstraint("id", "revision_id", name="uq_plan_runs_id_revision_id"),
        CheckConstraint(PLAN_RUN_STATUS_CHECK, name="ck_plan_runs_status"),
        CheckConstraint(PLAN_RUN_COMPLETION_CHECK, name="ck_plan_runs_completion"),
        CheckConstraint(PLAN_RUN_ATTEMPT_CHECK, name="ck_plan_runs_attempt_positive"),
        Index("uq_plan_runs_one_active", "plan_id", unique=True, sqlite_where=text("status = 'running'"), postgresql_where=text("status = 'running'")),
        Index("uq_plan_runs_one_success", "revision_id", unique=True, sqlite_where=text("status = 'succeeded'"), postgresql_where=text("status = 'succeeded'")),
        Index("ix_plan_runs_plan_id_created_at", "plan_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PlanTaskRun(Base):
    """The run-time state of one plan task inside one run (M3-B). The composite foreign keys guarantee that the task belongs
    to the run's own revision. The agent identity and version pin are COPIED from the immutable plan task at run creation and
    fixed. The result is NOT duplicated here: ``execution_id`` links the M2-B ``agent_executions`` row that holds it (a task
    only becomes ``running`` together with that link, and ``succeeded`` only after that execution succeeded)."""

    __tablename__ = "plan_task_runs"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="plan_task_runs_pkey"),
        ForeignKeyConstraint(["run_id", "revision_id"], ["plan_runs.id", "plan_runs.revision_id"], name="fk_plan_task_runs_run"),
        ForeignKeyConstraint(["revision_id", "task_key"], ["plan_tasks.revision_id", "plan_tasks.task_key"], name="fk_plan_task_runs_task"),
        ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_plan_task_runs_execution_id_agent_executions"),
        ForeignKeyConstraint(["agent_version_id"], ["custom_agent_versions.id"], name="fk_plan_task_runs_agent_version_id_custom_agent_versions"),
        UniqueConstraint("run_id", "task_key", name="uq_plan_task_runs_run_id_task_key"),
        UniqueConstraint("run_id", "position", name="uq_plan_task_runs_run_id_position"),
        UniqueConstraint("execution_id", name="uq_plan_task_runs_execution_id"),
        CheckConstraint(PLAN_TASK_RUN_STATUS_CHECK, name="ck_plan_task_runs_status"),
        CheckConstraint(PLAN_TASK_RUN_SOURCE_CHECK, name="ck_plan_task_runs_source"),
        CheckConstraint(PLAN_TASK_RUN_PIN_CHECK, name="ck_plan_task_runs_version_pin"),
        CheckConstraint(PLAN_TASK_RUN_EXECUTION_CHECK, name="ck_plan_task_runs_execution"),
        CheckConstraint(PLAN_TASK_RUN_COMPLETION_CHECK, name="ck_plan_task_runs_completion"),
        CheckConstraint(PLAN_TASK_RUN_OUTCOME_CHECK, name="ck_plan_task_runs_outcome"),
        Index("ix_plan_task_runs_agent_version_id", "agent_version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    task_key: Mapped[str] = mapped_column(String(32))
    position: Mapped[int] = mapped_column(Integer)
    level: Mapped[int] = mapped_column(Integer)
    agent_source: Mapped[str] = mapped_column(String(16))
    agent_key: Mapped[str] = mapped_column(String(64))
    agent_name: Mapped[str] = mapped_column(String(80))
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    agent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RunLease(Base):
    """Durable ownership of one RUNNING plan run (M4-A1). Exactly one process (``owner_id``) drives a run at a time; ownership is
    a lease that expires unless the owner renews it, and ``generation`` is the fencing token: it starts at 1 and increases by
    one on every takeover, so a stale owner (an older generation) can no longer change the run (every driver write checks
    ``(owner_id, generation)`` in its own transaction). ``cancel_requested_at`` is the persisted cancellation request that any
    process may set and the owner honours. The lease lives in its own table on purpose: the accepted ``plan_runs`` triggers
    (migration 0008) freeze a running run, so ownership must not be stored there. A run's lease is created in the same
    transaction as the run; the row is kept after the run ends (it is only history then)."""

    __tablename__ = "run_leases"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", name="run_leases_pkey"),
        ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_run_leases_run_id_plan_runs"),
        CheckConstraint("generation >= 1", name="ck_run_leases_generation_positive"),
        Index("ix_run_leases_expires_at", "expires_at"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(100))
    generation: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------------------------------------------- M4-A2

EVENT_TYPES = (
    "run_created", "run_claimed", "run_resumed", "task_started", "task_succeeded", "task_failed", "task_skipped", "task_interrupted",
    "task_cancelled", "task_reused", "cancellation_requested", "cancellation_acknowledged", "recovery_performed", "retry_authorized",
    "run_completed",
    # M4-C: the approval lifecycle and the durable suspension of a task (migration 0013)
    "approval_requested", "approval_approved", "approval_rejected", "approval_expired", "approval_invalidated", "approval_consumed", "task_suspended", "task_resumed",
    # P0 safety sprint: the dispatch state machine of a side-effecting tool call (migration 0016)
    "tool_dispatch_authorized", "tool_dispatch_started", "tool_dispatch_succeeded", "tool_dispatch_failed", "tool_dispatch_unknown", "tool_dispatch_cancelled",
    "tool_dispatch_replayed", "tool_dispatch_reconciled", "tool_dispatch_refused",
)
EVENT_ACTORS = ("user", "api", "worker", "recovery")
EVENT_TYPE_CHECK = "event_type IN (" + ", ".join(f"'{t}'" for t in EVENT_TYPES) + ")"
EVENT_ACTOR_CHECK = "actor_type IN (" + ", ".join(f"'{a}'" for a in EVENT_ACTORS) + ")"
PROCESS_KIND_CHECK = "kind IN ('api', 'worker')"


class ExecutionEvent(Base):
    """Append-only history of one plan run (M4-A2). Every row is written in the SAME transaction as the state change it describes,
    is scoped by (user_id, workspace_id) like the run, and can never be updated or deleted (triggers in migration 0010). ``id`` is
    the total order. ``detail`` is a small JSON object built only from a fixed whitelist of codes, keys, ids and numbers
    (``agentplatform.run_events``): never task text, results, credentials or provider output. ``actor_id`` is the user id for a
    human actor and the internal process id otherwise (never returned by the API)."""

    __tablename__ = "execution_events"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="execution_events_pkey"),
        ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_execution_events_run_id_plan_runs"),
        CheckConstraint(EVENT_TYPE_CHECK, name="ck_execution_events_type"),
        CheckConstraint(EVENT_ACTOR_CHECK, name="ck_execution_events_actor"),
        Index("ix_execution_events_run_id_id", "run_id", "id"),
        Index("ix_execution_events_workspace_id_id", "workspace_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    task_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    event_type: Mapped[str] = mapped_column(String(40))
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PlanRunRetry(Base):
    """Lineage of a retry attempt (M4-A2): run ``run_id`` is the explicit, authorised retry of run ``retry_of_run_id``. A run can be
    retried at most once (unique), so two concurrent retries cannot both create an attempt. ``acknowledged`` lists the task keys
    whose uncertain outcome the user explicitly accepted to re-run. Immutable (triggers)."""

    __tablename__ = "plan_run_retries"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", name="plan_run_retries_pkey"),
        ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_plan_run_retries_run_id_plan_runs"),
        ForeignKeyConstraint(["retry_of_run_id"], ["plan_runs.id"], name="fk_plan_run_retries_retry_of_run_id_plan_runs"),
        ForeignKeyConstraint(["authorized_by"], ["users.id"], name="fk_plan_run_retries_authorized_by_users"),
        UniqueConstraint("retry_of_run_id", name="uq_plan_run_retries_retry_of_run_id"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    retry_of_run_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    authorized_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    acknowledged: Mapped[str] = mapped_column(Text, default="[]")
    carried_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PlanTaskCarryover(Base):
    """A task of a retry run that is NOT executed again: it reuses the persisted result of ``source_task_run_id`` (the task run that
    really executed it). The new run has no task-run row for it (``plan_task_runs`` requires a unique execution per succeeded
    task); readers merge the two. Immutable (triggers)."""

    __tablename__ = "plan_task_carryovers"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "task_key", name="plan_task_carryovers_pkey"),
        ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_plan_task_carryovers_run_id_plan_runs"),
        ForeignKeyConstraint(["source_task_run_id"], ["plan_task_runs.id"], name="fk_plan_task_carryovers_source_task_run_id_plan_task_runs"),
        Index("ix_plan_task_carryovers_source_task_run_id", "source_task_run_id"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    task_key: Mapped[str] = mapped_column(String(32))
    source_task_run_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkerProcess(Base):
    """One row per live application process (an API process or a worker) with its own heartbeat (M4-A2). ``owner_id`` is the same
    id that run leases and execution owners carry; a process whose ``expires_at`` has passed is gone (its executions can be
    recovered). Mutable: the heartbeat renews it, a graceful shutdown expires it."""

    __tablename__ = "worker_processes"
    __table_args__ = (
        PrimaryKeyConstraint("owner_id", name="worker_processes_pkey"),
        CheckConstraint(PROCESS_KIND_CHECK, name="ck_worker_processes_kind"),
        Index("ix_worker_processes_expires_at", "expires_at"),
    )

    owner_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class ExecutionOwner(Base):
    """Which process runs an execution (M4-A2), written in the same transaction as the execution row. It gives standalone (REST and
    WebSocket) executions an owner: they are recovered when that process has expired, instead of at every other process's startup.
    An execution without a row (created before M4-A2) keeps the old single-process rule. Immutable (triggers)."""

    __tablename__ = "execution_owners"
    __table_args__ = (
        PrimaryKeyConstraint("execution_id", name="execution_owners_pkey"),
        ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_execution_owners_execution_id_agent_executions"),
        Index("ix_execution_owners_owner_id", "owner_id"),
    )

    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------------------------------------------- M4-B

TOOL_STATUSES = ("running", "succeeded", "failed", "timed_out", "rejected", "cancelled", "interrupted", "uncertain")
TOOL_STATUS_CHECK = "status IN (" + ", ".join(f"'{s}'" for s in TOOL_STATUSES) + ")"
TOOL_SIDE_EFFECT_CHECK = "side_effect IN ('none', 'idempotent', 'non_idempotent')"
TOOL_COMPLETION_CHECK = "(status = 'running' AND completed_at IS NULL) OR (status <> 'running' AND completed_at IS NOT NULL)"
TOOL_OUTCOME_CHECK = "status IN ('running', 'succeeded') AND error_code IS NULL OR status NOT IN ('running', 'succeeded') AND error_code IS NOT NULL"
TOOL_VERSION_CHECK = "tool_version IS NULL OR tool_version >= 1"


class ToolInvocation(Base):
    """One request by an agent to use a registered tool (M4-B), durable and auditable.

    A row is written BEFORE the tool runs (``running``: the intent, with the exact tool id and version, the hash of the canonical validated
    arguments, the execution, the agent and, for a plan task, the run and task) and finished exactly once. Triggers (migration 0011) fix
    its identity and its input and allow only ``running -> final``; nothing is deleted. A refused request (not permitted, invalid
    arguments, unknown tool, approval required) is recorded directly as ``rejected``. ``input`` and ``output`` are redacted, bounded
    JSON text; ``error_message`` is fixed safe text. ``(execution_id, seq)`` and ``idempotency_key`` are unique, so a step can never be
    dispatched twice. A call interrupted by a crash ends ``interrupted`` (``uncertain`` for a tool with side effects: it may have
    taken effect). ``args_hash`` + ``tool_id`` + ``tool_version`` + ``execution_id`` are what a future approval (M4-C) binds to."""

    __tablename__ = "tool_invocations"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="tool_invocations_pkey"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_tool_invocations_user_id_users"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_tool_invocations_workspace_id_workspaces"),
        ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_tool_invocations_execution_id_agent_executions"),
        ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_tool_invocations_run_id_plan_runs"),
        UniqueConstraint("execution_id", "seq", name="uq_tool_invocations_execution_id_seq"),
        UniqueConstraint("idempotency_key", name="uq_tool_invocations_idempotency_key"),
        CheckConstraint(TOOL_STATUS_CHECK, name="ck_tool_invocations_status"),
        CheckConstraint(TOOL_SIDE_EFFECT_CHECK, name="ck_tool_invocations_side_effect"),
        CheckConstraint(TOOL_COMPLETION_CHECK, name="ck_tool_invocations_completion"),
        CheckConstraint(TOOL_OUTCOME_CHECK, name="ck_tool_invocations_outcome"),
        CheckConstraint(TOOL_VERSION_CHECK, name="ck_tool_invocations_version"),
        CheckConstraint("seq >= 1", name="ck_tool_invocations_seq_positive"),
        Index("ix_tool_invocations_workspace_user_created", "workspace_id", "user_id", "created_at", "id"),
        Index("ix_tool_invocations_run_id", "run_id"),
        Index("ix_tool_invocations_tool", "tool_id", "tool_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    task_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    agent_key: Mapped[str] = mapped_column(String(64))
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    tool_id: Mapped[str] = mapped_column(String(64))
    tool_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seq: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    args_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    side_effect: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    input: Mapped[str | None] = mapped_column(Text, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired", "invalidated", "consumed")
APPROVAL_STATUS_CHECK = "status IN (" + ", ".join(f"'{s}'" for s in APPROVAL_STATUSES) + ")"


class ToolApproval(Base):
    """A human approval for ONE exact tool invocation (M4-C). Durable, immutable except for its lifecycle.

    The row binds the approval to the workspace and user, the execution (and plan run and task), the agent and its exact version, the exact tool
    id and version, the redacted canonical VALIDATED arguments and ``payload_hash`` = sha256 of the canonical envelope of all of those. Lifecycle
    (enforced by triggers, migration 0012): ``pending -> approved | rejected | expired | invalidated``, ``approved -> consumed | expired |
    invalidated``; every other state is final. ``consumed`` happens in the SAME transaction as the invocation's intent row, which is linked back
    through ``invocation_id``; ``(execution_id, seq)`` is unique, so one step has at most one approval and an approval is consumed at most once."""

    __tablename__ = "tool_approvals"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="tool_approvals_pkey"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_tool_approvals_user_id_users"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_tool_approvals_workspace_id_workspaces"),
        ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_tool_approvals_execution_id_agent_executions"),
        ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_tool_approvals_run_id_plan_runs"),
        ForeignKeyConstraint(["invocation_id"], ["tool_invocations.id"], name="fk_tool_approvals_invocation_id_tool_invocations"),
        ForeignKeyConstraint(["decided_by"], ["users.id"], name="fk_tool_approvals_decided_by_users"),
        UniqueConstraint("execution_id", "seq", name="uq_tool_approvals_execution_id_seq"),
        UniqueConstraint("invocation_id", name="uq_tool_approvals_invocation_id"),
        CheckConstraint(APPROVAL_STATUS_CHECK, name="ck_tool_approvals_status"),
        CheckConstraint("seq >= 1", name="ck_tool_approvals_seq_positive"),
        CheckConstraint("(status = 'consumed') = (invocation_id IS NOT NULL)", name="ck_tool_approvals_consumed_link"),
        Index("ix_tool_approvals_workspace_status_created", "workspace_id", "status", "created_at", "id"),
        Index("ix_tool_approvals_run_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    task_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    agent_key: Mapped[str] = mapped_column(String(64))
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    tool_id: Mapped[str] = mapped_column(String(64))
    tool_version: Mapped[int] = mapped_column(Integer)
    seq: Mapped[int] = mapped_column(Integer)
    risk: Mapped[str] = mapped_column(String(16))
    side_effect: Mapped[str] = mapped_column(String(16))
    args_json: Mapped[str] = mapped_column(Text)
    args_hash: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invocation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class ToolSuspension(Base):
    """The durable record that a plan task is SUSPENDED on one approval (M4-C, migration 0013). It holds the redacted conversation of the agent so far
    (``transcript``), the calls of the round and which one is waiting (``calls``, ``call_index``) so ANY process can resume the exact call. ``resumed_at`` is set
    once, in the same transaction that consumes the approval (or records the refusal); until then the suspension is OPEN and recovery leaves the task alone."""

    __tablename__ = "tool_suspensions"
    __table_args__ = (
        PrimaryKeyConstraint("approval_id", name="tool_suspensions_pkey"),
        ForeignKeyConstraint(["approval_id"], ["tool_approvals.id"], name="fk_tool_suspensions_approval_id_tool_approvals"),
        ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_tool_suspensions_execution_id_agent_executions"),
        ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_tool_suspensions_run_id_plan_runs"),
        UniqueConstraint("execution_id", "seq", name="uq_tool_suspensions_execution_id_seq"),
        CheckConstraint("seq >= 1 AND round_number >= 0 AND call_index >= 0", name="ck_tool_suspensions_positions"),
        Index("ix_tool_suspensions_run_id", "run_id"),
        Index("ix_tool_suspensions_execution_id", "execution_id"),
    )

    approval_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    task_key: Mapped[str] = mapped_column(String(32))
    seq: Mapped[int] = mapped_column(Integer)
    round_number: Mapped[int] = mapped_column(Integer)
    call_index: Mapped[int] = mapped_column(Integer)
    transcript: Mapped[str] = mapped_column(Text)
    calls: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ModelUsage(Base):
    """ONE provider request and what it used (M6, migration 0014). Append-only. Token counts are exactly what the provider reported (NULL when it did not report them: ``usage_known``
    is then False); ``cost_micros`` (millionths of a US dollar) is calculated from the reported tokens with the versioned price named in ``pricing_version`` and is NULL when
    the usage or the price is unknown (``cost_basis``). Nothing here is estimated or invented."""

    __tablename__ = "model_usage"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="model_usage_pkey"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_model_usage_user_id_users"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_model_usage_workspace_id_workspaces"),
        ForeignKeyConstraint(["plan_id"], ["plans.id"], name="fk_model_usage_plan_id_plans"),
        ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_model_usage_run_id_plan_runs"),
        ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_model_usage_execution_id_agent_executions"),
        CheckConstraint("purpose IN ('plan', 'agent')", name="ck_model_usage_purpose"),
        CheckConstraint("cost_basis IN ('calculated', 'unknown_usage', 'unknown_pricing')", name="ck_model_usage_cost_basis"),
        CheckConstraint("(cost_basis = 'calculated') = (cost_micros IS NOT NULL)", name="ck_model_usage_cost_consistent"),
        CheckConstraint("(input_tokens IS NULL OR input_tokens >= 0) AND (output_tokens IS NULL OR output_tokens >= 0) AND (cached_tokens IS NULL OR cached_tokens >= 0)", name="ck_model_usage_tokens"),
        Index("ix_model_usage_workspace_run", "workspace_id", "run_id"),
        Index("ix_model_usage_plan_id", "plan_id"),
        Index("ix_model_usage_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    task_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    agent_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    purpose: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_known: Mapped[bool] = mapped_column(Boolean)
    pricing_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cost_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cost_basis: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Notification(Base):
    """A durable in-app notification of ONE recipient about ONE thing (M6, migration 0014). UNIQUE (user_id, kind, ref_id): a retry or a recovery can never notify twice. Only ``read_at``
    changes (NULL -> a time, once). Recipients are chosen server-side in the transaction that creates what they are told about."""

    __tablename__ = "notifications"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="notifications_pkey"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_notifications_workspace_id_workspaces"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_notifications_user_id_users"),
        UniqueConstraint("user_id", "kind", "ref_id", name="uq_notifications_user_kind_ref"),
        CheckConstraint("kind IN ('approval_requested')", name="ck_notifications_kind"),
        Index("ix_notifications_user_read_created", "user_id", "read_at", "created_at"),
        Index("ix_notifications_ref_id", "ref_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(String(32))
    ref_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ProviderEvent(Base):
    """One thing that happened while a provider request was retried (M6b, migration 0015). Append-only; no prompt, answer or credential."""

    __tablename__ = "provider_events"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="provider_events_pkey"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_provider_events_user_id_users"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_provider_events_workspace_id_workspaces"),
        ForeignKeyConstraint(["plan_id"], ["plans.id"], name="fk_provider_events_plan_id_plans"),
        ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_provider_events_run_id_plan_runs"),
        ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_provider_events_execution_id_agent_executions"),
        CheckConstraint("kind IN ('waiting', 'retrying', 'recovered', 'exhausted', 'failed')", name="ck_provider_events_kind"),
        CheckConstraint("attempt >= 1 AND (wait_ms IS NULL OR wait_ms >= 0)", name="ck_provider_events_numbers"),
        Index("ix_provider_events_workspace_run", "workspace_id", "run_id", "created_at"),
        Index("ix_provider_events_plan_id", "plan_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    task_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    provider: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))
    attempt: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    wait_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


DISPATCH_STATES = ("authorized", "dispatching", "succeeded", "failed", "unknown", "reconciled_succeeded", "reconciled_failed", "cancelled")


class ToolDispatch(Base):
    """The dispatch state machine of ONE invocation of a side-effecting tool (migration 0016). ``authorized``: the intent is durable and the approval consumed, nothing sent. ``dispatching``:
    persisted BEFORE anything is sent. ``succeeded`` / ``failed``: CONFIRMED. ``unknown``: the outcome cannot be proven (never retried automatically). ``reconciled_*``: set only by an audited
    ``tool_reconciliations`` record. ``cancelled``: never sent. Triggers allow exactly the legal transitions and fix the identity columns. ``operation_key`` is the stable identity of the LOGICAL
    operation across attempts and executions; ``idempotency_key`` is what the remote service receives."""

    __tablename__ = "tool_dispatches"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="tool_dispatches_pkey"),
        ForeignKeyConstraint(["invocation_id"], ["tool_invocations.id"], name="fk_tool_dispatches_invocation_id_tool_invocations"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_tool_dispatches_user_id_users"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_tool_dispatches_workspace_id_workspaces"),
        ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_tool_dispatches_execution_id_agent_executions"),
        ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_tool_dispatches_run_id_plan_runs"),
        UniqueConstraint("invocation_id", name="uq_tool_dispatches_invocation_id"),
        UniqueConstraint("operation_key", "generation", name="uq_tool_dispatches_operation_generation"),
        CheckConstraint("state IN (" + ", ".join(f"'{s}'" for s in DISPATCH_STATES) + ")", name="ck_tool_dispatches_state"),
        CheckConstraint("generation >= 1 AND tool_version >= 1", name="ck_tool_dispatches_numbers"),
        CheckConstraint("(state IN ('authorized', 'cancelled')) OR dispatch_started_at IS NOT NULL", name="ck_tool_dispatches_started"),
        CheckConstraint("(state IN ('authorized', 'dispatching')) = (completed_at IS NULL)", name="ck_tool_dispatches_completed"),
        Index("ix_tool_dispatches_operation_key", "operation_key"),
        Index("ix_tool_dispatches_run_task", "run_id", "task_key"),
        Index("ix_tool_dispatches_execution_id", "execution_id"),
        Index("ix_tool_dispatches_workspace_state", "workspace_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    invocation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    operation_key: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(80))
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    task_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tool_id: Mapped[str] = mapped_column(String(64))
    tool_version: Mapped[int] = mapped_column(Integer)
    args_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24))
    external_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ToolReconciliation(Base):
    """The append-only evidence, decision, actor, time and reason of one reconciliation attempt of an ``unknown`` dispatch (migration 0016). ``operator_attestation`` is a person's
    statement, NOT proof of the remote outcome, and is stored and shown as such."""

    __tablename__ = "tool_reconciliations"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="tool_reconciliations_pkey"),
        ForeignKeyConstraint(["dispatch_id"], ["tool_dispatches.id"], name="fk_tool_reconciliations_dispatch_id_tool_dispatches"),
        ForeignKeyConstraint(["invocation_id"], ["tool_invocations.id"], name="fk_tool_reconciliations_invocation_id_tool_invocations"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_tool_reconciliations_user_id_users"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_tool_reconciliations_workspace_id_workspaces"),
        ForeignKeyConstraint(["actor_user_id"], ["users.id"], name="fk_tool_reconciliations_actor_user_id_users"),
        CheckConstraint("decision IN ('confirmed_succeeded', 'confirmed_failed', 'unresolved')", name="ck_tool_reconciliations_decision"),
        CheckConstraint("evidence_kind IN ('remote_query', 'operator_attestation')", name="ck_tool_reconciliations_evidence_kind"),
        CheckConstraint("length(reason) >= 3", name="ck_tool_reconciliations_reason"),
        Index("ix_tool_reconciliations_dispatch_id", "dispatch_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    dispatch_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    invocation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    decision: Mapped[str] = mapped_column(String(24))
    evidence_kind: Mapped[str] = mapped_column(String(24))
    evidence: Mapped[str] = mapped_column(String(500))
    reason: Mapped[str] = mapped_column(String(500))
    external_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class WorkspaceInvitation(Base):
    """An invitation to a workspace for a person who may not have signed in yet (migration 0017). Only the SHA-256 of the 256-bit secret is stored; accepting requires a signed-in person whose VERIFIED e-mail is
    ``email_normalized``; single-use (``accepted_at`` set by a compare-and-swap with the membership insert), expiring, revocable; at most one open invitation per workspace and e-mail."""

    __tablename__ = "workspace_invitations"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="workspace_invitations_pkey"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_workspace_invitations_workspace_id_workspaces"),
        ForeignKeyConstraint(["invited_by"], ["users.id"], name="fk_workspace_invitations_invited_by_users"),
        ForeignKeyConstraint(["accepted_by"], ["users.id"], name="fk_workspace_invitations_accepted_by_users"),
        UniqueConstraint("token_hash", name="uq_workspace_invitations_token_hash"),
        CheckConstraint("role IN ('admin', 'member')", name="ck_workspace_invitations_role"),
        CheckConstraint("expires_at > created_at", name="ck_workspace_invitations_expiry"),
        CheckConstraint("NOT (accepted_at IS NOT NULL AND revoked_at IS NOT NULL)", name="ck_workspace_invitations_single_outcome"),
        CheckConstraint("(accepted_at IS NULL) = (accepted_by IS NULL)", name="ck_workspace_invitations_acceptor"),
        Index("uq_workspace_invitations_open", "workspace_id", "email_normalized", unique=True, sqlite_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
              postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL")),
        Index("ix_workspace_invitations_workspace_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    email_normalized: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64))
    invited_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
