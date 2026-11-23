"""M6b: migration 0015_resilience_required_action (SQLite files in tmp_path; PostgreSQL is checked in test_postgres_integration)."""

import uuid

import pytest
from alembic.script import ScriptDirectory

from migration_support import Db, make_cfg
from tests.test_plans_schema import STAMP, U, W, seed

PREV, HEAD = "0014_usage_notifications", "0015_resilience_required_action"


@pytest.fixture
def db(tmp_path, monkeypatch):
    return Db(tmp_path / "res.db", monkeypatch)


def event_sql(**o):
    v = {"id": uuid.uuid4().hex, "user_id": U, "workspace_id": W, "provider": "groq", "kind": "waiting", "attempt": 1, "wait_ms": 100, **o}
    vals = ", ".join("NULL" if x is None else (f"'{x}'" if isinstance(x, str) else str(x)) for x in v.values())
    return f"INSERT INTO provider_events ({', '.join(v)}, created_at) VALUES ({vals}, {STAMP})"


def test_graph_column_table_triggers_and_a_clean_downgrade_that_keeps_the_plan_task_triggers(db):
    script = ScriptDirectory.from_config(make_cfg())
    assert script.get_heads() == ["0018_agent_slug_per_user"] and script.get_revision(HEAD).down_revision == PREV
    db.upgrade(PREV)
    before = db.master()
    triggers = sorted(r[0] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'plan_tasks'"))
    db.upgrade(HEAD)
    assert "required_tool" in db.rows("SELECT sql FROM sqlite_master WHERE name = 'plan_tasks'")[0][0]
    db.downgrade(PREV)
    assert db.master() == before
    assert sorted(r[0] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'plan_tasks'")) == triggers  # DROP COLUMN kept them
    db.upgrade("head")


def test_provider_events_are_append_only_and_checked(db):
    db.upgrade()
    seed(db)
    db.run(event_sql())
    for bad in (event_sql(kind="panic"), event_sql(attempt=0), event_sql(wait_ms=-5)):
        with pytest.raises(Exception):
            db.run(bad)
    for statement in ("UPDATE provider_events SET attempt = 2", "DELETE FROM provider_events"):
        with pytest.raises(Exception):
            db.run(statement)
    assert db.rows("SELECT count(*) FROM provider_events") == [(1,)]
