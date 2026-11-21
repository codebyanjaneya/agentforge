"""M4-C finalization: migration 0013_suspension (SQLite files in tmp_path; PostgreSQL is checked in test_postgres_integration)."""

import pytest
from alembic.script import ScriptDirectory

from migration_support import BACKEND, Db, make_cfg
from tests.test_plans_schema import seed

APPROVALS, SUSPENSION = "0012_approvals", "0013_suspension"
NEW_TYPES = ("approval_requested", "approval_approved", "approval_rejected", "approval_expired", "approval_invalidated", "approval_consumed", "task_suspended", "task_resumed")


@pytest.fixture
def db(tmp_path, monkeypatch):
    return Db(tmp_path / "suspension.db", monkeypatch)


def test_the_revision_graph_and_file_conventions():
    script = ScriptDirectory.from_config(make_cfg())
    assert script.get_heads() == ["0017_invitations"] and script.get_revision(SUSPENSION).down_revision == APPROVALS
    text = (BACKEND / "migrations" / "versions" / "0013_suspension.py").read_text(encoding="utf-8")
    assert "tool_suspensions" in text and "downgrade" in text


def test_upgrade_keeps_every_event_row_trigger_and_index_and_adds_the_new_vocabulary_and_table(db):
    db.upgrade(APPROVALS)
    seed(db)
    assert "approval_requested" not in db.master()
    before_indexes = sorted(r[0] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'execution_events'"))
    before_triggers = sorted(r[0] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'execution_events'"))
    assert before_triggers == ["execution_events_guard_no_delete", "execution_events_guard_no_update"]
    db.upgrade(SUSPENSION)
    assert sorted(r[0] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'execution_events'")) == before_indexes
    assert sorted(r[0] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'execution_events'")) == before_triggers  # still append-only
    sql = db.rows("SELECT sql FROM sqlite_master WHERE name = 'execution_events'")[0][0]
    for t in NEW_TYPES:
        assert f"'{t}'" in sql
    assert [r[0] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tool_suspensions'")] == ["tool_suspensions"]


def test_downgrade_restores_the_old_vocabulary_and_a_reupgrade_is_clean(db):
    db.upgrade(SUSPENSION)
    db.downgrade(APPROVALS)
    assert "approval_requested" not in db.rows("SELECT sql FROM sqlite_master WHERE name = 'execution_events'")[0][0]
    assert db.rows("SELECT name FROM sqlite_master WHERE name = 'tool_suspensions'") == []
    assert sorted(r[0] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'execution_events'")) == ["execution_events_guard_no_delete", "execution_events_guard_no_update"]
    db.upgrade("head")
    assert "approval_requested" in db.rows("SELECT sql FROM sqlite_master WHERE name = 'execution_events'")[0][0]
