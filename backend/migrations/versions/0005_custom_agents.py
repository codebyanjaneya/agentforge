"""custom agents and their immutable versions (M2-A)

Revision ID: 0005_custom_agents
Revises: 0004_memory_ownership
Create Date: 2026-11-15

Adds the agent-platform foundation; no existing table is touched, no existing row is read or changed:

* ``custom_agents``         one row per user-defined agent: owner ``(user_id, workspace_id)`` (both NOT NULL, FKs),
                            stable per-workspace ``slug`` (unique), ``status`` ('active'|'archived'), the
                            ``latest_version`` / ``current_version`` counters and the optional template provenance.
* ``custom_agent_versions`` one row per version of the definition (JSON), unique per ``(agent_id, version)``.

IMMUTABILITY. Version rows are insert-only: triggers reject every UPDATE and DELETE (SQLite: ``BEFORE UPDATE`` /
``BEFORE DELETE`` triggers; PostgreSQL: one plpgsql function and a ``BEFORE UPDATE OR DELETE`` trigger). Rolling back an
agent inserts a NEW version copied from an old one, so history is never altered. Foreign keys are NO ACTION
(nothing cascades). Built-in templates are code, not rows: they are not part of this schema.

``downgrade()`` removes exactly these objects (the data in them is discarded). Production policy is forward-only
(restore from a verified backup).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0005_custom_agents"
down_revision: Union[str, None] = "0004_memory_ownership"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FUNCTION = "custom_agent_versions_immutable"
MESSAGE = "custom_agent_versions rows are immutable"


def _dialect() -> str:
    return op.get_context().dialect.name


def upgrade() -> None:
    op.create_table(
        "custom_agents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.String(length=64), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="custom_agents_pkey"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_custom_agents_user_id_users"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_custom_agents_workspace_id_workspaces"),
        sa.UniqueConstraint("workspace_id", "slug", name="uq_custom_agents_workspace_id_slug"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_custom_agents_status"),
        sa.CheckConstraint("latest_version >= 1", name="ck_custom_agents_latest_version_positive"),
        sa.CheckConstraint("current_version >= 1 AND current_version <= latest_version", name="ck_custom_agents_current_version_range"),
        sa.CheckConstraint(
            "(template_id IS NULL AND template_version IS NULL) OR (template_id IS NOT NULL AND template_version IS NOT NULL)",
            name="ck_custom_agents_template_pair",
        ),
    )
    op.create_index("ix_custom_agents_workspace_id_user_id", "custom_agents", ["workspace_id", "user_id"], unique=False)

    op.create_table(
        "custom_agent_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("change_note", sa.String(length=200), nullable=True),
        sa.Column("restored_from_version", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="custom_agent_versions_pkey"),
        sa.ForeignKeyConstraint(["agent_id"], ["custom_agents.id"], name="fk_custom_agent_versions_agent_id_custom_agents"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_custom_agent_versions_created_by_users"),
        sa.UniqueConstraint("agent_id", "version", name="uq_custom_agent_versions_agent_id_version"),
        sa.CheckConstraint("version >= 1", name="ck_custom_agent_versions_version_positive"),
        sa.CheckConstraint(
            "restored_from_version IS NULL OR (restored_from_version >= 1 AND restored_from_version < version)",
            name="ck_custom_agent_versions_restored_from",
        ),
    )

    if _dialect() == "sqlite":
        for event in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER custom_agent_versions_no_{event.lower()} BEFORE {event} ON custom_agent_versions "
                f"BEGIN SELECT RAISE(ABORT, '{MESSAGE}'); END"
            )
    else:
        op.execute(
            f"CREATE FUNCTION {FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            f"BEGIN RAISE EXCEPTION '{MESSAGE}' USING ERRCODE = 'integrity_constraint_violation'; END; $$"
        )
        op.execute(
            f"CREATE TRIGGER {FUNCTION} BEFORE UPDATE OR DELETE ON custom_agent_versions "
            f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()"
        )


def downgrade() -> None:
    if _dialect() == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS custom_agent_versions_no_update")
        op.execute("DROP TRIGGER IF EXISTS custom_agent_versions_no_delete")
    else:
        op.execute(f"DROP TRIGGER IF EXISTS {FUNCTION} ON custom_agent_versions")
    op.drop_table("custom_agent_versions")
    if _dialect() != "sqlite":
        op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}()")
    op.drop_index("ix_custom_agents_workspace_id_user_id", table_name="custom_agents")
    op.drop_table("custom_agents")
