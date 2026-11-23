"""Read-only pre-flight classifier (T1-F). SQLite throw-away files in tmp_path only.

SQLite is NOT PostgreSQL: PostgreSQL runtime behaviour is unverified, and the PostgreSQL-only
catalog checks are exercised with mocks. Nothing here touches a configured database.
"""

import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event

from baseline_support import baseline_metadata
from migrations import preflight as pf

BACKEND = Path(__file__).resolve().parent.parent
HEADS, REVS = {"0001_baseline"}, {"0001_baseline"}


AGENT_MEMORIES_OK = (
    "CREATE TABLE agent_memories (id INTEGER NOT NULL, agent_name VARCHAR(100) NOT NULL, "
    "user_email VARCHAR(255), summary TEXT NOT NULL, created_at DATETIME NOT NULL, PRIMARY KEY (id))"
)
INDEXES = [
    "CREATE INDEX ix_agent_memories_agent_name ON agent_memories (agent_name)",
    "CREATE INDEX ix_agent_memories_user_email ON agent_memories (user_email)",
]
TELEGRAM_OK = (
    "CREATE TABLE telegram_links (chat_id BIGINT NOT NULL, email VARCHAR(255), "
    "active_agent VARCHAR(100) NOT NULL, updated_at DATETIME NOT NULL, PRIMARY KEY (chat_id))"
)


def sql(*statements):
    return list(statements)


def matching():
    return sql(AGENT_MEMORIES_OK, *INDEXES, TELEGRAM_OK)


def versioned(rows):
    return sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)",
               *[f"INSERT INTO alembic_version VALUES ({r})" for r in rows])


SCENARIOS = {
    # name: (statements, expected state, expected substring in findings)
    "empty": ([], pf.EMPTY, None),
    "match": (matching(), pf.MATCH, None),
    "historical_missing_user_email": (
        sql("CREATE TABLE agent_memories (id INTEGER NOT NULL, agent_name VARCHAR(100) NOT NULL, "
            "summary TEXT NOT NULL, created_at DATETIME NOT NULL, PRIMARY KEY (id))", INDEXES[0], TELEGRAM_OK),
        pf.DIVERGENT, "add_column: agent_memories.user_email"),
    "historical_missing_everything_plus_unmanaged": (
        sql("CREATE TABLE agent_memories (id INTEGER NOT NULL, agent_name VARCHAR(100) NOT NULL, "
            "summary TEXT NOT NULL, created_at DATETIME NOT NULL, PRIMARY KEY (id))",
            "CREATE TABLE legacy_notes (x INTEGER)"),
        pf.DIVERGENT, "add_table: telegram_links"),
    "missing_table": (sql(AGENT_MEMORIES_OK, *INDEXES), pf.DIVERGENT, "add_table: telegram_links"),
    "missing_index": (sql(AGENT_MEMORIES_OK, INDEXES[0], TELEGRAM_OK), pf.DIVERGENT, "add_index: ix_agent_memories_user_email"),
    "type_drift": (
        sql(AGENT_MEMORIES_OK.replace("VARCHAR(100)", "VARCHAR(50)"), *INDEXES, TELEGRAM_OK),
        pf.DIVERGENT, "modify_type: agent_memories.agent_name"),
    "nullability_drift": (
        sql(AGENT_MEMORIES_OK.replace("agent_name VARCHAR(100) NOT NULL", "agent_name VARCHAR(100)"), *INDEXES, TELEGRAM_OK),
        pf.DIVERGENT, "modify_nullable: agent_memories.agent_name"),
    "extra_unmanaged_table": (matching() + ["CREATE TABLE legacy_notes (x INTEGER)"], pf.DIVERGENT, "remove_table: legacy_notes"),
    "only_unmanaged_table_is_not_empty": (sql("CREATE TABLE legacy_notes (x INTEGER)"), pf.DIVERGENT, "remove_table: legacy_notes"),
    "extra_unmanaged_column": (
        sql(AGENT_MEMORIES_OK.replace("PRIMARY KEY (id))", "extra INTEGER, PRIMARY KEY (id))"), *INDEXES, TELEGRAM_OK),
        pf.DIVERGENT, "remove_column: agent_memories.extra"),
    "extra_unique_index": (matching() + ["CREATE UNIQUE INDEX ux_x ON telegram_links (email)"], pf.DIVERGENT, "remove_index: ux_x"),
    "wrong_primary_key": (
        sql(AGENT_MEMORIES_OK.replace("PRIMARY KEY (id)", "PRIMARY KEY (summary)"), *INDEXES, TELEGRAM_OK),
        pf.DIVERGENT, "primary key differs: agent_memories"),
    "extra_check_constraint": (
        sql(AGENT_MEMORIES_OK.replace("PRIMARY KEY (id))", "PRIMARY KEY (id), CHECK (id > 0))"), *INDEXES, TELEGRAM_OK),
        pf.DIVERGENT, "unexpected check constraint: agent_memories"),
    "versioned_current": (matching() + versioned(["'0001_baseline'"]), pf.VERSIONED_CURRENT, None),
    "versioned_unknown": (matching() + versioned(["'ffff'"]), pf.VERSIONED_UNKNOWN, None),
    "versioned_no_rows": (matching() + versioned([]), pf.VERSIONED_UNKNOWN, "malformed"),
    "versioned_two_rows": (matching() + versioned(["'0001_baseline'", "'ffff'"]), pf.VERSIONED_UNKNOWN, "malformed"),
    "versioned_null": (
        matching() + ["CREATE TABLE alembic_version (version_num VARCHAR(32))",
                      "INSERT INTO alembic_version VALUES (NULL)"],
        pf.VERSIONED_UNKNOWN, "malformed"),
    "versioned_wrong_column": (
        matching() + ["CREATE TABLE alembic_version (other VARCHAR(32))"], pf.VERSIONED_UNKNOWN, "malformed"),
    "versioned_alone_known": (versioned(["'0001_baseline'"]), pf.VERSIONED_CURRENT, None),
}


