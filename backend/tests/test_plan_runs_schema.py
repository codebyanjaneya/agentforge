"""M3-B: migration 0008_plan_runs (SQLite files in tmp_path; PostgreSQL is checked in test_postgres_integration)."""

import re
import sys
import uuid

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

from migration_support import BACKEND, Db, make_cfg
from tests.test_agent_executions_schema import execution_sql
from tests.test_plans_schema import STAMP, U, W, accepted, dep_sql, plan_sql, revision_sql, seed, seed_agent, task_sql

VERSION_FILE = BACKEND / "migrations" / "versions" / "0008_plan_runs.py"
PLANS, RUNS = "0007_plans", "0008_plan_runs"
RUN_TABLES = ("plan_runs", "plan_task_runs")
GUARD = "never deleted"


@pytest.fixture
def db(tmp_path, monkeypatch):
    return Db(tmp_path / "runs.db", monkeypatch)


def fresh_models(import_fresh):
    import_fresh("memory.models")
    return sys.modules["memory.models"]


def refused(db, *statements, expect=None):
    engine = db.engine(fk=True)
    try:
        with pytest.raises(Exception) as info:
            with engine.begin() as conn:
                for statement in statements:
                    conn.exec_driver_sql(statement)
        assert isinstance(info.value, IntegrityError) or GUARD in str(info.value) or "legal state transitions" in str(info.value)
        if expect:
            assert expect.upper() in str(info.value).upper(), str(info.value)[:200]
    finally:
        engine.dispose()


def run_sql(plan_id, revision_id, attempt=1, status="running", **o):
    v = {"id": uuid.uuid4().hex, "plan_id": plan_id, "revision_id": revision_id, "user_id": U, "workspace_id": W, "attempt": attempt, "status": status,
         "error_code": None, "error_message": None, **o}
    done = "NULL" if status == "running" else STAMP
    vals = ", ".join("NULL" if x is None else (f"'{x}'" if isinstance(x, str) else str(x)) for x in v.values())
    return f"INSERT INTO plan_runs ({', '.join(v)}, created_at, completed_at) VALUES ({vals}, {STAMP}, {done})", v["id"]


def trun_sql(run_id, revision_id, key="t1", position=0, status="pending", **o):
    v = {"id": uuid.uuid4().hex, "run_id": run_id, "revision_id": revision_id, "task_key": key, "position": position, "level": 0, "agent_source": "builtin",
         "agent_key": "sales", "agent_name": "Sales", "agent_version_id": None, "agent_version": None, "status": status, "execution_id": None,
         "error_code": None, "error_message": None, **o}
    started = "NULL" if status == "pending" else STAMP
    done = "NULL" if status in ("pending", "running") else STAMP
    vals = ", ".join("NULL" if x is None else (f"'{x}'" if isinstance(x, str) else str(x)) for x in v.values())
    return f"INSERT INTO plan_task_runs ({', '.join(v)}, created_at, started_at, completed_at) VALUES ({vals}, {STAMP}, {started}, {done})", v["id"]


@pytest.fixture
def base(db):
    """A migrated database with one plan, one revision with tasks t1 and t2 (t2 depends on t1) and one custom agent version."""
    db.upgrade()
    seed(db)
    _, version = seed_agent(db)
    sql, pid = plan_sql(status="approved", approved_revision=1, approved_at="2026-01-01 00:00:00.000000")
    rev, rid = revision_sql(pid)
    accepted(db, sql, rev, task_sql(rid, "t1", 0), task_sql(rid, "t2", 1, agent_source="custom", agent_key="my-agent", agent_name="My",
                                                           agent_version_id=version, agent_version=1), dep_sql(rid, "t2", "t1"))
    return db, pid, rid, version


def exec_id(db, **kw):
    sql, eid = execution_sql(user_id=U, workspace_id=W, **kw)
    accepted(db, sql)
    return eid


# ------------------------------------------------------------------------------------------------ revision, upgrade, downgrade

