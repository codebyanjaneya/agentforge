"""M3-A: migration 0007_plans (SQLite files in tmp_path; PostgreSQL is checked in test_postgres_integration)."""

import re
import sys
import uuid

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

from migration_support import BACKEND, Db, make_cfg

VERSION_FILE = BACKEND / "migrations" / "versions" / "0007_plans.py"
EXECUTIONS, PLANS = "0006_agent_executions", "0007_plans"
U, W = (uuid.uuid4().hex for _ in range(2))
STAMP = "'2026-01-01 00:00:00.000000'"
PLAN_TABLES = ("plans", "plan_revisions", "plan_tasks", "plan_task_dependencies")


@pytest.fixture
def db(tmp_path, monkeypatch):
    return Db(tmp_path / "plans.db", monkeypatch)


def fresh_models(import_fresh):
    import_fresh("memory.models")
    return sys.modules["memory.models"]


def seed(db):
    db.run(
        f"INSERT INTO users (id, email, email_normalized, is_active, created_at) VALUES ('{U}','u@example.com','u@example.com',1,{STAMP})",
        f"INSERT INTO workspaces (id, name, is_personal, personal_for_user_id, created_at) VALUES ('{W}','P',1,'{U}',{STAMP})",
        f"INSERT INTO workspace_memberships (workspace_id, user_id, role, created_at) VALUES ('{W}','{U}','owner',{STAMP})",
    )


def seed_agent(db):
    agent, version = uuid.uuid4().hex, uuid.uuid4().hex
    db.run(
        "INSERT INTO custom_agents (id, user_id, workspace_id, slug, status, latest_version, current_version, created_at, updated_at) "
        f"VALUES ('{agent}','{U}','{W}','my-agent','active',1,1,{STAMP},{STAMP})",
        "INSERT INTO custom_agent_versions (id, agent_id, version, definition, content_hash, created_by, created_at) "
        f"VALUES ('{version}','{agent}',1,'{{}}','{'h' * 64}','{U}',{STAMP})",
    )
    return agent, version


def plan_sql(**o):
    v = {"id": uuid.uuid4().hex, "user_id": U, "workspace_id": W, "objective": "Launch the product", "status": "draft", "latest_revision": 1,
         "approved_revision": None, "approved_at": None, **o}
    cols = ", ".join(v) + ", created_at, updated_at"
    vals = ", ".join("NULL" if x is None else (f"'{x}'" if isinstance(x, str) else str(x)) for x in v.values())
    return f"INSERT INTO plans ({cols}) VALUES ({vals}, {STAMP}, {STAMP})", v["id"]


def revision_sql(plan_id, revision=1, **o):
    v = {"id": uuid.uuid4().hex, "plan_id": plan_id, "revision": revision, "origin": "model", "interpretation": "Meaning", "warnings": "[]",
         "unsupported": "[]", "provider": "groq", "model": "m", **o}
    vals = ", ".join("NULL" if x is None else (f"'{x}'" if isinstance(x, str) else str(x)) for x in v.values())
    return f"INSERT INTO plan_revisions ({', '.join(v)}, created_at) VALUES ({vals}, {STAMP})", v["id"]


def task_sql(revision_id, key="t1", position=0, **o):
    v = {"id": uuid.uuid4().hex, "revision_id": revision_id, "task_key": key, "position": position, "level": 0, "title": "T", "description": "D",
         "expected_output": "E", "rationale": "R", "dependency_reason": None, "complexity": "low", "priority": "normal", "agent_source": "builtin",
         "agent_key": "sales", "agent_name": "Sales", "agent_version_id": None, "agent_version": None, **o}
    vals = ", ".join("NULL" if x is None else (f"'{x}'" if isinstance(x, str) else str(x)) for x in v.values())
    return f"INSERT INTO plan_tasks ({', '.join(v)}) VALUES ({vals})"


def dep_sql(revision_id, task, depends_on):
    return f"INSERT INTO plan_task_dependencies (revision_id, task_key, depends_on_key) VALUES ('{revision_id}','{task}','{depends_on}')"