def build(tmp_path, statements, filename="pf.db"):
    engine = create_engine(f"sqlite:///{(tmp_path / filename).as_posix()}")
    with engine.begin() as conn:
        for s in statements:
            conn.exec_driver_sql(s)
    return engine


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_classification_scenarios(tmp_path, name):
    statements, state, finding = SCENARIOS[name]
    engine = build(tmp_path, statements)
    with engine.connect() as conn:
        report = pf.classify(conn, HEADS, REVS)
    engine.dispose()
    assert report.state == state, report
    if finding:
        assert any(finding in f for f in report.findings), report.findings
    if state in (pf.MATCH, pf.EMPTY):
        assert report.findings == ()


def test_match_comes_from_create_all_of_the_frozen_baseline_metadata(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'ca.db').as_posix()}")
    baseline_metadata().create_all(engine)  # test-only helper; never in preflight
    with engine.connect() as conn:
        assert pf.classify(conn, HEADS, REVS).state == pf.MATCH
    engine.dispose()


# M1-A.1: unversioned databases are compared with the frozen 0001_baseline schema, never the live
# models; the head-schema / baseline / CLI-output cases live in tests/test_preflight_baseline.py.


def test_known_non_head_revision_is_distinct_from_current(tmp_path):
    engine = build(tmp_path, matching() + versioned(["'0001_baseline'"]))
    with engine.connect() as conn:
        report = pf.classify(conn, {"0002_next"}, {"0001_baseline", "0002_next"})
    engine.dispose()
    assert report.state == pf.VERSIONED_BEHIND and report.exit_code == 11
    assert "not a head" in report.findings[0]


def test_real_script_directory_knows_the_revision_chain():
    assert pf.known_revisions() == ({"0018_agent_slug_per_user"}, {
        "0001_baseline", "0002_identity", "0003_workspaces", "0004_memory_ownership", "0005_custom_agents", "0006_agent_executions", "0007_plans", "0008_plan_runs", "0009_run_leases", "0010_orchestration", "0011_tools", "0012_approvals", "0013_suspension", "0014_usage_notifications", "0015_resilience_required_action", "0016_durable_dispatch", "0017_invitations", "0018_agent_slug_per_user"})


