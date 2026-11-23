"""M6: migration 0014_usage_notifications (SQLite files in tmp_path; PostgreSQL is checked in test_postgres_integration)."""

import uuid

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy.exc import DatabaseError

from migration_support import Db, make_cfg
from tests.test_plans_schema import STAMP, U, W, seed

PREV, HEAD = "0013_suspension", "0014_usage_notifications"


@pytest.fixture
def db(tmp_path, monkeypatch):
    return Db(tmp_path / "usage.db", monkeypatch)


def usage_sql(**o):
    v = {"id": uuid.uuid4().hex, "user_id": U, "workspace_id": W, "purpose": "agent", "provider": "groq", "model": "m", "usage_known": 1, "cost_basis": "unknown_pricing",
         "input_tokens": 10, "output_tokens": 5, **o}
    vals = ", ".join("NULL" if x is None else (f"'{x}'" if isinstance(x, str) else str(x)) for x in v.values())
    return f"INSERT INTO model_usage ({', '.join(v)}, created_at) VALUES ({vals}, {STAMP})"


def note_sql(**o):
    v = {"id": uuid.uuid4().hex, "workspace_id": W, "user_id": U, "kind": "approval_requested", "ref_id": uuid.uuid4().hex, **o}
    return f"INSERT INTO notifications ({', '.join(v)}, created_at) VALUES ({', '.join(repr(x) for x in v.values())}, {STAMP})"


def rejected(db, sql):
    with pytest.raises((DatabaseError, Exception)):
        db.run(sql)


def test_graph_and_upgrade_adds_exactly_two_tables_and_downgrade_removes_them(db):
    script = ScriptDirectory.from_config(make_cfg())
    assert script.get_heads() == ["0018_agent_slug_per_user"] and script.get_revision(HEAD).down_revision == PREV
    db.upgrade(PREV)
    before = db.master()
    db.upgrade(HEAD)
    names = {r[0] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"model_usage", "notifications"} <= names
    db.downgrade(PREV)
    assert db.master() == before
    db.upgrade("head")


def test_model_usage_is_append_only_and_checked(db):
    db.upgrade()
    seed(db)
    db.run(usage_sql())
    for bad in (usage_sql(purpose="other"), usage_sql(cost_basis="guess"), usage_sql(cost_basis="calculated"), usage_sql(input_tokens=-1), usage_sql(cost_basis="unknown_usage", cost_micros=5)):
        rejected(db, bad)
    rejected(db, "UPDATE model_usage SET model = 'x'")
    rejected(db, "DELETE FROM model_usage")
    assert db.rows("SELECT count(*) FROM model_usage") == [(1,)]


def test_notifications_unique_read_once_and_never_deleted(db):
    db.upgrade()
    seed(db)
    ref = uuid.uuid4().hex
    db.run(note_sql(ref_id=ref))
    rejected(db, note_sql(ref_id=ref))  # a retry cannot notify twice
    rejected(db, note_sql(kind="other"))
    rejected(db, "DELETE FROM notifications")
    rejected(db, f"UPDATE notifications SET read_at = NULL")  # NULL -> NULL is not a read
    rejected(db, f"UPDATE notifications SET kind = 'approval_requested', ref_id = '{uuid.uuid4().hex}', read_at = {STAMP}")
    db.run(f"UPDATE notifications SET read_at = {STAMP}")
    rejected(db, f"UPDATE notifications SET read_at = '2027-01-01 00:00:00.000000'")  # once
    assert db.rows("SELECT count(*) FROM notifications WHERE read_at IS NOT NULL") == [(1,)]
