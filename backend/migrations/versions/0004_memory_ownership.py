"""memory ownership references on agent_memories (M1-C)

Revision ID: 0004_memory_ownership
Revises: 0003_workspaces
Create Date: 2026-11-14

Adds ownership REFERENCES to ``agent_memories`` and the history index; nothing else changes:

* ``user_id``      UUID NULL, FK ``fk_agent_memories_user_id_users``           -> users.id
* ``workspace_id`` UUID NULL, FK ``fk_agent_memories_workspace_id_workspaces`` -> workspaces.id
* index ``ix_agent_memories_user_id_created_at_id`` on ``(user_id, created_at DESC, id DESC)``
  (stable newest-first history for one user; ``id DESC`` is the tie-breaker).

LEGACY DATA RULES. Both new columns are nullable and are NEVER backfilled: no value is derived from
``user_email``, content, timestamps or anything else, so every existing row survives byte for byte with
NULL ownership. Legacy rows are quarantined by construction: later ownership-scoped queries filter on
``user_id`` and therefore never match NULL-owned rows. Voice-call memory is not migrated into user memory.

DIALECTS. PostgreSQL uses ordinary ``ALTER TABLE`` operations. SQLite cannot add a named foreign key
through Alembic's ``add_constraint`` and cannot drop a column that carries one, so there: the upgrade adds
each column with an inline named ``REFERENCES`` clause (raw ``ADD COLUMN``; existing rows untouched) and the
downgrade rebuilds ``agent_memories`` from the baseline definition under its own name (so that the stored
DDL is byte-identical to the 0003 state) and copies the rows back. SQLite DDL is not transactional in this
runner, so the rebuild is for development/test only.

``downgrade()`` removes exactly these objects and restores the 0003_workspaces schema, keeping every row
(the ownership values stored in the dropped columns are, of course, discarded). Production policy is
forward-only (restore from a verified backup).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0004_memory_ownership"
down_revision: Union[str, None] = "0003_workspaces"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_agent_memories_user_id_created_at_id"
FK_USER = "fk_agent_memories_user_id_users"
FK_WORKSPACE = "fk_agent_memories_workspace_id_workspaces"


def _is_sqlite() -> bool:
    return op.get_context().dialect.name == "sqlite"


def upgrade() -> None:
    if _is_sqlite():
        uuid_type = sa.Uuid().compile(dialect=op.get_context().dialect)  # CHAR(32) on SQLite
        op.execute(f"ALTER TABLE agent_memories ADD COLUMN user_id {uuid_type} CONSTRAINT {FK_USER} REFERENCES users (id)")
        op.execute(
            f"ALTER TABLE agent_memories ADD COLUMN workspace_id {uuid_type} "
            f"CONSTRAINT {FK_WORKSPACE} REFERENCES workspaces (id)"
        )
    else:
        op.add_column("agent_memories", sa.Column("user_id", sa.Uuid(), nullable=True))
        op.add_column("agent_memories", sa.Column("workspace_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(FK_USER, "agent_memories", "users", ["user_id"], ["id"])
        op.create_foreign_key(FK_WORKSPACE, "agent_memories", "workspaces", ["workspace_id"], ["id"])
    op.create_index(INDEX_NAME, "agent_memories", ["user_id", sa.text("created_at DESC"), sa.text("id DESC")], unique=False)


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="agent_memories")
    if _is_sqlite():
        # Rebuild under the real name from the 0001_baseline definition, then restore the baseline indexes.
        op.execute("ALTER TABLE agent_memories RENAME TO agent_memories_m1c_old")
        op.create_table(
            "agent_memories",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("agent_name", sa.String(length=100), nullable=False),
            sa.Column("user_email", sa.String(length=255), nullable=True),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.execute(
            "INSERT INTO agent_memories (id, agent_name, user_email, summary, created_at) "
            "SELECT id, agent_name, user_email, summary, created_at FROM agent_memories_m1c_old"
        )
        op.drop_table("agent_memories_m1c_old")  # takes its own indexes with it
        op.create_index(op.f("ix_agent_memories_agent_name"), "agent_memories", ["agent_name"], unique=False)
        op.create_index(op.f("ix_agent_memories_user_email"), "agent_memories", ["user_email"], unique=False)
    else:
        op.drop_constraint(FK_WORKSPACE, "agent_memories", type_="foreignkey")
        op.drop_constraint(FK_USER, "agent_memories", type_="foreignkey")
        op.drop_column("agent_memories", "workspace_id")
        op.drop_column("agent_memories", "user_id")