def test_exit_codes_are_the_approved_mapping():
    assert pf.EXIT_CODES == {
        pf.EMPTY: 0, pf.MATCH: 10, pf.VERSIONED_CURRENT: 11, pf.VERSIONED_BEHIND: 11,
        pf.DIVERGENT: 20, pf.VERSIONED_UNKNOWN: 21, pf.UNINSPECTABLE: 30,
    }
    assert pf.EXIT_USAGE == 2
    assert SCENARIOS["versioned_unknown"][1] != pf.MATCH


# --------------------------------------------------------------- read-only guarantee


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_only_select_and_pragma_statements_are_issued(tmp_path, name):
    engine = build(tmp_path, SCENARIOS[name][0])
    issued = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, stmt, *a: issued.append(stmt))
    with engine.connect() as conn:
        pf.classify(conn, HEADS, REVS)
    engine.dispose()
    assert issued, "expected at least the inspection reads"
    bad = [s for s in issued if not re.match(r"\s*(SELECT|PRAGMA)\b", s, re.I)]
    assert bad == []


def test_database_file_is_unchanged_by_classification(tmp_path):
    engine = build(tmp_path, matching())
    engine.dispose()
    db = tmp_path / "pf.db"
    before = db.read_bytes()
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    with engine.connect() as conn:
        pf.classify(conn, HEADS, REVS)
    engine.dispose()
    assert db.read_bytes() == before


def test_source_has_no_write_or_migration_calls_and_no_env_filter():
    src = (BACKEND / "migrations" / "preflight.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    body = code.split('"""', 2)[2]  # drop the module docstring
    for forbidden in ("create_all", "command.", "stamp(", "upgrade(", "downgrade(", "include_object",
                      "CREATE ", "DROP ", "ALTER ", "INSERT ", "UPDATE ", "DELETE ", "commit("):
        assert forbidden not in body, forbidden


# ------------------------------------------------- failures never become EMPTY / leak text


def test_inspection_failure_is_uninspectable_and_leaks_nothing(monkeypatch):
    def boom(_):
        raise RuntimeError("postgresql://user:SENTINEL-PW@host/db")

    monkeypatch.setattr(pf, "inspect", boom)
    report = pf.classify(object(), HEADS, REVS)
    assert report.state == pf.UNINSPECTABLE and report.exit_code == 30
    assert "SENTINEL" not in report.render() and "RuntimeError" in report.render()


def test_main_connection_failure_is_uninspectable_without_credentials(monkeypatch, capsys):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:SENTINEL-PW@example.invalid/db")

    def boom(*a, **k):
        raise RuntimeError("could not connect to postgresql://user:SENTINEL-PW@example.invalid")

    monkeypatch.setattr(pf, "create_engine", boom)
    assert pf.main() == 30
    out = capsys.readouterr()
    assert "UNINSPECTABLE" in out.out and "RuntimeError" in out.out
    assert "SENTINEL" not in out.out + out.err and "example.invalid" not in out.out + out.err


def test_main_unopenable_sqlite_file_is_uninspectable_not_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'no_such_dir' / 'x.db').as_posix()}")
    assert pf.main() == 30
    assert "UNINSPECTABLE" in capsys.readouterr().out


def test_main_classifies_temp_sqlite_and_reports_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert pf.main() == 0  # a brand-new SQLite file is a successful, empty inspection
    assert "EMPTY" in capsys.readouterr().out
    engine = build(tmp_path, matching(), "m.db")
    engine.dispose()
    assert pf.main() == 10


