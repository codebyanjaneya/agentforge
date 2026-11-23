"""M2-A: migration 0005_custom_agents (SQLite files in tmp_path; PostgreSQL is checked in test_postgres_integration).

SQLite is NOT PostgreSQL: this proves structure, constraint and trigger behaviour and reversibility on SQLite only.
"""

import re
import sys
import uuid

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

from migration_support import BACKEND, Db, make_cfg

VERSION_FILE = BACKEND / "migrations" / "versions" / "0005_custom_agents.py"
OWNERSHIP, AGENTS = "0004_memory_ownership", "0005_custom_agents"
NEW_TABLES = {"custom_agents", "custom_agent_versions"}
U, W, U2 = (uuid.uuid4().hex for _ in range(3))


@pytest.fixture
def db(tmp_path, monkeypatch):
    return Db(tmp_path / "agents.db", monkeypatch)


def fresh_models(import_fresh):
    import_fresh("memory.models")
    return sys.modules["memory.models"]


def seed(db):
    """Rows in every pre-existing table, so a migration that touched them would show."""
    db.run(
        "INSERT INTO agent_memories (agent_name, user_email, summary, created_at) VALUES ('ceo','legacy@example.com','legacy summary','2026-01-02 03:04:05.000000')",
        f"INSERT INTO users (id, email, email_normalized, is_active, created_at) VALUES ('{U}','u@example.com','u@example.com',1,'2026-01-02 03:04:05.000000')",
        f"INSERT INTO users (id, email, email_normalized, is_active, created_at) VALUES ('{U2}','u2@example.com','u2@example.com',1,'2026-01-02 03:04:05.000000')",
        f"INSERT INTO workspaces (id, name, is_personal, personal_for_user_id, created_at) VALUES ('{W}','P',1,'{U}','2026-01-02 03:04:05.000000')",
        f"INSERT INTO workspace_memberships (workspace_id, user_id, role, created_at) VALUES ('{W}','{U}','owner','2026-01-02 03:04:05.000000')",
        "INSERT INTO telegram_links (chat_id, email, active_agent, updated_at) VALUES (7,'t@example.com','ceo','2026-01-02 03:04:05.000000')",
    )


TABLES_BEFORE = ("agent_memories", "users", "auth_identities", "workspaces", "workspace_memberships", "telegram_links")


def snapshot(db):
    return {t: db.rows(f"SELECT * FROM {t} ORDER BY 1") for t in TABLES_BEFORE}


# ------------------------------------------------------------------------------------------------ revision

def test_revision_graph_and_file_conventions():
    script = ScriptDirectory.from_config(make_cfg())
    assert script.get_heads() == ["0018_agent_slug_per_user"]  # M2-B, M3-A, M3-B and M4-A1 sit on top of 0005
    rev = script.get_revision(AGENTS)
    assert rev.revision == "0005_custom_agents" and rev.down_revision == OWNERSHIP
    assert rev.branch_labels == set() and rev.dependencies is None
    assert len(AGENTS) <= 32  # alembic_version.version_num is VARCHAR(32)
    source = VERSION_FILE.read_text(encoding="utf-8")
    assert 'revision: str = "0005_custom_agents"' in source
    # additive only: no statement touches an existing table
    assert not re.search(r"ALTER TABLE|op\.(add_column|drop_column|alter_column|batch_alter_table)", source)
    for existing in ("agent_memories", "telegram_links"):
        assert existing not in source.replace("agent_memories_", "")
    assert "DROP TABLE users" not in source


def test_check_expressions_in_the_migration_match_the_models(import_fresh):
    models = fresh_models(import_fresh)
    source = re.sub(r"\s+", "", VERSION_FILE.read_text(encoding="utf-8"))
    for expression in (models.AGENT_STATUS_CHECK, models.AGENT_LATEST_VERSION_CHECK, models.AGENT_CURRENT_VERSION_CHECK,
                       models.AGENT_TEMPLATE_PAIR_CHECK, models.AGENT_VERSION_NUMBER_CHECK, models.AGENT_VERSION_RESTORED_CHECK):
        assert re.sub(r"\s+", "", expression) in source


# ------------------------------------------------------------------------------------------------ upgrade

