"""tool invocations: durable, auditable use of registered tools by agents (M4-B)

Revision ID: 0011_tools
Revises: 0010_orchestration
Create Date: 2026-11-18

Adds ONE table, ``tool_invocations``; no existing table, column, index, constraint or trigger is touched.

A row is the durable record of one request by an agent to use a registered tool: the exact tool id and version, the execution (and, for a
plan task, the run and task) and the agent it belongs to, the hash of the canonical validated arguments (what a future approval binds to),
the redacted input and output, and the outcome. ``(execution_id, seq)`` and ``idempotency_key`` are UNIQUE: a step cannot be dispatched
twice. Statuses: running, succeeded, failed, timed_out, rejected, cancelled, interrupted, uncertain.

GUARANTEES. Triggers: an invocation is never deleted; its identity, its tool, its input and its start are fixed from creation; it only moves
``running -> final`` once and a finished row is frozen. Foreign keys are NO ACTION (no cascades). Existing data is preserved.

``downgrade()`` removes exactly these objects (the rows in them are discarded). Production policy is forward-only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0011_tools"
down_revision: Union[str, None] = "0010_orchestration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATUSES = ("running", "succeeded", "failed", "timed_out", "rejected", "cancelled", "interrupted", "uncertain")
STATUS_CHECK = "status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")"
SIDE_EFFECT_CHECK = "side_effect IN ('none', 'idempotent', 'non_idempotent')"
COMPLETION_CHECK = "(status = 'running' AND completed_at IS NULL) OR (status <> 'running' AND completed_at IS NOT NULL)"
OUTCOME_CHECK = "status IN ('running', 'succeeded') AND error_code IS NULL OR status NOT IN ('running', 'succeeded') AND error_code IS NOT NULL"
VERSION_CHECK = "tool_version IS NULL OR tool_version >= 1"
MESSAGE = "tool invocations are never deleted, their identity and input are fixed and only the running -> final transition is allowed"
FUNCTION = "tool_invocations_guard"
FIXED = ("id", "user_id", "workspace_id", "execution_id", "agent_key", "tool_id", "seq", "idempotency_key", "side_effect", "created_at", "started_at")
FIXED_NULLABLE = ("run_id", "task_key", "agent_version_id", "tool_version", "args_hash", "input")


def _dialect() -> str:
    return op.get_context().dialect.name


def upgrade() -> None:
    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("task_key", sa.String(length=32), nullable=True),
        sa.Column("agent_key", sa.String(length=64), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=True),
        sa.Column("tool_id", sa.String(length=64), nullable=False),
        sa.Column("tool_version", sa.Integer(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("args_hash", sa.String(length=64), nullable=True),
        sa.Column("side_effect", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("input", sa.Text(), nullable=True),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("error_message", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="tool_invocations_pkey"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_tool_invocations_user_id_users"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_tool_invocations_workspace_id_workspaces"),
        sa.ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_tool_invocations_execution_id_agent_executions"),
        sa.ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_tool_invocations_run_id_plan_runs"),
        sa.UniqueConstraint("execution_id", "seq", name="uq_tool_invocations_execution_id_seq"),
        sa.UniqueConstraint("idempotency_key", name="uq_tool_invocations_idempotency_key"),
        sa.CheckConstraint(STATUS_CHECK, name="ck_tool_invocations_status"),
        sa.CheckConstraint(SIDE_EFFECT_CHECK, name="ck_tool_invocations_side_effect"),
        sa.CheckConstraint(COMPLETION_CHECK, name="ck_tool_invocations_completion"),
        sa.CheckConstraint(OUTCOME_CHECK, name="ck_tool_invocations_outcome"),
        sa.CheckConstraint(VERSION_CHECK, name="ck_tool_invocations_version"),
        sa.CheckConstraint("seq >= 1", name="ck_tool_invocations_seq_positive"),
    )
    op.create_index("ix_tool_invocations_workspace_user_created", "tool_invocations", ["workspace_id", "user_id", "created_at", "id"], unique=False)
    op.create_index("ix_tool_invocations_run_id", "tool_invocations", ["run_id"], unique=False)
    op.create_index("ix_tool_invocations_tool", "tool_invocations", ["tool_id", "tool_version"], unique=False)

    if _dialect() == "sqlite":
        changed = " OR ".join([f"NEW.{c} <> OLD.{c}" for c in FIXED] + [f"NEW.{c} IS NOT OLD.{c}" for c in FIXED_NULLABLE])
        op.execute(f"CREATE TRIGGER tool_invocations_no_update BEFORE UPDATE ON tool_invocations WHEN OLD.status <> 'running' OR {changed} BEGIN SELECT RAISE(ABORT, '{MESSAGE}'); END")
        op.execute(f"CREATE TRIGGER tool_invocations_no_delete BEFORE DELETE ON tool_invocations BEGIN SELECT RAISE(ABORT, '{MESSAGE}'); END")
    else:
        changed = " OR ".join([f"NEW.{c} <> OLD.{c}" for c in FIXED] + [f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in FIXED_NULLABLE])
        op.execute(
            f"CREATE FUNCTION {FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"IF TG_OP = 'DELETE' THEN RAISE EXCEPTION '{MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; "
            f"IF OLD.status <> 'running' OR {changed} THEN RAISE EXCEPTION '{MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; RETURN NEW; END; $$"
        )
        op.execute(f"CREATE TRIGGER {FUNCTION} BEFORE UPDATE OR DELETE ON tool_invocations FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()")


def downgrade() -> None:
    if _dialect() == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS tool_invocations_no_update")
        op.execute("DROP TRIGGER IF EXISTS tool_invocations_no_delete")
    else:
        op.execute(f"DROP TRIGGER IF EXISTS {FUNCTION} ON tool_invocations")
    op.drop_index("ix_tool_invocations_tool", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_run_id", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_workspace_user_created", table_name="tool_invocations")
    op.drop_table("tool_invocations")
    if _dialect() != "sqlite":
        op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}()")
