"""Baseline revision 0001_baseline (T1-E).

Nothing here touches a configured database. Offline tests only generate SQL; structural tests
call the revision's upgrade()/downgrade() in-process on a throw-away SQLite file in tmp_path.
SQLite is NOT PostgreSQL: PostgreSQL runtime parity (SERIAL/BIGSERIAL sequences, PK names,
reflection) is unverified; the PostgreSQL checks below compare compiled/offline DDL text only.
"""

import ast
import io
import re
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from baseline_support import baseline_metadata

BACKEND = Path(__file__).resolve().parent.parent
INI = BACKEND / "alembic.ini"
REVISION = "0001_baseline"
VERSION_FILE = BACKEND / "migrations" / "versions" / "0001_baseline.py"


def real_config(output=None) -> Config:
    cfg = Config(str(INI), output_buffer=output)
    cfg.attributes["configure_logger"] = False
    return cfg


def baseline_module():
    return ScriptDirectory.from_config(real_config()).get_revision(REVISION).module


def sqlite_engine(tmp_path):
    return create_engine(f"sqlite:///{(tmp_path / 'baseline.db').as_posix()}")


def squash(sql: str) -> str:
    return re.sub(r"\s+", "", sql)


# ------------------------------------------------------------------ discovery


def test_revision_is_discoverable_stable_and_the_root():
    script = ScriptDirectory.from_config(real_config())
    assert script.get_bases() == [REVISION]  # still the only root
    assert script.get_heads() == ["0017_invitations"]  # the baseline is no longer the head (M1-A, M1-B, M1-C, M2-A, M2-B, M3-A, M3-B, M4-A1)
    rev = script.get_revision(REVISION)
    assert rev.revision == "0001_baseline" and rev.down_revision is None
    assert rev.branch_labels == set() and rev.dependencies is None
    assert [r.revision for r in script.walk_revisions()] == [
        "0017_invitations", "0016_durable_dispatch", "0015_resilience_required_action", "0014_usage_notifications", "0013_suspension", "0012_approvals", "0011_tools", "0010_orchestration", "0009_run_leases", "0008_plan_runs", "0007_plans", "0006_agent_executions", "0005_custom_agents", "0004_memory_ownership", "0003_workspaces", "0002_identity", REVISION]
    assert script.get_revision("0002_identity").down_revision == REVISION
    assert script.get_revision("0003_workspaces").down_revision == "0002_identity"
    assert script.get_revision("0004_memory_ownership").down_revision == "0003_workspaces"
    assert script.get_revision("0005_custom_agents").down_revision == "0004_memory_ownership"
    assert script.get_revision("0006_agent_executions").down_revision == "0005_custom_agents"
    assert script.get_revision("0007_plans").down_revision == "0006_agent_executions"
    assert script.get_revision("0008_plan_runs").down_revision == "0007_plans"
    assert script.get_revision("0009_run_leases").down_revision == "0008_plan_runs"
    assert script.get_revision("0010_orchestration").down_revision == "0009_run_leases"
    assert script.get_revision("0011_tools").down_revision == "0010_orchestration"
    assert script.get_revision("0012_approvals").down_revision == "0011_tools"
    assert script.get_revision("0013_suspension").down_revision == "0012_approvals"
    assert script.get_revision("0014_usage_notifications").down_revision == "0013_suspension"
    assert script.get_revision("0015_resilience_required_action").down_revision == "0014_usage_notifications"
    assert script.get_revision("0016_durable_dispatch").down_revision == "0015_resilience_required_action"
    assert len(REVISION) <= 32  # alembic_version.version_num is VARCHAR(32)
    assert Path(rev.path).resolve() == VERSION_FILE.resolve()


# ------------------------------------------------- offline SQL (real env + revision)


def _offline_sql() -> str:
    """Offline SQL for the BASELINE revision only (explicit target; later revisions are tested elsewhere)."""
    out = io.StringIO()
    command.upgrade(real_config(out), f"base:{REVISION}", sql=True)
    return out.getvalue()


def _metadata_statements(metadata) -> set[str]:
    dialect = postgresql.dialect()
    found = set()
    for table in metadata.tables.values():
        found.add(squash(str(CreateTable(table).compile(dialect=dialect))))
        found.update(squash(str(CreateIndex(ix).compile(dialect=dialect))) for ix in table.indexes)
    return found


