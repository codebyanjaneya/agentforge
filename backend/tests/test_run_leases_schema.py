"""M4-A1: migration 0009_run_leases (SQLite files in tmp_path; PostgreSQL is checked in test_postgres_integration)."""

import re
import sys

import pytest
from alembic.script import ScriptDirectory

from migration_support import BACKEND, Db, make_cfg
from tests.test_plan_runs_schema import refused, run_sql, trun_sql
from tests.test_plans_schema import STAMP, accepted, plan_sql, revision_sql, seed, task_sql

VERSION_FILE = BACKEND / "migrations" / "versions" / "0009_run_leases.py"
RUNS, LEASES = "0008_plan_runs", "0009_run_leases"
TABLES = ("plans", "plan_revisions", "plan_tasks", "plan_runs", "plan_task_runs", "agent_executions")


@pytest.fixture
def db(tmp_path, monkeypatch):
    return Db(tmp_path / "leases.db", monkeypatch)


def snapshot(db):
    return {t: db.rows(f"SELECT * FROM {t} ORDER BY 1") for t in TABLES}


def test_revision_graph_and_file_conventions():
    script = ScriptDirectory.from_config(make_cfg())
    assert script.get_heads() == ["0017_invitations"]  # M4-A2 sits on top of 0009
    rev = script.get_revision(LEASES)
    assert rev.down_revision == RUNS and rev.branch_labels == set() and rev.dependencies is None and len(LEASES) <= 32
    text = VERSION_FILE.read_text(encoding="utf-8")
    assert 'revision: str = "0009_run_leases"' in text
    assert not re.search(r"ALTER TABLE|op\.(add_column|drop_column|alter_column|batch_alter_table)|CREATE TRIGGER|DROP TRIGGER", text)  # one new table; no trigger is touched


def test_upgrade_adds_only_the_lease_table_keeps_rows_and_leaves_the_accepted_triggers_alone(db, import_fresh):
    import_fresh("memory.models")
    models = sys.modules["memory.models"]
    db.upgrade(RUNS)
    seed(db)
    sql, pid = plan_sql(status="approved", approved_revision=1, approved_at="2026-01-01 00:00:00.000000")
    rev, rid = revision_sql(pid)
    run, run_id = run_sql(pid, rid, 1, "running")
    accepted(db, sql, rev, task_sql(rid, "t1", 0), run, trun_sql(run_id, rid, "t1", 0)[0])
    before_master, before_rows = db.master(), snapshot(db)
    db.upgrade(LEASES)
    assert snapshot(db) == before_rows  # a running run from before the migration is untouched (it simply has no lease yet)
    old = {(t, n): s for t, n, _tbl, s in before_master}
    after = db.master()
    for t, n, _tbl, s in after:
        if (t, n) in old:
            assert s == old[(t, n)], f"{t} {n} was altered"  # every earlier object, including the plan_runs triggers, is byte for byte unchanged
    assert {n for t, n, _tbl, _s in after if (t, n) not in old and not n.startswith("sqlite_autoindex")} == {"run_leases", "ix_run_leases_expires_at"}
    assert db.rows("SELECT count(*) FROM run_leases") == [(0,)]
    assert db.diffs(models.Base.metadata, exclude_tables=("execution_events", "plan_run_retries", "plan_task_carryovers", "worker_processes", "execution_owners", "tool_invocations", "tool_approvals", "tool_suspensions", "model_usage", "notifications", "provider_events", "tool_dispatches", "tool_reconciliations", "workspace_invitations",)) == []  # the migrated schema equals the live models (0010's tables are not created yet)


def test_downgrade_drops_only_the_lease_table_and_reupgrade_is_identical(db, import_fresh):
    import_fresh("memory.models")
    db.upgrade(RUNS)
    seed(db)
    before_master, before_rows = db.master(), snapshot(db)
    db.upgrade(LEASES)
    head_master = db.master()
    db.downgrade(RUNS)
    assert db.master() == before_master and snapshot(db) == before_rows  # runs and everything else are kept byte for byte
    assert db.rows("SELECT version_num FROM alembic_version") == [(RUNS,)]
    db.upgrade(LEASES)
    assert db.master() == head_master


def test_lease_constraints_and_the_run_triggers_still_hold(db):
    db.upgrade(LEASES)
    seed(db)
    sql, pid = plan_sql(status="approved", approved_revision=1, approved_at="2026-01-01 00:00:00.000000")
    rev, rid = revision_sql(pid)
    run, run_id = run_sql(pid, rid, 1, "running")
    accepted(db, sql, rev, task_sql(rid, "t1", 0), run)
    good = f"INSERT INTO run_leases (run_id, owner_id, generation, expires_at, created_at, updated_at) VALUES ('{run_id}', 'o', 1, {STAMP}, {STAMP}, {STAMP})"
    refused(db, good.replace("'o', 1", "'o', 0"), expect="CHECK")  # generation is >= 1
    refused(db, good.replace(run_id, "0" * 32), expect="FOREIGN KEY")  # only a real run can have a lease
    db.run(good)
    refused(db, good)  # one lease per run (primary key)
    refused(db, f"DELETE FROM plan_runs WHERE id = '{run_id}'")  # the lease references the run: the run cannot disappear
    db.run(f"UPDATE run_leases SET owner_id = 'p', generation = 2, expires_at = {STAMP} WHERE run_id = '{run_id}'")  # a lease IS mutable
    assert db.rows("SELECT owner_id, generation FROM run_leases") == [("p", 2)]
    refused(db, f"UPDATE plan_runs SET status = 'running' WHERE id = '{run_id}'")  # the accepted 0008 trigger on running runs is intact: why the lease is a separate table
