"""Startup schema-version guard (T1-H). SQLite files in tmp_path and mocks only.

SQLite is NOT PostgreSQL: PostgreSQL runtime behaviour (current_schema(), postgresql_readonly,
connect_timeout, real driver errors) is unverified; those paths are exercised with mocks.
"""

import inspect as pyinspect
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.pool import NullPool

from migrations import schema_guard as sg
from migrations.schema_guard import SchemaVersionError, verify_schema_version

BACKEND = Path(__file__).resolve().parent.parent
SENTINEL = "SENTINEL-PW-must-never-appear"
# The real head of the migration chain. It moves with every migration, so it is read from the script
# directory; tests that need a fixed chain use toy script directories (literal revisions below).
HEAD = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini"))).get_current_head()


# ------------------------------------------------------------------ helpers


def toy_ini(tmp_path, revisions) -> Path:
    """An alembic.ini + versions/ with the given [(revision, down_revision_or_None)]."""
    root = tmp_path / "toy"
    (root / "migrations" / "versions").mkdir(parents=True)
    (root / "alembic.ini").write_text("[alembic]\nscript_location = %(here)s/migrations\n", encoding="utf-8")
    for rev, down in revisions:
        (root / "migrations" / "versions" / f"{rev}.py").write_text(
            f'revision = "{rev}"\ndown_revision = {down!r}\nbranch_labels = None\ndepends_on = None\n'
            "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n",
            encoding="utf-8",
        )
    return root / "alembic.ini"


def db_url(tmp_path, name="g.db") -> str:
    return f"sqlite:///{(tmp_path / name).as_posix()}"


def make_db(tmp_path, statements=(), name="g.db") -> str:
    url = db_url(tmp_path, name)
    engine = create_engine(url)
    with engine.begin() as conn:
        for s in statements:
            conn.exec_driver_sql(s)
    engine.dispose()
    return url


VERSION = "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"


def versioned(*revs):
    return [VERSION, *[f"INSERT INTO alembic_version VALUES ('{r}')" for r in revs]]


def code_of(url, **kw) -> str:
    with pytest.raises(SchemaVersionError) as exc:
        verify_schema_version(url, **kw)
    return exc.value.code


# ------------------------------------------------- acceptance and fail-closed cases


def test_accepts_exactly_one_known_revision_equal_to_the_single_head(tmp_path):
    assert verify_schema_version(make_db(tmp_path, versioned(HEAD))) == HEAD


def test_real_alembic_upgrade_head_is_accepted_and_the_guard_writes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "real.db"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    before = db.read_bytes()
    assert verify_schema_version(f"sqlite:///{db.as_posix()}") == HEAD
    assert db.read_bytes() == before


@pytest.mark.parametrize(
    "statements, code",
    [
        ([], "NO_SCHEMA_VERSION"),  # brand-new empty database
        (["CREATE TABLE agent_memories (id INTEGER)"], "NO_SCHEMA_VERSION"),  # tables, never versioned
        ([VERSION], "NO_SCHEMA_VERSION"),  # version table, zero rows
        (versioned(HEAD, "ffff"), "MULTIPLE_VERSION_ROWS"),
        (versioned(HEAD, HEAD), "MULTIPLE_VERSION_ROWS"),  # duplicate rows (no PK on the fixture table)
        (versioned("ffff"), "UNKNOWN_REVISION"),
        (["CREATE TABLE alembic_version (version_num VARCHAR(32))", "INSERT INTO alembic_version VALUES (NULL)"],
         "SCHEMA_VERSION_UNREADABLE"),
        (["CREATE TABLE alembic_version (version_num VARCHAR(32))", "INSERT INTO alembic_version VALUES ('')"],
         "SCHEMA_VERSION_UNREADABLE"),  # an empty revision is invalid, never "known"
        (["CREATE TABLE alembic_version (other VARCHAR(32))"], "SCHEMA_VERSION_UNREADABLE"),
    ],
)
def test_fails_closed_on_every_unacceptable_version_state(tmp_path, statements, code):
    assert code_of(make_db(tmp_path, statements)) == code


def test_known_but_non_head_revision_is_behind(tmp_path):
    ini = toy_ini(tmp_path, [("0001_baseline", None), ("0002_next", "0001_baseline")])
    assert code_of(make_db(tmp_path, versioned("0001_baseline")), ini_path=ini) == "SCHEMA_BEHIND_HEAD"
    assert verify_schema_version(make_db(tmp_path, versioned("0002_next"), "b.db"), ini_path=ini) == "0002_next"


def test_database_ahead_of_the_scripts_is_an_unknown_revision(tmp_path):
    ini = toy_ini(tmp_path, [("0001_baseline", None)])
    assert code_of(make_db(tmp_path, versioned("0002_next")), ini_path=ini) == "UNKNOWN_REVISION"