def test_revision_graph_and_file_conventions():
    script = ScriptDirectory.from_config(make_cfg())
    assert script.get_heads() == ["0018_agent_slug_per_user"]  # M4-A1 and M4-A2 sit on top of 0008
    rev = script.get_revision(RUNS)
    assert rev.down_revision == PLANS and rev.branch_labels == set() and rev.dependencies is None and len(RUNS) <= 32
    assert 'revision: str = "0008_plan_runs"' in VERSION_FILE.read_text(encoding="utf-8")
    assert not re.search(r"ALTER TABLE|op\.(add_column|drop_column|alter_column|batch_alter_table)", VERSION_FILE.read_text(encoding="utf-8"))  # additive only


def test_check_expressions_in_the_migration_match_the_models(import_fresh):
    models = fresh_models(import_fresh)
    source = re.sub(r"\s+", "", VERSION_FILE.read_text(encoding="utf-8").replace('"\n            "', "").replace('"\n        "', ""))
    for expression in (models.PLAN_RUN_STATUS_CHECK, models.PLAN_RUN_COMPLETION_CHECK, models.PLAN_RUN_ATTEMPT_CHECK, models.PLAN_TASK_RUN_STATUS_CHECK,
                       models.PLAN_TASK_RUN_PIN_CHECK, models.PLAN_TASK_RUN_SOURCE_CHECK, models.PLAN_TASK_RUN_EXECUTION_CHECK,
                       models.PLAN_TASK_RUN_COMPLETION_CHECK, models.PLAN_TASK_RUN_OUTCOME_CHECK):
        assert re.sub(r"\s+", "", expression) in source


TABLES_BEFORE = ("users", "workspaces", "custom_agents", "custom_agent_versions", "agent_executions", "plans", "plan_revisions", "plan_tasks", "plan_task_dependencies")


def snapshot(db):
    return {t: db.rows(f"SELECT * FROM {t} ORDER BY 1") for t in TABLES_BEFORE}