def test_upgrade_adds_exactly_the_new_objects_and_keeps_every_existing_row(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade(OWNERSHIP)
    seed(db)
    before_master, before_rows = db.master(), snapshot(db)
    db.upgrade(AGENTS)
    assert snapshot(db) == before_rows  # no existing row changed
    after = db.master()
    old_ddl = {(t, n): sql for t, n, _tbl, sql in before_master}
    for t, n, _tbl, sql in after:
        if (t, n) in old_ddl:
            assert sql == old_ddl[(t, n)], f"{t} {n} was altered"  # every pre-existing object is byte-for-byte unchanged
    added = {(t, n) for t, n, _tbl, _sql in after} - set(old_ddl)
    names = {n for _t, n in added}
    assert {"custom_agents", "custom_agent_versions", "ix_custom_agents_workspace_id_user_id",
            "custom_agent_versions_no_update", "custom_agent_versions_no_delete"} <= names
    assert {n for t, n in added if t == "table"} == NEW_TABLES
    assert db.rows("SELECT version_num FROM alembic_version") == [(AGENTS,)]
    assert db.diffs(models.Base.metadata, exclude_tables=("agent_executions", "plans", "plan_revisions", "plan_tasks", "plan_task_dependencies", "plan_runs", "plan_task_runs", "run_leases", "execution_events", "plan_run_retries", "plan_task_carryovers", "worker_processes", "execution_owners", "tool_invocations", "tool_approvals", "tool_suspensions", "model_usage", "notifications", "provider_events", "tool_dispatches", "tool_reconciliations", "workspace_invitations")) == []  # equals the live models (later tables not yet created)
    assert "ix_custom_agents_workspace_id_user_id" in {r[0] for r in db.rows("SELECT name FROM pragma_index_list('custom_agents')")}


def test_upgrade_from_an_empty_chain_reaches_the_same_schema(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade()
    assert db.rows("SELECT version_num FROM alembic_version") == [("0018_agent_slug_per_user",)]
    assert db.rows("SELECT count(*) FROM custom_agents") == [(0,)]
    assert db.diffs(models.Base.metadata) == []


# ------------------------------------------------------------------------------------------------ constraints

def agent_sql(**o):
    v = {"id": uuid.uuid4().hex, "user_id": U, "workspace_id": W, "slug": "my-agent", "status": "active", "latest_version": 1,
         "current_version": 1, "template_id": None, "template_version": None, **o}
    cols = ", ".join(v)
    vals = ", ".join("NULL" if x is None else (f"'{x}'" if isinstance(x, str) else str(x)) for x in v.values())
    return f"INSERT INTO custom_agents ({cols}, created_at, updated_at) VALUES ({vals}, '2026-01-01 00:00:00.000000', '2026-01-01 00:00:00.000000')", v["id"]


def version_sql(agent_id, **o):
    v = {"id": uuid.uuid4().hex, "agent_id": agent_id, "version": 1, "content_hash": "h" * 64, "restored_from_version": None, "created_by": U, **o}
    cols = ", ".join(v)
    vals = ", ".join("NULL" if x is None else (f"'{x}'" if isinstance(x, str) else str(x)) for x in v.values())
    return f"INSERT INTO custom_agent_versions ({cols}, definition, created_at) VALUES ({vals}, '{{}}', '2026-01-01 00:00:00.000000')"


@pytest.fixture
def migrated(db):
    db.upgrade()
    seed(db)
    return db


def refused(db, *statements, expect=None):
    engine = db.engine(fk=True)
    try:
        with pytest.raises(IntegrityError) as info:
            with engine.begin() as conn:
                for statement in statements:
                    conn.exec_driver_sql(statement)
        if expect:
            assert expect.upper() in str(info.value).upper()
    finally:
        engine.dispose()


def accepted(db, *statements):
    engine = db.engine(fk=True)
    try:
        with engine.begin() as conn:
            for statement in statements:
                conn.exec_driver_sql(statement)
    finally:
        engine.dispose()


def test_agent_row_constraints(migrated):
    db = migrated
    sql, _ = agent_sql()
    accepted(db, sql)
    refused(db, agent_sql(slug="my-agent")[0], expect="UNIQUE")  # one slug per workspace
    refused(db, agent_sql(slug="other", status="deleted")[0], expect="CHECK")
    refused(db, agent_sql(slug="other", latest_version=0, current_version=0)[0], expect="CHECK")
    refused(db, agent_sql(slug="other", latest_version=2, current_version=3)[0], expect="CHECK")  # current beyond latest
    refused(db, agent_sql(slug="other", latest_version=2, current_version=0)[0], expect="CHECK")
    refused(db, agent_sql(slug="other", template_id="sales-outreach")[0], expect="CHECK")  # half a template pair
    refused(db, agent_sql(slug="other", template_version=1)[0], expect="CHECK")
    accepted(db, agent_sql(slug="templated", template_id="sales-outreach", template_version=1)[0])
    accepted(db, agent_sql(slug="two-versions", latest_version=2, current_version=1)[0])
    refused(db, agent_sql(slug="orphan-user", user_id=uuid.uuid4().hex)[0], expect="FOREIGN KEY")
    refused(db, agent_sql(slug="orphan-ws", workspace_id=uuid.uuid4().hex)[0], expect="FOREIGN KEY")
    for column in ("user_id", "workspace_id", "slug", "status", "latest_version", "current_version"):
        refused(db, agent_sql(**{"slug": "nulls", column: None})[0], expect="NOT NULL")
    assert db.rows("SELECT slug FROM custom_agents ORDER BY slug") == [("my-agent",), ("templated",), ("two-versions",)]


def test_version_row_constraints_and_the_same_slug_in_another_workspace(migrated):
    db = migrated
    second_ws = uuid.uuid4().hex
    accepted(db, f"INSERT INTO workspaces (id, name, is_personal, personal_for_user_id, created_at) VALUES ('{second_ws}','T',0,NULL,'2026-01-01 00:00:00.000000')")
    sql, agent = agent_sql()
    accepted(db, sql, agent_sql(workspace_id=second_ws)[0])  # same slug, different workspace: allowed
    accepted(db, version_sql(agent, version=1))
    refused(db, version_sql(agent, version=1), expect="UNIQUE")
    refused(db, version_sql(agent, version=0), expect="CHECK")
    refused(db, version_sql(agent, version=2, restored_from_version=2), expect="CHECK")  # must restore an EARLIER version
    refused(db, version_sql(agent, version=2, restored_from_version=0), expect="CHECK")
    refused(db, version_sql(agent, version=2, restored_from_version=5), expect="CHECK")
    accepted(db, version_sql(agent, version=2, restored_from_version=1))
    refused(db, version_sql(uuid.uuid4().hex, version=1), expect="FOREIGN KEY")
    refused(db, version_sql(agent, version=3, created_by=uuid.uuid4().hex), expect="FOREIGN KEY")
    refused(db, "INSERT INTO custom_agent_versions (id, agent_id, version, content_hash, created_by, created_at) "
                f"VALUES ('{uuid.uuid4().hex}','{agent}',9,'h','{U}','2026-01-01 00:00:00.000000')", expect="NOT NULL")  # definition required
    assert db.rows(f"SELECT version FROM custom_agent_versions WHERE agent_id = '{agent}' ORDER BY version") == [(1,), (2,)]


def test_no_cascades_existing_parents_cannot_be_deleted_from_under_an_agent(migrated):
    db = migrated
    sql, agent = agent_sql()
    accepted(db, sql, version_sql(agent))
    refused(db, f"DELETE FROM users WHERE id = '{U}'", expect="FOREIGN KEY")
    refused(db, f"DELETE FROM workspaces WHERE id = '{W}'", expect="FOREIGN KEY")
    refused(db, f"DELETE FROM custom_agents WHERE id = '{agent}'", expect="FOREIGN KEY")  # its version still references it


def test_version_rows_are_insert_only(migrated):
    db = migrated
    sql, agent = agent_sql()
    accepted(db, sql, version_sql(agent))
    for statement in ("UPDATE custom_agent_versions SET change_note = 'x'", "UPDATE custom_agent_versions SET definition = '{\"a\": 1}'",
                      "UPDATE custom_agent_versions SET version = 7", "DELETE FROM custom_agent_versions"):
        refused(db, statement, expect="immutable")
    accepted(db, version_sql(agent, version=2))  # inserts stay possible
    assert db.rows("SELECT count(*) FROM custom_agent_versions") == [(2,)]
    accepted(db, "UPDATE custom_agents SET updated_at = '2026-02-02 00:00:00.000000'")  # the agent row itself is mutable


# ------------------------------------------------------------------------------------------------ downgrade

def test_downgrade_restores_the_exact_previous_schema_keeps_rows_and_reupgrade_is_identical(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade(OWNERSHIP)
    seed(db)
    before_master, before_rows = db.master(), snapshot(db)
    db.upgrade(AGENTS)
    head_master = db.master()
    assert head_master != before_master
    sql, agent = agent_sql()
    accepted(db, sql, version_sql(agent))
    db.downgrade(OWNERSHIP)  # development/test behaviour; production is forward-only
    assert db.master() == before_master  # every object, byte for byte, as at 0004 (tables, indexes AND triggers gone)
    assert snapshot(db) == before_rows  # no pre-existing row lost or changed
    assert db.rows("SELECT version_num FROM alembic_version") == [(OWNERSHIP,)]
    assert db.diffs(models.Base.metadata, exclude_ownership=True) == []  # the 0004 schema again (0005 objects are excluded)
    db.upgrade(AGENTS)  # re-upgrade to the same revision (0006 has its own tests)
    assert db.master() == head_master
    assert db.rows("SELECT count(*) FROM custom_agents") == [(0,)]
    db.upgrade()
    assert db.diffs(models.Base.metadata) == []
