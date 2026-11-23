"""M4-A2: migration 0010_orchestration (SQLite files in tmp_path; PostgreSQL is checked in test_postgres_integration)."""

import re
import sys

import pytest
from alembic.script import ScriptDirectory

from migration_support import BACKEND, Db, make_cfg
from tests.test_plan_runs_schema import refused, run_sql, trun_sql
from tests.test_plans_schema import STAMP, accepted, plan_sql, revision_sql, seed, task_sql

VERSION_FILE = BACKEND / "migrations" / "versions" / "0010_orchestration.py"
LEASES, ORCH = "0009_run_leases", "0010_orchestration"
NEW_TABLES = {"execution_events", "plan_run_retries", "plan_task_carryovers", "worker_processes", "execution_owners"}
OLD_TABLES = ("plans", "plan_revisions", "plan_tasks", "plan_runs", "plan_task_runs", "agent_executions", "run_leases")


@pytest.fixture
def db(tmp_path, monkeypatch):
    return Db(tmp_path / "orchestration.db", monkeypatch)


def snapshot(db):
    return {t: db.rows(f"SELECT * FROM {t} ORDER BY 1") for t in OLD_TABLES}


def seeded(db):
    seed(db)
    sql, pid = plan_sql(status="approved", approved_revision=1, approved_at="2026-01-01 00:00:00.000000")
    rev, rid = revision_sql(pid)
    run, run_id = run_sql(pid, rid, 1, "running")
    task_run, task_run_id = trun_sql(run_id, rid, "t1", 0)
    accepted(db, sql, rev, task_sql(rid, "t1", 0), run, task_run)
    return run_id, task_run_id


def test_revision_graph_and_file_conventions():
    script = ScriptDirectory.from_config(make_cfg())
    assert script.get_heads() == ["0018_agent_slug_per_user"]  # M4-B (0011) and M4-C (0012) sit on top of 0010
    rev = script.get_revision(ORCH)
    assert rev.down_revision == LEASES and rev.branch_labels == set() and rev.dependencies is None and len(ORCH) <= 32
    text = VERSION_FILE.read_text(encoding="utf-8")
    assert f'revision: str = "{ORCH}"' in text
    assert not re.search(r"ALTER TABLE|op\.(add_column|drop_column|alter_column|batch_alter_table)", text)  # five new tables; nothing existing is altered


def test_upgrade_adds_exactly_the_new_objects_keeps_rows_and_leaves_every_accepted_object_untouched(db, import_fresh):
    import_fresh("memory.models")
    models = sys.modules["memory.models"]
    db.upgrade(LEASES)
    seeded(db)
    before_master, before_rows = db.master(), snapshot(db)
    db.upgrade(ORCH)
    assert snapshot(db) == before_rows  # existing runs, tasks and executions are byte for byte unchanged
    old = {(t, n): s for t, n, _tbl, s in before_master}
    after = db.master()
    for t, n, _tbl, s in after:
        if (t, n) in old:
            assert s == old[(t, n)], f"{t} {n} was altered"  # including the 0006, 0008 and 0009 triggers
    added = {n for t, n, _tbl, _s in after if (t, n) not in old and not n.startswith("sqlite_autoindex")}
    assert added == NEW_TABLES | {"ix_execution_events_run_id_id", "ix_execution_events_workspace_id_id", "ix_plan_task_carryovers_source_task_run_id", "ix_worker_processes_expires_at",
                                  "ix_execution_owners_owner_id", "execution_events_guard_no_update", "execution_events_guard_no_delete", "plan_run_retries_guard_no_update",
                                  "plan_run_retries_guard_no_delete", "plan_task_carryovers_guard_no_update", "plan_task_carryovers_guard_no_delete",
                                  "execution_owners_guard_no_update", "execution_owners_guard_no_delete"}
    assert {db.rows(f"SELECT count(*) FROM {t}")[0][0] for t in NEW_TABLES} == {0}
    assert db.diffs(models.Base.metadata, exclude_tables=("tool_invocations", "tool_approvals", "tool_suspensions", "model_usage", "notifications", "provider_events", "tool_dispatches", "tool_reconciliations", "workspace_invitations")) == []  # the migrated schema equals the live models (0011's table is not created yet)


def test_downgrade_drops_only_the_new_objects_and_reupgrade_is_identical(db, import_fresh):
    import_fresh("memory.models")
    db.upgrade(LEASES)
    seeded(db)
    before_master, before_rows = db.master(), snapshot(db)
    db.upgrade(ORCH)
    head_master = db.master()
    db.downgrade(LEASES)
    assert db.master() == before_master and snapshot(db) == before_rows  # tables, indexes and triggers of 0010 gone, everything else byte for byte
    assert db.rows("SELECT version_num FROM alembic_version") == [(LEASES,)]
    db.upgrade(ORCH)
    assert db.master() == head_master


def test_constraints_and_the_append_only_triggers(db):
    db.upgrade(ORCH)
    run_id, task_run_id = seeded(db)
    event = (f"INSERT INTO execution_events (run_id, user_id, workspace_id, event_type, actor_type, created_at) "
             f"SELECT id, user_id, workspace_id, '{{type}}', '{{actor}}', {STAMP} FROM plan_runs WHERE id = '{run_id}'")
    refused(db, event.format(type="free_text", actor="user"), expect="CHECK")  # a closed vocabulary of event types
    refused(db, event.format(type="run_created", actor="admin"), expect="CHECK")  # and of actors
    db.run(event.format(type="run_created", actor="user"), event.format(type="run_completed", actor="api"))
    assert db.rows("SELECT id, event_type FROM execution_events ORDER BY id") == [(1, "run_created"), (2, "run_completed")]  # ids grow: the total order
    for table, column in (("execution_events", "task_key"), ("plan_task_carryovers", "task_key"), ("plan_run_retries", "carried_count"), ("execution_owners", "owner_id")):
        if table == "plan_task_carryovers":
            db.run(f"INSERT INTO plan_task_carryovers (run_id, task_key, source_task_run_id, created_at) VALUES ('{run_id}', 't1', '{task_run_id}', {STAMP})")
        if table == "plan_run_retries":
            continue  # needs a second run; covered with real data in the retry tests
        if table == "execution_owners":
            continue  # needs an execution row; covered in the limits tests
        for statement in (f"UPDATE {table} SET {column} = {column}", f"DELETE FROM {table}"):
            refused(db, statement, expect="append-only")
    refused(db, f"INSERT INTO worker_processes (owner_id, kind, started_at, heartbeat_at, expires_at) VALUES ('p', 'daemon', {STAMP}, {STAMP}, {STAMP})", expect="CHECK")
    db.run(f"INSERT INTO worker_processes (owner_id, kind, started_at, heartbeat_at, expires_at) VALUES ('p', 'api', {STAMP}, {STAMP}, {STAMP})")
    db.run("UPDATE worker_processes SET expires_at = started_at")  # the heartbeat row is mutable
    refused(db, f"INSERT INTO plan_task_carryovers (run_id, task_key, source_task_run_id, created_at) VALUES ('{run_id}', 'x', '{'0' * 32}', {STAMP})", expect="FOREIGN KEY")
    refused(db, f"INSERT INTO execution_events (run_id, user_id, workspace_id, event_type, actor_type, created_at) VALUES ('{'0' * 32}', '{'0' * 32}', '{'0' * 32}', 'run_created', 'user', {STAMP})",
            expect="FOREIGN KEY")  # an event belongs to a real run
