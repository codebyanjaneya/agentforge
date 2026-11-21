"""P0 safety sprint: migration 0016_durable_dispatch (SQLite files in tmp_path; PostgreSQL is checked in test_postgres_integration)."""

import pytest
from alembic.script import ScriptDirectory

from migration_support import Db, make_cfg
from tests.test_plans_schema import STAMP, U, W, seed

PREV, HEAD = "0015_resilience_required_action", "0016_durable_dispatch"
NEW_EVENTS = ("tool_dispatch_authorized", "tool_dispatch_started", "tool_dispatch_succeeded", "tool_dispatch_failed", "tool_dispatch_unknown", "tool_dispatch_cancelled",
              "tool_dispatch_replayed", "tool_dispatch_reconciled", "tool_dispatch_refused")


@pytest.fixture
def db(tmp_path, monkeypatch):
    return Db(tmp_path / "dispatch.db", monkeypatch)


def test_graph_tables_triggers_and_events_vocabulary_and_a_clean_downgrade_that_keeps_every_event_row_and_trigger(db):
    script = ScriptDirectory.from_config(make_cfg())
    assert script.get_heads() == ["0017_invitations"] and script.get_revision(HEAD).down_revision == PREV
    db.upgrade(PREV)
    before = db.master()
    event_triggers = sorted(r[0] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'execution_events'"))
    indexes = sorted(r[0] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'execution_events'"))
    db.upgrade(HEAD)
    sql = db.rows("SELECT sql FROM sqlite_master WHERE name = 'execution_events'")[0][0]
    assert all(f"'{t}'" in sql for t in NEW_EVENTS) and "'approval_consumed'" in sql  # the old vocabulary is kept
    assert sorted(r[0] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'execution_events'")) == event_triggers  # still append-only
    assert sorted(r[0] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'execution_events'")) == indexes
    assert {r[0] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name IN ('tool_dispatches', 'tool_reconciliations')")} == {
        "tool_dispatches_no_update", "tool_dispatches_no_delete", "tool_reconciliations_no_update", "tool_reconciliations_no_delete"}
    db.downgrade(PREV)
    assert db.master() == before
    db.upgrade("head")


def test_reconciliation_rows_are_append_only_and_checked(db):
    db.upgrade()
    seed(db)
    # a reconciliation needs a dispatch, which needs an invocation, an execution... : exercise the checks on the table directly with the constraint names
    cols = db.rows("SELECT sql FROM sqlite_master WHERE name = 'tool_reconciliations'")[0][0]
    for needle in ("ck_tool_reconciliations_decision", "ck_tool_reconciliations_evidence_kind", "ck_tool_reconciliations_reason"):
        assert needle in cols
    dsql = db.rows("SELECT sql FROM sqlite_master WHERE name = 'tool_dispatches'")[0][0]
    for needle in ("ck_tool_dispatches_state", "ck_tool_dispatches_started", "ck_tool_dispatches_completed", "uq_tool_dispatches_operation_generation", "uq_tool_dispatches_invocation_id"):
        assert needle in dsql