@pytest.mark.parametrize(
    "revisions",
    [[], [("a1", None), ("b1", None)]],  # no scripts; two independent heads
)
def test_script_directory_must_have_exactly_one_head(tmp_path, revisions):
    ini = toy_ini(tmp_path, revisions)
    # Even a database recording one of the revisions is refused (and no connection is attempted).
    called = []
    code = code_of(make_db(tmp_path, versioned("a1")), ini_path=ini,
                   engine_factory=lambda *a, **k: called.append(1) or create_engine("sqlite://"))
    assert code == "SCRIPT_HEADS_INVALID" and called == []


def test_missing_or_broken_script_directory_fails_closed(tmp_path):
    url = make_db(tmp_path, versioned(HEAD))
    assert code_of(url, ini_path=tmp_path / "nope.ini") == "SCRIPT_HEADS_INVALID"
    (tmp_path / "bad.ini").write_text("[alembic]\nscript_location = %(here)s/does_not_exist\n", encoding="utf-8")
    assert code_of(url, ini_path=tmp_path / "bad.ini") == "SCRIPT_HEADS_INVALID"


def test_unreachable_database_fails_closed_not_as_empty(tmp_path):
    assert code_of(f"sqlite:///{(tmp_path / 'no_such_dir' / 'x.db').as_posix()}") == "DB_UNREACHABLE"


def test_behaviour_is_environment_independent_by_construction():
    assert list(pyinspect.signature(verify_schema_version).parameters) == [
        "database_url", "ini_path", "engine_factory"]  # no APP_ENV / environment input


# --------------------------------------------------- read-only and credential safety


def test_guard_issues_only_selects_and_creates_nothing_on_an_empty_database(tmp_path):
    url = make_db(tmp_path)
    issued = []
    engines = []

    def factory(*a, **k):
        engine = create_engine(*a, **k)
        engines.append(engine)
        event.listen(engine, "before_cursor_execute", lambda c, cur, stmt, *x: issued.append(stmt))
        return engine

    assert code_of(url, engine_factory=factory) == "NO_SCHEMA_VERSION"
    assert issued and all(re.match(r"\s*(SELECT|PRAGMA)\b", s, re.I) for s in issued)
    assert inspect(create_engine(url)).get_table_names() == []  # no alembic_version created


def test_database_file_unchanged_for_every_failure_and_success(tmp_path):
    for i, statements in enumerate([versioned(HEAD), versioned("ffff"), [VERSION], versioned(HEAD, "x")]):
        url = make_db(tmp_path, statements, f"d{i}.db")
        path = tmp_path / f"d{i}.db"
        before = path.read_bytes()
        try:
            verify_schema_version(url)
        except SchemaVersionError:
            pass
        assert path.read_bytes() == before


def test_postgres_style_failures_never_leak_url_or_driver_text(tmp_path, caplog, capsys):
    secret_url = f"postgresql://user:{SENTINEL}@db-host.invalid/appdb"

    def boom(*a, **k):
        raise RuntimeError(f"could not connect to {secret_url}")

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SchemaVersionError) as exc:
            verify_schema_version(secret_url, engine_factory=boom)
    out = capsys.readouterr()
    shown = "|".join([str(exc.value), repr(exc.value), caplog.text, out.out, out.err])
    assert exc.value.code == "DB_UNREACHABLE" and exc.value.error_class == "RuntimeError"
    assert SENTINEL not in shown and "db-host" not in shown and "appdb" not in shown
    assert exc.value.__cause__ is None and exc.value.__suppress_context__ is True


# --------------------------------------- PostgreSQL branch, connection handling (mocks only)


class FakeRaw:
    def __init__(self, log, schema="public", fail_options=False, fail_schema=False, dialect="postgresql"):
        self.log, self.schema = log, schema
        self.fail_options, self.fail_schema = fail_options, fail_schema
        self.dialect = SimpleNamespace(name=dialect)
        self.closed, self.options = False, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def execution_options(self, **kw):
        self.options = kw
        if self.fail_options:
            raise RuntimeError(f"postgresql://u:{SENTINEL}@h/db")
        return self

    def execute(self, stmt, params=None):
        assert str(stmt) == "SELECT current_schema()"
        self.log.append("current_schema")
        if self.fail_schema:
            raise RuntimeError(f"postgresql://u:{SENTINEL}@h/db")
        return SimpleNamespace(scalar=lambda: self.schema)


class FakeEngine:
    def __init__(self, raw):
        self.raw, self.disposed = raw, False

    def connect(self):
        return self.raw

    def dispose(self):
        self.disposed = True


def pg_setup(monkeypatch, tmp_path, recorded=(HEAD,), **raw_kw):
    log, factory_calls = [], []
    raw = FakeRaw(log, **raw_kw)
    engine = FakeEngine(raw)

    class FakeContext:
        @staticmethod
        def configure(conn):
            log.append("configure")
            return SimpleNamespace(get_current_heads=lambda: log.append("get_current_heads") or tuple(recorded))

    monkeypatch.setattr(sg, "MigrationContext", FakeContext)

    def factory(url, **kw):
        factory_calls.append((url, kw))
        return engine

    return raw, engine, log, factory, factory_calls


