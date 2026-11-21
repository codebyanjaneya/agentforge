"""baseline: agent_memories and telegram_links as defined at T0

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-12

Reproduces memory/models.py exactly (PostgreSQL: SERIAL / BIGSERIAL primary keys, naive
TIMESTAMP columns, unnamed primary keys, no server defaults, no foreign/unique/check
constraints). Defaults such as datetime.utcnow and "ceo" are Python-side only and are
deliberately NOT part of the schema.

Execute this revision only on an EMPTY database. An existing database that already matches
must be adopted with `alembic stamp 0001_baseline` after a clean pre-flight (T1-F/T1-G),
never by running it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_memories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_name", sa.String(length=100), nullable=False),
        sa.Column("user_email", sa.String(length=255), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agent_memories_agent_name"), "agent_memories", ["agent_name"], unique=False)
    op.create_index(op.f("ix_agent_memories_user_email"), "agent_memories", ["user_email"], unique=False)

    op.create_table(
        "telegram_links",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("active_agent", sa.String(length=100), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "The baseline revision 0001_baseline is intentionally irreversible: it may represent the "
        "adoption of a pre-existing database, and downgrading would drop application tables and "
        "data. Restore from a verified backup instead."
    )
