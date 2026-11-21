"""durable side-effect dispatch ledger, reconciliation records and their run-history events (P0 safety sprint)

Revision ID: 0016_durable_dispatch
Revises: 0015_resilience_required_action
Create Date: 2026-11-19

THE PROBLEM. A task could fail after an external action had been dispatched, and a retry re-ran the task from scratch with a fresh idempotency key, so the action could be repeated. The
coarse ``tool_invocations`` row (``running`` -> final, immutable) cannot say whether a side effect was ever SENT, and a timeout or an error of a side-effecting tool was recorded as a
confirmed failure although the effect may have happened.

1. ``tool_dispatches``: ONE row per invocation of a side-effecting tool, the explicit state machine of its dispatch. States: ``authorized`` (the intent is durable and the approval was consumed;
   nothing was sent) -> ``dispatching`` (persisted BEFORE the first byte leaves) -> ``succeeded`` | ``failed`` (CONFIRMED) | ``unknown`` (the outcome cannot be proven); ``authorized`` ->
   ``cancelled`` (never sent); ``unknown`` -> ``reconciled_succeeded`` | ``reconciled_failed`` (only through a ``tool_reconciliations`` record). Triggers allow exactly these transitions, fix the
   identity columns, and forbid deletes. ``operation_key`` is the stable identity of the LOGICAL operation across attempts and executions; (``operation_key``, ``generation``) is unique and
   ``idempotency_key`` is what is sent to the remote service.
2. ``tool_reconciliations``: the append-only evidence/decision/actor/time/reason of every reconciliation attempt (including ``unresolved``). Never updated or deleted.
3. ``execution_events`` gains nine event types (``tool_dispatch_*``). Existing rows, ids, indexes and the append-only triggers are preserved (SQLite rebuilds the table to change a CHECK;
   PostgreSQL alters the constraint).

``downgrade()`` removes the two tables and restores the previous vocabulary (events of the new types are discarded; the append-only trigger is lifted for that one statement).
Production policy is forward-only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0016_durable_dispatch"
down_revision: Union[str, None] = "0015_resilience_required_action"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_TYPES = ("run_created", "run_claimed", "run_resumed", "task_started", "task_succeeded", "task_failed", "task_skipped", "task_interrupted", "task_cancelled",
             "task_reused", "cancellation_requested", "cancellation_acknowledged", "recovery_performed", "retry_authorized", "run_completed",
             "approval_requested", "approval_approved", "approval_rejected", "approval_expired", "approval_invalidated", "approval_consumed", "task_suspended", "task_resumed")
NEW_TYPES = ("tool_dispatch_authorized", "tool_dispatch_started", "tool_dispatch_succeeded", "tool_dispatch_failed", "tool_dispatch_unknown", "tool_dispatch_cancelled",
             "tool_dispatch_replayed", "tool_dispatch_reconciled", "tool_dispatch_refused")
ACTORS = ("user", "api", "worker", "recovery")
EVENT_MESSAGE = "execution events are append-only: rows are never updated or deleted"
EVENT_GUARD = "execution_events_guard"
STATES = ("authorized", "dispatching", "succeeded", "failed", "unknown", "reconciled_succeeded", "reconciled_failed", "cancelled")
DISPATCH_MESSAGE = "a dispatch moves only authorized -> dispatching | cancelled, dispatching -> succeeded | failed | unknown, unknown -> reconciled_succeeded | reconciled_failed, and is never deleted"
DISPATCH_FUNCTION = "tool_dispatches_guard"
DISPATCH_FIXED = ("id", "invocation_id", "user_id", "workspace_id", "operation_key", "generation", "idempotency_key", "execution_id", "tool_id", "tool_version", "args_hash", "created_at")
DISPATCH_FIXED_NULLABLE = ("run_id", "task_key")
RECON_MESSAGE = "reconciliations are append-only: rows are never updated or deleted"
RECON_FUNCTION = "tool_reconciliations_guard"
LEGAL = ("(OLD.state = 'authorized' AND NEW.state IN ('dispatching', 'cancelled')) OR (OLD.state = 'dispatching' AND NEW.state IN ('succeeded', 'failed', 'unknown')) "
         "OR (OLD.state = 'unknown' AND NEW.state IN ('reconciled_succeeded', 'reconciled_failed'))")


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
        "tool_dispatches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("invocation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("operation_key", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("task_key", sa.String(length=32), nullable=True),
        sa.Column("tool_id", sa.String(length=64), nullable=False),
        sa.Column("tool_version", sa.Integer(), nullable=False),
        sa.Column("args_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("external_ref", sa.String(length=100), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("dispatch_started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="tool_dispatches_pkey"),
        sa.ForeignKeyConstraint(["invocation_id"], ["tool_invocations.id"], name="fk_tool_dispatches_invocation_id_tool_invocations"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_tool_dispatches_user_id_users"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_tool_dispatches_workspace_id_workspaces"),
        sa.ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_tool_dispatches_execution_id_agent_executions"),
        sa.ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_tool_dispatches_run_id_plan_runs"),
        sa.UniqueConstraint("invocation_id", name="uq_tool_dispatches_invocation_id"),
        sa.UniqueConstraint("operation_key", "generation", name="uq_tool_dispatches_operation_generation"),
        sa.CheckConstraint("state IN (" + ", ".join(f"'{s}'" for s in STATES) + ")", name="ck_tool_dispatches_state"),
        sa.CheckConstraint("generation >= 1 AND tool_version >= 1", name="ck_tool_dispatches_numbers"),
        sa.CheckConstraint("(state IN ('authorized', 'cancelled')) OR dispatch_started_at IS NOT NULL", name="ck_tool_dispatches_started"),
        sa.CheckConstraint("(state IN ('authorized', 'dispatching')) = (completed_at IS NULL)", name="ck_tool_dispatches_completed"),
    )
    op.create_index("ix_tool_dispatches_operation_key", "tool_dispatches", ["operation_key"], unique=False)
    op.create_index("ix_tool_dispatches_run_task", "tool_dispatches", ["run_id", "task_key"], unique=False)
    op.create_index("ix_tool_dispatches_execution_id", "tool_dispatches", ["execution_id"], unique=False)
    op.create_index("ix_tool_dispatches_workspace_state", "tool_dispatches", ["workspace_id", "state"], unique=False)

    op.create_table(
        "tool_reconciliations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dispatch_id", sa.Uuid(), nullable=False),
        sa.Column("invocation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("evidence_kind", sa.String(length=24), nullable=False),
        sa.Column("evidence", sa.String(length=500), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("external_ref", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="tool_reconciliations_pkey"),
        sa.ForeignKeyConstraint(["dispatch_id"], ["tool_dispatches.id"], name="fk_tool_reconciliations_dispatch_id_tool_dispatches"),
        sa.ForeignKeyConstraint(["invocation_id"], ["tool_invocations.id"], name="fk_tool_reconciliations_invocation_id_tool_invocations"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_tool_reconciliations_user_id_users"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_tool_reconciliations_workspace_id_workspaces"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], name="fk_tool_reconciliations_actor_user_id_users"),
        sa.CheckConstraint("decision IN ('confirmed_succeeded', 'confirmed_failed', 'unresolved')", name="ck_tool_reconciliations_decision"),
        sa.CheckConstraint("evidence_kind IN ('remote_query', 'operator_attestation')", name="ck_tool_reconciliations_evidence_kind"),
        sa.CheckConstraint("length(reason) >= 3", name="ck_tool_reconciliations_reason"),
    )
    op.create_index("ix_tool_reconciliations_dispatch_id", "tool_reconciliations", ["dispatch_id", "created_at"], unique=False)

    fixed = " OR ".join([f"NEW.{c} <> OLD.{c}" for c in DISPATCH_FIXED])
    if _dialect() == "sqlite":
        fixed_null = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in DISPATCH_FIXED_NULLABLE)
        op.execute(f"CREATE TRIGGER tool_dispatches_no_update BEFORE UPDATE ON tool_dispatches WHEN NOT ({LEGAL}) OR {fixed} OR {fixed_null} BEGIN SELECT RAISE(ABORT, '{DISPATCH_MESSAGE}'); END")
        op.execute(f"CREATE TRIGGER tool_dispatches_no_delete BEFORE DELETE ON tool_dispatches BEGIN SELECT RAISE(ABORT, '{DISPATCH_MESSAGE}'); END")
        op.execute(f"CREATE TRIGGER tool_reconciliations_no_update BEFORE UPDATE ON tool_reconciliations BEGIN SELECT RAISE(ABORT, '{RECON_MESSAGE}'); END")
        op.execute(f"CREATE TRIGGER tool_reconciliations_no_delete BEFORE DELETE ON tool_reconciliations BEGIN SELECT RAISE(ABORT, '{RECON_MESSAGE}'); END")
        _sqlite_rebuild_events(OLD_TYPES + NEW_TYPES)
    else:
        fixed_null = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in DISPATCH_FIXED_NULLABLE)
        op.execute(
            f"CREATE FUNCTION {DISPATCH_FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"IF TG_OP = 'DELETE' THEN RAISE EXCEPTION '{DISPATCH_MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; "
            f"IF NOT ({LEGAL}) OR {fixed} OR {fixed_null} THEN RAISE EXCEPTION '{DISPATCH_MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; RETURN NEW; END; $$"
        )
        op.execute(f"CREATE TRIGGER {DISPATCH_FUNCTION} BEFORE UPDATE OR DELETE ON tool_dispatches FOR EACH ROW EXECUTE FUNCTION {DISPATCH_FUNCTION}()")
        op.execute(f"CREATE FUNCTION {RECON_FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION '{RECON_MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END; $$")
        op.execute(f"CREATE TRIGGER {RECON_FUNCTION} BEFORE UPDATE OR DELETE ON tool_reconciliations FOR EACH ROW EXECUTE FUNCTION {RECON_FUNCTION}()")
        op.drop_constraint("ck_execution_events_type", "execution_events", type_="check")
        op.create_check_constraint("ck_execution_events_type", "execution_events", _check(OLD_TYPES + NEW_TYPES))


def downgrade() -> None:
    if _dialect() == "sqlite":
        for t in ("tool_dispatches_no_update", "tool_dispatches_no_delete", "tool_reconciliations_no_update", "tool_reconciliations_no_delete"):
            op.execute(f"DROP TRIGGER IF EXISTS {t}")
        op.execute("DROP TRIGGER IF EXISTS execution_events_guard_no_update")
        op.execute("DROP TRIGGER IF EXISTS execution_events_guard_no_delete")
        op.execute("DELETE FROM execution_events WHERE event_type IN (" + ", ".join(f"'{t}'" for t in NEW_TYPES) + ")")
        _sqlite_rebuild_events(OLD_TYPES)
    else:
        op.execute(f"DROP TRIGGER IF EXISTS {DISPATCH_FUNCTION} ON tool_dispatches")
        op.execute(f"DROP TRIGGER IF EXISTS {RECON_FUNCTION} ON tool_reconciliations")
        op.execute(f"ALTER TABLE execution_events DISABLE TRIGGER {EVENT_GUARD}")
        op.execute("DELETE FROM execution_events WHERE event_type IN (" + ", ".join(f"'{t}'" for t in NEW_TYPES) + ")")
        op.execute(f"ALTER TABLE execution_events ENABLE TRIGGER {EVENT_GUARD}")
        op.drop_constraint("ck_execution_events_type", "execution_events", type_="check")
        op.create_check_constraint("ck_execution_events_type", "execution_events", _check(OLD_TYPES))
    op.drop_index("ix_tool_reconciliations_dispatch_id", table_name="tool_reconciliations")
    op.drop_table("tool_reconciliations")
    op.drop_index("ix_tool_dispatches_workspace_state", table_name="tool_dispatches")
    op.drop_index("ix_tool_dispatches_execution_id", table_name="tool_dispatches")
    op.drop_index("ix_tool_dispatches_run_task", table_name="tool_dispatches")
    op.drop_index("ix_tool_dispatches_operation_key", table_name="tool_dispatches")
    op.drop_table("tool_dispatches")
    if _dialect() != "sqlite":
        op.execute(f"DROP FUNCTION IF EXISTS {DISPATCH_FUNCTION}()")
        op.execute(f"DROP FUNCTION IF EXISTS {RECON_FUNCTION}()")