PG_URL = f"postgresql://user:{SENTINEL}@db-host.invalid/appdb"


def test_postgres_public_schema_with_head_is_accepted_with_timeout_nullpool_readonly(monkeypatch, tmp_path):
    raw, engine, log, factory, calls = pg_setup(monkeypatch, tmp_path)
    assert verify_schema_version(PG_URL, engine_factory=factory) == HEAD
    assert calls == [(PG_URL, {"poolclass": NullPool, "connect_args": {"connect_timeout": 10}})]
    assert raw.options == {"postgresql_readonly": True} and raw.closed and engine.disposed


def test_sqlite_gets_no_connect_timeout(tmp_path):
    calls = []
    url = make_db(tmp_path, versioned(HEAD))

    def factory(u, **kw):
        calls.append(kw)
        return create_engine(u, **kw)

    verify_schema_version(url, engine_factory=factory)
    assert calls == [{"poolclass": NullPool}]


@pytest.mark.parametrize("schema", ["tenant1", None, ""])
@pytest.mark.parametrize("recorded", [(HEAD,), ("ffff",), (), (HEAD, "x")])
def test_non_public_or_null_schema_fails_closed_for_every_version_state(monkeypatch, tmp_path, schema, recorded):
    raw, engine, log, factory, _ = pg_setup(monkeypatch, tmp_path, recorded=recorded, schema=schema)
    assert code_of(PG_URL, engine_factory=factory) == "NON_PUBLIC_SCHEMA"
    assert log == ["current_schema"]  # alembic_version was never looked at
    assert raw.closed and engine.disposed


def test_schema_check_runs_before_the_version_is_read(monkeypatch, tmp_path):
    _, _, log, factory, _ = pg_setup(monkeypatch, tmp_path)
    verify_schema_version(PG_URL, engine_factory=factory)
    assert log == ["current_schema", "configure", "get_current_heads"]


def test_schema_query_failure_is_sanitized_and_blocks_the_version_read(monkeypatch, tmp_path, caplog, capsys):
    raw, engine, log, factory, _ = pg_setup(monkeypatch, tmp_path, fail_schema=True)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SchemaVersionError) as exc:
            verify_schema_version(PG_URL, engine_factory=factory)
    out = capsys.readouterr()
    assert exc.value.code == "SCHEMA_CHECK_FAILED" and log == ["current_schema"]
    assert SENTINEL not in "|".join([str(exc.value), caplog.text, out.out, out.err])
    assert raw.closed and engine.disposed


def test_failure_applying_the_readonly_option_still_closes_connection_and_engine(monkeypatch, tmp_path):
    raw, engine, log, factory, _ = pg_setup(monkeypatch, tmp_path, fail_options=True)
    with pytest.raises(SchemaVersionError) as exc:
        verify_schema_version(PG_URL, engine_factory=factory)
    assert exc.value.code == "SCHEMA_CHECK_FAILED" and SENTINEL not in str(exc.value)
    assert raw.closed and engine.disposed and log == []


def test_connect_failure_disposes_the_engine(monkeypatch, tmp_path):
    engine = FakeEngine(None)
    engine.connect = lambda: (_ for _ in ()).throw(RuntimeError(SENTINEL))
    with pytest.raises(SchemaVersionError) as exc:
        verify_schema_version(PG_URL, engine_factory=lambda *a, **k: engine)
    assert exc.value.code == "DB_UNREACHABLE" and engine.disposed and SENTINEL not in str(exc.value)


def test_sqlite_never_issues_the_postgresql_schema_query(tmp_path):
    url = make_db(tmp_path, versioned(HEAD))
    issued = []

    def factory(u, **kw):
        engine = create_engine(u, **kw)
        event.listen(engine, "before_cursor_execute", lambda c, cur, stmt, *x: issued.append(stmt))
        return engine

    verify_schema_version(url, engine_factory=factory)
    assert issued and not any("current_schema" in s for s in issued)


def test_secret_str_url_is_accepted(tmp_path):
    from pydantic import SecretStr

    assert verify_schema_version(SecretStr(make_db(tmp_path, versioned(HEAD)))) == HEAD


def test_guard_source_has_no_write_or_migration_calls():
    code = Path(sg.__file__).read_text(encoding="utf-8").split('"""', 2)[2]
    for forbidden in ("create_all", "command.", "stamp(", "upgrade(", "downgrade(", "commit(",
                      "CREATE ", "DROP ", "ALTER ", "INSERT ", "UPDATE ", "DELETE ", "_ensure_version_table"):
        assert forbidden not in code, forbidden
