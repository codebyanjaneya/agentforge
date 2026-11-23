"""M2-B: migration 0006_agent_executions (SQLite files in tmp_path; PostgreSQL is checked in test_postgres_integration)."""

import re
import sys
import uuid

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

from migration_support import BACKEND, Db, make_cfg

VERSION_FILE = BACKEND / "migrations" / "versions" / "0006_agent_executions.py"
AGENTS, EXECUTIONS = "0005_custom_agents", "0006_agent_executions"
U, W, U2 = (uuid.uuid4().hex for _ in range(3))
STAMP = "'2026-01-01 00:00:00.000000'"


@pytest.fixture
def db(tmp_path, monkeypatch):
    return Db(tmp_path / "executions.db", monkeypatch)


def fresh_models(import_fresh):
    import_fresh("memory.models")
    return sys.modules["memory.models"]


def seed(db):
    db.run(
        "INSERT INTO agent_memories (agent_name, user_email, summary, created_at) VALUES ('ceo','legacy@example.com','legacy summary',"
        f"{STAMP})",
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


TABLES_BEFORE = ("agent_memories", "users", "workspaces", "workspace_memberships", "custom_agents", "custom_agent_versions")


def snapshot(db):
    return {t: db.rows(f"SELECT * FROM {t} ORDER BY 1") for t in TABLES_BEFORE}


def execution_sql(**o):
    v = {"id": uuid.uuid4().hex, "user_id": U, "workspace_id": W, "agent_source": "builtin", "agent_key": "sales", "agent_name": "Sales",
         "agent_version_id": None, "agent_version": None, "status": "running", "task": "do it", "result": None, "error_code": None,
         "error_message": None, "provider": "groq", "model": None, **o}
    done = None if v["status"] == "running" else STAMP
    cols = ", ".join(v) + ", created_at, started_at, completed_at"
    vals = ", ".join("NULL" if x is None else (f"'{x}'" if isinstance(x, str) else str(x)) for x in v.values())
    return f"INSERT INTO agent_executions ({cols}) VALUES ({vals}, {STAMP}, {STAMP}, {done or 'NULL'})", v["id"]


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
        assert isinstance(info.value, IntegrityError) or "append-only" in str(info.value)
        if expect:
            assert expect.upper() in str(info.value).upper()
    finally:
        engine.dispose()


@pytest.fixture
def migrated(db):
    db.upgrade()
    seed(db)
    return db


# ------------------------------------------------------------------------------------------------ revision and upgrade

def test_revision_graph_and_file_conventions():
    script = ScriptDirectory.from_config(make_cfg())
    assert script.get_heads() == ["0018_agent_slug_per_user"]  # M3-A, M3-B and M4-A1 sit on top of 0006
    rev = script.get_revision(EXECUTIONS)
    assert rev.revision == "0006_agent_executions" and rev.down_revision == AGENTS
    assert rev.branch_labels == set() and rev.dependencies is None and len(EXECUTIONS) <= 32
    source = VERSION_FILE.read_text(encoding="utf-8")
    assert 'revision: str = "0006_agent_executions"' in source
    assert not re.search(r"ALTER TABLE|op\.(add_column|drop_column|alter_column|batch_alter_table)", source)  # additive only


def test_check_expressions_in_the_migration_match_the_models(import_fresh):
    models = fresh_models(import_fresh)
    source = re.sub(r"\s+", "", VERSION_FILE.read_text(encoding="utf-8").replace('"\n            "', ""))
    for expression in (models.EXECUTION_SOURCE_CHECK, models.EXECUTION_STATUS_CHECK, models.EXECUTION_VERSION_PIN_CHECK,
                       models.EXECUTION_COMPLETION_CHECK, models.EXECUTION_OUTCOME_CHECK):
        assert re.sub(r"\s+", "", expression) in source


def test_upgrade_adds_exactly_the_new_objects_and_keeps_every_existing_row(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade(AGENTS)
    seed(db)
    seed_agent(db)
    before_master, before_rows = db.master(), snapshot(db)
    db.upgrade(EXECUTIONS)
    assert snapshot(db) == before_rows
    after = db.master()
    old_ddl = {(t, n): sql for t, n, _tbl, sql in before_master}
    for t, n, _tbl, sql in after:
        if (t, n) in old_ddl:
            assert sql == old_ddl[(t, n)], f"{t} {n} was altered"  # every pre-existing object, including the 0005 triggers, is unchanged
    added = {(t, n) for t, n, _tbl, _sql in after} - set(old_ddl)
    assert {n for _t, n in added} == {"agent_executions", "ix_agent_executions_workspace_id_user_id_created_at_id",
                                      "ix_agent_executions_agent_version_id", "agent_executions_no_update", "agent_executions_no_delete",
                                      "sqlite_autoindex_agent_executions_1"}  # the last one is SQLite's own index for the primary key
    assert db.rows("SELECT version_num FROM alembic_version") == [(EXECUTIONS,)]
    assert db.diffs(models.Base.metadata, exclude_tables=("plans", "plan_revisions", "plan_tasks", "plan_task_dependencies", "plan_runs", "plan_task_runs", "run_leases", "execution_events", "plan_run_retries", "plan_task_carryovers", "worker_processes", "execution_owners", "tool_invocations", "tool_approvals", "tool_suspensions", "model_usage", "notifications", "provider_events", "tool_dispatches", "tool_reconciliations", "workspace_invitations")) == []  # equals the live models (0007's tables not yet created)


# ------------------------------------------------------------------------------------------------ constraints

def test_row_constraints(migrated):
    db = migrated
    agent, version = seed_agent(db)
    accepted(db, execution_sql()[0])  # a running built-in
    accepted(db, execution_sql(agent_source="custom", agent_key="my-agent", agent_name="My Agent", agent_version_id=version, agent_version=1)[0])
    accepted(db, execution_sql(status="succeeded", result="ok")[0], execution_sql(status="failed", error_code="timeout")[0],
             execution_sql(status="cancelled", error_code="cancelled")[0])
    refused(db, execution_sql(agent_source="robot")[0], expect="CHECK")
    refused(db, execution_sql(status="queued")[0], expect="CHECK")
    # the version pin: a custom execution MUST pin a version, a built-in one MUST NOT
    refused(db, execution_sql(agent_source="custom", agent_key="my-agent")[0], expect="CHECK")
    refused(db, execution_sql(agent_source="custom", agent_key="my-agent", agent_version_id=version)[0], expect="CHECK")
    refused(db, execution_sql(agent_source="custom", agent_key="my-agent", agent_version_id=version, agent_version=0)[0], expect="CHECK")
    refused(db, execution_sql(agent_version_id=version, agent_version=1)[0], expect="CHECK")
    refused(db, execution_sql(agent_version=1)[0], expect="CHECK")
    # lifecycle and outcome consistency
    refused(db, execution_sql(status="succeeded", error_code="timeout")[0], expect="CHECK")
    refused(db, execution_sql(status="failed")[0], expect="CHECK")  # a failure needs a code
    refused(db, f"INSERT INTO agent_executions (id, user_id, workspace_id, agent_source, agent_key, agent_name, status, task, created_at, started_at, completed_at) "
                f"VALUES ('{uuid.uuid4().hex}','{U}','{W}','builtin','sales','Sales','running','t',{STAMP},{STAMP},{STAMP})", expect="CHECK")  # running with completed_at
    refused(db, f"INSERT INTO agent_executions (id, user_id, workspace_id, agent_source, agent_key, agent_name, status, task, created_at, started_at) "
                f"VALUES ('{uuid.uuid4().hex}','{U}','{W}','builtin','sales','Sales','succeeded','t',{STAMP},{STAMP})", expect="CHECK")  # final without completed_at
    # references
    refused(db, execution_sql(user_id=uuid.uuid4().hex)[0], expect="FOREIGN KEY")
    refused(db, execution_sql(workspace_id=uuid.uuid4().hex)[0], expect="FOREIGN KEY")
    refused(db, execution_sql(agent_source="custom", agent_key="x", agent_version_id=uuid.uuid4().hex, agent_version=1)[0], expect="FOREIGN KEY")
    for column in ("user_id", "workspace_id", "agent_source", "agent_key", "agent_name", "status", "task"):
        refused(db, execution_sql(**{column: None})[0], expect="NOT NULL")
    assert db.rows("SELECT count(*) FROM agent_executions") == [(5,)]


def test_no_cascades_and_parents_cannot_disappear(migrated):
    db = migrated
    agent, version = seed_agent(db)
    sql, eid = execution_sql(agent_source="custom", agent_key="my-agent", agent_name="My Agent", agent_version_id=version, agent_version=1)
    accepted(db, sql)
    refused(db, f"DELETE FROM users WHERE id = '{U}'", expect="FOREIGN KEY")
    refused(db, f"DELETE FROM workspaces WHERE id = '{W}'", expect="FOREIGN KEY")
    refused(db, f"DELETE FROM custom_agent_versions WHERE id = '{version}'")  # also immutable since 0005
    assert db.rows("SELECT count(*) FROM agent_executions") == [(1,)]


# ------------------------------------------------------------------------------------------------ guards

def test_finished_rows_are_frozen_and_identity_and_pin_are_fixed_from_the_start(migrated):
    db = migrated
    agent, version = seed_agent(db)
    other_version = uuid.uuid4().hex
    db.run("INSERT INTO custom_agent_versions (id, agent_id, version, definition, content_hash, created_by, created_at) "
           f"VALUES ('{other_version}','{agent}',2,'{{}}','{'g' * 64}','{U}',{STAMP})")
    sql, running = execution_sql(agent_source="custom", agent_key="my-agent", agent_name="My Agent", agent_version_id=version, agent_version=1)
    accepted(db, sql)
    for statement in (f"UPDATE agent_executions SET agent_version_id = '{other_version}', agent_version = 2 WHERE id = '{running}'",
                      f"UPDATE agent_executions SET agent_version_id = NULL WHERE id = '{running}'",
                      f"UPDATE agent_executions SET agent_source = 'builtin' WHERE id = '{running}'",
                      f"UPDATE agent_executions SET agent_key = 'sales' WHERE id = '{running}'",
                      f"UPDATE agent_executions SET agent_name = 'Renamed' WHERE id = '{running}'",
                      f"UPDATE agent_executions SET task = 'changed' WHERE id = '{running}'",
                      f"UPDATE agent_executions SET user_id = '{U2}' WHERE id = '{running}'",
                      f"UPDATE agent_executions SET workspace_id = '{uuid.uuid4().hex}' WHERE id = '{running}'",
                      f"UPDATE agent_executions SET id = '{uuid.uuid4().hex}' WHERE id = '{running}'",
                      f"UPDATE agent_executions SET created_at = '2020-01-01 00:00:00.000000' WHERE id = '{running}'",
                      f"UPDATE agent_executions SET started_at = '2020-01-01 00:00:00.000000' WHERE id = '{running}'",
                      "DELETE FROM agent_executions"):
        refused(db, statement, expect="append-only")
    # the one allowed transition: running -> final, changing only the outcome columns
    accepted(db, f"UPDATE agent_executions SET status = 'succeeded', result = 'done', provider = 'groq', model = 'm', completed_at = {STAMP} WHERE id = '{running}'")
    assert db.rows(f"SELECT status, result, agent_version FROM agent_executions WHERE id = '{running}'") == [("succeeded", "done", 1)]
    for statement in (f"UPDATE agent_executions SET result = 'tampered' WHERE id = '{running}'",
                      f"UPDATE agent_executions SET status = 'running', completed_at = NULL WHERE id = '{running}'",
                      f"UPDATE agent_executions SET status = 'failed', error_code = 'timeout' WHERE id = '{running}'",
                      "DELETE FROM agent_executions"):
        refused(db, statement, expect="append-only")


# ------------------------------------------------------------------------------------------------ downgrade

def test_downgrade_restores_the_exact_previous_schema_keeps_rows_and_reupgrade_is_identical(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade(AGENTS)
    seed(db)
    seed_agent(db)
    before_master, before_rows = db.master(), snapshot(db)
    db.upgrade(EXECUTIONS)
    head_master = db.master()
    assert head_master != before_master
    accepted(db, execution_sql()[0])
    db.downgrade(AGENTS)  # development/test behaviour; production is forward-only
    assert db.master() == before_master  # tables, indexes AND triggers of 0006 are gone, everything else byte for byte
    assert snapshot(db) == before_rows
    assert db.rows("SELECT version_num FROM alembic_version") == [(AGENTS,)]
    assert db.diffs(models.Base.metadata, exclude_tables=("agent_executions", "plans", "plan_revisions", "plan_tasks", "plan_task_dependencies", "plan_runs", "plan_task_runs", "run_leases", "execution_events", "plan_run_retries", "plan_task_carryovers", "worker_processes", "execution_owners", "tool_invocations", "tool_approvals", "tool_suspensions", "model_usage", "notifications", "provider_events", "tool_dispatches", "tool_reconciliations", "workspace_invitations")) == []
    db.upgrade(EXECUTIONS)  # re-upgrade to the same revision (0007 has its own tests)
    assert db.master() == head_master
    assert db.rows("SELECT count(*) FROM agent_executions") == [(0,)]
    db.upgrade()
    assert db.diffs(models.Base.metadata) == []
