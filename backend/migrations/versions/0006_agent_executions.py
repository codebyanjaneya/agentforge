"""agent executions with version pinning (M2-B)

Revision ID: 0006_agent_executions
Revises: 0005_custom_agents
Create Date: 2026-11-15

Adds ``agent_executions``: one row per execution of a built-in or custom agent. No existing table is touched.

* ownership: ``user_id`` and ``workspace_id`` NOT NULL (FKs) - taken from the verified scope, never from a request;
* identity: ``agent_source`` ('builtin'|'custom'), ``agent_key``, ``agent_name``;
* PIN: ``agent_version_id`` (FK to the immutable ``custom_agent_versions`` row) and ``agent_version`` are both set for
  a custom execution and both NULL for a built-in one (CHECK ``ck_agent_executions_version_pin``): built-in prompts
  are code, so there is deliberately no fake version id;
* lifecycle: ``status`` running|succeeded|failed|cancelled, ``completed_at`` set exactly when the status is final,
  ``error_code`` set exactly for failures (CHECKs);
* metadata: ``provider`` and ``model`` (no secrets), redacted ``task``, ``result``, fixed-text ``error_message``.

GUARDS. Triggers (SQLite: ``BEFORE UPDATE`` / ``BEFORE DELETE``; PostgreSQL: one plpgsql function and a
``BEFORE UPDATE OR DELETE`` row trigger) make a row append-only once it has finished and fix its identity and pin from
the moment it is created: only the running -> final transition may change ``status``, ``result``, ``error_*``,
``provider``, ``model`` and ``completed_at``. Rows can never be deleted. Foreign keys are NO ACTION (no cascades).

``downgrade()`` removes exactly these objects (the data in them is discarded). Production policy is forward-only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0006_agent_executions"
down_revision: Union[str, None] = "0005_custom_agents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FUNCTION = "agent_executions_guard"
MESSAGE = "agent_executions rows are append-only after completion and their identity and version pin are fixed"
INDEX_HISTORY = "ix_agent_executions_workspace_id_user_id_created_at_id"
INDEX_VERSION = "ix_agent_executions_agent_version_id"

# Columns whose value is fixed from the moment the row is created.
FIXED = ("id", "user_id", "workspace_id", "agent_source", "agent_key", "agent_name", "task", "created_at", "started_at")
FIXED_NULLABLE = ("agent_version_id", "agent_version")


def _dialect() -> str:
    return op.get_context().dialect.name


def upgrade() -> None:
    op.create_table(
        "agent_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_source", sa.String(length=16), nullable=False),
        sa.Column("agent_key", sa.String(length=64), nullable=False),
        sa.Column("agent_name", sa.String(length=80), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=True),
        sa.Column("agent_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("error_message", sa.String(length=300), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="agent_executions_pkey"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_agent_executions_user_id_users"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_agent_executions_workspace_id_workspaces"),
        sa.ForeignKeyConstraint(
            ["agent_version_id"], ["custom_agent_versions.id"], name="fk_agent_executions_agent_version_id_custom_agent_versions"
        ),
        sa.CheckConstraint("agent_source IN ('builtin', 'custom')", name="ck_agent_executions_source"),
        sa.CheckConstraint("status IN ('running', 'succeeded', 'failed', 'cancelled')", name="ck_agent_executions_status"),
        sa.CheckConstraint(
            "(agent_source = 'custom' AND agent_version_id IS NOT NULL AND agent_version IS NOT NULL AND agent_version >= 1) "
            "OR (agent_source = 'builtin' AND agent_version_id IS NULL AND agent_version IS NULL)",
            name="ck_agent_executions_version_pin",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND completed_at IS NULL) OR (status <> 'running' AND completed_at IS NOT NULL)",
            name="ck_agent_executions_completion",
        ),
        sa.CheckConstraint(
            "(status <> 'succeeded' OR error_code IS NULL) AND (status <> 'failed' OR error_code IS NOT NULL)",
            name="ck_agent_executions_outcome",
        ),
    )
    op.create_index(
        INDEX_HISTORY, "agent_executions",
        ["workspace_id", "user_id", sa.text("created_at DESC"), sa.text("id DESC")], unique=False,
    )
    op.create_index(INDEX_VERSION, "agent_executions", ["agent_version_id"], unique=False)

    if _dialect() == "sqlite":
        changed = " OR ".join(
            [f"NEW.{c} <> OLD.{c}" for c in FIXED] + [f"NEW.{c} IS NOT OLD.{c}" for c in FIXED_NULLABLE]
        )
        op.execute(
            "CREATE TRIGGER agent_executions_no_update BEFORE UPDATE ON agent_executions "
            f"WHEN OLD.status <> 'running' OR {changed} BEGIN SELECT RAISE(ABORT, '{MESSAGE}'); END"
        )
        op.execute(f"CREATE TRIGGER agent_executions_no_delete BEFORE DELETE ON agent_executions BEGIN SELECT RAISE(ABORT, '{MESSAGE}'); END")
    else:
        changed = " OR ".join(
            [f"NEW.{c} <> OLD.{c}" for c in FIXED] + [f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in FIXED_NULLABLE]
        )
        op.execute(
            f"CREATE FUNCTION {FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"IF TG_OP = 'DELETE' THEN RAISE EXCEPTION '{MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; "
            f"IF OLD.status <> 'running' OR {changed} THEN "
            f"RAISE EXCEPTION '{MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END IF; "
            f"RETURN NEW; END; $$"
        )
        op.execute(f"CREATE TRIGGER {FUNCTION} BEFORE UPDATE OR DELETE ON agent_executions FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()")


def downgrade() -> None:
    if _dialect() == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS agent_executions_no_update")
        op.execute("DROP TRIGGER IF EXISTS agent_executions_no_delete")
    else:
        op.execute(f"DROP TRIGGER IF EXISTS {FUNCTION} ON agent_executions")
    op.drop_index(INDEX_VERSION, table_name="agent_executions")
    op.drop_index(INDEX_HISTORY, table_name="agent_executions")
    op.drop_table("agent_executions")
    if _dialect() != "sqlite":
        op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}()")