def accepted(db, *statements):
    engine = db.engine(fk=True)
    try:
        with engine.begin() as conn:
            for statement in statements:
                conn.exec_driver_sql(statement)
    finally:
        engine.dispose()


def refused(db, *statements, expect=None):
    engine = db.engine(fk=True)
    try:
        with pytest.raises(Exception) as info:
            with engine.begin() as conn:
                for statement in statements:
                    conn.exec_driver_sql(statement)
        assert isinstance(info.value, IntegrityError) or "insert-only" in str(info.value)
        if expect:
            assert expect.upper() in str(info.value).upper()
    finally:
        engine.dispose()


@pytest.fixture
def migrated(db):
    db.upgrade()
    seed(db)
    return db


TABLES_BEFORE = ("users", "workspaces", "workspace_memberships", "custom_agents", "custom_agent_versions", "agent_executions")


def snapshot(db):
    return {t: db.rows(f"SELECT * FROM {t} ORDER BY 1") for t in TABLES_BEFORE}


# ------------------------------------------------------------------------------------------------ revision and upgrade

def test_revision_graph_and_file_conventions():
    script = ScriptDirectory.from_config(make_cfg())
    assert script.get_heads() == ["0017_invitations"]  # M3-B and M4-A1 sit on top
    rev = script.get_revision(PLANS)
    assert rev.down_revision == EXECUTIONS and rev.branch_labels == set() and rev.dependencies is None and len(PLANS) <= 32
    source = VERSION_FILE.read_text(encoding="utf-8")
    assert 'revision: str = "0007_plans"' in source
    assert not re.search(r"ALTER TABLE|op\.(add_column|drop_column|alter_column|batch_alter_table)", source)  # additive only


def test_check_expressions_in_the_migration_match_the_models(import_fresh):
    models = fresh_models(import_fresh)
    source = re.sub(r"\s+", "", VERSION_FILE.read_text(encoding="utf-8").replace('"\n            "', "").replace('"\n        "', ""))
    for expression in (models.PLAN_STATUS_CHECK, models.PLAN_REVISION_POSITIVE_CHECK, models.PLAN_APPROVAL_CHECK, models.PLAN_REVISION_NUMBER_CHECK,
                       models.PLAN_ORIGIN_CHECK, models.PLAN_TASK_SOURCE_CHECK, models.PLAN_TASK_COMPLEXITY_CHECK, models.PLAN_TASK_PRIORITY_CHECK,
                       models.PLAN_TASK_POSITION_CHECK, models.PLAN_TASK_PIN_CHECK, models.PLAN_DEPENDENCY_SELF_CHECK):
        assert re.sub(r"\s+", "", expression) in source