def test_offline_sql_matches_metadata_ddl_exactly(capsys):
    sql = _offline_sql()
    code = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))  # drop comments
    app_statements = {
        squash(s) for s in code.split(";")
        if re.search(r"CREATE (UNIQUE )?(TABLE|INDEX) (?!alembic_version)", s)
    }
    assert app_statements == _metadata_statements(baseline_metadata())  # frozen baseline metadata
    # The version table is Alembic's own and is recorded after the schema.
    assert "CREATE TABLE alembic_version" in sql and "INSERT INTO alembic_version" in sql
    assert "'0001_baseline'" in sql
    out = capsys.readouterr()
    assert "postgresql://" not in sql + out.out + out.err


def test_offline_sql_has_expected_definitions_and_no_unapproved_objects():
    sql = _offline_sql()
    tables = {m.group(1): m.group(0) for m in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", sql, re.S)}
    assert set(tables) == {"alembic_version", "agent_memories", "telegram_links"}
    am, tl = squash(tables["agent_memories"]), squash(tables["telegram_links"])
    assert "idSERIALNOTNULL" in am and "agent_nameVARCHAR(100)NOTNULL" in am
    assert "user_emailVARCHAR(255)," in am and "summaryTEXTNOTNULL" in am
    assert "created_atTIMESTAMPWITHOUTTIMEZONENOTNULL" in am and "PRIMARYKEY(id)" in am
    assert "chat_idBIGSERIALNOTNULL" in tl and "emailVARCHAR(255)," in tl
    assert "active_agentVARCHAR(100)NOTNULL" in tl and "updated_atTIMESTAMPWITHOUTTIMEZONENOTNULL" in tl
    assert "PRIMARYKEY(chat_id)" in tl
    assert "CREATE INDEX ix_agent_memories_agent_name ON agent_memories (agent_name)" in sql
    assert "CREATE INDEX ix_agent_memories_user_email ON agent_memories (user_email)" in sql
    assert len(re.findall(r"CREATE (UNIQUE )?INDEX", sql)) == 2 and "UNIQUE" not in sql
    for name in ("agent_memories", "telegram_links"):
        body = tables[name]
        for forbidden in ("DEFAULT", "FOREIGN", "REFERENCES", "UNIQUE", "CHECK", "CONSTRAINT"):
            assert forbidden not in body, (name, forbidden)
    assert not re.search(r"\b(DROP|DELETE|TRUNCATE|ALTER)\b", sql)


# -------------------------------------- structural checks on SQLite (not PostgreSQL)


def _upgrade_in_process(engine):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            baseline_module().upgrade()


def test_upgrade_matches_current_metadata_on_sqlite(tmp_path):
    baseline = baseline_metadata()  # the baseline revision is compared with the frozen baseline metadata
    engine = sqlite_engine(tmp_path)
    _upgrade_in_process(engine)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts=dict(compare_type=True, compare_server_default=True))
        assert compare_metadata(ctx, baseline) == []  # no diffs either way
        insp = inspect(conn)
        assert set(insp.get_table_names()) == {"agent_memories", "telegram_links"}
        for name, table in baseline.tables.items():
            cols = insp.get_columns(name)
            assert [c["name"] for c in cols] == [c.name for c in table.columns]  # order
            assert [c["nullable"] for c in cols if c["name"] not in ("id", "chat_id")] == [
                c.nullable for c in table.columns if c.name not in ("id", "chat_id")
            ]
            assert all(c["default"] is None for c in cols)  # no server defaults
            assert insp.get_pk_constraint(name)["constrained_columns"] == [c.name for c in table.primary_key]
            assert insp.get_foreign_keys(name) == [] and insp.get_unique_constraints(name) == []
            assert insp.get_check_constraints(name) == []
        indexes = {(i["name"], tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes("agent_memories")}
        assert indexes == {
            ("ix_agent_memories_agent_name", ("agent_name",), False),
            ("ix_agent_memories_user_email", ("user_email",), False),
        }
        assert insp.get_indexes("telegram_links") == []
    engine.dispose()


def test_revision_source_contains_no_destructive_or_data_operations():
    tree = ast.parse(VERSION_FILE.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert set(funcs) == {"upgrade", "downgrade"}

    def called(fn):
        return {c.func.attr for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}

    up = called(funcs["upgrade"])
    assert up <= {"create_table", "create_index", "f", "Column", "String", "Integer", "BigInteger", "Text",
                  "DateTime", "PrimaryKeyConstraint"}, up
    # downgrade does nothing but raise
    assert len(funcs["downgrade"].body) == 1 and isinstance(funcs["downgrade"].body[0], ast.Raise)
    assert not called(funcs["downgrade"])


# -------------------------------------------------------------- irreversible downgrade


def test_downgrade_raises_and_issues_no_statements(tmp_path):
    engine = sqlite_engine(tmp_path)
    _upgrade_in_process(engine)
    issued = []
    event.listen(engine, "before_cursor_execute", lambda conn, cur, stmt, *a: issued.append(stmt))
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="intentionally irreversible"):
                baseline_module().downgrade()
    assert issued == []
    assert set(inspect(engine).get_table_names()) == {"agent_memories", "telegram_links"}
    assert len(inspect(engine).get_indexes("agent_memories")) == 2
    engine.dispose()


def test_offline_downgrade_raises_and_emits_no_destructive_sql():
    out = io.StringIO()
    with pytest.raises(RuntimeError, match="intentionally irreversible"):
        command.downgrade(real_config(out), "0001_baseline:base", sql=True)
    assert not re.search(r"\b(DROP|DELETE|TRUNCATE|ALTER)\b", out.getvalue())


# ------------------------------------------- actual online runner (T1-E-R1), SQLite only
# Runs the real `command.upgrade` through the real env.py online path against a throw-away
# SQLite file in tmp_path. The URL reaches env.py the way production does (DATABASE_URL via
# config.get_database_url), but only through monkeypatch, so it exists for this test alone
# and is validated by the unchanged rules (sqlite is accepted only with APP_ENV=test).
# This is NOT PostgreSQL: the advisory-lock branch is skipped and PostgreSQL parity is unverified.


def test_actual_online_upgrade_on_temporary_sqlite(tmp_path, monkeypatch):
    import sqlalchemy
    from sqlalchemy.pool import NullPool

    baseline = baseline_metadata()
    db = tmp_path / "online_upgrade.db"
    assert not db.exists()
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    engines = []
    real_create_engine = sqlalchemy.create_engine

    def spy(*args, **kwargs):
        engines.append((str(args[0]), kwargs.get("poolclass")))
        return real_create_engine(*args, **kwargs)

    monkeypatch.setattr(sqlalchemy, "create_engine", spy)

    command.upgrade(real_config(), REVISION)  # the real online runner (no --sql), explicitly the baseline

    # Exactly one engine, built by env.py for the tmp_path SQLite file, with NullPool.
    assert engines == [(f"sqlite:///{db.as_posix()}", NullPool)]
    assert db.exists() and db.resolve().parent == tmp_path.resolve()

    engine = real_create_engine(f"sqlite:///{db.as_posix()}")
    try:
        with engine.connect() as conn:
            insp = inspect(conn)
            # Both application tables and nothing else besides Alembic's own version table.
            assert set(insp.get_table_names()) == {"agent_memories", "telegram_links", "alembic_version"}
            indexes = {i["name"] for i in insp.get_indexes("agent_memories")}
            assert indexes == {"ix_agent_memories_agent_name", "ix_agent_memories_user_email"}
            assert insp.get_indexes("telegram_links") == []
            # alembic_version holds exactly the baseline revision.
            rows = conn.exec_driver_sql("SELECT version_num FROM alembic_version").fetchall()
            assert [tuple(r) for r in rows] == [("0001_baseline",)]
            # The migrated schema equals the metadata (managed tables only; Alembic's own filtered out).
            ctx = MigrationContext.configure(
                conn,
                opts=dict(compare_type=True, compare_server_default=True,
                          include_object=lambda obj, name, type_, reflected, compare_to:
                          not (type_ == "table" and name == "alembic_version")),
            )
            assert compare_metadata(ctx, baseline) == []
    finally:
        engine.dispose()
