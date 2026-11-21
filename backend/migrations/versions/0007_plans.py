"""plans, immutable plan revisions, tasks and dependencies (M3-A)

Revision ID: 0007_plans
Revises: 0006_agent_executions
Create Date: 2026-11-16

Adds the planning foundation; no existing table is touched (M2-B execution records are untouched):

* ``plans``                  mutable header: owner ``(user_id, workspace_id)`` NOT NULL FKs, redacted objective, status
                             (draft|approved|cancelled), ``latest_revision``, the approved revision and timestamps;
* ``plan_revisions``         one IMMUTABLE row per revision (origin model|revised, interpretation, validation findings,
                             the provider/model that proposed it);
* ``plan_tasks``             the tasks of one revision, each assigned to a built-in or custom agent; a custom task PINS the
                             exact immutable ``custom_agent_versions`` row (same pin CHECK as M2-B executions);
* ``plan_task_dependencies`` the dependency edges of one revision (composite FKs to the tasks, no self-edges).

GUARDS. The three revision tables are insert-only: triggers reject every UPDATE and DELETE (SQLite: ``BEFORE UPDATE`` and
``BEFORE DELETE`` triggers per table; PostgreSQL: one shared plpgsql function and a ``BEFORE UPDATE OR DELETE`` row trigger
per table). Editing a plan therefore writes a NEW revision; an earlier revision, and the agent versions it pinned, can
never change. Only the ``plans`` header is updated (status, latest revision, approval). Acyclicity cannot be a CHECK: the DAG
validator guarantees it before a revision is written and revisions are immutable afterwards. Foreign keys are NO ACTION
(no cascades).

``downgrade()`` removes exactly these objects (the data in them is discarded). Production policy is forward-only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0007_plans"
down_revision: Union[str, None] = "0006_agent_executions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FUNCTION = "plan_rows_immutable"
MESSAGE = "plan revisions, tasks and dependencies are insert-only"
IMMUTABLE_TABLES = ("plan_revisions", "plan_tasks", "plan_task_dependencies")
INDEX_PLANS = "ix_plans_workspace_id_user_id_created_at_id"
INDEX_TASK_VERSION = "ix_plan_tasks_agent_version_id"


def _dialect() -> str:
    return op.get_context().dialect.name


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("latest_revision", sa.Integer(), nullable=False),
        sa.Column("approved_revision", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="plans_pkey"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_plans_user_id_users"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_plans_workspace_id_workspaces"),
        sa.CheckConstraint("status IN ('draft', 'approved', 'cancelled')", name="ck_plans_status"),
        sa.CheckConstraint("latest_revision >= 1", name="ck_plans_latest_revision_positive"),
        sa.CheckConstraint(
            "(status = 'approved' AND approved_revision IS NOT NULL AND approved_at IS NOT NULL AND approved_revision >= 1 "
            "AND approved_revision <= latest_revision) "
            "OR (status <> 'approved' AND approved_revision IS NULL AND approved_at IS NULL)",
            name="ck_plans_approval",
        ),
    )
    op.create_index(INDEX_PLANS, "plans", ["workspace_id", "user_id", sa.text("created_at DESC"), sa.text("id DESC")], unique=False)

    op.create_table(
        "plan_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("interpretation", sa.Text(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("unsupported", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="plan_revisions_pkey"),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], name="fk_plan_revisions_plan_id_plans"),
        sa.UniqueConstraint("plan_id", "revision", name="uq_plan_revisions_plan_id_revision"),
        sa.CheckConstraint("revision >= 1", name="ck_plan_revisions_revision_positive"),
        sa.CheckConstraint("origin IN ('model', 'revised')", name="ck_plan_revisions_origin"),
    )

    op.create_table(
        "plan_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(length=32), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("expected_output", sa.Text(), nullable=False),
        sa.Column("rationale", sa.String(length=400), nullable=False),
        sa.Column("dependency_reason", sa.String(length=300), nullable=True),
        sa.Column("complexity", sa.String(length=8), nullable=False),
        sa.Column("priority", sa.String(length=8), nullable=False),
        sa.Column("agent_source", sa.String(length=16), nullable=False),
        sa.Column("agent_key", sa.String(length=64), nullable=False),
        sa.Column("agent_name", sa.String(length=80), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=True),
        sa.Column("agent_version", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="plan_tasks_pkey"),
        sa.ForeignKeyConstraint(["revision_id"], ["plan_revisions.id"], name="fk_plan_tasks_revision_id_plan_revisions"),
        sa.ForeignKeyConstraint(
            ["agent_version_id"], ["custom_agent_versions.id"], name="fk_plan_tasks_agent_version_id_custom_agent_versions"
        ),
        sa.UniqueConstraint("revision_id", "task_key", name="uq_plan_tasks_revision_id_task_key"),
        sa.UniqueConstraint("revision_id", "position", name="uq_plan_tasks_revision_id_position"),
        sa.CheckConstraint("agent_source IN ('builtin', 'custom')", name="ck_plan_tasks_source"),
        sa.CheckConstraint("complexity IN ('low', 'medium', 'high')", name="ck_plan_tasks_complexity"),
        sa.CheckConstraint("priority IN ('low', 'normal', 'high')", name="ck_plan_tasks_priority"),
        sa.CheckConstraint("position >= 0 AND level >= 0", name="ck_plan_tasks_position"),
        sa.CheckConstraint(
            "(agent_source = 'custom' AND agent_version_id IS NOT NULL AND agent_version IS NOT NULL AND agent_version >= 1) "
            "OR (agent_source = 'builtin' AND agent_version_id IS NULL AND agent_version IS NULL)",
            name="ck_plan_tasks_version_pin",
        ),
    )
    op.create_index(INDEX_TASK_VERSION, "plan_tasks", ["agent_version_id"], unique=False)

    op.create_table(
        "plan_task_dependencies",
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(length=32), nullable=False),
        sa.Column("depends_on_key", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("revision_id", "task_key", "depends_on_key", name="plan_task_dependencies_pkey"),
        sa.ForeignKeyConstraint(
            ["revision_id", "task_key"], ["plan_tasks.revision_id", "plan_tasks.task_key"], name="fk_plan_task_dependencies_task"
        ),
        sa.ForeignKeyConstraint(
            ["revision_id", "depends_on_key"], ["plan_tasks.revision_id", "plan_tasks.task_key"], name="fk_plan_task_dependencies_depends_on"
        ),
        sa.CheckConstraint("task_key <> depends_on_key", name="ck_plan_task_dependencies_not_self"),
    )

    if _dialect() == "sqlite":
        for table in IMMUTABLE_TABLES:
            for event in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{event.lower()} BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT, '{MESSAGE}'); END")
    else:
        op.execute(
            f"CREATE FUNCTION {FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            f"BEGIN RAISE EXCEPTION '{MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END; $$"
        )
        for table in IMMUTABLE_TABLES:
            op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()")


def downgrade() -> None:
    if _dialect() == "sqlite":
        for table in IMMUTABLE_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
    else:
        for table in IMMUTABLE_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.drop_table("plan_task_dependencies")
    op.drop_index(INDEX_TASK_VERSION, table_name="plan_tasks")
    op.drop_table("plan_tasks")
    op.drop_table("plan_revisions")
    op.drop_index(INDEX_PLANS, table_name="plans")
    op.drop_table("plans")
    if _dialect() != "sqlite":
        op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}()")
