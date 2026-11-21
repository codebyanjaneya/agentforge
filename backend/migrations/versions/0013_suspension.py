"""durable suspension of a task waiting for a human approval, and the approval lifecycle in the run history (M4-C finalization)

Revision ID: 0013_suspension
Revises: 0012_approvals
Create Date: 2026-11-18

1. ONE new table, ``tool_suspensions``: the durable record that a plan task is SUSPENDED on one approval. It stores the redacted conversation of the
   agent so far and where the tool loop stopped, so ANY process can resume the exact call later. ``resumed_at`` is set exactly once, in the SAME
   transaction that consumes the approval (or records the refusal), so a suspension is "open" until its outcome is durable. Triggers: never deleted; the
   transcript and position never change; ``resumed_at`` may only go NULL -> value.
2. ``execution_events``: the closed event vocabulary (a CHECK constraint) gains eight types: approval_requested, approval_approved, approval_rejected,
   approval_expired, approval_invalidated, approval_consumed, task_suspended, task_resumed. Existing rows, ids, indexes and the append-only triggers are
   preserved (SQLite rebuilds the table to change a CHECK; PostgreSQL alters the constraint).

``downgrade()`` removes the table and restores the old vocabulary; event rows of the new types are discarded (the append-only trigger is lifted for that
one statement). Production policy is forward-only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0013_suspension"
down_revision: Union[str, None] = "0012_approvals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_TYPES = ("run_created", "run_claimed", "run_resumed", "task_started", "task_succeeded", "task_failed", "task_skipped", "task_interrupted", "task_cancelled",
             "task_reused", "cancellation_requested", "cancellation_acknowledged", "recovery_performed", "retry_authorized", "run_completed")
NEW_TYPES = ("approval_requested", "approval_approved", "approval_rejected", "approval_expired", "approval_invalidated", "approval_consumed", "task_suspended", "task_resumed")
ACTORS = ("user", "api", "worker", "recovery")
EVENT_MESSAGE = "execution events are append-only: rows are never updated or deleted"
EVENT_GUARD = "execution_events_guard"
SUSP_MESSAGE = "suspensions are never deleted, their transcript is fixed and resumed_at is set once"
SUSP_FUNCTION = "tool_suspensions_guard"
SUSP_FIXED = ("approval_id", "execution_id", "run_id", "task_key", "seq", "round_number", "call_index", "transcript", "calls", "created_at")


def _dialect() -> str:
    return op.get_context().dialect.name


def _check(types) -> str:
    return "event_type IN (" + ", ".join(f"'{t}'" for t in types) + ")"


def _events_table(types) -> None:
    op.create_table(
        "execution_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(length=32), nullable=True),
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=100), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="execution_events_pkey"),
        sa.ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_execution_events_run_id_plan_runs"),
        sa.CheckConstraint(_check(types), name="ck_execution_events_type"),
        sa.CheckConstraint("actor_type IN (" + ", ".join(f"'{a}'" for a in ACTORS) + ")", name="ck_execution_events_actor"),
    )


def _sqlite_rebuild_events(types) -> None:
    """SQLite cannot change a CHECK in place: keep the old table aside, build the table anew under its real name (every constraint name preserved) with the given
    vocabulary, copy every row (ids kept), drop the old one, restore the indexes and the append-only triggers."""
    op.execute("DROP TRIGGER IF EXISTS execution_events_guard_no_update")
    op.execute("DROP TRIGGER IF EXISTS execution_events_guard_no_delete")
    op.rename_table("execution_events", "execution_events_old")
    op.drop_index("ix_execution_events_run_id_id", table_name="execution_events_old")
    op.drop_index("ix_execution_events_workspace_id_id", table_name="execution_events_old")
    _events_table(types)
    op.execute("INSERT INTO execution_events (id, run_id, user_id, workspace_id, task_key, execution_id, event_type, actor_type, actor_id, detail, created_at) "
               "SELECT id, run_id, user_id, workspace_id, task_key, execution_id, event_type, actor_type, actor_id, detail, created_at FROM execution_events_old")
    op.drop_table("execution_events_old")
    op.create_index("ix_execution_events_run_id_id", "execution_events", ["run_id", "id"], unique=False)
    op.create_index("ix_execution_events_workspace_id_id", "execution_events", ["workspace_id", "id"], unique=False)
    op.execute(f"CREATE TRIGGER {EVENT_GUARD}_no_update BEFORE UPDATE ON execution_events BEGIN SELECT RAISE(ABORT, '{EVENT_MESSAGE}'); END")
    op.execute(f"CREATE TRIGGER {EVENT_GUARD}_no_delete BEFORE DELETE ON execution_events BEGIN SELECT RAISE(ABORT, '{EVENT_MESSAGE}'); END")


def upgrade() -> None:
    op.create_table(
        "tool_suspensions",
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(length=32), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("call_index", sa.Integer(), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=False),
        sa.Column("calls", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resumed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("approval_id", name="tool_suspensions_pkey"),
        sa.ForeignKeyConstraint(["approval_id"], ["tool_approvals.id"], name="fk_tool_suspensions_approval_id_tool_approvals"),
        sa.ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_tool_suspensions_execution_id_agent_executions"),
        sa.ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_tool_suspensions_run_id_plan_runs"),
        sa.UniqueConstraint("execution_id", "seq", name="uq_tool_suspensions_execution_id_seq"),
        sa.CheckConstraint("seq >= 1 AND round_number >= 0 AND call_index >= 0", name="ck_tool_suspensions_positions"),
    )
    op.create_index("ix_tool_suspensions_run_id", "tool_suspensions", ["run_id"], unique=False)
    op.create_index("ix_tool_suspensions_execution_id", "tool_suspensions", ["execution_id"], unique=False)

    if _dialect() == "sqlite":
        changed = " OR ".join(f"NEW.{c} <> OLD.{c}" for c in SUSP_FIXED)
        op.execute(f"CREATE TRIGGER tool_suspensions_no_update BEFORE UPDATE ON tool_suspensions WHEN OLD.resumed_at IS NOT NULL OR {changed} BEGIN SELECT RAISE(ABORT, '{SUSP_MESSAGE}'); END")
        op.execute(f"CREATE TRIGGER tool_suspensions_no_delete BEFORE DELETE ON tool_suspensions BEGIN SELECT RAISE(ABORT, '{SUSP_MESSAGE}'); END")
        _sqlite_rebuild_events(OLD_TYPES + NEW_TYPES)
    else:
        changed = " OR ".join(f"NEW.{c} <> OLD.{c}" for c in SUSP_FIXED)
        op.execute(
            f"CREATE FUNCTION {SUSP_FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"IF TG_OP = 'DELETE' THEN RAISE EXCEPTION '{SUSP_MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; "
            f"IF OLD.resumed_at IS NOT NULL OR {changed} THEN RAISE EXCEPTION '{SUSP_MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; RETURN NEW; END; $$"
        )
        op.execute(f"CREATE TRIGGER {SUSP_FUNCTION} BEFORE UPDATE OR DELETE ON tool_suspensions FOR EACH ROW EXECUTE FUNCTION {SUSP_FUNCTION}()")
        op.drop_constraint("ck_execution_events_type", "execution_events", type_="check")
        op.create_check_constraint("ck_execution_events_type", "execution_events", _check(OLD_TYPES + NEW_TYPES))


def downgrade() -> None:
    if _dialect() == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS tool_suspensions_no_update")
        op.execute("DROP TRIGGER IF EXISTS tool_suspensions_no_delete")
        op.execute("DROP TRIGGER IF EXISTS execution_events_guard_no_update")
        op.execute("DROP TRIGGER IF EXISTS execution_events_guard_no_delete")
        op.execute("DELETE FROM execution_events WHERE event_type IN (" + ", ".join(f"'{t}'" for t in NEW_TYPES) + ")")
        _sqlite_rebuild_events(OLD_TYPES)
    else:
        op.execute(f"DROP TRIGGER IF EXISTS {SUSP_FUNCTION} ON tool_suspensions")
        op.execute(f"ALTER TABLE execution_events DISABLE TRIGGER {EVENT_GUARD}")
        op.execute("DELETE FROM execution_events WHERE event_type IN (" + ", ".join(f"'{t}'" for t in NEW_TYPES) + ")")
        op.execute(f"ALTER TABLE execution_events ENABLE TRIGGER {EVENT_GUARD}")
        op.drop_constraint("ck_execution_events_type", "execution_events", type_="check")
        op.create_check_constraint("ck_execution_events_type", "execution_events", _check(OLD_TYPES))
    op.drop_index("ix_tool_suspensions_execution_id", table_name="tool_suspensions")
    op.drop_index("ix_tool_suspensions_run_id", table_name="tool_suspensions")
    op.drop_table("tool_suspensions")
    if _dialect() != "sqlite":
        op.execute(f"DROP FUNCTION IF EXISTS {SUSP_FUNCTION}()")
