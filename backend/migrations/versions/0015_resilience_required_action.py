"""provider retry events and the structural required action of a task (M6b)

Revision ID: 0015_resilience_required_action
Revises: 0014_usage_notifications
Create Date: 2026-11-19

Adds ONE table and ONE nullable column; nothing existing is changed.

``provider_events`` is the append-only record of what happened while a provider request was retried: ``waiting`` (about to sleep, with the wait), ``retrying``, ``recovered``,
``exhausted`` (the retry budget of a rate limit is spent) and ``failed`` (any other final failure), with the attempt number and the fixed error code. It holds no prompt, no answer and no
credential. Rows are never updated or deleted.

``plan_tasks.required_tool`` is the exact tool reference (``id@version``) a task MUST invoke to count as done, resolved and validated server-side when the plan is made; NULL for a task
that requires no specific action.

``downgrade()`` removes exactly these. Production policy is forward-only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0015_resilience_required_action"
down_revision: Union[str, None] = "0014_usage_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MESSAGE = "provider events are append-only: rows are never updated or deleted"
FUNCTION = "provider_events_guard"
KINDS = ("waiting", "retrying", "recovered", "exhausted", "failed")


def _dialect() -> str:
    return op.get_context().dialect.name


def upgrade() -> None:
    op.create_table(
        "provider_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("task_key", sa.String(length=32), nullable=True),
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("wait_ms", sa.Integer(), nullable=True),
        sa.Column("detail", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="provider_events_pkey"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_provider_events_user_id_users"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_provider_events_workspace_id_workspaces"),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], name="fk_provider_events_plan_id_plans"),
        sa.ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_provider_events_run_id_plan_runs"),
        sa.ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_provider_events_execution_id_agent_executions"),
        sa.CheckConstraint("kind IN (" + ", ".join(f"'{k}'" for k in KINDS) + ")", name="ck_provider_events_kind"),
        sa.CheckConstraint("attempt >= 1 AND (wait_ms IS NULL OR wait_ms >= 0)", name="ck_provider_events_numbers"),
    )
    op.create_index("ix_provider_events_workspace_run", "provider_events", ["workspace_id", "run_id", "created_at"], unique=False)
    op.create_index("ix_provider_events_plan_id", "provider_events", ["plan_id"], unique=False)
    op.add_column("plan_tasks", sa.Column("required_tool", sa.String(length=100), nullable=True))
    if _dialect() == "sqlite":
        op.execute(f"CREATE TRIGGER provider_events_no_update BEFORE UPDATE ON provider_events BEGIN SELECT RAISE(ABORT, '{MESSAGE}'); END")
        op.execute(f"CREATE TRIGGER provider_events_no_delete BEFORE DELETE ON provider_events BEGIN SELECT RAISE(ABORT, '{MESSAGE}'); END")
    else:
        op.execute(f"CREATE FUNCTION {FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION '{MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END; $$")
        op.execute(f"CREATE TRIGGER {FUNCTION} BEFORE UPDATE OR DELETE ON provider_events FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()")


def downgrade() -> None:
    if _dialect() == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS provider_events_no_update")
        op.execute("DROP TRIGGER IF EXISTS provider_events_no_delete")
        op.execute("ALTER TABLE plan_tasks DROP COLUMN required_tool")  # a batch rebuild would drop the table's triggers; the native DROP COLUMN keeps them
    else:
        op.execute(f"DROP TRIGGER IF EXISTS {FUNCTION} ON provider_events")
        op.drop_column("plan_tasks", "required_tool")
    op.drop_index("ix_provider_events_plan_id", table_name="provider_events")
    op.drop_index("ix_provider_events_workspace_run", table_name="provider_events")
    op.drop_table("provider_events")
    if _dialect() != "sqlite":
        op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}()")