def test_main_configuration_error_is_exit_2_without_values(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_ENV", "test")
    assert pf.main() == 2
    out = capsys.readouterr()
    assert "DATABASE_URL" in out.err and out.out == ""


# ----------------------------------------------- PostgreSQL-only checks (mocks only)


class FakeConn:
    def __init__(self, schema="public", sequences=None):
        self.schema = schema
        self.sequences = sequences if sequences is not None else {
            ("agent_memories", "id"): "public.agent_memories_id_seq",
            ("telegram_links", "chat_id"): "public.telegram_links_chat_id_seq",
        }
        self.statements = []

    def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        if "current_schema" in str(stmt):
            return SimpleNamespace(scalar=lambda: self.schema)
        return SimpleNamespace(scalar=lambda: self.sequences.get((params["t"], params["c"])))


class FakeInspector:
    def __init__(self, nextval=True, pk_names=None):
        self.nextval = nextval
        self.pk_names = pk_names or {"agent_memories": "agent_memories_pkey", "telegram_links": "telegram_links_pkey"}

    def get_columns(self, table):
        col = {"agent_memories": "id", "telegram_links": "chat_id"}[table]
        default = f"nextval('{table}_{col}_seq'::regclass)" if self.nextval else None
        return [{"name": col, "default": default}]

    def get_pk_constraint(self, table):
        return {"name": self.pk_names[table]}


def test_pg_checks_clean():
    conn = FakeConn()
    assert pf.pg_checks(conn, FakeInspector()) == []
    assert all(s.lstrip().upper().startswith("SELECT") for s in conn.statements)


def test_pg_checks_report_each_problem():
    findings = pf.pg_checks(
        FakeConn(schema="other", sequences={("agent_memories", "id"): "s"}),
        FakeInspector(nextval=False, pk_names={"agent_memories": "weird_pk", "telegram_links": "telegram_links_pkey"}),
    )
    text_ = " | ".join(findings)
    assert "current schema is not public" in text_
    assert "telegram_links.chat_id has no owned sequence" in text_
    assert "agent_memories.id has no nextval default" in text_
    assert "agent_memories primary key is not named agent_memories_pkey" in text_
    assert "agent_memories.id has no owned sequence" not in text_


def test_pg_checks_run_only_on_postgresql_and_make_a_match_divergent(monkeypatch):
    calls = []
    monkeypatch.setattr(pf, "pg_checks", lambda conn, insp: calls.append(1) or ["current schema is not public"])
    from sqlalchemy import inspect as real_inspect

    class Wrap:  # real SQLite connection whose dialect name is faked
        def __init__(self, conn, dialect):
            self._c, self.dialect = conn, dialect

        def __getattr__(self, n):
            return getattr(self._c, n)

    engine = create_engine("sqlite://")
    baseline_metadata().create_all(engine)
    with engine.connect() as conn:
        insp = real_inspect(conn)
        assert pf._schema_findings(conn, insp) == [] and calls == []  # SQLite: never runs
        fake = Wrap(conn, SimpleNamespace(name="postgresql"))
        monkeypatch.setattr(pf, "MigrationContext", SimpleNamespace(configure=lambda c, opts: pf_ctx(conn, opts)))
        assert pf._schema_findings(fake, insp) == ["current schema is not public"] and calls == [1]
    engine.dispose()


def pf_ctx(conn, opts):
    from alembic.runtime.migration import MigrationContext
    return MigrationContext.configure(conn, opts=opts)


# ------------------------------------------------------------- isolation and CLI


def _run(args, env, cwd=BACKEND):
    base = {"PATH": "", "SYSTEMROOT": "C:\\Windows", "SystemDrive": "C:", "PYTHON_DOTENV_DISABLED": "1"}
    return subprocess.run([sys.executable, *args], cwd=cwd, env={**base, **env},
                          capture_output=True, text=True, timeout=60)


def test_module_cli_exit_codes_and_no_application_imports(tmp_path):
    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    env = {"APP_ENV": "test", "DATABASE_URL": url, "PYTHONPATH": str(BACKEND)}
    ok = _run(["-m", "migrations.preflight"], env)
    assert ok.returncode == 0 and "EMPTY" in ok.stdout, ok.stderr
    bad = _run(["-m", "migrations.preflight"], {"APP_ENV": "test", "PYTHONPATH": str(BACKEND)})
    assert bad.returncode == 2 and "DATABASE_URL" in bad.stderr
    probe = _run(["-c", "import sys, migrations.preflight; "
                        "print(any(m in sys.modules for m in ('memory.long_term','main','app_factory','groq')))"], env)
    assert probe.stdout.strip() == "False", probe.stderr


# ------------------------------------- T1-F-R1: PostgreSQL schema before EMPTY (mocks only)


class PgConn:
    """Stands in for a PostgreSQL connection; only current_schema() is answered."""

    def __init__(self, schema, fail=False):
        self.dialect = SimpleNamespace(name="postgresql")
        self.schema, self.fail, self.statements = schema, fail, []

    def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        if self.fail:
            raise RuntimeError("postgresql://user:SENTINEL-PW@host/db")
        return SimpleNamespace(scalar=lambda: self.schema)


class TablesInspector:
    def __init__(self, tables):
        self.tables = tables

    def get_table_names(self):
        return self.tables


def _pg_classify(monkeypatch, schema, tables, fail=False):
    conn = PgConn(schema, fail)
    monkeypatch.setattr(pf, "inspect", lambda c: TablesInspector(tables))
    reached = []
    monkeypatch.setattr(pf, "_schema_findings", lambda c, i: reached.append(1) or [])
    return pf.classify(conn, HEADS, REVS), conn, reached


def test_pg_public_schema_without_tables_is_empty(monkeypatch):
    report, conn, reached = _pg_classify(monkeypatch, "public", [])
    assert report.state == pf.EMPTY and conn.statements == ["SELECT current_schema()"] and reached == []


def test_pg_public_schema_with_tables_proceeds_to_schema_comparison(monkeypatch):
    report, _, reached = _pg_classify(monkeypatch, "public", ["agent_memories", "telegram_links"])
    assert report.state == pf.MATCH and reached == [1]  # _schema_findings stubbed to no findings


@pytest.mark.parametrize("tables", [[], ["agent_memories", "telegram_links"], ["legacy_notes"]])
def test_pg_non_public_schema_is_divergent_never_empty(monkeypatch, tables):
    report, conn, reached = _pg_classify(monkeypatch, "tenant1", tables)
    assert report.state == pf.DIVERGENT and report.exit_code == 20
    assert report.findings == ("current schema is not public",)
    assert reached == [] and all(s.startswith("SELECT") for s in conn.statements)


def test_pg_null_schema_is_divergent_not_empty(monkeypatch):
    assert _pg_classify(monkeypatch, None, [])[0].state == pf.DIVERGENT


def test_pg_schema_query_failure_is_uninspectable_not_empty(monkeypatch):
    report, _, _ = _pg_classify(monkeypatch, "public", [], fail=True)
    assert report.state == pf.UNINSPECTABLE and "SENTINEL" not in report.render()


def test_sqlite_classification_never_queries_current_schema(tmp_path):
    engine = build(tmp_path, [])
    issued = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, stmt, *a: issued.append(stmt))
    with engine.connect() as conn:
        assert pf.classify(conn, HEADS, REVS).state == pf.EMPTY
    engine.dispose()
    assert not any("current_schema" in s for s in issued)


