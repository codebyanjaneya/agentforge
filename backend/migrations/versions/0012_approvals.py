"""tool approvals: a human approval bound to ONE exact tool invocation (M4-C)

Revision ID: 0012_approvals
Revises: 0011_tools
Create Date: 2026-11-18

Adds ONE table, ``tool_approvals``; no existing table, column, index, constraint or trigger is touched.

A row binds a human decision to the exact action it authorises: workspace and user, execution (and plan run and task), agent and exact agent
version, exact tool id and version, the redacted canonical VALIDATED arguments, and ``payload_hash`` = sha256 of the canonical envelope of all of
those. Lifecycle: pending -> approved | rejected | expired | invalidated; approved -> consumed | expired | invalidated; all other states are
final. ``consumed`` requires (CHECK) and records the invocation it authorised; ``(execution_id, seq)`` is UNIQUE (one approval per step) and so is
``invocation_id`` (an approval authorises one invocation). Triggers: never deleted; the binding columns never change; only the transitions above.
Foreign keys are NO ACTION. ``downgrade()`` removes exactly these objects. Production policy is forward-only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012_approvals"
down_revision: Union[str, None] = "0011_tools"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATUSES = ("pending", "approved", "rejected", "expired", "invalidated", "consumed")
MESSAGE = "tool approvals are never deleted, their binding is fixed and only the documented lifecycle transitions are allowed"
FUNCTION = "tool_approvals_guard"
FIXED = ("id", "user_id", "workspace_id", "execution_id", "agent_key", "tool_id", "tool_version", "seq", "risk", "side_effect", "args_json", "args_hash", "payload_hash", "created_at", "expires_at")
FIXED_NULLABLE = ("run_id", "task_key", "agent_version_id", "reason")


def _dialect() -> str:
    return op.get_context().dialect.name


def upgrade() -> None:
    op.create_table(
        "tool_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("task_key", sa.String(length=32), nullable=True),
        sa.Column("agent_key", sa.String(length=64), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=True),
        sa.Column("tool_id", sa.String(length=64), nullable=False),
        sa.Column("tool_version", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("risk", sa.String(length=16), nullable=False),
        sa.Column("side_effect", sa.String(length=16), nullable=False),
        sa.Column("args_json", sa.Text(), nullable=False),
        sa.Column("args_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("invocation_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="tool_approvals_pkey"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_tool_approvals_user_id_users"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_tool_approvals_workspace_id_workspaces"),
        sa.ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_tool_approvals_execution_id_agent_executions"),
        sa.ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_tool_approvals_run_id_plan_runs"),
        sa.ForeignKeyConstraint(["invocation_id"], ["tool_invocations.id"], name="fk_tool_approvals_invocation_id_tool_invocations"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], name="fk_tool_approvals_decided_by_users"),
        sa.UniqueConstraint("execution_id", "seq", name="uq_tool_approvals_execution_id_seq"),
        sa.UniqueConstraint("invocation_id", name="uq_tool_approvals_invocation_id"),
        sa.CheckConstraint("status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")", name="ck_tool_approvals_status"),
        sa.CheckConstraint("seq >= 1", name="ck_tool_approvals_seq_positive"),
        sa.CheckConstraint("(status = 'consumed') = (invocation_id IS NOT NULL)", name="ck_tool_approvals_consumed_link"),
    )
    op.create_index("ix_tool_approvals_workspace_status_created", "tool_approvals", ["workspace_id", "status", "created_at", "id"], unique=False)
    op.create_index("ix_tool_approvals_run_id", "tool_approvals", ["run_id"], unique=False)

    transition = ("NOT ((OLD.status = 'pending' AND NEW.status IN ('approved', 'rejected', 'expired', 'invalidated')) "
                  "OR (OLD.status = 'approved' AND NEW.status IN ('consumed', 'expired', 'invalidated')))")
    if _dialect() == "sqlite":
        changed = " OR ".join([f"NEW.{c} <> OLD.{c}" for c in FIXED] + [f"NEW.{c} IS NOT OLD.{c}" for c in FIXED_NULLABLE])
        op.execute(f"CREATE TRIGGER tool_approvals_no_update BEFORE UPDATE ON tool_approvals WHEN {transition} OR {changed} BEGIN SELECT RAISE(ABORT, '{MESSAGE}'); END")
        op.execute(f"CREATE TRIGGER tool_approvals_no_delete BEFORE DELETE ON tool_approvals BEGIN SELECT RAISE(ABORT, '{MESSAGE}'); END")
    else:
        changed = " OR ".join([f"NEW.{c} <> OLD.{c}" for c in FIXED] + [f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in FIXED_NULLABLE])
        op.execute(
            f"CREATE FUNCTION {FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"IF TG_OP = 'DELETE' THEN RAISE EXCEPTION '{MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; "
            f"IF {transition} OR {changed} THEN RAISE EXCEPTION '{MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; RETURN NEW; END; $$"
        )
        op.execute(f"CREATE TRIGGER {FUNCTION} BEFORE UPDATE OR DELETE ON tool_approvals FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()")


def downgrade() -> None:
    if _dialect() == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS tool_approvals_no_update")
        op.execute("DROP TRIGGER IF EXISTS tool_approvals_no_delete")
    else:
        op.execute(f"DROP TRIGGER IF EXISTS {FUNCTION} ON tool_approvals")
    op.drop_index("ix_tool_approvals_run_id", table_name="tool_approvals")
    op.drop_index("ix_tool_approvals_workspace_status_created", table_name="tool_approvals")
    op.drop_table("tool_approvals")
    if _dialect() != "sqlite":
        op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}()")
