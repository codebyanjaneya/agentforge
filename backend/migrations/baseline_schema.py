"""Frozen description of the ``0001_baseline`` schema, for the adoption pre-flight.

An UNVERSIONED legacy database (no ``alembic_version``) is adopted by verifying that it matches the
baseline and then ``alembic stamp 0001_baseline``; only afterwards is it upgraded to the head. The
live ``memory.models`` metadata grows with every migration, so the pre-flight must compare such a
database with THIS fixed description, never with the current models.

This module is a hand-written copy of what ``versions/0001_baseline.py`` creates. It must NEVER be
edited when the models change. Three independent checks keep it honest (tests/test_models_metadata.py,
tests/test_baseline_revision.py, tests/test_preflight_baseline.py): its compiled PostgreSQL DDL equals
the T1-A plan DDL, it equals the schema that the baseline migration really creates (``compare_metadata``
is empty and the offline SQL matches), and the pre-flight output names the baseline it compared with.

Imports only SQLAlchemy: no application settings, engine, models or test code.
"""

from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, String, Table, Text

BASELINE_REVISION = "0001_baseline"
BASELINE_TABLES = frozenset({"agent_memories", "telegram_links"})


def baseline_metadata() -> MetaData:
    """A fresh MetaData holding exactly the two tables created by 0001_baseline."""
    md = MetaData()
    Table(
        "agent_memories",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("agent_name", String(100), nullable=False, index=True),
        Column("user_email", String(255), nullable=True, index=True),
        Column("summary", Text, nullable=False),
        Column("created_at", DateTime, nullable=False),
    )
    Table(
        "telegram_links",
        md,
        Column("chat_id", BigInteger, primary_key=True),
        Column("email", String(255), nullable=True),
        Column("active_agent", String(100), nullable=False),
        Column("updated_at", DateTime, nullable=False),
    )
    return md