# --------------------------------- T1-F-R1: connection cleanup in main() (mocks only)


class FakeRaw:
    def __init__(self, fail_options=False):
        self.closed, self.options, self.fail_options = False, None, fail_options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def execution_options(self, **kw):
        self.options = kw
        if self.fail_options:
            raise RuntimeError("postgresql://user:SENTINEL-PW@example.invalid/db")
        return self


class FakeEngine:
    def __init__(self, raw):
        self.raw, self.disposed = raw, False

    def connect(self):
        return self.raw

    def dispose(self):
        self.disposed = True


def _patch_engine(monkeypatch, raw):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused.db")
    engine, created = FakeEngine(raw), []
    monkeypatch.setattr(pf, "create_engine", lambda *a, **k: created.append(k) or engine)
    return engine, created


def test_main_requests_readonly_option_uses_nullpool_and_cleans_up(monkeypatch, capsys):
    from sqlalchemy.pool import NullPool

    raw = FakeRaw()
    engine, created = _patch_engine(monkeypatch, raw)
    seen = []
    monkeypatch.setattr(pf, "classify", lambda conn: seen.append(conn) or pf.Report(pf.EMPTY))
    assert pf.main() == 0
    assert raw.options == {"postgresql_readonly": True}
    assert seen == [raw] and raw.closed and engine.disposed
    assert created == [{"poolclass": NullPool}]
    assert "EMPTY" in capsys.readouterr().out


def test_main_closes_raw_connection_and_disposes_when_option_application_fails(monkeypatch, capsys):
    raw = FakeRaw(fail_options=True)
    engine, _ = _patch_engine(monkeypatch, raw)
    monkeypatch.setattr(pf, "classify", lambda conn: pytest.fail("must not classify"))
    assert pf.main() == 30
    out = capsys.readouterr()
    assert raw.closed and engine.disposed
    assert "UNINSPECTABLE" in out.out and "RuntimeError" in out.out
    assert "SENTINEL" not in out.out + out.err and "example.invalid" not in out.out + out.err


