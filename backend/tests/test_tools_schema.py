"""M4-B: migration 0011_tools (SQLite files in tmp_path; PostgreSQL is checked in test_postgres_integration)."""

import re
import sys

import pytest
from alembic.script import ScriptDirectory

from migration_support import BACKEND, Db, make_cfg
from tests.test_agent_executions_schema import execution_sql
from tests.test_plan_runs_schema import refused
from tests.test_plans_schema import STAMP, U, W, seed

VERSION_FILE = BACKEND / "migrations" / "versions" / "0011_tools.py"
ORCH, TOOLS = "0010_orchestration", "0011_tools"
OLD_TABLES = ("agent_executions", "plans", "plan_runs", "plan_task_runs", "run_leases", "execution_events", "worker_processes")


@pytest.fixture
def db(tmp_path, monkeypatch):
    return Db(tmp_path / "tools.db", monkeypatch)


def snapshot(db):
    return {t: db.rows(f"SELECT * FROM {t} ORDER BY 1") for t in OLD_TABLES}


def invocation(execution_id, seq=1, status="running", **o):
    v = {"id": f"{seq:032x}", "user_id": U, "workspace_id": W, "execution_id": execution_id, "agent_key": "research", "tool_id": "math.calculate", "tool_version": 1, "seq": seq,
         "idempotency_key": f"{seq:064x}", "args_hash": "a" * 64, "side_effect": "none", "status": status, "input": "{}", "error_code": None, **o}
    done = "NULL" if status == "running" else STAMP
    vals = ", ".join("NULL" if x is None else (f"'{x}'" if isinstance(x, str) else str(x)) for x in v.values())
    return f"INSERT INTO tool_invocations ({', '.join(v)}, created_at, started_at, completed_at) VALUES ({vals}, {STAMP}, {STAMP}, {done})"


def test_revision_graph_and_file_conventions():
    script = ScriptDirectory.from_config(make_cfg())
    assert script.get_heads() == ["0017_invitations"]  # M4-C sits on top of 0011
    assert script.get_revision("0012_approvals").down_revision == TOOLS
    rev = script.get_revision(TOOLS)
    assert rev.down_revision == ORCH and rev.branch_labels == set() and rev.dependencies is None and len(TOOLS) <= 32
    text = VERSION_FILE.read_text(encoding="utf-8")
    assert f'revision: str = "{TOOLS}"' in text and not re.search(r"ALTER TABLE|op\.(add_column|drop_column|alter_column|batch_alter_table)", text)


def test_upgrade_adds_exactly_one_table_keeps_every_row_and_every_accepted_object(db, import_fresh):
    import_fresh("memory.models")
    models = sys.modules["memory.models"]
    db.upgrade(ORCH)
    seed(db)
    sql, eid = execution_sql(user_id=U, workspace_id=W, status="running")
    db.run(sql)
    before_master, before_rows = db.master(), snapshot(db)
    db.upgrade(TOOLS)
    assert snapshot(db) == before_rows
    old = {(t, n): s for t, n, _tbl, s in before_master}
    after = db.master()
    for t, n, _tbl, s in after:
        if (t, n) in old:
            assert s == old[(t, n)], f"{t} {n} was altered"
    assert {n for t, n, _tbl, _s in after if (t, n) not in old and not n.startswith("sqlite_autoindex")} == {
        "tool_invocations", "ix_tool_invocations_workspace_user_created", "ix_tool_invocations_run_id", "ix_tool_invocations_tool", "tool_invocations_no_update", "tool_invocations_no_delete"}
    assert db.diffs(models.Base.metadata, exclude_tables=("tool_approvals", "tool_suspensions", "model_usage", "notifications", "provider_events", "tool_dispatches", "tool_reconciliations", "workspace_invitations",)) == []  # the migrated schema equals the live models


def test_downgrade_drops_only_the_new_objects_and_reupgrade_is_identical(db, import_fresh):
    import_fresh("memory.models")
    db.upgrade(ORCH)
    seed(db)
    before_master, before_rows = db.master(), snapshot(db)
    db.upgrade(TOOLS)
    head_master = db.master()
    db.downgrade(ORCH)
    assert db.master() == before_master and snapshot(db) == before_rows and db.rows("SELECT version_num FROM alembic_version") == [(ORCH,)]
    db.upgrade(TOOLS)
    assert db.master() == head_master


def test_constraints_and_triggers(db):
    db.upgrade(TOOLS)
    seed(db)
    sql, eid = execution_sql(user_id=U, workspace_id=W, status="running")
    db.run(sql)
    db.run(invocation(eid, 1))
    db.run(invocation(eid, 2, status="succeeded"), invocation(eid, 3, status="rejected", error_code="not_permitted"))
    refused(db, invocation(eid, 1).replace(f"'{1:032x}'", f"'{9:032x}'").replace(f"'{1:064x}'", f"'{9:064x}'"), expect="UNIQUE")  # (execution, seq) is unique: no double dispatch
    refused(db, invocation(eid, 7).replace(f"'{7:064x}'", f"'{1:064x}'"), expect="UNIQUE")  # so is the idempotency key
    refused(db, invocation(eid, 8, status="weird"), expect="CHECK")
    refused(db, invocation(eid, 8, status="failed", error_code=None), expect="CHECK")  # a failure has a code
    refused(db, invocation(eid, 8, status="succeeded", error_code="tool_error"), expect="CHECK")
    refused(db, invocation(eid, 8, side_effect="sometimes"), expect="CHECK")
    refused(db, invocation(eid, 8).replace("1, 8,", "0, 8,", 1), expect="CHECK")  # the version of a tool is >= 1
    refused(db, invocation("0" * 32, 8), expect="FOREIGN KEY")  # an invocation belongs to a real execution
    refused(db, f"UPDATE tool_invocations SET status = 'running', completed_at = NULL WHERE seq = 2", expect="never deleted")  # a finished row is frozen
    refused(db, "UPDATE tool_invocations SET tool_id = 'other.tool' WHERE seq = 1", expect="never deleted")  # identity is fixed
    refused(db, "UPDATE tool_invocations SET input = '{\"x\":1}' WHERE seq = 1", expect="never deleted")  # and so is the input
    refused(db, "DELETE FROM tool_invocations", expect="never deleted")
    db.run(f"UPDATE tool_invocations SET status = 'succeeded', completed_at = {STAMP}, output = '{{}}' WHERE seq = 1")  # running -> final is the one legal transition
    refused(db, f"UPDATE tool_invocations SET status = 'failed', error_code = 'tool_error' WHERE seq = 1", expect="never deleted")
