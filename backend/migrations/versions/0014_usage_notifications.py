"""model usage accounting and in-app notifications (M6)

Revision ID: 0014_usage_notifications
Revises: 0013_suspension
Create Date: 2026-11-19

Adds TWO tables; no existing table, column, index, constraint or trigger is touched.

``model_usage`` is the append-only record of ONE provider request: who (workspace, user), where (plan, run, task, execution, agent), what (provider, model, purpose
``plan`` or ``agent``) and how much. Token counts are exactly what the provider reported, NULL when it did not (``usage_known`` says which); nothing is estimated or
invented. ``cost_micros`` (millionths of a US dollar) is calculated from the reported tokens with an explicit, versioned price (``pricing_version``) and is NULL when the
usage or the price is unknown (``cost_basis`` says why). Rows are never updated or deleted.

``notifications`` is the durable in-app notification of ONE user (the recipient) about ONE thing (``kind`` + ``ref_id``): UNIQUE (user_id, kind, ref_id), so a retry, a
recovery or a re-run can never notify twice. Only ``read_at`` may change (NULL -> a time, once); rows are never deleted. Recipients are chosen server-side (the workspace's
owners and admins) in the transaction that creates what they are notified about.

``downgrade()`` removes exactly these objects. Production policy is forward-only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0014_usage_notifications"
down_revision: Union[str, None] = "0013_suspension"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

USAGE_MESSAGE = "model usage is append-only: rows are never updated or deleted"
USAGE_FUNCTION = "model_usage_guard"
NOTE_MESSAGE = "notifications are never deleted and only read_at may change, once"
NOTE_FUNCTION = "notifications_guard"
NOTE_FIXED = ("id", "workspace_id", "user_id", "kind", "ref_id", "created_at")
NOTE_FIXED_NULLABLE = ("run_id",)


def _dialect() -> str:
    return op.get_context().dialect.name


def upgrade() -> None:
    op.create_table(
        "model_usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("task_key", sa.String(length=32), nullable=True),
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        sa.Column("agent_key", sa.String(length=64), nullable=True),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_tokens", sa.Integer(), nullable=True),
        sa.Column("usage_known", sa.Boolean(), nullable=False),
        sa.Column("pricing_version", sa.String(length=32), nullable=True),
        sa.Column("cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("cost_basis", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="model_usage_pkey"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_model_usage_user_id_users"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_model_usage_workspace_id_workspaces"),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], name="fk_model_usage_plan_id_plans"),
        sa.ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_model_usage_run_id_plan_runs"),
        sa.ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_model_usage_execution_id_agent_executions"),
        sa.CheckConstraint("purpose IN ('plan', 'agent')", name="ck_model_usage_purpose"),
        sa.CheckConstraint("cost_basis IN ('calculated', 'unknown_usage', 'unknown_pricing')", name="ck_model_usage_cost_basis"),
        sa.CheckConstraint("(cost_basis = 'calculated') = (cost_micros IS NOT NULL)", name="ck_model_usage_cost_consistent"),
        sa.CheckConstraint("(input_tokens IS NULL OR input_tokens >= 0) AND (output_tokens IS NULL OR output_tokens >= 0) AND (cached_tokens IS NULL OR cached_tokens >= 0)", name="ck_model_usage_tokens"),
    )
    op.create_index("ix_model_usage_workspace_run", "model_usage", ["workspace_id", "run_id"], unique=False)
    op.create_index("ix_model_usage_plan_id", "model_usage", ["plan_id"], unique=False)
    op.create_index("ix_model_usage_workspace_created", "model_usage", ["workspace_id", "created_at"], unique=False)

    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("ref_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="notifications_pkey"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_notifications_workspace_id_workspaces"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_notifications_user_id_users"),
        sa.UniqueConstraint("user_id", "kind", "ref_id", name="uq_notifications_user_kind_ref"),
        sa.CheckConstraint("kind IN ('approval_requested')", name="ck_notifications_kind"),
    )
    op.create_index("ix_notifications_user_read_created", "notifications", ["user_id", "read_at", "created_at"], unique=False)
    op.create_index("ix_notifications_ref_id", "notifications", ["ref_id"], unique=False)

    if _dialect() == "sqlite":
        op.execute(f"CREATE TRIGGER model_usage_no_update BEFORE UPDATE ON model_usage BEGIN SELECT RAISE(ABORT, '{USAGE_MESSAGE}'); END")
        op.execute(f"CREATE TRIGGER model_usage_no_delete BEFORE DELETE ON model_usage BEGIN SELECT RAISE(ABORT, '{USAGE_MESSAGE}'); END")
        changed = " OR ".join([f"NEW.{c} <> OLD.{c}" for c in NOTE_FIXED] + [f"NEW.{c} IS NOT OLD.{c}" for c in NOTE_FIXED_NULLABLE])
        op.execute(f"CREATE TRIGGER notifications_no_update BEFORE UPDATE ON notifications WHEN OLD.read_at IS NOT NULL OR NEW.read_at IS NULL OR {changed} BEGIN SELECT RAISE(ABORT, '{NOTE_MESSAGE}'); END")
        op.execute(f"CREATE TRIGGER notifications_no_delete BEFORE DELETE ON notifications BEGIN SELECT RAISE(ABORT, '{NOTE_MESSAGE}'); END")
    else:
        op.execute(f"CREATE FUNCTION {USAGE_FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION '{USAGE_MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END; $$")
        op.execute(f"CREATE TRIGGER {USAGE_FUNCTION} BEFORE UPDATE OR DELETE ON model_usage FOR EACH ROW EXECUTE FUNCTION {USAGE_FUNCTION}()")
        changed = " OR ".join([f"NEW.{c} <> OLD.{c}" for c in NOTE_FIXED] + [f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in NOTE_FIXED_NULLABLE])
        op.execute(
            f"CREATE FUNCTION {NOTE_FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"IF TG_OP = 'DELETE' THEN RAISE EXCEPTION '{NOTE_MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; "
            f"IF OLD.read_at IS NOT NULL OR NEW.read_at IS NULL OR {changed} THEN RAISE EXCEPTION '{NOTE_MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; RETURN NEW; END; $$"
        )
        op.execute(f"CREATE TRIGGER {NOTE_FUNCTION} BEFORE UPDATE OR DELETE ON notifications FOR EACH ROW EXECUTE FUNCTION {NOTE_FUNCTION}()")


def downgrade() -> None:
    if _dialect() == "sqlite":
        for t in ("model_usage_no_update", "model_usage_no_delete", "notifications_no_update", "notifications_no_delete"):
            op.execute(f"DROP TRIGGER IF EXISTS {t}")
    else:
        op.execute(f"DROP TRIGGER IF EXISTS {USAGE_FUNCTION} ON model_usage")
        op.execute(f"DROP TRIGGER IF EXISTS {NOTE_FUNCTION} ON notifications")
    op.drop_index("ix_notifications_ref_id", table_name="notifications")
    op.drop_index("ix_notifications_user_read_created", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_model_usage_workspace_created", table_name="model_usage")
    op.drop_index("ix_model_usage_plan_id", table_name="model_usage")
    op.drop_index("ix_model_usage_workspace_run", table_name="model_usage")
    op.drop_table("model_usage")
    if _dialect() != "sqlite":
        op.execute(f"DROP FUNCTION IF EXISTS {USAGE_FUNCTION}()")
        op.execute(f"DROP FUNCTION IF EXISTS {NOTE_FUNCTION}()")