def test_main_disposes_engine_when_connect_fails(monkeypatch):
    engine, _ = _patch_engine(monkeypatch, FakeRaw())
    engine.connect = lambda: (_ for _ in ()).throw(RuntimeError("SENTINEL-PW"))
    assert pf.main() == 30 and engine.disposed


# ------------- T1-F-R2: schema guard runs before version classification (mocks + SQLite only)


class VersionedPgConn(PgConn):
    """PostgreSQL stand-in that can also answer the alembic_version SELECT, logging all calls."""

    def __init__(self, schema, rows, log, fail=False):
        super().__init__(schema, fail)
        self.rows, self.log = rows, log

    def execute(self, stmt, params=None):
        if "alembic_version" in str(stmt):
            self.log.append("version_select")
            return SimpleNamespace(fetchall=lambda: self.rows)
        self.log.append("current_schema")
        return super().execute(stmt, params)


class VersionedInspector:
    def __init__(self, log, columns=("version_num",)):
        self.log, self.columns = log, columns

    def get_table_names(self):
        self.log.append("get_table_names")
        return ["agent_memories", "telegram_links", "alembic_version"]

    def get_columns(self, table):
        self.log.append("get_columns")
        return [{"name": c} for c in self.columns]


def _versioned_pg(monkeypatch, schema, rows, fail=False):
    log = []
    monkeypatch.setattr(pf, "inspect", lambda c: VersionedInspector(log))
    conn = VersionedPgConn(schema, rows, log, fail)
    return pf.classify(conn, HEADS, REVS), log


KNOWN, UNKNOWN, MALFORMED = [("0001_baseline",)], [("ffff",)], [("0001_baseline",), ("ffff",)]


def test_public_schema_known_version_keeps_existing_classification(monkeypatch):
    report, log = _versioned_pg(monkeypatch, "public", KNOWN)
    assert report.state == pf.VERSIONED_CURRENT and report.exit_code == 11


@pytest.mark.parametrize("rows", [UNKNOWN, MALFORMED, []])
def test_public_schema_unknown_or_malformed_version_keeps_existing_classification(monkeypatch, rows):
    report, _ = _versioned_pg(monkeypatch, "public", rows)
    assert report.state == pf.VERSIONED_UNKNOWN and report.exit_code == 21


@pytest.mark.parametrize("rows", [KNOWN, UNKNOWN, MALFORMED, []])
def test_non_public_schema_with_any_version_state_is_divergent(monkeypatch, rows):
    report, log = _versioned_pg(monkeypatch, "tenant1", rows)
    assert report.state == pf.DIVERGENT and report.findings == ("current schema is not public",)
    assert "version_select" not in log and "get_table_names" not in log  # nothing inspected


@pytest.mark.parametrize("rows", [KNOWN, UNKNOWN])
def test_null_schema_with_versioned_database_is_divergent(monkeypatch, rows):
    assert _versioned_pg(monkeypatch, None, rows)[0].state == pf.DIVERGENT


@pytest.mark.parametrize("rows", [KNOWN, UNKNOWN])
def test_schema_query_failure_with_versioned_database_is_uninspectable(monkeypatch, rows):
    report, log = _versioned_pg(monkeypatch, "public", rows, fail=True)
    assert report.state == pf.UNINSPECTABLE and report.exit_code == 30
    assert "SENTINEL" not in report.render() and "RuntimeError" in report.render()
    assert "version_select" not in log and "get_table_names" not in log


def test_schema_check_happens_before_any_table_or_version_reading(monkeypatch):
    _, log = _versioned_pg(monkeypatch, "public", KNOWN)
    assert log[0] == "current_schema"
    assert log.index("current_schema") < log.index("get_table_names") < log.index("version_select")


def test_sqlite_versioned_database_never_issues_the_schema_query(tmp_path):
    engine = build(tmp_path, matching() + versioned(["'0001_baseline'"]))
    issued = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, stmt, *a: issued.append(stmt))
    with engine.connect() as conn:
        assert pf.classify(conn, HEADS, REVS).state == pf.VERSIONED_CURRENT
    engine.dispose()
    assert issued and not any("current_schema" in s for s in issued)