def test_upgrade_adds_exactly_the_new_objects_and_keeps_every_existing_row(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade(PLANS)
    seed(db)
    seed_agent(db)
    sql, pid = plan_sql()
    rev, rid = revision_sql(pid)
    accepted(db, sql, rev, task_sql(rid, "t1", 0))
    before_master, before_rows = db.master(), snapshot(db)
    db.upgrade(RUNS)
    assert snapshot(db) == before_rows
    after = db.master()
    old_ddl = {(t, n): s for t, n, _tbl, s in before_master}
    for t, n, _tbl, s in after:
        if (t, n) in old_ddl:
            assert s == old_ddl[(t, n)], f"{t} {n} was altered"  # earlier objects (incl. the 0006 and 0007 triggers) are unchanged
    added = {n for t, n, _tbl, _s in after if (t, n) not in old_ddl and not n.startswith("sqlite_autoindex")}
    assert added == {"plan_runs", "plan_task_runs", "uq_plan_runs_one_active", "uq_plan_runs_one_success", "ix_plan_runs_plan_id_created_at",
                     "ix_plan_task_runs_agent_version_id", "plan_runs_no_update", "plan_runs_no_delete", "plan_task_runs_no_update", "plan_task_runs_no_delete"}
    assert db.diffs(models.Base.metadata, exclude_tables=("run_leases", "execution_events", "plan_run_retries", "plan_task_carryovers", "worker_processes", "execution_owners", "tool_invocations", "tool_approvals", "tool_suspensions", "model_usage", "notifications", "provider_events", "tool_dispatches", "tool_reconciliations", "workspace_invitations",)) == []  # the migrated schema equals the live models (0009's table is not created yet)


def test_downgrade_restores_the_exact_previous_schema_keeps_rows_and_reupgrade_is_identical(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade(PLANS)
    seed(db)
    before_master, before_rows = db.master(), snapshot(db)
    db.upgrade(RUNS)
    head_master = db.master()
    assert head_master != before_master
    db.downgrade(PLANS)
    assert db.master() == before_master  # tables, indexes and triggers of 0008 are gone, everything else byte for byte
    assert snapshot(db) == before_rows and db.rows("SELECT version_num FROM alembic_version") == [(PLANS,)]
    assert db.diffs(models.Base.metadata, exclude_tables=RUN_TABLES + ("run_leases", "execution_events", "plan_run_retries", "plan_task_carryovers", "worker_processes", "execution_owners", "tool_invocations", "tool_approvals", "tool_suspensions", "model_usage", "notifications", "provider_events", "tool_dispatches", "tool_reconciliations", "workspace_invitations",)) == []
    db.upgrade(RUNS)
    assert db.master() == head_master and db.diffs(models.Base.metadata, exclude_tables=("run_leases", "execution_events", "plan_run_retries", "plan_task_carryovers", "worker_processes", "execution_owners", "tool_invocations", "tool_approvals", "tool_suspensions", "model_usage", "notifications", "provider_events", "tool_dispatches", "tool_reconciliations", "workspace_invitations",)) == []


# ------------------------------------------------------------------------------------------------ constraints

def test_run_constraints_and_the_duplicate_execution_indexes(base):
    db, pid, rid, _ = base
    sql, run = run_sql(pid, rid)
    accepted(db, sql)
    refused(db, run_sql(pid, rid, 2)[0], expect="UNIQUE")  # one running run per plan
    refused(db, run_sql(pid, rid, 1, "failed", error_code="x")[0], expect="UNIQUE")  # (plan, attempt)
    accepted(db, f"UPDATE plan_runs SET status = 'failed', completed_at = {STAMP} WHERE id = '{run}'")
    accepted(db, run_sql(pid, rid, 2, "succeeded")[0])
    refused(db, run_sql(pid, rid, 3, "succeeded")[0], expect="UNIQUE")  # one succeeded run per revision: no duplicate execution
    accepted(db, run_sql(pid, rid, 3, "partial")[0], run_sql(pid, rid, 4, "cancelled")[0])  # other finished runs are history and may repeat
    refused(db, run_sql(pid, rid, 5, "queued")[0], expect="CHECK")
    refused(db, run_sql(pid, rid, 0, "failed")[0], expect="CHECK")
    refused(db, f"INSERT INTO plan_runs (id, plan_id, revision_id, user_id, workspace_id, attempt, status, created_at) VALUES ('{uuid.uuid4().hex}','{pid}','{rid}','{U}','{W}',9,'failed',{STAMP})", expect="CHECK")  # final without completed_at
    refused(db, f"INSERT INTO plan_runs (id, plan_id, revision_id, user_id, workspace_id, attempt, status, created_at, completed_at) VALUES ('{uuid.uuid4().hex}','{pid}','{rid}','{U}','{W}',9,'running',{STAMP},{STAMP})", expect="CHECK")
    refused(db, run_sql(uuid.uuid4().hex, rid, 7, "failed")[0], expect="FOREIGN KEY")
    refused(db, run_sql(pid, uuid.uuid4().hex, 7, "failed")[0], expect="FOREIGN KEY")
    refused(db, run_sql(pid, rid, 7, "failed", user_id=uuid.uuid4().hex)[0], expect="FOREIGN KEY")
    refused(db, run_sql(pid, rid, 7, "failed", workspace_id=uuid.uuid4().hex)[0], expect="FOREIGN KEY")


def test_task_run_constraints_pin_and_composite_references(base):
    db, pid, rid, version = base
    run_stmt, run = run_sql(pid, rid)
    accepted(db, run_stmt)
    accepted(db, trun_sql(run, rid, "t1", 0)[0])
    accepted(db, trun_sql(run, rid, "t2", 1, agent_source="custom", agent_key="my-agent", agent_name="My", agent_version_id=version, agent_version=1)[0])
    refused(db, trun_sql(run, rid, "t1", 5)[0], expect="UNIQUE")  # one task run per task per run
    refused(db, trun_sql(run, rid, "t9", 0)[0], expect="UNIQUE")
    refused(db, trun_sql(run, rid, "ghost", 7)[0], expect="FOREIGN KEY")  # the task must exist in the run's own revision
    other_rev, other_rid = revision_sql(pid, 2)
    accepted(db, other_rev, task_sql(other_rid, "x1", 0))
    refused(db, trun_sql(run, other_rid, "x1", 7)[0], expect="FOREIGN KEY")  # a task of ANOTHER revision cannot join this run
    refused(db, trun_sql(uuid.uuid4().hex, rid, "t1", 7)[0], expect="FOREIGN KEY")
    # pin, status and outcome consistency
    refused(db, trun_sql(run, rid, "t3", 7, agent_source="custom", agent_key="x")[0], expect="CHECK")
    refused(db, trun_sql(run, rid, "t3", 7, agent_version_id=version, agent_version=1)[0], expect="CHECK")
    refused(db, trun_sql(run, rid, "t3", 7, status="queued")[0], expect="CHECK")
    refused(db, trun_sql(run, rid, "t3", 7, status="running")[0], expect="CHECK")  # running without an execution link
    refused(db, trun_sql(run, rid, "t3", 7, status="succeeded")[0], expect="CHECK")
    refused(db, trun_sql(run, rid, "t3", 7, status="failed")[0], expect="CHECK")  # a failure needs a code
    refused(db, trun_sql(run, rid, "t3", 7, status="skipped")[0], expect="CHECK")
    refused(db, trun_sql(run, rid, "t3", 7, status="pending", execution_id=exec_id(db))[0], expect="CHECK")  # unstarted tasks have no execution
    refused(db, trun_sql(run, rid, "t3", 7, status="succeeded", execution_id=exec_id(db), error_code="timeout")[0], expect="CHECK")
    refused(db, trun_sql(run, rid, "t3", 7, status="running", execution_id=uuid.uuid4().hex)[0], expect="FOREIGN KEY")
    assert db.rows("SELECT count(*) FROM plan_task_runs") == [(2,)]


def test_an_execution_can_back_only_one_task_run(base):
    db, pid, rid, _ = base
    run_stmt, run = run_sql(pid, rid)
    accepted(db, run_stmt)
    first, first_id = trun_sql(run, rid, "t1", 0)
    second, _ = trun_sql(run, rid, "t2", 1)
    accepted(db, first, second)
    execution = exec_id(db)
    accepted(db, f"UPDATE plan_task_runs SET status = 'running', execution_id = '{execution}', started_at = {STAMP} WHERE id = '{first_id}'")
    refused(db, f"UPDATE plan_task_runs SET status = 'running', execution_id = '{execution}', started_at = {STAMP} WHERE task_key = 't2'", expect="UNIQUE")


def test_no_cascades_and_parents_cannot_disappear(base):
    db, pid, rid, version = base
    run_stmt, run = run_sql(pid, rid)
    accepted(db, run_stmt, trun_sql(run, rid, "t1", 0)[0])
    for table, where in (("users", f"id = '{U}'"), ("workspaces", f"id = '{W}'"), ("plans", f"id = '{pid}'"), ("plan_revisions", f"id = '{rid}'"),
                         ("custom_agent_versions", f"id = '{version}'")):
        refused(db, f"DELETE FROM {table} WHERE {where}")
    assert db.rows("SELECT count(*) FROM plan_runs") == [(1,)] and db.rows("SELECT count(*) FROM plan_task_runs") == [(1,)]


# ------------------------------------------------------------------------------------------------ guards (legal transitions)

def test_runs_only_move_running_to_final_and_never_change_identity_or_disappear(base):
    db, pid, rid, _ = base
    run_stmt, run = run_sql(pid, rid)
    accepted(db, run_stmt)
    for statement in (f"UPDATE plan_runs SET attempt = 9 WHERE id = '{run}'", f"UPDATE plan_runs SET user_id = '{uuid.uuid4().hex}' WHERE id = '{run}'",
                      f"UPDATE plan_runs SET workspace_id = '{uuid.uuid4().hex}' WHERE id = '{run}'", f"UPDATE plan_runs SET revision_id = '{uuid.uuid4().hex}' WHERE id = '{run}'",
                      f"UPDATE plan_runs SET plan_id = '{uuid.uuid4().hex}' WHERE id = '{run}'", f"UPDATE plan_runs SET created_at = '2020-01-01 00:00:00.000000' WHERE id = '{run}'",
                      f"UPDATE plan_runs SET id = '{uuid.uuid4().hex}' WHERE id = '{run}'", "DELETE FROM plan_runs"):
        refused(db, statement)
    refused(db, f"UPDATE plan_runs SET status = 'running' WHERE id = '{run}'")  # no-op "transitions" are not legal either
    accepted(db, f"UPDATE plan_runs SET status = 'partial', error_code = 'interrupted', completed_at = {STAMP} WHERE id = '{run}'")
    for statement in (f"UPDATE plan_runs SET status = 'succeeded' WHERE id = '{run}'", f"UPDATE plan_runs SET status = 'running', completed_at = NULL WHERE id = '{run}'",
                      f"UPDATE plan_runs SET error_code = 'x' WHERE id = '{run}'", "DELETE FROM plan_runs"):
        refused(db, statement)  # a finished run is frozen


def test_task_runs_follow_only_the_legal_transitions(base):
    db, pid, rid, version = base
    run_stmt, run = run_sql(pid, rid)
    accepted(db, run_stmt)
    stmts = [trun_sql(run, rid, f"t{i}", i - 1) for i in (1, 2)]
    t1, t1_id = stmts[0]
    t2, t2_id = stmts[1]
    accepted(db, t1, t2)
    execution = exec_id(db)
    # illegal from pending
    for statement in (f"UPDATE plan_task_runs SET status = 'succeeded', completed_at = {STAMP} WHERE id = '{t1_id}'",
                      f"UPDATE plan_task_runs SET status = 'pending' WHERE id = '{t1_id}'",
                      f"UPDATE plan_task_runs SET agent_key = 'ceo' WHERE id = '{t1_id}'", f"UPDATE plan_task_runs SET agent_version = 3 WHERE id = '{t1_id}'",
                      f"UPDATE plan_task_runs SET task_key = 'zz' WHERE id = '{t1_id}'", f"UPDATE plan_task_runs SET position = 9 WHERE id = '{t1_id}'",
                      f"UPDATE plan_task_runs SET run_id = '{uuid.uuid4().hex}' WHERE id = '{t1_id}'", "DELETE FROM plan_task_runs"):
        refused(db, statement)
    # pending -> running links the execution
    accepted(db, f"UPDATE plan_task_runs SET status = 'running', execution_id = '{execution}', started_at = {STAMP} WHERE id = '{t1_id}'")
    for statement in (f"UPDATE plan_task_runs SET status = 'pending', execution_id = NULL, started_at = NULL WHERE id = '{t1_id}'",
                      f"UPDATE plan_task_runs SET status = 'skipped', error_code = 'x', completed_at = {STAMP} WHERE id = '{t1_id}'",
                      f"UPDATE plan_task_runs SET status = 'succeeded', execution_id = '{exec_id(db)}', completed_at = {STAMP} WHERE id = '{t1_id}'",  # the link is fixed
                      f"UPDATE plan_task_runs SET status = 'succeeded', started_at = '2020-01-01 00:00:00.000000', completed_at = {STAMP} WHERE id = '{t1_id}'",
                      "DELETE FROM plan_task_runs"):
        refused(db, statement)
    accepted(db, f"UPDATE plan_task_runs SET status = 'succeeded', completed_at = {STAMP} WHERE id = '{t1_id}'")
    for statement in (f"UPDATE plan_task_runs SET status = 'failed', error_code = 'x' WHERE id = '{t1_id}'", f"UPDATE plan_task_runs SET status = 'running' WHERE id = '{t1_id}'",
                      f"UPDATE plan_task_runs SET error_code = 'x' WHERE id = '{t1_id}'"):
        refused(db, statement)  # a finished task run is frozen
    # the other legal exits from pending: skipped, cancelled and failed-before-start
    accepted(db, f"UPDATE plan_task_runs SET status = 'skipped', error_code = 'dependency_failed', completed_at = {STAMP} WHERE id = '{t2_id}'")
    assert db.rows("SELECT task_key, status FROM plan_task_runs ORDER BY position") == [("t1", "succeeded"), ("t2", "skipped")]
    for exit_status, extra in (("cancelled", ""), ("failed", ", error_code = 'agent_unavailable'")):
        run2, rid2 = run_sql(pid, rid, 5 + len(exit_status), "failed")
        accepted(db, run2)
        one, one_id = trun_sql(rid2, rid, "t1", 0)
        accepted(db, one)
        accepted(db, f"UPDATE plan_task_runs SET status = '{exit_status}', completed_at = {STAMP}{extra} WHERE id = '{one_id}'")