def test_upgrade_adds_exactly_the_new_objects_and_keeps_every_existing_row(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade(EXECUTIONS)
    seed(db)
    seed_agent(db)
    before_master, before_rows = db.master(), snapshot(db)
    db.upgrade(PLANS)
    assert snapshot(db) == before_rows
    after = db.master()
    old_ddl = {(t, n): sql for t, n, _tbl, sql in before_master}
    for t, n, _tbl, sql in after:
        if (t, n) in old_ddl:
            assert sql == old_ddl[(t, n)], f"{t} {n} was altered"  # every earlier object (incl. M2-B's execution triggers) is unchanged
    added = {n for t, n, _tbl, _sql in after if (t, n) not in old_ddl}
    assert {n for n in added if not n.startswith("sqlite_autoindex")} == {
        *PLAN_TABLES, "ix_plans_workspace_id_user_id_created_at_id", "ix_plan_tasks_agent_version_id",
        *(f"{t}_no_{e}" for t in PLAN_TABLES[1:] for e in ("update", "delete"))}
    assert db.rows("SELECT version_num FROM alembic_version") == [(PLANS,)]
    assert db.diffs(models.Base.metadata, exclude_tables=("plan_runs", "plan_task_runs", "run_leases", "execution_events", "plan_run_retries", "plan_task_carryovers", "worker_processes", "execution_owners", "tool_invocations", "tool_approvals", "tool_suspensions", "model_usage", "notifications", "provider_events", "tool_dispatches", "tool_reconciliations", "workspace_invitations")) == []  # equals the live models (0008's tables not yet created)


# ------------------------------------------------------------------------------------------------ constraints

def test_row_constraints(migrated):
    db = migrated
    _, version = seed_agent(db)
    plan, pid = plan_sql()
    accepted(db, plan)
    accepted(db, plan_sql(status="approved", approved_revision=1, approved_at=STAMP.strip("'"))[0], plan_sql(status="cancelled")[0])
    refused(db, plan_sql(status="running")[0], expect="CHECK")
    refused(db, plan_sql(latest_revision=0)[0], expect="CHECK")
    refused(db, plan_sql(status="approved")[0], expect="CHECK")  # approved needs a revision and a time
    refused(db, plan_sql(status="approved", approved_revision=3, approved_at=STAMP.strip("'"))[0], expect="CHECK")  # beyond the latest revision
    refused(db, plan_sql(status="draft", approved_revision=1)[0], expect="CHECK")
    refused(db, plan_sql(user_id=uuid.uuid4().hex)[0], expect="FOREIGN KEY")
    refused(db, plan_sql(workspace_id=uuid.uuid4().hex)[0], expect="FOREIGN KEY")
    for column in ("user_id", "workspace_id", "objective", "status"):
        refused(db, plan_sql(**{column: None})[0], expect="NOT NULL")
    rev, rid = revision_sql(pid)
    accepted(db, rev)
    refused(db, revision_sql(pid, 1)[0], expect="UNIQUE")  # one row per (plan, revision)
    refused(db, revision_sql(pid, 0)[0], expect="CHECK")
    refused(db, revision_sql(pid, 2, origin="robot")[0], expect="CHECK")
    refused(db, revision_sql(uuid.uuid4().hex, 1)[0], expect="FOREIGN KEY")
    accepted(db, task_sql(rid, "t1", 0), task_sql(rid, "t2", 1, agent_source="custom", agent_key="my-agent", agent_name="My", agent_version_id=version, agent_version=1))
    refused(db, task_sql(rid, "t1", 5), expect="UNIQUE")  # duplicate task id inside a revision
    refused(db, task_sql(rid, "t9", 0), expect="UNIQUE")  # duplicate position
    refused(db, task_sql(rid, "t3", 2, complexity="huge"), expect="CHECK")
    refused(db, task_sql(rid, "t3", 2, priority="urgent"), expect="CHECK")
    refused(db, task_sql(rid, "t3", 2, agent_source="robot"), expect="CHECK")
    refused(db, task_sql(rid, "t3", -1), expect="CHECK")
    # the version pin: a custom task MUST pin a version, a built-in one MUST NOT
    refused(db, task_sql(rid, "t3", 2, agent_source="custom", agent_key="x"), expect="CHECK")
    refused(db, task_sql(rid, "t3", 2, agent_source="custom", agent_key="x", agent_version_id=version), expect="CHECK")
    refused(db, task_sql(rid, "t3", 2, agent_version_id=version, agent_version=1), expect="CHECK")
    refused(db, task_sql(rid, "t3", 2, agent_source="custom", agent_key="x", agent_version_id=uuid.uuid4().hex, agent_version=1), expect="FOREIGN KEY")
    refused(db, task_sql(uuid.uuid4().hex, "t3", 2), expect="FOREIGN KEY")
    accepted(db, dep_sql(rid, "t2", "t1"))
    refused(db, dep_sql(rid, "t2", "t1"), expect="UNIQUE")  # duplicate dependency
    refused(db, dep_sql(rid, "t1", "t1"), expect="CHECK")  # self dependency
    refused(db, dep_sql(rid, "t1", "ghost"), expect="FOREIGN KEY")  # missing dependency target
    other, oid = revision_sql(pid, 2)
    accepted(db, other, task_sql(oid, "x1", 0))
    refused(db, dep_sql(rid, "t1", "x1"), expect="FOREIGN KEY")  # an edge cannot cross revisions (composite FK)
    assert db.rows("SELECT count(*) FROM plan_task_dependencies") == [(1,)]


def test_no_cascades_and_parents_cannot_disappear(migrated):
    db = migrated
    agent, version = seed_agent(db)
    sql, pid = plan_sql()
    rev, rid = revision_sql(pid)
    accepted(db, sql, rev, task_sql(rid, "t1", 0, agent_source="custom", agent_key="my-agent", agent_name="My", agent_version_id=version, agent_version=1))
    refused(db, f"DELETE FROM users WHERE id = '{U}'", expect="FOREIGN KEY")
    refused(db, f"DELETE FROM workspaces WHERE id = '{W}'", expect="FOREIGN KEY")
    refused(db, f"DELETE FROM plans WHERE id = '{pid}'", expect="FOREIGN KEY")
    refused(db, f"DELETE FROM custom_agent_versions WHERE id = '{version}'")  # a pinned version can never disappear
    assert db.rows("SELECT count(*) FROM plan_tasks") == [(1,)]


# ------------------------------------------------------------------------------------------------ guards

def test_revision_tables_are_insert_only_but_the_header_is_updatable(migrated):
    db = migrated
    sql, pid = plan_sql()
    rev, rid = revision_sql(pid)
    accepted(db, sql, rev, task_sql(rid, "t1", 0), task_sql(rid, "t2", 1), dep_sql(rid, "t2", "t1"))
    for statement in ("UPDATE plan_revisions SET interpretation = 'x'", "UPDATE plan_revisions SET revision = 9", "DELETE FROM plan_revisions",
                      "UPDATE plan_tasks SET title = 'x'", "UPDATE plan_tasks SET agent_key = 'ceo'", "UPDATE plan_tasks SET level = 3", "DELETE FROM plan_tasks",
                      "UPDATE plan_task_dependencies SET depends_on_key = 't2'", "DELETE FROM plan_task_dependencies"):
        refused(db, statement, expect="insert-only")
    accepted(db, f"UPDATE plans SET status = 'cancelled', updated_at = {STAMP} WHERE id = '{pid}'")
    assert db.rows(f"SELECT status FROM plans WHERE id = '{pid}'") == [("cancelled",)]
    assert db.rows("SELECT count(*) FROM plan_revisions") == [(1,)] and db.rows("SELECT count(*) FROM plan_tasks") == [(2,)]


# ------------------------------------------------------------------------------------------------ downgrade

def test_downgrade_restores_the_exact_previous_schema_keeps_rows_and_reupgrade_is_identical(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade(EXECUTIONS)
    seed(db)
    seed_agent(db)
    before_master, before_rows = db.master(), snapshot(db)
    db.upgrade(PLANS)
    head_master = db.master()
    assert head_master != before_master
    accepted(db, plan_sql()[0])
    db.downgrade(EXECUTIONS)  # development/test behaviour; production is forward-only
    assert db.master() == before_master  # tables, indexes AND triggers of 0007 are gone, everything else byte for byte
    assert snapshot(db) == before_rows
    assert db.rows("SELECT version_num FROM alembic_version") == [(EXECUTIONS,)]
    assert db.diffs(models.Base.metadata, exclude_tables=PLAN_TABLES + ("plan_runs", "plan_task_runs", "run_leases", "execution_events", "plan_run_retries", "plan_task_carryovers", "worker_processes", "execution_owners", "tool_invocations", "tool_approvals", "tool_suspensions", "model_usage", "notifications", "provider_events", "tool_dispatches", "tool_reconciliations", "workspace_invitations")) == []
    db.upgrade(PLANS)
    assert db.master() == head_master
    assert db.rows("SELECT count(*) FROM plans") == [(0,)]
    db.upgrade()
    assert db.diffs(models.Base.metadata) == []
