"""plan runs and task runs: execution of approved plans (M3-B)

Revision ID: 0008_plan_runs
Revises: 0007_plans
Create Date: 2026-11-16

Adds the run-time state of plan execution; no existing table is touched (plans, revisions and M2-B executions are unchanged):

* ``plan_runs``       one execution attempt of one immutable plan revision, owned by ``(user_id, workspace_id)``;
* ``plan_task_runs``  the state of each plan task inside a run; the result is NOT copied: ``execution_id`` links the M2-B
                      ``agent_executions`` row. Composite foreign keys tie every task run to its run's OWN revision.

GUARANTEES. Partial unique indexes: one ``running`` run per plan and one ``succeeded`` run per revision (no duplicate
execution; a concurrent second start fails in the database). Triggers: runs and task runs can never be deleted; identity and
the agent version pin are fixed from creation; a run only moves ``running`` -> final; a task run only moves
``pending`` -> running|skipped|cancelled|failed (never started) and ``running`` -> succeeded|failed|cancelled, and its execution link is fixed once
set. A finished row is frozen. Foreign keys are NO ACTION (no cascades).

``downgrade()`` removes exactly these objects (the data in them is discarded). Production policy is forward-only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0008_plan_runs"
down_revision: Union[str, None] = "0007_plans"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUN_MESSAGE = "plan runs are never deleted, their identity is fixed and a finished run is frozen"
TASK_MESSAGE = "plan task runs are never deleted, their identity and agent pin are fixed and only legal state transitions are allowed"
RUN_FUNCTION = "plan_runs_guard"
TASK_FUNCTION = "plan_task_runs_guard"
RUN_FIXED = ("id", "plan_id", "revision_id", "user_id", "workspace_id", "attempt", "created_at")
TASK_FIXED = ("id", "run_id", "revision_id", "task_key", "position", "level", "agent_source", "agent_key", "agent_name", "created_at")
TASK_FIXED_NULLABLE = ("agent_version_id", "agent_version")
LEGAL = "(OLD.status = 'pending' AND NEW.status IN ('running', 'skipped', 'cancelled', 'failed')) OR (OLD.status = 'running' AND NEW.status IN ('succeeded', 'failed', 'cancelled'))"
ACTIVE_WHERE = sa.text("status = 'running'")
SUCCESS_WHERE = sa.text("status = 'succeeded'")


def _dialect() -> str:
    return op.get_context().dialect.name


def upgrade() -> None:
    op.create_table(
        "plan_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("error_message", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="plan_runs_pkey"),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], name="fk_plan_runs_plan_id_plans"),
        sa.ForeignKeyConstraint(["revision_id"], ["plan_revisions.id"], name="fk_plan_runs_revision_id_plan_revisions"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_plan_runs_user_id_users"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_plan_runs_workspace_id_workspaces"),
        sa.UniqueConstraint("plan_id", "attempt", name="uq_plan_runs_plan_id_attempt"),
        sa.UniqueConstraint("id", "revision_id", name="uq_plan_runs_id_revision_id"),
        sa.CheckConstraint("status IN ('running', 'succeeded', 'partial', 'failed', 'cancelled')", name="ck_plan_runs_status"),
        sa.CheckConstraint(
            "(status = 'running' AND completed_at IS NULL) OR (status <> 'running' AND completed_at IS NOT NULL)",
            name="ck_plan_runs_completion",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_plan_runs_attempt_positive"),
    )
    op.create_index("uq_plan_runs_one_active", "plan_runs", ["plan_id"], unique=True, sqlite_where=ACTIVE_WHERE, postgresql_where=ACTIVE_WHERE)
    op.create_index("uq_plan_runs_one_success", "plan_runs", ["revision_id"], unique=True, sqlite_where=SUCCESS_WHERE, postgresql_where=SUCCESS_WHERE)
    op.create_index("ix_plan_runs_plan_id_created_at", "plan_runs", ["plan_id", "created_at"], unique=False)

    op.create_table(
        "plan_task_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(length=32), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("agent_source", sa.String(length=16), nullable=False),
        sa.Column("agent_key", sa.String(length=64), nullable=False),
        sa.Column("agent_name", sa.String(length=80), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=True),
        sa.Column("agent_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("error_message", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="plan_task_runs_pkey"),
        sa.ForeignKeyConstraint(["run_id", "revision_id"], ["plan_runs.id", "plan_runs.revision_id"], name="fk_plan_task_runs_run"),
        sa.ForeignKeyConstraint(["revision_id", "task_key"], ["plan_tasks.revision_id", "plan_tasks.task_key"], name="fk_plan_task_runs_task"),
        sa.ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_plan_task_runs_execution_id_agent_executions"),
        sa.ForeignKeyConstraint(
            ["agent_version_id"], ["custom_agent_versions.id"], name="fk_plan_task_runs_agent_version_id_custom_agent_versions"
        ),
        sa.UniqueConstraint("run_id", "task_key", name="uq_plan_task_runs_run_id_task_key"),
        sa.UniqueConstraint("run_id", "position", name="uq_plan_task_runs_run_id_position"),
        sa.UniqueConstraint("execution_id", name="uq_plan_task_runs_execution_id"),
        sa.CheckConstraint("status IN ('pending', 'running', 'succeeded', 'failed', 'skipped', 'cancelled')", name="ck_plan_task_runs_status"),
        sa.CheckConstraint("agent_source IN ('builtin', 'custom')", name="ck_plan_task_runs_source"),
        sa.CheckConstraint(
            "(agent_source = 'custom' AND agent_version_id IS NOT NULL AND agent_version IS NOT NULL AND agent_version >= 1) "
            "OR (agent_source = 'builtin' AND agent_version_id IS NULL AND agent_version IS NULL)",
            name="ck_plan_task_runs_version_pin",
        ),
        sa.CheckConstraint(
            "(status IN ('running', 'succeeded') AND execution_id IS NOT NULL) "
            "OR (status IN ('pending', 'skipped') AND execution_id IS NULL) OR status IN ('failed', 'cancelled')",
            name="ck_plan_task_runs_execution",
        ),
        sa.CheckConstraint(
            "(status IN ('pending', 'running') AND completed_at IS NULL) OR (status NOT IN ('pending', 'running') AND completed_at IS NOT NULL)",
            name="ck_plan_task_runs_completion",
        ),
        sa.CheckConstraint(
            "(status <> 'succeeded' OR error_code IS NULL) AND (status NOT IN ('failed', 'skipped') OR error_code IS NOT NULL)",
            name="ck_plan_task_runs_outcome",
        ),
    )
    op.create_index("ix_plan_task_runs_agent_version_id", "plan_task_runs", ["agent_version_id"], unique=False)

    if _dialect() == "sqlite":
        run_changed = " OR ".join(f"NEW.{c} <> OLD.{c}" for c in RUN_FIXED)
        op.execute(
            "CREATE TRIGGER plan_runs_no_update BEFORE UPDATE ON plan_runs "
            f"WHEN OLD.status <> 'running' OR NEW.status = 'running' OR {run_changed} BEGIN SELECT RAISE(ABORT, '{RUN_MESSAGE}'); END"
        )
        op.execute(f"CREATE TRIGGER plan_runs_no_delete BEFORE DELETE ON plan_runs BEGIN SELECT RAISE(ABORT, '{RUN_MESSAGE}'); END")
        task_changed = " OR ".join(
            [f"NEW.{c} <> OLD.{c}" for c in TASK_FIXED] + [f"NEW.{c} IS NOT OLD.{c}" for c in TASK_FIXED_NULLABLE]
            + ["(OLD.status = 'running' AND (NEW.execution_id IS NOT OLD.execution_id OR NEW.started_at IS NOT OLD.started_at))"]
        )
        op.execute(
            "CREATE TRIGGER plan_task_runs_no_update BEFORE UPDATE ON plan_task_runs "
            f"WHEN NOT ({LEGAL}) OR {task_changed} BEGIN SELECT RAISE(ABORT, '{TASK_MESSAGE}'); END"
        )
        op.execute(f"CREATE TRIGGER plan_task_runs_no_delete BEFORE DELETE ON plan_task_runs BEGIN SELECT RAISE(ABORT, '{TASK_MESSAGE}'); END")
    else:
        run_changed = " OR ".join(f"NEW.{c} <> OLD.{c}" for c in RUN_FIXED)
        op.execute(
            f"CREATE FUNCTION {RUN_FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"IF TG_OP = 'DELETE' THEN RAISE EXCEPTION '{RUN_MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; "
            f"IF OLD.status <> 'running' OR NEW.status = 'running' OR {run_changed} THEN "
            f"RAISE EXCEPTION '{RUN_MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; RETURN NEW; END; $$"
        )
        op.execute(f"CREATE TRIGGER {RUN_FUNCTION} BEFORE UPDATE OR DELETE ON plan_runs FOR EACH ROW EXECUTE FUNCTION {RUN_FUNCTION}()")
        task_changed = " OR ".join(
            [f"NEW.{c} <> OLD.{c}" for c in TASK_FIXED] + [f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in TASK_FIXED_NULLABLE]
            + ["(OLD.status = 'running' AND (NEW.execution_id IS DISTINCT FROM OLD.execution_id OR NEW.started_at IS DISTINCT FROM OLD.started_at))"]
        )
        op.execute(
            f"CREATE FUNCTION {TASK_FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"IF TG_OP = 'DELETE' THEN RAISE EXCEPTION '{TASK_MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; "
            f"IF NOT ({LEGAL}) OR {task_changed} THEN "
            f"RAISE EXCEPTION '{TASK_MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; RETURN NEW; END; $$"
        )
        op.execute(f"CREATE TRIGGER {TASK_FUNCTION} BEFORE UPDATE OR DELETE ON plan_task_runs FOR EACH ROW EXECUTE FUNCTION {TASK_FUNCTION}()")


def downgrade() -> None:
    if _dialect() == "sqlite":
        for name in ("plan_task_runs_no_update", "plan_task_runs_no_delete", "plan_runs_no_update", "plan_runs_no_delete"):
            op.execute(f"DROP TRIGGER IF EXISTS {name}")
    else:
        op.execute(f"DROP TRIGGER IF EXISTS {TASK_FUNCTION} ON plan_task_runs")
        op.execute(f"DROP TRIGGER IF EXISTS {RUN_FUNCTION} ON plan_runs")
    op.drop_index("ix_plan_task_runs_agent_version_id", table_name="plan_task_runs")
    op.drop_table("plan_task_runs")
    op.drop_index("ix_plan_runs_plan_id_created_at", table_name="plan_runs")
    op.drop_index("uq_plan_runs_one_success", table_name="plan_runs")
    op.drop_index("uq_plan_runs_one_active", table_name="plan_runs")
    op.drop_table("plan_runs")
    if _dialect() != "sqlite":
        op.execute(f"DROP FUNCTION IF EXISTS {TASK_FUNCTION}()")
        op.execute(f"DROP FUNCTION IF EXISTS {RUN_FUNCTION}()")
