"""durable orchestration: execution events, retry lineage, task carry-over, process registry, execution owners (M4-A2)

Revision ID: 0010_orchestration
Revises: 0009_run_leases
Create Date: 2026-11-18

Adds FIVE tables and nothing else; no existing table, column, index, constraint or trigger is touched (the accepted 0006, 0008 and
0009 objects stay byte for byte as they are):

* ``execution_events``     append-only history of a plan run, scoped by (user_id, workspace_id); ``id`` is the total order;
* ``plan_run_retries``     lineage of an explicit retry attempt (``run_id`` retries ``retry_of_run_id``; a run is retried at most once);
* ``plan_task_carryovers`` a task of a retry run that REUSES the persisted result of an earlier task run instead of running again;
* ``worker_processes``     one heartbeat row per application process (API or worker), the authority for "is this process alive";
* ``execution_owners``     which process runs an execution (gives standalone REST/WebSocket executions an owner).

GUARANTEES. Triggers make ``execution_events``, ``plan_run_retries``, ``plan_task_carryovers`` and ``execution_owners`` append-only:
UPDATE and DELETE are refused. ``worker_processes`` is mutable (heartbeat). Foreign keys are NO ACTION. Existing data is preserved;
an execution or run that predates this migration simply has no owner / event rows.

``downgrade()`` drops exactly these objects (the rows in them are discarded). Production policy is forward-only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0010_orchestration"
down_revision: Union[str, None] = "0009_run_leases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EVENT_TYPES = (
    "run_created", "run_claimed", "run_resumed", "task_started", "task_succeeded", "task_failed", "task_skipped", "task_interrupted",
    "task_cancelled", "task_reused", "cancellation_requested", "cancellation_acknowledged", "recovery_performed", "retry_authorized",
    "run_completed",
)
EVENT_ACTORS = ("user", "api", "worker", "recovery")
EVENT_TYPE_CHECK = "event_type IN (" + ", ".join(f"'{t}'" for t in EVENT_TYPES) + ")"
EVENT_ACTOR_CHECK = "actor_type IN (" + ", ".join(f"'{a}'" for a in EVENT_ACTORS) + ")"
APPEND_ONLY = {  # table -> (trigger/function base name, message)
    "execution_events": ("execution_events_guard", "execution events are append-only: rows are never updated or deleted"),
    "plan_run_retries": ("plan_run_retries_guard", "retry lineage is append-only: rows are never updated or deleted"),
    "plan_task_carryovers": ("plan_task_carryovers_guard", "task carry-overs are append-only: rows are never updated or deleted"),
    "execution_owners": ("execution_owners_guard", "execution owners are append-only: rows are never updated or deleted"),
}


def _dialect() -> str:
    return op.get_context().dialect.name


def upgrade() -> None:
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
        sa.CheckConstraint(EVENT_TYPE_CHECK, name="ck_execution_events_type"),
        sa.CheckConstraint(EVENT_ACTOR_CHECK, name="ck_execution_events_actor"),
    )
    op.create_index("ix_execution_events_run_id_id", "execution_events", ["run_id", "id"], unique=False)
    op.create_index("ix_execution_events_workspace_id_id", "execution_events", ["workspace_id", "id"], unique=False)

    op.create_table(
        "plan_run_retries",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("retry_of_run_id", sa.Uuid(), nullable=False),
        sa.Column("authorized_by", sa.Uuid(), nullable=False),
        sa.Column("acknowledged", sa.Text(), nullable=False),
        sa.Column("carried_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", name="plan_run_retries_pkey"),
        sa.ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_plan_run_retries_run_id_plan_runs"),
        sa.ForeignKeyConstraint(["retry_of_run_id"], ["plan_runs.id"], name="fk_plan_run_retries_retry_of_run_id_plan_runs"),
        sa.ForeignKeyConstraint(["authorized_by"], ["users.id"], name="fk_plan_run_retries_authorized_by_users"),
        sa.UniqueConstraint("retry_of_run_id", name="uq_plan_run_retries_retry_of_run_id"),
    )

    op.create_table(
        "plan_task_carryovers",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(length=32), nullable=False),
        sa.Column("source_task_run_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "task_key", name="plan_task_carryovers_pkey"),
        sa.ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_plan_task_carryovers_run_id_plan_runs"),
        sa.ForeignKeyConstraint(["source_task_run_id"], ["plan_task_runs.id"], name="fk_plan_task_carryovers_source_task_run_id_plan_task_runs"),
    )
    op.create_index("ix_plan_task_carryovers_source_task_run_id", "plan_task_carryovers", ["source_task_run_id"], unique=False)

    op.create_table(
        "worker_processes",
        sa.Column("owner_id", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", name="worker_processes_pkey"),
        sa.CheckConstraint("kind IN ('api', 'worker')", name="ck_worker_processes_kind"),
    )
    op.create_index("ix_worker_processes_expires_at", "worker_processes", ["expires_at"], unique=False)

    op.create_table(
        "execution_owners",
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("execution_id", name="execution_owners_pkey"),
        sa.ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], name="fk_execution_owners_execution_id_agent_executions"),
    )
    op.create_index("ix_execution_owners_owner_id", "execution_owners", ["owner_id"], unique=False)

    for table, (name, message) in APPEND_ONLY.items():
        if _dialect() == "sqlite":
            op.execute(f"CREATE TRIGGER {name}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, '{message}'); END")
            op.execute(f"CREATE TRIGGER {name}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, '{message}'); END")
        else:
            op.execute(
                f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                f"RAISE EXCEPTION '{message}' USING ERRCODE = 'integrity_constraint_violation'; END; $$"
            )
            op.execute(f"CREATE TRIGGER {name} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION {name}()")


def downgrade() -> None:
    for table, (name, _message) in APPEND_ONLY.items():
        if _dialect() == "sqlite":
            op.execute(f"DROP TRIGGER IF EXISTS {name}_no_update")
            op.execute(f"DROP TRIGGER IF EXISTS {name}_no_delete")
        else:
            op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    op.drop_index("ix_execution_owners_owner_id", table_name="execution_owners")
    op.drop_table("execution_owners")
    op.drop_index("ix_worker_processes_expires_at", table_name="worker_processes")
    op.drop_table("worker_processes")
    op.drop_index("ix_plan_task_carryovers_source_task_run_id", table_name="plan_task_carryovers")
    op.drop_table("plan_task_carryovers")
    op.drop_table("plan_run_retries")
    op.drop_index("ix_execution_events_workspace_id_id", table_name="execution_events")
    op.drop_index("ix_execution_events_run_id_id", table_name="execution_events")
    op.drop_table("execution_events")
    if _dialect() != "sqlite":
        for _table, (name, _message) in APPEND_ONLY.items():
            op.execute(f"DROP FUNCTION IF EXISTS {name}()")
