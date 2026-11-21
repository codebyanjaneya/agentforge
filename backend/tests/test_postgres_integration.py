"""PostgreSQL runtime verification (T1-I). Opt-in: needs a DISPOSABLE PostgreSQL server.

Run only against a throw-away instance (e.g. a Docker container started just for this):

    AGENTFORGE_TEST_PG_ADMIN_URL=postgresql://<user>:<password>@127.0.0.1:<port>/postgres
    AGENTFORGE_TEST_PG_TOKEN=<value of the server setting agentforge.disposable>

Safety rails (all enforced before any test touches the server): the host must be loopback, the
port must not be 5432, and the server must report ``SHOW agentforge.disposable`` equal to the
token (a custom setting that only the disposable instance is started with). Every test works in
its own freshly created database, which is dropped afterwards. Without the variables the whole
module is skipped, so the normal SQLite suite is unaffected. No mocks are used here: these tests
exercise real PostgreSQL behaviour.
"""

import io
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from baseline_support import baseline_metadata
from migration_support import is_agent_platform_object, is_ownership_object
from migrations import preflight as pf
from migrations.schema_guard import SchemaVersionError, verify_schema_version

ADMIN_ENV, TOKEN_ENV = "AGENTFORGE_TEST_PG_ADMIN_URL", "AGENTFORGE_TEST_PG_TOKEN"
BACKEND = Path(__file__).resolve().parent.parent
BASELINE = "0001_baseline"
IDENTITY = "0002_identity"
# The real head of the migration chain (moves with every migration; read from the script directory).
HEAD = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini"))).get_current_head()
SENTINEL = "SENTINEL-PG-PW-must-never-appear"

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.environ.get(ADMIN_ENV), reason=f"{ADMIN_ENV} not set (no disposable PostgreSQL)"),
]

SYSTEM_SCHEMAS = "('pg_catalog','information_schema','pg_toast')"


# ----------------------------------------------------------------------- fixtures


class PgDb:
    def __init__(self, admin_url: str, name: str):
        self.name = name
        self.url_obj = make_url(admin_url).set(database=name)
        self.url = self.url_obj.render_as_string(hide_password=False)

    def url_with(self, **query) -> str:
        return self.url_obj.update_query_dict(query).render_as_string(hide_password=False)

    def engine(self):
        return create_engine(self.url)

    def rows(self, sql: str, **params):
        engine = self.engine()
        try:
            with engine.connect() as conn:
                return [tuple(r) for r in conn.execute(text(sql), params).fetchall()]
        finally:
            engine.dispose()

    def run(self, *statements: str):
        engine = self.engine()
        try:
            with engine.begin() as conn:
                for s in statements:
                    conn.exec_driver_sql(s)
        finally:
            engine.dispose()


@pytest.fixture(scope="session")
def pg_admin():
    admin_url = os.environ[ADMIN_ENV]
    url = make_url(admin_url)
    assert url.host in ("127.0.0.1", "localhost", "::1"), "refusing a non-loopback PostgreSQL host"
    assert url.port and url.port != 5432, "refusing port 5432 (never the developer's default instance)"
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    deadline = time.time() + 40
    while True:
        try:
            with engine.connect() as conn:
                marker = conn.execute(text("SHOW agentforge.disposable")).scalar()
                version = conn.execute(text("SELECT version()")).scalar()
            break
        except Exception as exc:  # the container may still be restarting after initdb
            if time.time() > deadline:
                pytest.fail(f"disposable PostgreSQL not reachable ({type(exc).__name__})")
            time.sleep(1)
    assert marker and marker == os.environ.get(TOKEN_ENV), "server is not marked as the disposable test instance"
    yield SimpleNamespace(engine=engine, url=admin_url, version=version)
    engine.dispose()


@pytest.fixture
def pg_db(pg_admin):
    name = f"af_t1i_{uuid.uuid4().hex[:12]}"
    with pg_admin.engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    db = PgDb(pg_admin.url, name)
    yield db
    with pg_admin.engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def alembic_env(monkeypatch):
    """Point env.py's online path (APP_ENV=development, PostgreSQL URL) at one database."""

    def _cfg(db: PgDb, buffer=None, script_location=None) -> Config:
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("DATABASE_URL", db.url)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        cfg = Config(str(BACKEND / "alembic.ini"), stdout=buffer or io.StringIO())
        cfg.attributes["configure_logger"] = False
        if script_location:
            cfg.set_main_option("script_location", str(script_location))
        return cfg

    return _cfg


# --------------------------------------------------------------- catalog helpers

_NOT_SYSTEM = f"NOT IN {SYSTEM_SCHEMAS}"


def shape(db: PgDb, include_version=False) -> dict:
    """Structure only (no OIDs, no data): comparable between two databases."""
    skip = "" if include_version else "AND table_name <> 'alembic_version'"
    skip_c = "" if include_version else "AND conrelid::regclass::text <> 'alembic_version'"
    skip_i = "" if include_version else "AND tablename <> 'alembic_version'"
    return {
        "columns": db.rows(
            "SELECT table_schema, table_name, column_name, data_type, character_maximum_length, is_nullable, "
            f"column_default FROM information_schema.columns WHERE table_schema {_NOT_SYSTEM} {skip} "
            "ORDER BY 1,2,ordinal_position"),
        "indexes": db.rows(
            f"SELECT schemaname, tablename, indexname, indexdef FROM pg_indexes WHERE schemaname {_NOT_SYSTEM} {skip_i} ORDER BY 1,2,3"),
        "constraints": db.rows(
            "SELECT conrelid::regclass::text, conname, contype, pg_get_constraintdef(oid) FROM pg_constraint "
            f"WHERE connamespace IN (SELECT oid FROM pg_namespace WHERE nspname {_NOT_SYSTEM}) {skip_c} ORDER BY 1,2"),
        "sequences": db.rows(
            "SELECT sequencename, data_type, start_value, min_value, max_value, increment_by, cycle, cache_size "
            f"FROM pg_sequences WHERE schemaname {_NOT_SYSTEM} AND sequencename NOT LIKE 'alembic%' ORDER BY 1"),
        "ownership": db.rows(
            "SELECT s.relname, t.relname, a.attname FROM pg_class s "
            "JOIN pg_depend d ON d.objid = s.oid AND d.deptype = 'a' "
            "JOIN pg_class t ON t.oid = d.refobjid JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid "
            "WHERE s.relkind = 'S' ORDER BY 1"),
    }


def fingerprint(db: PgDb) -> dict:
    """Everything a read-only operation must leave untouched: structure, object identity
    (OID/relfilenode: no table recreated or rewritten), sequence state and row data."""
    fp = shape(db, include_version=True)
    fp["relations"] = db.rows(
        "SELECT n.nspname, c.relname, c.relkind, c.oid::bigint, c.relfilenode::bigint FROM pg_class c "
        f"JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname {_NOT_SYSTEM} ORDER BY 1,2")
    fp["sequence_state"] = db.rows(
        f"SELECT schemaname, sequencename, last_value FROM pg_sequences WHERE schemaname {_NOT_SYSTEM} ORDER BY 1,2")
    data = {}
    for schema, table in db.rows(
            "SELECT table_schema, table_name FROM information_schema.tables "
            f"WHERE table_schema {_NOT_SYSTEM} AND table_type = 'BASE TABLE' ORDER BY 1,2"):
        data[(schema, table)] = db.rows(
            f'SELECT count(*), md5(coalesce(string_agg(t::text, \'|\' ORDER BY t::text), \'\')) FROM "{schema}"."{table}" t')
    fp["data"] = data
    return fp


def _before_ownership(skip_tables=("alembic_version",)):
    """include_object for comparing a database from BEFORE 0004 with the live metadata, minus exactly
    the M1-C objects (the ownership columns, their foreign keys and the history index) and the M2-A tables (0005)."""

    def include(obj, name, type_, reflected, compare_to):
        if type_ == "table" and name in skip_tables:
            return False
        return not (is_ownership_object(obj, name, type_) or is_agent_platform_object(obj, name, type_))

    return include


def create_baseline(db: PgDb):
    """A legacy database exactly as create_all built it at 0001_baseline (frozen baseline metadata)."""
    engine = db.engine()
    baseline_metadata().create_all(engine)  # test-only: never in preflight
    engine.dispose()


def create_all(db: PgDb):
    """A database with the LIVE (head) metadata and no alembic_version."""
    import memory.models as models

    engine = db.engine()
    models.Base.metadata.create_all(engine)  # test-only: builds the "legacy" create_all database
    engine.dispose()


def classify(db: PgDb, url=None, heads=None, revisions=None, log=None):
    """Classify exactly the way preflight.main() opens its connection."""
    engine = create_engine(url or db.url)
    if log is not None:
        event.listen(engine, "before_cursor_execute",
                     lambda c, cur, stmt, *a: log.append((stmt, cur.connection.readonly)))
    try:
        with engine.connect() as raw:
            conn = raw.execution_options(postgresql_readonly=True)
            return pf.classify(conn, heads, revisions)
    finally:
        engine.dispose()


def guard_code(url, **kw) -> str:
    with pytest.raises(SchemaVersionError) as exc:
        verify_schema_version(url, **kw)
    return exc.value.code


# ------------------------------------------------------------------ server identity


def test_server_identity(pg_admin):
    print("\nPG VERSION:", pg_admin.version.split(",")[0])
    assert pg_admin.version.startswith("PostgreSQL 16.")


# ---------------------------------------------- Alembic online upgrade on PostgreSQL


def test_online_upgrade_creates_exactly_the_baseline_schema(pg_db, alembic_env):
    command.upgrade(alembic_env(pg_db), BASELINE)  # explicit revision: later revisions are tested separately

    assert pg_db.rows("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
                      "ORDER BY 1") == [("agent_memories",), ("alembic_version",), ("telegram_links",)]
    assert pg_db.rows("SELECT version_num FROM alembic_version") == [(BASELINE,)]

    cols = pg_db.rows("SELECT table_name, column_name, data_type, character_maximum_length, is_nullable, "
                      "column_default FROM information_schema.columns WHERE table_schema = 'public' "
                      "AND table_name <> 'alembic_version' ORDER BY table_name, ordinal_position")
    by_name = {(t, c): (dt, ln, nl, df) for t, c, dt, ln, nl, df in cols}
    assert by_name[("agent_memories", "id")][:3] == ("integer", None, "NO")
    assert by_name[("agent_memories", "id")][3].startswith("nextval('agent_memories_id_seq'")
    assert by_name[("agent_memories", "agent_name")] == ("character varying", 100, "NO", None)
    assert by_name[("agent_memories", "user_email")] == ("character varying", 255, "YES", None)
    assert by_name[("agent_memories", "summary")] == ("text", None, "NO", None)
    assert by_name[("agent_memories", "created_at")] == ("timestamp without time zone", None, "NO", None)
    assert by_name[("telegram_links", "chat_id")][:3] == ("bigint", None, "NO")
    assert by_name[("telegram_links", "chat_id")][3].startswith("nextval('telegram_links_chat_id_seq'")
    assert by_name[("telegram_links", "email")] == ("character varying", 255, "YES", None)
    assert by_name[("telegram_links", "active_agent")] == ("character varying", 100, "NO", None)
    assert by_name[("telegram_links", "updated_at")] == ("timestamp without time zone", None, "NO", None)
    assert len(cols) == 9  # no other columns

    # SERIAL / BIGSERIAL: owned sequences of the right width, and working defaults.
    assert pg_db.rows("SELECT pg_get_serial_sequence('agent_memories', 'id')") == [("public.agent_memories_id_seq",)]
    assert pg_db.rows("SELECT pg_get_serial_sequence('telegram_links', 'chat_id')") == [("public.telegram_links_chat_id_seq",)]
    assert dict(pg_db.rows("SELECT sequencename, data_type FROM pg_sequences WHERE schemaname = 'public'")) == {
        "agent_memories_id_seq": "integer", "telegram_links_chat_id_seq": "bigint"}

    # Constraints: PostgreSQL-assigned primary-key names; nothing else.
    assert pg_db.rows("SELECT conrelid::regclass::text, conname, contype FROM pg_constraint "
                      "WHERE connamespace = 'public'::regnamespace ORDER BY 1") == [
        ("agent_memories", "agent_memories_pkey", "p"),
        ("alembic_version", "alembic_version_pkc", "p"),
        ("telegram_links", "telegram_links_pkey", "p"),
    ]
    indexes = dict(pg_db.rows("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"))
    assert set(indexes) == {"agent_memories_pkey", "ix_agent_memories_agent_name", "ix_agent_memories_user_email",
                            "telegram_links_pkey", "alembic_version_pkc"}
    assert "UNIQUE" not in indexes["ix_agent_memories_agent_name"] and "(agent_name)" in indexes["ix_agent_memories_agent_name"]
    assert "UNIQUE" not in indexes["ix_agent_memories_user_email"] and "(user_email)" in indexes["ix_agent_memories_user_email"]

    # The defaults really work (and Python-side defaults are NOT in the schema).
    pg_db.run("INSERT INTO agent_memories (agent_name, summary, created_at) VALUES ('ceo', 's', now())",
              "INSERT INTO telegram_links (email, active_agent, updated_at) VALUES (NULL, 'ceo', now())")
    assert pg_db.rows("SELECT id FROM agent_memories") == [(1,)]
    assert pg_db.rows("SELECT chat_id FROM telegram_links") == [(1,)]

    # The advisory lock is transaction-scoped: nothing is left held after the upgrade.
    assert pg_db.rows("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                      "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())") == [(0,)]


def test_upgrade_head_and_create_all_produce_the_same_schema(pg_db, pg_admin, alembic_env):
    command.upgrade(alembic_env(pg_db), "head")
    other = PgDb(pg_admin.url, f"af_t1i_{uuid.uuid4().hex[:12]}")
    with pg_admin.engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{other.name}"'))
    try:
        create_all(other)
        assert shape(pg_db) == shape(other)
    finally:
        with pg_admin.engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{other.name}" WITH (FORCE)'))


def test_alembic_compare_metadata_is_empty_after_upgrade_on_postgresql(pg_db, alembic_env):
    import memory.models as models

    command.upgrade(alembic_env(pg_db), "head")
    engine = pg_db.engine()
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts=dict(
                compare_type=True, compare_server_default=True,
                include_object=lambda o, n, t, r, c: not (t == "table" and n == "alembic_version")))
            assert compare_metadata(ctx, models.Base.metadata) == []
    finally:
        engine.dispose()


def test_upgrade_on_a_create_all_database_fails_and_changes_nothing(pg_db, alembic_env):
    create_all(pg_db)
    before = fingerprint(pg_db)
    with pytest.raises(Exception) as exc:
        command.upgrade(alembic_env(pg_db), "head")
    assert "already exists" in str(exc.value).lower() or "DuplicateTable" in type(exc.value).__name__ or \
        "DuplicateTable" in repr(exc.value)
    assert fingerprint(pg_db) == before  # rolled back: no alembic_version, nothing altered


def test_failed_migration_rolls_back_everything_and_releases_the_lock(pg_db, alembic_env, tmp_path):
    root = tmp_path / "toy_migrations"
    (root / "versions").mkdir(parents=True)
    (root / "env.py").write_text((BACKEND / "migrations" / "env.py").read_text(encoding="utf-8"), encoding="utf-8")
    (root / "script.py.mako").write_text((BACKEND / "migrations" / "script.py.mako").read_text(encoding="utf-8"), encoding="utf-8")
    (root / "versions" / "t1_one.py").write_text(
        'revision = "t1_one"\ndown_revision = None\nbranch_labels = None\ndepends_on = None\n'
        "import sqlalchemy as sa\nfrom alembic import op\n"
        "def upgrade():\n    op.create_table('rb_probe_one', sa.Column('id', sa.Integer(), primary_key=True))\n"
        "def downgrade():\n    pass\n", encoding="utf-8")
    (root / "versions" / "t1_two.py").write_text(
        'revision = "t1_two"\ndown_revision = "t1_one"\nbranch_labels = None\ndepends_on = None\n'
        "import sqlalchemy as sa\nfrom alembic import op\n"
        "def upgrade():\n    op.create_table('rb_probe_two', sa.Column('id', sa.Integer(), primary_key=True))\n"
        "    raise RuntimeError('deliberate failure after DDL')\n"
        "def downgrade():\n    pass\n", encoding="utf-8")
    before = fingerprint(pg_db)
    with pytest.raises(RuntimeError, match="deliberate failure"):
        command.upgrade(alembic_env(pg_db, script_location=root), "head")
    assert fingerprint(pg_db) == before  # neither probe table nor alembic_version survived
    assert pg_db.rows("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                      "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())") == [(0,)]


def test_advisory_lock_serializes_concurrent_migration_runs(pg_db, alembic_env):
    key = int(re.search(r"ADVISORY_LOCK_KEY\s*=\s*([\d_]+)",
                        (BACKEND / "migrations" / "env.py").read_text(encoding="utf-8")).group(1).replace("_", ""))
    cfg = alembic_env(pg_db)
    holder = pg_db.engine()
    result = {}
    try:
        with holder.connect() as hold:
            hold.exec_driver_sql(f"SELECT pg_advisory_lock({key})")  # another "migration run" holds the lock
            hold.commit()

            def run():
                try:
                    command.upgrade(cfg, "head")
                    result["ok"] = True
                except Exception as exc:  # pragma: no cover - reported via assertion below
                    result["error"] = repr(exc)

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            waiting = 0
            for _ in range(100):
                waiting = pg_db.rows("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")[0][0]
                if waiting:
                    break
                time.sleep(0.1)
            assert waiting == 1, "the upgrade did not block on the advisory lock"
            assert worker.is_alive() and not result
            # Blocked before any DDL: no application table exists yet.
            assert pg_db.rows("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' "
                              "AND table_name = 'agent_memories'") == [(0,)]
            hold.exec_driver_sql(f"SELECT pg_advisory_unlock({key})")  # release
            hold.commit()
            worker.join(timeout=60)
        assert result == {"ok": True}, result
        assert pg_db.rows("SELECT version_num FROM alembic_version") == [(HEAD,)]
    finally:
        holder.dispose()


# --------------------------------------------------- stamp / current / downgrade on PostgreSQL


def test_stamp_current_and_followup_upgrade_on_a_matching_database(pg_db, alembic_env):
    create_baseline(pg_db)
    app_shape_before = shape(pg_db)
    assert classify(pg_db).state == pf.MATCH  # matches the frozen baseline

    command.stamp(alembic_env(pg_db), BASELINE)  # explicit revision id, as the runbook requires
    assert pg_db.rows("SELECT version_num FROM alembic_version") == [(BASELINE,)]
    assert shape(pg_db) == app_shape_before  # stamp wrote only the version table

    out = io.StringIO()
    command.current(alembic_env(pg_db, buffer=out))
    assert BASELINE in out.getvalue() and "(head)" not in out.getvalue()  # known, but no longer the head
    print("alembic current after stamping the baseline ->", out.getvalue().strip())

    command.upgrade(alembic_env(pg_db), "head")  # applies the later revisions only
    assert classify(pg_db).state == pf.VERSIONED_CURRENT
    assert verify_schema_version(pg_db.url) == HEAD
    before = fingerprint(pg_db)
    command.upgrade(alembic_env(pg_db), "head")  # now a no-op
    assert fingerprint(pg_db) == before
    out = io.StringIO()
    command.current(alembic_env(pg_db, buffer=out))
    assert HEAD in out.getvalue() and "(head)" in out.getvalue()


def test_alembic_current_on_an_unversioned_database_is_read_only(pg_db, alembic_env):
    create_all(pg_db)
    before = fingerprint(pg_db)
    out = io.StringIO()
    command.current(alembic_env(pg_db, buffer=out))
    after = fingerprint(pg_db)
    print("\nalembic current on unversioned DB prints:", repr(out.getvalue()),
          "| alembic_version created:", ("public", "alembic_version") in after["data"])
    assert out.getvalue().strip() == ""
    assert after == before  # `current` must not create alembic_version (the runbook relies on this)


def test_baseline_downgrade_refuses_and_changes_nothing_on_postgresql(pg_db, alembic_env):
    command.upgrade(alembic_env(pg_db), "head")
    before = fingerprint(pg_db)
    with pytest.raises(RuntimeError, match="intentionally irreversible"):
        command.downgrade(alembic_env(pg_db), "base")
    assert fingerprint(pg_db) == before


# ------------------------------------------------------- read-only transaction enforcement


def _open_readonly(engine):
    raw = engine.connect()
    return raw, raw.execution_options(postgresql_readonly=True)


def test_postgresql_readonly_option_makes_the_session_read_only(pg_db):
    pg_db.run("CREATE TABLE ro_probe (id integer)")
    engine = pg_db.engine()
    try:
        raw, conn = _open_readonly(engine)
        with raw:
            assert conn.execute(text("SHOW transaction_read_only")).scalar() == "on"
            for statement in ("CREATE TABLE ro_denied (id integer)", "INSERT INTO ro_probe VALUES (1)",
                              "ALTER TABLE ro_probe ADD COLUMN x integer", "DROP TABLE ro_probe"):
                with pytest.raises(DBAPIError) as exc:
                    conn.exec_driver_sql(statement)
                assert "read-only transaction" in str(exc.value).lower(), statement
                conn.rollback()
        # Not sticky: the same engine's next (NullPool) connection can write again.
        with engine.begin() as normal:
            assert normal.execute(text("SHOW transaction_read_only")).scalar() == "off"
            normal.exec_driver_sql("INSERT INTO ro_probe VALUES (1)")
    finally:
        engine.dispose()
    assert pg_db.rows("SELECT count(*) FROM ro_probe") == [(1,)]
    assert pg_db.rows("SELECT count(*) FROM information_schema.tables WHERE table_name = 'ro_denied'") == [(0,)]


def test_readonly_option_is_reset_when_a_pooled_connection_is_returned(pg_db):
    engine = create_engine(pg_db.url, pool_size=1, max_overflow=0)  # one pooled connection, reused
    try:
        raw, conn = _open_readonly(engine)
        with raw:
            assert conn.execute(text("SHOW transaction_read_only")).scalar() == "on"
        with engine.connect() as again:
            assert again.execute(text("SHOW transaction_read_only")).scalar() == "off"
    finally:
        engine.dispose()


def test_inspection_statements_run_with_readonly_set_and_are_reads_only(pg_db):
    """Every statement the pre-flight issues through a cursor event runs with the read-only
    attribute set and is a SELECT/SHOW. LIMIT OF THIS TEST: SQLAlchemy's own first-connect dialect
    initialisation queries (version, current_schema, standard_conforming_strings) run before the
    option can be applied and do not fire cursor events, so they are NOT observed here."""
    log = []
    classify(pg_db, log=log)
    print("\nobserved statements:", len(log), "| observed before read-only was set:", [s.strip()[:50] for s, ro in log if not ro])
    assert log and all(ro for _, ro in log), "observed statements must all run with read-only set"
    assert all(re.match(r"\s*(select|show)\b", s, re.I) for s, _ in log)


# --------------------------------------------------------------- pre-flight on PostgreSQL


def test_preflight_empty_database(pg_db):
    log = []
    report = classify(pg_db, log=log)
    assert report.state == pf.EMPTY and report.exit_code == 0 and report.findings == ()
    assert all(re.match(r"\s*(select|show)\b", s, re.I) for s, _ in log)


def test_preflight_match_on_a_create_all_database(pg_db):
    create_baseline(pg_db)
    before = fingerprint(pg_db)
    report = classify(pg_db)
    print("\npreflight on create_all DB:", report.state, report.findings)
    assert report.state == pf.MATCH and report.findings == ()
    assert report.target.startswith("0001_baseline (frozen baseline schema, NOT the current head ")
    assert fingerprint(pg_db) == before  # read-only: structure, identities, sequences and data unchanged


def test_preflight_head_schema_without_a_version_table_is_divergent_on_postgresql(pg_db):
    create_all(pg_db)  # the CURRENT (head) schema, but no alembic_version
    before = fingerprint(pg_db)
    report = classify(pg_db)
    print("preflight on a head-schema DB without alembic_version:", report.state, report.findings)
    assert report.state == pf.DIVERGENT and report.exit_code == 20  # never MATCH
    assert "remove_table: users" in report.findings and "remove_table: auth_identities" in report.findings
    assert any("matches the current head schema but has no alembic_version" in f for f in report.findings)
    assert fingerprint(pg_db) == before


@pytest.mark.parametrize(
    "mutation, expected",
    [
        (["DROP INDEX ix_agent_memories_user_email", "ALTER TABLE agent_memories DROP COLUMN user_email"],
         "add_column: agent_memories.user_email"),  # the historical schema
        (["DROP TABLE telegram_links"], "add_table: telegram_links"),
        (["CREATE TABLE legacy_notes (x integer)"], "remove_table: legacy_notes"),
        (["ALTER TABLE agent_memories ADD COLUMN extra integer"], "remove_column: agent_memories.extra"),
        (["ALTER TABLE agent_memories ALTER COLUMN agent_name TYPE varchar(50)"], "modify_type: agent_memories.agent_name"),
        (["ALTER TABLE agent_memories ALTER COLUMN summary DROP NOT NULL"], "modify_nullable: agent_memories.summary"),
        (["DROP INDEX ix_agent_memories_agent_name"], "add_index: ix_agent_memories_agent_name"),
        (["CREATE UNIQUE INDEX ux_extra ON telegram_links (email)"], "remove_index: ux_extra"),
        (["ALTER TABLE agent_memories ADD CONSTRAINT chk CHECK (id > 0)"], "unexpected check constraint: agent_memories"),
        (["ALTER TABLE agent_memories RENAME CONSTRAINT agent_memories_pkey TO weird_pk"],
         "agent_memories primary key is not named agent_memories_pkey"),
        (["ALTER TABLE agent_memories ALTER COLUMN id DROP DEFAULT"], "agent_memories.id has no nextval default"),
        (["ALTER SEQUENCE telegram_links_chat_id_seq OWNED BY NONE"], "telegram_links.chat_id has no owned sequence"),
    ],
)
def test_preflight_divergent_variants_on_postgresql(pg_db, mutation, expected):
    create_baseline(pg_db)
    pg_db.run(*mutation)
    before = fingerprint(pg_db)
    report = classify(pg_db)
    print(f"\n[{expected}] ->", report.state)
    assert report.state == pf.DIVERGENT and report.exit_code == 20, report
    assert any(expected in f for f in report.findings), report.findings
    assert fingerprint(pg_db) == before


def test_preflight_versioned_states_on_postgresql(pg_db, alembic_env):
    command.upgrade(alembic_env(pg_db), "head")
    assert classify(pg_db).state == pf.VERSIONED_CURRENT
    assert classify(pg_db, heads={"0002_next"}, revisions={HEAD, "0002_next"}).state == pf.VERSIONED_BEHIND
    pg_db.run("UPDATE alembic_version SET version_num = 'ffff'")
    assert classify(pg_db).state == pf.VERSIONED_UNKNOWN
    pg_db.run(f"INSERT INTO alembic_version VALUES ('{HEAD}')")  # two rows (the PK is on version_num)
    assert classify(pg_db).state == pf.VERSIONED_UNKNOWN
    pg_db.run("DELETE FROM alembic_version")
    assert classify(pg_db).state == pf.VERSIONED_UNKNOWN


def test_preflight_current_schema_enforcement_on_postgresql(pg_db, alembic_env):
    pg_db.run("CREATE SCHEMA tenant1")
    to_tenant = pg_db.url_with(options="-csearch_path=tenant1")
    to_nowhere = pg_db.url_with(options="-csearch_path=no_such_schema")  # current_schema() is NULL
    with create_engine(to_nowhere).connect() as c:
        assert c.execute(text("SELECT current_schema()")).scalar() is None
    for url in (to_tenant, to_nowhere):  # empty in both cases: must NOT be EMPTY
        report = classify(pg_db, url=url)
        assert report.state == pf.DIVERGENT and report.findings == ("current schema is not public",), report

    command.upgrade(alembic_env(pg_db), "head")  # versioned database in public
    for url in (to_tenant, to_nowhere):  # versioned, but seen through a non-public schema
        report = classify(pg_db, url=url)
        assert report.state == pf.DIVERGENT and report.findings == ("current schema is not public",), report
    assert classify(pg_db).state == pf.VERSIONED_CURRENT  # public: unchanged behaviour


def test_preflight_cli_exit_codes_and_credential_safety_on_postgresql(pg_db):
    def run(url):
        env = {**os.environ, "APP_ENV": "development", "DATABASE_URL": url, "PYTHON_DOTENV_DISABLED": "1",
               "PYTHONPATH": str(BACKEND)}
        env.pop("GROQ_API_KEY", None)
        return subprocess.run([sys.executable, "-m", "migrations.preflight"], cwd=BACKEND, env=env,
                              capture_output=True, text=True, timeout=120)

    password = make_url(pg_db.url).password
    r = run(pg_db.url)
    assert r.returncode == 0 and "EMPTY" in r.stdout, r.stderr
    create_baseline(pg_db)
    r = run(pg_db.url)
    assert r.returncode == 10 and "MATCH" in r.stdout, r.stdout + r.stderr
    assert "compared against: 0001_baseline (frozen baseline schema, NOT the current head" in r.stdout
    pg_db.run("ALTER TABLE agent_memories DROP COLUMN user_email")
    assert run(pg_db.url).returncode == 20
    bad = make_url(pg_db.url).set(password=SENTINEL).render_as_string(hide_password=False)
    r = run(bad)
    assert r.returncode == 30 and "UNINSPECTABLE" in r.stdout
    for stream in (r.stdout, r.stderr):
        assert SENTINEL not in stream and password not in stream
        assert "127.0.0.1" not in stream and "af_disposable" not in stream and pg_db.name not in stream


# ----------------------------------------------------------------- schema guard on PostgreSQL


def test_guard_accepts_the_head_and_writes_nothing(pg_db, alembic_env):
    command.upgrade(alembic_env(pg_db), "head")
    before = fingerprint(pg_db)
    assert verify_schema_version(pg_db.url) == HEAD
    assert fingerprint(pg_db) == before


def test_guard_fail_closed_cases_on_postgresql(pg_db, alembic_env, tmp_path):
    # Empty database, then application tables without a version: both "no recorded version".
    before = fingerprint(pg_db)
    assert guard_code(pg_db.url) == "NO_SCHEMA_VERSION"
    assert fingerprint(pg_db) == before  # no alembic_version created
    create_all(pg_db)
    before = fingerprint(pg_db)
    assert guard_code(pg_db.url) == "NO_SCHEMA_VERSION"
    assert fingerprint(pg_db) == before

    command.stamp(alembic_env(pg_db), HEAD)
    assert verify_schema_version(pg_db.url) == HEAD
    pg_db.run("DELETE FROM alembic_version")
    assert guard_code(pg_db.url) == "NO_SCHEMA_VERSION"  # table present, zero rows
    pg_db.run("INSERT INTO alembic_version VALUES ('ffff')")
    assert guard_code(pg_db.url) == "UNKNOWN_REVISION"
    pg_db.run(f"INSERT INTO alembic_version VALUES ('{HEAD}')")
    assert guard_code(pg_db.url) == "MULTIPLE_VERSION_ROWS"
    pg_db.run("DELETE FROM alembic_version", f"INSERT INTO alembic_version VALUES ('{HEAD}')")

    # Known but not the head (toy script directory with a newer head).
    toy = tmp_path / "toy"
    (toy / "migrations" / "versions").mkdir(parents=True)
    (toy / "alembic.ini").write_text("[alembic]\nscript_location = %(here)s/migrations\n", encoding="utf-8")
    for rev, down in ((HEAD, None), ("0002_next", HEAD)):
        (toy / "migrations" / "versions" / f"{rev}.py").write_text(
            f'revision = "{rev}"\ndown_revision = {down!r}\nbranch_labels = None\ndepends_on = None\n'
            "def upgrade():\n    pass\ndef downgrade():\n    pass\n", encoding="utf-8")
    assert guard_code(pg_db.url, ini_path=toy / "alembic.ini") == "SCHEMA_BEHIND_HEAD"


def test_guard_current_schema_enforcement_on_postgresql(pg_db, alembic_env):
    command.upgrade(alembic_env(pg_db), "head")  # a valid, versioned database in public
    pg_db.run("CREATE SCHEMA tenant1")
    for url in (pg_db.url_with(options="-csearch_path=tenant1"),
                pg_db.url_with(options="-csearch_path=no_such_schema")):
        before = fingerprint(pg_db)
        assert guard_code(url) == "NON_PUBLIC_SCHEMA"
        assert fingerprint(pg_db) == before
    assert verify_schema_version(pg_db.url) == HEAD


def test_guard_unreachable_wrong_password_and_missing_database_are_sanitised(pg_db):
    password = make_url(pg_db.url).password
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    urls = {
        "refused": make_url(pg_db.url).set(port=closed_port).render_as_string(hide_password=False),
        "bad_password": make_url(pg_db.url).set(password=SENTINEL).render_as_string(hide_password=False),
        "no_database": make_url(pg_db.url).set(database="af_no_such_database").render_as_string(hide_password=False),
    }
    for label, url in urls.items():
        with pytest.raises(SchemaVersionError) as exc:
            verify_schema_version(url)
        shown = f"{exc.value} | {exc.value!r} | {exc.value.error_class}"
        print(f"\n{label}: code={exc.value.code} error_class={exc.value.error_class}")
        assert exc.value.code == "DB_UNREACHABLE", label
        for secret in (SENTINEL, password, "127.0.0.1", "af_disposable", pg_db.name, "af_no_such_database", str(closed_port)):
            assert secret not in shown, (label, secret)


def _awake_seconds() -> float:
    """Elapsed-time clock that does NOT count time the machine spends suspended.

    On Windows time.monotonic()/perf_counter() keep counting through (Modern) Standby, which
    once turned a 10 s timeout into a measured 4882 s (the host slept mid-test; Kernel-Power
    events 506/507 bracketed exactly that interval). QueryUnbiasedInterruptTime excludes it.
    Linux/macOS CLOCK_MONOTONIC already excludes suspend.
    """
    if sys.platform == "win32":
        import ctypes

        ticks = ctypes.c_ulonglong()
        assert ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(ticks))
        return ticks.value / 1e7  # 100 ns units
    return time.monotonic()


def test_guard_connect_timeout_is_ten_seconds_on_postgresql():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]
    held = []
    stop = threading.Event()

    def accept_and_stay_silent():
        listener.settimeout(0.5)
        while not stop.is_set():
            try:
                held.append(listener.accept()[0])  # accepts the TCP connection, never answers
            except OSError:
                pass

    thread = threading.Thread(target=accept_and_stay_silent, daemon=True)
    thread.start()
    try:
        wall0, mono0, awake0 = time.time(), time.monotonic(), _awake_seconds()
        code = guard_code(f"postgresql://u:{SENTINEL}@127.0.0.1:{port}/db")
        wall, mono, awake = time.time() - wall0, time.monotonic() - mono0, _awake_seconds() - awake0
    finally:
        stop.set()
        thread.join(timeout=5)
        for s in held:
            s.close()
        listener.close()
    print(f"\nguard gave up on a silent server with {code}: awake-clock {awake:.2f}s "
          f"(monotonic {mono:.2f}s, wall {wall:.2f}s)")
    assert code == "DB_UNREACHABLE"
    assert held, "the silent listener must actually have accepted the connection"
    assert 9.0 <= awake <= 16.0, (awake, mono, wall)  # connect_timeout=10, not unbounded


def test_real_startup_lifespan_against_postgresql(pg_db, alembic_env, import_main, monkeypatch):
    from fastapi.testclient import TestClient

    main = import_main(APP_ENV="development", DATABASE_URL=pg_db.url)
    monkeypatch.setattr(main, "start_telegram_bot", lambda: pytest.fail("Telegram bot must not start"))
    from migrations.schema_guard import SchemaVersionError as FreshError

    before = fingerprint(pg_db)
    with pytest.raises(FreshError) as exc:
        with TestClient(main.app):
            pass
    assert exc.value.code == "NO_SCHEMA_VERSION"
    assert fingerprint(pg_db) == before  # startup created nothing on PostgreSQL

    command.upgrade(alembic_env(pg_db), "head")
    migrated = fingerprint(pg_db)
    with TestClient(main.app) as client:
        assert client.get("/health").json() == {"status": "ok"}
    after = fingerprint(pg_db)
    # M4-A2: startup registers THIS process (one heartbeat row in worker_processes) and writes nothing else; the schema is untouched
    process_rows = after["data"].pop(("public", "worker_processes"))
    assert process_rows[0][0] == 1 and migrated["data"].pop(("public", "worker_processes"))[0][0] == 0
    assert pg_db.rows("SELECT kind FROM worker_processes") == [("api",)]
    assert after == migrated


# ------------------------------------------------ M1-A: identity migration 0002 on PostgreSQL


def _seed_baseline(pg_db: PgDb):
    pg_db.run(
        "INSERT INTO agent_memories (agent_name, user_email, summary, created_at) "
        "VALUES ('ceo', 'legacy@example.com', 'legacy summary', now())",
        "INSERT INTO agent_memories (agent_name, user_email, summary, created_at) "
        "VALUES ('sales', NULL, 'orphan', now())",
        "INSERT INTO telegram_links (email, active_agent, updated_at) "
        "VALUES ('legacy@example.com', 'ceo', now())",
    )


def _insert_user(conn, user_id, email):
    conn.execute(text("INSERT INTO users (id, email, email_normalized, is_active, created_at) "
                      "VALUES (:id, :email, :norm, true, now())"),
                 {"id": user_id, "email": email, "norm": email.casefold()})


def _insert_identity(conn, user_id, provider, subject):
    conn.execute(text("INSERT INTO auth_identities (id, user_id, provider, provider_subject, email_at_link, created_at) "
                      "VALUES (:id, :uid, :p, :s, 'x@example.com', now())"),
                 {"id": uuid.uuid4(), "uid": user_id, "p": provider, "s": subject})


def test_identity_migration_schema_behaviour_and_reversibility_on_postgresql(pg_db, alembic_env):
    import memory.models as models

    command.upgrade(alembic_env(pg_db), BASELINE)
    _seed_baseline(pg_db)
    baseline_fp = fingerprint(pg_db)

    command.upgrade(alembic_env(pg_db), IDENTITY)
    assert pg_db.rows("SELECT version_num FROM alembic_version") == [(IDENTITY,)]
    upgraded_shape = shape(pg_db)
    # The migration created no rows (nothing derived from the legacy email).
    assert pg_db.rows("SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM auth_identities)") == [(0, 0)]

    cols = {(t, c): (dt, ln, nl, df) for t, c, dt, ln, nl, df in pg_db.rows(
        "SELECT table_name, column_name, data_type, character_maximum_length, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name IN ('users', 'auth_identities')")}
    ts = "timestamp without time zone"
    assert cols == {
        ("users", "id"): ("uuid", None, "NO", None),
        ("users", "email"): ("character varying", 320, "NO", None),
        ("users", "email_normalized"): ("character varying", 320, "NO", None),
        ("users", "display_name"): ("character varying", 255, "YES", None),
        ("users", "is_active"): ("boolean", None, "NO", None),
        ("users", "created_at"): (ts, None, "NO", None),
        ("auth_identities", "id"): ("uuid", None, "NO", None),
        ("auth_identities", "user_id"): ("uuid", None, "NO", None),
        ("auth_identities", "provider"): ("character varying", 32, "NO", None),
        ("auth_identities", "provider_subject"): ("character varying", 255, "NO", None),
        ("auth_identities", "email_at_link"): ("character varying", 320, "NO", None),
        ("auth_identities", "created_at"): (ts, None, "NO", None),
    }
    assert pg_db.rows(
        "SELECT conrelid::regclass::text, conname, contype, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid IN ('users'::regclass, 'auth_identities'::regclass) ORDER BY 1, 2") == [
        ("auth_identities", "auth_identities_pkey", "p", "PRIMARY KEY (id)"),
        ("auth_identities", "fk_auth_identities_user_id_users", "f", "FOREIGN KEY (user_id) REFERENCES users(id)"),
        ("auth_identities", "uq_auth_identities_provider_provider_subject", "u", "UNIQUE (provider, provider_subject)"),
        ("users", "users_pkey", "p", "PRIMARY KEY (id)"),
    ]
    indexes = dict(pg_db.rows("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
                              "AND tablename IN ('users', 'auth_identities')"))
    assert set(indexes) == {"users_pkey", "ix_users_email_normalized", "auth_identities_pkey",
                            "uq_auth_identities_provider_provider_subject", "ix_auth_identities_user_id"}
    assert indexes["ix_users_email_normalized"] == (
        "CREATE INDEX ix_users_email_normalized ON public.users USING btree (email_normalized)")
    assert indexes["ix_auth_identities_user_id"] == (
        "CREATE INDEX ix_auth_identities_user_id ON public.auth_identities USING btree (user_id)")
    assert pg_db.rows("SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                      "WHERE c.relname = 'ix_users_email_normalized' AND i.indisunique") == [(0,)]  # email: not unique

    # Constraint behaviour on a real server.
    first, second = uuid.uuid4(), uuid.uuid4()
    engine = pg_db.engine()
    try:
        with engine.begin() as conn:
            _insert_user(conn, first, "same@example.com")
            _insert_user(conn, second, "same@example.com")  # same email, a different user: allowed
            _insert_identity(conn, first, "google", "subject-A")
            _insert_identity(conn, second, "google", "subject-B")
        for label, action, expected in (
            ("duplicate (provider, subject)", lambda c: _insert_identity(c, second, "google", "subject-A"),
             "uq_auth_identities_provider_provider_subject"),
            ("unknown user", lambda c: _insert_identity(c, uuid.uuid4(), "google", "subject-C"),
             "fk_auth_identities_user_id_users"),
        ):
            with pytest.raises(DBAPIError) as exc:
                with engine.begin() as conn:
                    action(conn)
            assert expected in str(exc.value), label
        with engine.begin() as conn:
            _insert_identity(conn, first, "github", "subject-A")  # same subject, another provider: allowed
    finally:
        engine.dispose()
    assert pg_db.rows("SELECT count(*) FROM users WHERE email = 'same@example.com'") == [(2,)]
    assert pg_db.rows("SELECT count(DISTINCT user_id) FROM auth_identities WHERE email_at_link = 'x@example.com'") == [(2,)]

    # Metadata parity with the models on PostgreSQL reflection.
    engine = pg_db.engine()
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts=dict(
                compare_type=True, compare_server_default=True,
                include_object=_before_ownership(("alembic_version", "workspaces", "workspace_memberships"))))
            assert compare_metadata(ctx, models.Base.metadata) == []  # the schema at 0002_identity
    finally:
        engine.dispose()

    # Downgrade (development/test behaviour) restores the exact baseline, identities and all.
    command.downgrade(alembic_env(pg_db), BASELINE)
    assert fingerprint(pg_db) == baseline_fp  # structure, object identities, sequence state and row data
    # Re-upgrade (explicitly to 0002) succeeds and yields the same structure.
    command.upgrade(alembic_env(pg_db), IDENTITY)
    assert shape(pg_db) == upgraded_shape


def test_adoption_path_legacy_baseline_database_stamp_then_upgrade_on_postgresql(pg_db, pg_admin, alembic_env):
    """The runbook's MATCH path after M1-A/M1-A.1: a legacy (baseline-only) database is verified
    against the frozen baseline, stamped at the BASELINE, then upgraded to head."""
    create_baseline(pg_db)  # a legacy database exactly as create_all built it
    _seed_baseline(pg_db)
    legacy_fp = fingerprint(pg_db)
    legacy_sql = "SELECT id, agent_name, user_email, summary, created_at FROM agent_memories ORDER BY id"
    legacy_rows = pg_db.rows(legacy_sql)

    report = classify(pg_db)  # no patching: unversioned databases are compared with the frozen baseline
    assert report.state == pf.MATCH  # incl. the PostgreSQL sequence / default / PK-name checks
    assert report.target.startswith("0001_baseline (frozen baseline schema, NOT the current head ")

    command.stamp(alembic_env(pg_db), BASELINE)  # explicit revision id, never head
    out = io.StringIO()
    command.current(alembic_env(pg_db, buffer=out))
    assert BASELINE in out.getvalue() and "(head)" not in out.getvalue()  # known, but behind the head
    assert guard_code(pg_db.url) == "SCHEMA_BEHIND_HEAD"  # the startup guard refuses a database behind the head

    command.upgrade(alembic_env(pg_db), "head")  # applies the later revisions (0002, 0003) only
    assert pg_db.rows("SELECT version_num FROM alembic_version") == [(HEAD,)]
    assert verify_schema_version(pg_db.url) == HEAD
    after = fingerprint(pg_db)
    baseline_names = {"agent_memories", "agent_memories_id_seq", "agent_memories_pkey", "ix_agent_memories_agent_name",
                      "ix_agent_memories_user_email", "telegram_links", "telegram_links_chat_id_seq", "telegram_links_pkey"}
    pick = lambda fp: [r for r in fp["relations"] if r[1] in baseline_names]  # noqa: E731
    assert pick(after) == pick(legacy_fp)  # baseline objects were not recreated or rewritten
    # Legacy rows byte-identical. agent_memories is compared on its baseline columns: since M1-C its rows
    # also carry two NULL ownership columns, which changes the whole-row text that the fingerprint hashes.
    assert pg_db.rows(legacy_sql) == legacy_rows
    assert after["data"][("public", "telegram_links")] == legacy_fp["data"][("public", "telegram_links")]
    assert pg_db.rows("SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM auth_identities)") == [(0, 0)]

    other = PgDb(pg_admin.url, f"af_t1i_{uuid.uuid4().hex[:12]}")
    with pg_admin.engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{other.name}"'))
    try:
        command.upgrade(alembic_env(other), "head")
        assert shape(pg_db) == shape(other)  # adopted legacy database == freshly migrated database
    finally:
        with pg_admin.engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{other.name}" WITH (FORCE)'))


# ------------------------------------------ M1-B: workspace migration 0003 on PostgreSQL

WORKSPACES = "0003_workspaces"


def _insert_workspace(conn, ws_id, name, is_personal=False, personal_for_user_id=None):
    conn.execute(text("INSERT INTO workspaces (id, name, is_personal, personal_for_user_id, created_at) "
                      "VALUES (:id, :name, :personal, :uid, now())"),
                 {"id": ws_id, "name": name, "personal": is_personal, "uid": personal_for_user_id})


def _insert_membership(conn, ws_id, user_id, role):
    conn.execute(text("INSERT INTO workspace_memberships (workspace_id, user_id, role, created_at) "
                      "VALUES (:w, :u, :r, now())"), {"w": ws_id, "u": user_id, "r": role})


def _rejected(engine, expected_constraint, action):
    """Run ``action`` in its own transaction; PostgreSQL must reject it and name the constraint."""
    with pytest.raises(DBAPIError) as exc:
        with engine.begin() as conn:
            action(conn)
    assert expected_constraint in str(exc.value), (expected_constraint, str(exc.value)[:200])


def test_workspace_migration_schema_constraints_and_reversibility_on_postgresql(pg_db, alembic_env):
    import memory.models as models

    command.upgrade(alembic_env(pg_db), IDENTITY)
    _seed_baseline(pg_db)
    u1, u2, u3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    engine = pg_db.engine()
    try:
        with engine.begin() as conn:
            for user_id, email in ((u1, "a@example.com"), (u2, "b@example.com"), (u3, "c@example.com")):
                _insert_user(conn, user_id, email)
        identity_fp = fingerprint(pg_db)  # the exact 0002_identity state, with rows

        command.upgrade(alembic_env(pg_db), WORKSPACES)
        assert pg_db.rows("SELECT version_num FROM alembic_version") == [(WORKSPACES,)]
        workspaces_shape = shape(pg_db)
        assert pg_db.rows("SELECT (SELECT count(*) FROM workspaces), (SELECT count(*) FROM workspace_memberships)") == [(0, 0)]

        ts = "timestamp without time zone"
        cols = {(t, c): (dt, ln, nl, df) for t, c, dt, ln, nl, df in pg_db.rows(
            "SELECT table_name, column_name, data_type, character_maximum_length, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name IN ('workspaces', 'workspace_memberships')")}
        assert cols == {
            ("workspaces", "id"): ("uuid", None, "NO", None),
            ("workspaces", "name"): ("character varying", 255, "NO", None),
            ("workspaces", "is_personal"): ("boolean", None, "NO", None),
            ("workspaces", "personal_for_user_id"): ("uuid", None, "YES", None),
            ("workspaces", "created_at"): (ts, None, "NO", None),
            ("workspace_memberships", "workspace_id"): ("uuid", None, "NO", None),
            ("workspace_memberships", "user_id"): ("uuid", None, "NO", None),
            ("workspace_memberships", "role"): ("character varying", 16, "NO", None),
            ("workspace_memberships", "created_at"): (ts, None, "NO", None),
        }
        constraints = {(t, n): (k, d) for t, n, k, d in pg_db.rows(
            "SELECT conrelid::regclass::text, conname, contype, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid IN ('workspaces'::regclass, 'workspace_memberships'::regclass)")}
        for key, definition in sorted(constraints.items()):
            print("PG constraint", key, definition)  # evidence: how PostgreSQL stores the CHECK expressions
        assert {k: v[0] for k, v in constraints.items()} == {
            ("workspaces", "workspaces_pkey"): "p",
            ("workspaces", "uq_workspaces_personal_for_user_id"): "u",
            ("workspaces", "fk_workspaces_personal_for_user_id_users"): "f",
            ("workspaces", "ck_workspaces_personal_consistency"): "c",
            ("workspace_memberships", "workspace_memberships_pkey"): "p",
            ("workspace_memberships", "fk_workspace_memberships_workspace_id_workspaces"): "f",
            ("workspace_memberships", "fk_workspace_memberships_user_id_users"): "f",
            ("workspace_memberships", "ck_workspace_memberships_role"): "c",
        }
        assert constraints[("workspaces", "workspaces_pkey")][1] == "PRIMARY KEY (id)"
        assert constraints[("workspaces", "uq_workspaces_personal_for_user_id")][1] == "UNIQUE (personal_for_user_id)"
        assert constraints[("workspaces", "fk_workspaces_personal_for_user_id_users")][1] == (
            "FOREIGN KEY (personal_for_user_id) REFERENCES users(id)")
        assert constraints[("workspace_memberships", "workspace_memberships_pkey")][1] == "PRIMARY KEY (workspace_id, user_id)"
        assert constraints[("workspace_memberships", "fk_workspace_memberships_workspace_id_workspaces")][1] == (
            "FOREIGN KEY (workspace_id) REFERENCES workspaces(id)")
        assert constraints[("workspace_memberships", "fk_workspace_memberships_user_id_users")][1] == (
            "FOREIGN KEY (user_id) REFERENCES users(id)")
        role_check = constraints[("workspace_memberships", "ck_workspace_memberships_role")][1]
        assert set(re.findall(r"'([a-z]+)'", role_check)) == {"owner", "admin", "member"} and "viewer" not in role_check
        personal_check = constraints[("workspaces", "ck_workspaces_personal_consistency")][1]
        for token in ("is_personal", "personal_for_user_id IS NOT NULL", "personal_for_user_id IS NULL", "NOT is_personal"):
            assert token in personal_check, (token, personal_check)
        indexes = dict(pg_db.rows("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
                                  "AND tablename IN ('workspaces', 'workspace_memberships')"))
        assert set(indexes) == {"workspaces_pkey", "uq_workspaces_personal_for_user_id",
                                "workspace_memberships_pkey", "ix_workspace_memberships_user_id"}
        assert indexes["uq_workspaces_personal_for_user_id"] == (
            "CREATE UNIQUE INDEX uq_workspaces_personal_for_user_id ON public.workspaces USING btree (personal_for_user_id)")
        assert indexes["ix_workspace_memberships_user_id"] == (
            "CREATE INDEX ix_workspace_memberships_user_id ON public.workspace_memberships USING btree (user_id)")

        # Constraint behaviour on a real server.
        team_a, team_b, team_c, personal_1, personal_2 = (uuid.uuid4() for _ in range(5))
        with engine.begin() as conn:
            for ws_id, name in ((team_a, "team a"), (team_b, "team b"), (team_c, "team c")):
                _insert_workspace(conn, ws_id, name)  # several NULL personal_for_user_id values: allowed
            _insert_workspace(conn, personal_1, "personal 1", True, u1)
            _insert_workspace(conn, personal_2, "personal 2", True, u2)  # another user: allowed
        _rejected(engine, "uq_workspaces_personal_for_user_id",
                  lambda c: _insert_workspace(c, uuid.uuid4(), "second personal for u1", True, u1))
        _rejected(engine, "ck_workspaces_personal_consistency",
                  lambda c: _insert_workspace(c, uuid.uuid4(), "personal without user", True, None))
        _rejected(engine, "ck_workspaces_personal_consistency",
                  lambda c: _insert_workspace(c, uuid.uuid4(), "team with owner column", False, u3))
        _rejected(engine, "fk_workspaces_personal_for_user_id_users",
                  lambda c: _insert_workspace(c, uuid.uuid4(), "unknown user", True, uuid.uuid4()))
        with engine.begin() as conn:
            for user_id, role in ((u1, "owner"), (u2, "admin"), (u3, "member")):
                _insert_membership(conn, team_a, user_id, role)  # all three valid roles
        for bad_role in ("viewer", "superuser", "", "Owner"):
            _rejected(engine, "ck_workspace_memberships_role", lambda c, r=bad_role: _insert_membership(c, team_b, u1, r))
        _rejected(engine, "workspace_memberships_pkey", lambda c: _insert_membership(c, team_a, u1, "member"))
        _rejected(engine, "fk_workspace_memberships_workspace_id_workspaces",
                  lambda c: _insert_membership(c, uuid.uuid4(), u1, "member"))
        _rejected(engine, "fk_workspace_memberships_user_id_users",
                  lambda c: _insert_membership(c, team_b, uuid.uuid4(), "member"))
        with engine.begin() as conn:
            _insert_membership(conn, team_b, u1, "member")  # same user, another workspace: allowed
        assert pg_db.rows("SELECT count(*) FROM workspaces WHERE personal_for_user_id IS NULL") == [(3,)]

        # Metadata parity with the models (whole schema) on PostgreSQL reflection. Note: Alembic does
        # not compare CHECK constraints; they were verified above from pg_constraint.
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts=dict(
                compare_type=True, compare_server_default=True,
                include_object=_before_ownership()))
            assert compare_metadata(ctx, models.Base.metadata) == []  # the schema at 0003_workspaces
    finally:
        engine.dispose()

    # Downgrade (development/test behaviour) restores the exact 0002_identity state, workspaces and all.
    command.downgrade(alembic_env(pg_db), IDENTITY)
    assert fingerprint(pg_db) == identity_fp  # structure, object identities, sequence state, rows
    command.upgrade(alembic_env(pg_db), WORKSPACES)  # re-upgrade (explicitly to 0003) succeeds with the same structure
    assert shape(pg_db) == workspaces_shape
    assert pg_db.rows("SELECT (SELECT count(*) FROM workspaces), (SELECT count(*) FROM workspace_memberships)") == [(0, 0)]


def test_adoption_path_reaches_the_workspace_head_on_postgresql(pg_db, pg_admin, alembic_env):
    """The unversioned baseline MATCH / stamp / upgrade path applies the later revisions and the guard accepts it."""
    create_baseline(pg_db)
    assert classify(pg_db).state == pf.MATCH
    command.stamp(alembic_env(pg_db), BASELINE)
    assert guard_code(pg_db.url) == "SCHEMA_BEHIND_HEAD"
    command.upgrade(alembic_env(pg_db), "head")
    assert pg_db.rows("SELECT version_num FROM alembic_version") == [(HEAD,)]
    assert verify_schema_version(pg_db.url) == HEAD
    assert classify(pg_db).state == pf.VERSIONED_CURRENT
    # ... and a head-schema database WITHOUT a version table is still DIVERGENT, never a baseline match.
    other = PgDb(pg_admin.url, f"af_t1i_{uuid.uuid4().hex[:12]}")
    with pg_admin.engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{other.name}"'))
    try:
        create_all(other)
        report = classify(other)
        assert report.state == pf.DIVERGENT
        assert report.target == f"0001_baseline (frozen baseline schema, NOT the current head {HEAD})"
        assert not any(f.startswith("unexpected check constraint") for f in report.findings)
        assert any("matches the current head schema but has no alembic_version" in f for f in report.findings)
    finally:
        with pg_admin.engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{other.name}" WITH (FORCE)'))


# --------------------------------------------- M1-C: memory ownership migration 0004 on PostgreSQL

OWNERSHIP = "0004_memory_ownership"
OWNERSHIP_INDEX = "ix_agent_memories_user_id_created_at_id"
FK_USER = "fk_agent_memories_user_id_users"
FK_WORKSPACE = "fk_agent_memories_workspace_id_workspaces"


def _insert_memory(conn, user_id=None, workspace_id=None, summary="owned"):
    conn.execute(text("INSERT INTO agent_memories (agent_name, user_email, summary, created_at, user_id, workspace_id) "
                      "VALUES ('ceo', NULL, :s, now(), :u, :w)"), {"s": summary, "u": user_id, "w": workspace_id})


def test_ownership_migration_schema_index_legacy_rows_and_reversibility_on_postgresql(pg_db, alembic_env):
    import memory.models as models

    command.upgrade(alembic_env(pg_db), WORKSPACES)
    _seed_baseline(pg_db)  # legacy rows: with an email, orphaned
    pg_db.run("INSERT INTO agent_memories (agent_name, user_email, summary, created_at) "
              "VALUES ('ceo', 'voice-call@agentforge.local', 'a phone call', now())")
    user_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    engine = pg_db.engine()
    try:
        with engine.begin() as conn:
            _insert_user(conn, user_id, "owner@example.com")  # a user whose email a backfill could have matched
            _insert_workspace(conn, workspace_id, "w")
        pg_db.run("INSERT INTO agent_memories (agent_name, user_email, summary, created_at) "
                  "VALUES ('ceo', 'owner@example.com', 'looks like the owner', now())")
    finally:
        engine.dispose()
    legacy_sql = "SELECT id, agent_name, user_email, summary, created_at FROM agent_memories ORDER BY id"
    legacy_rows = pg_db.rows(legacy_sql)
    workspaces_fp = fingerprint(pg_db)  # the exact 0003_workspaces state, with rows

    command.upgrade(alembic_env(pg_db), OWNERSHIP)
    assert pg_db.rows("SELECT version_num FROM alembic_version") == [(OWNERSHIP,)]
    ownership_shape = shape(pg_db)

    # Legacy rows survive unchanged with NULL ownership; nothing was backfilled or created.
    assert pg_db.rows(legacy_sql) == legacy_rows and len(legacy_rows) == 4
    assert pg_db.rows("SELECT user_id, workspace_id FROM agent_memories ORDER BY id") == [(None, None)] * 4
    assert pg_db.rows("SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM workspaces), "
                      "(SELECT count(*) FROM auth_identities), (SELECT count(*) FROM workspace_memberships)") == [(1, 1, 0, 0)]

    cols = [(c, dt, ln, nl, df) for c, dt, ln, nl, df in pg_db.rows(
        "SELECT column_name, data_type, character_maximum_length, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'agent_memories' "
        "ORDER BY ordinal_position")]
    assert cols == [
        ("id", "integer", None, "NO", "nextval('agent_memories_id_seq'::regclass)"),
        ("agent_name", "character varying", 100, "NO", None),
        ("user_email", "character varying", 255, "YES", None),
        ("summary", "text", None, "NO", None),
        ("created_at", "timestamp without time zone", None, "NO", None),
        ("user_id", "uuid", None, "YES", None),
        ("workspace_id", "uuid", None, "YES", None),
    ]
    constraints = {n: (k, d) for n, k, d in pg_db.rows(
        "SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'agent_memories'::regclass")}
    assert constraints == {
        "agent_memories_pkey": ("p", "PRIMARY KEY (id)"),
        FK_USER: ("f", "FOREIGN KEY (user_id) REFERENCES users(id)"),
        FK_WORKSPACE: ("f", "FOREIGN KEY (workspace_id) REFERENCES workspaces(id)"),
    }
    indexes = dict(pg_db.rows("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
                              "AND tablename = 'agent_memories'"))
    assert set(indexes) == {"agent_memories_pkey", "ix_agent_memories_agent_name", "ix_agent_memories_user_email", OWNERSHIP_INDEX}
    print("PG history index:", indexes[OWNERSHIP_INDEX])
    assert indexes[OWNERSHIP_INDEX] == (
        f"CREATE INDEX {OWNERSHIP_INDEX} ON public.agent_memories USING btree (user_id, created_at DESC, id DESC)")
    # Column order and the DESC flags straight from the catalog (pg_index.indoption bit 0 = DESC).
    assert pg_db.rows(
        "SELECT a.attname, (i.indoption[k.n - 1] & 1) = 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "CROSS JOIN LATERAL generate_series(1, i.indnkeyatts) AS k(n) "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[k.n - 1] "
        f"WHERE c.relname = '{OWNERSHIP_INDEX}' ORDER BY k.n") == [("user_id", False), ("created_at", True), ("id", True)]
    assert pg_db.rows(f"SELECT indisunique FROM pg_index WHERE indexrelid = '{OWNERSHIP_INDEX}'::regclass") == [(False,)]

    # The index serves the history query shape without a sort (sequential scans disabled: tiny table).
    engine = pg_db.engine()
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
            plan = "\n".join(r[0] for r in conn.execute(text(
                "EXPLAIN SELECT id FROM agent_memories WHERE user_id = :u ORDER BY created_at DESC, id DESC"), {"u": user_id}))
            print("PG plan:", plan.replace(chr(10), " | ")[:200])
            assert OWNERSHIP_INDEX in plan and "Sort" not in plan
    finally:
        engine.dispose()

    # Downgrade (development/test behaviour) restores the exact 0003_workspaces state; every row is kept.
    command.downgrade(alembic_env(pg_db), WORKSPACES)
    assert fingerprint(pg_db) == workspaces_fp  # structure, object identities, sequence state and row data
    assert pg_db.rows(legacy_sql) == legacy_rows
    command.upgrade(alembic_env(pg_db), OWNERSHIP)  # re-upgrade succeeds and yields the same structure (later revisions: own tests)
    assert shape(pg_db) == ownership_shape
    assert pg_db.rows("SELECT user_id, workspace_id FROM agent_memories ORDER BY id") == [(None, None)] * 4  # still no backfill

    # Foreign-key behaviour on a real server (run after the reversibility checks: it writes rows).
    engine = pg_db.engine()
    try:
        with engine.begin() as conn:
            _insert_memory(conn, user_id, workspace_id, "both")
            _insert_memory(conn, user_id, None, "user only")
            _insert_memory(conn, None, workspace_id, "workspace only")
            _insert_memory(conn, None, None, "no owner")  # NULL ownership stays permitted
        _rejected(engine, FK_USER, lambda c: _insert_memory(c, uuid.uuid4(), None))
        _rejected(engine, FK_WORKSPACE, lambda c: _insert_memory(c, None, uuid.uuid4()))
        _rejected(engine, FK_USER, lambda c: _insert_memory(c, uuid.uuid4(), workspace_id))
        command.upgrade(alembic_env(pg_db), "head")  # the live models describe the head, which now includes 0005
        with engine.connect() as conn:  # Alembic does not compare sort order: the DESC flags were checked above
            ctx = MigrationContext.configure(conn, opts=dict(
                compare_type=True, compare_server_default=True,
                include_object=lambda o, n, t, r, c: not (t == "table" and n == "alembic_version")))
            diffs = compare_metadata(ctx, models.Base.metadata)
            print("PG compare_metadata vs live models:", diffs)
            assert diffs == []
    finally:
        engine.dispose()
    assert pg_db.rows("SELECT count(*) FROM agent_memories WHERE user_id IS NULL AND workspace_id IS NULL") == [(5,)]


def test_adoption_path_reaches_the_ownership_head_and_legacy_rows_stay_unowned_on_postgresql(pg_db, alembic_env):
    """Unversioned baseline MATCH / stamp / upgrade now applies 0002 to 0007 over real legacy rows."""
    create_baseline(pg_db)
    _seed_baseline(pg_db)
    legacy_sql = "SELECT id, agent_name, user_email, summary, created_at FROM agent_memories ORDER BY id"
    legacy_rows = pg_db.rows(legacy_sql)
    assert classify(pg_db).state == pf.MATCH
    command.stamp(alembic_env(pg_db), BASELINE)
    command.upgrade(alembic_env(pg_db), "head")
    assert pg_db.rows("SELECT version_num FROM alembic_version") == [(HEAD,)] and HEAD == "0017_invitations"
    assert verify_schema_version(pg_db.url) == HEAD
    assert pg_db.rows(legacy_sql) == legacy_rows  # legacy rows byte-identical
    assert pg_db.rows("SELECT user_id, workspace_id FROM agent_memories") == [(None, None)] * 2  # quarantined, never assigned
    assert classify(pg_db).state == pf.VERSIONED_CURRENT


# ---------------------------------------------- M1-D: authentication provisioning on PostgreSQL


def _auth_counts(pg_db):
    return pg_db.rows("SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM workspaces), "
                      "(SELECT count(*) FROM workspace_memberships), (SELECT count(*) FROM auth_identities)")[0]


def test_auth_provisioning_atomicity_race_handling_and_constraint_detection_on_postgresql(pg_db, alembic_env, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    from auth import provisioning
    from auth.errors import InactiveUserError
    from auth.provisioning import resolve_principal
    from auth_support import claims
    from memory.models import AuthIdentity, User, Workspace, WorkspaceMembership

    command.upgrade(alembic_env(pg_db), "head")
    engine = create_engine(pg_db.url, pool_size=10, max_overflow=10)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        # First valid token: user, personal workspace, owner membership and identity, all linked.
        principal = resolve_principal(factory, claims(subject="pg-sub-1", email="pg@example.com"))
        assert _auth_counts(pg_db) == (1, 1, 1, 1)
        with factory() as s:
            user = s.scalars(select(User)).one()
            workspace = s.scalars(select(Workspace)).one()
            membership = s.scalars(select(WorkspaceMembership)).one()
            identity = s.scalars(select(AuthIdentity)).one()
        assert user.id == principal.user_id and isinstance(user.id, uuid.UUID)
        assert (workspace.is_personal, workspace.personal_for_user_id) == (True, user.id)
        assert (membership.workspace_id, membership.user_id, membership.role) == (workspace.id, user.id, "owner")
        assert (identity.user_id, identity.provider, identity.provider_subject, identity.email_at_link) == (
            user.id, "google", "pg-sub-1", "pg@example.com")
        assert resolve_principal(factory, claims(subject="pg-sub-1", email="pg@example.com")).user_id == principal.user_id

        # Conflict detection on REAL PostgreSQL errors: only the identity constraint is a lost race.
        with pytest.raises(DBAPIError) as exc:
            with engine.begin() as conn:
                _insert_identity(conn, principal.user_id, "google", "pg-sub-1")
        assert exc.value.orig.diag.constraint_name == "uq_auth_identities_provider_provider_subject"
        assert provisioning._is_identity_conflict(exc.value)
        with pytest.raises(DBAPIError) as exc:
            with engine.begin() as conn:
                _insert_workspace(conn, uuid.uuid4(), "second personal", True, principal.user_id)
        assert exc.value.orig.diag.constraint_name == "uq_workspaces_personal_for_user_id"
        assert not provisioning._is_identity_conflict(exc.value)
        with pytest.raises(DBAPIError) as exc:
            with engine.begin() as conn:
                _insert_membership(conn, uuid.uuid4(), principal.user_id, "owner")  # unknown workspace
        assert not provisioning._is_identity_conflict(exc.value)

        # Same email, different subject: a different user (no merging). The inactive user is never reactivated.
        other = resolve_principal(factory, claims(subject="pg-sub-2", email="pg@example.com"))
        assert other.user_id != principal.user_id and _auth_counts(pg_db) == (2, 2, 2, 2)
        pg_db.run("UPDATE users SET is_active = false WHERE id = '" + str(other.user_id) + "'")
        with pytest.raises(InactiveUserError):
            resolve_principal(factory, claims(subject="pg-sub-2", email="changed@example.com"))
        assert pg_db.rows("SELECT is_active, email FROM users WHERE id = '" + str(other.user_id) + "'") == [(False, "pg@example.com")]
        assert _auth_counts(pg_db) == (2, 2, 2, 2)

        # A failure mid-provisioning rolls back all four records; an unrelated integrity error propagates.
        monkeypatch.setattr(provisioning, "AuthIdentity", lambda **kw: (_ for _ in ()).throw(RuntimeError("simulated")))
        with pytest.raises(RuntimeError, match="simulated"):
            resolve_principal(factory, claims(subject="pg-sub-fail"))
        monkeypatch.undo()
        monkeypatch.setattr(provisioning, "OWNER_ROLE", "viewer")
        with pytest.raises(IntegrityError_()) as exc:
            resolve_principal(factory, claims(subject="pg-sub-badrole"))
        assert exc.value.orig.diag.constraint_name == "ck_workspace_memberships_role"
        monkeypatch.undo()
        assert _auth_counts(pg_db) == (2, 2, 2, 2)  # neither failed attempt left anything behind

        # Real concurrency: 8 threads race to provision the same new identity.
        threads, conflicts, local = 8, [], threading.local()
        barrier = threading.Barrier(threads, timeout=60)
        real_find, real_conflict = provisioning._find, provisioning._is_identity_conflict

        def synced_find(session_factory, verified):
            result = real_find(session_factory, verified)
            if result is None and not getattr(local, "waited", False):
                local.waited = True
                barrier.wait()
            return result

        monkeypatch.setattr(provisioning, "_find", synced_find)
        monkeypatch.setattr(provisioning, "_is_identity_conflict", lambda e: conflicts.append(real_conflict(e)) or conflicts[-1])
        with ThreadPoolExecutor(max_workers=threads) as pool:
            futures = [pool.submit(resolve_principal, factory, claims(subject="pg-race")) for _ in range(threads)]
            results = [f.result(timeout=120) for f in futures]
        assert len({p.user_id for p in results}) == 1  # everyone got the same internal user
        assert _auth_counts(pg_db) == (3, 3, 3, 3)  # exactly one new user, workspace, owner membership and identity
        assert conflicts and all(conflicts)  # real lost races, each recognised as the identity constraint only
        print("PG concurrency: threads =", threads, "| recognised lost races =", len(conflicts))
        assert pg_db.rows("SELECT count(*) FROM workspace_memberships WHERE role = 'owner'") == [(3,)]
    finally:
        engine.dispose()


def IntegrityError_():
    from sqlalchemy.exc import IntegrityError

    return IntegrityError


def test_auth_dependency_end_to_end_against_postgresql(pg_db, alembic_env):
    from cryptography.hazmat.primitives import serialization  # noqa: F401
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from auth.dependencies import get_auth_config, get_principal, get_session_factory
    from auth.principal import Principal
    from auth_support import auth_config, make_token, new_keypair

    command.upgrade(alembic_env(pg_db), "head")
    private, public = new_keypair()
    engine = create_engine(pg_db.url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()

    @app.get("/whoami")
    def whoami(principal: Principal = Depends(get_principal)):
        return {"user_id": str(principal.user_id), "email": principal.email}

    app.dependency_overrides[get_auth_config] = lambda: auth_config(public)
    app.dependency_overrides[get_session_factory] = lambda: factory
    client = TestClient(app, raise_server_exceptions=False)
    try:
        good = {"Authorization": "Bearer " + make_token(private, claims={"sub": "pg-e2e", "email": "e2e@example.com"})}
        first, second = client.get("/whoami", headers=good), client.get("/whoami", headers=good)
        assert first.status_code == second.status_code == 200 and first.json() == second.json()
        assert _auth_counts(pg_db) == (1, 1, 1, 1)
        assert client.get("/whoami").status_code == 401
        assert client.get("/whoami", headers={"Authorization": "Bearer " + make_token(new_keypair()[0])}).status_code == 401
        pg_db.run("UPDATE users SET is_active = false")
        r = client.get("/whoami", headers=good)
        assert r.status_code == 403 and r.json() == {"detail": "Forbidden"}
        assert pg_db.rows("SELECT is_active FROM users") == [(False,)]
    finally:
        engine.dispose()


def test_authz_scope_isolation_legacy_quarantine_and_owner_invariant_on_postgresql(pg_db, alembic_env):
    """M1-E on a real PostgreSQL: native uuid ownership predicates, quarantine, role checks, owner invariant under races."""
    from sqlalchemy.orm import sessionmaker

    from auth.principal import Principal
    from authz.errors import AuthorizationError, ScopeError
    from authz.workspaces import authorize_memory_scope, change_role, check_workspace_invariants, remove_membership
    from memory.repository import MemoryRepository

    command.upgrade(alembic_env(pg_db), "head")
    engine = create_engine(pg_db.url, pool_size=12)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    uids = {n: uuid.uuid4() for n in ("a", "b", "m1", "m2", "m3", "m4")}
    pw = {n: uuid.uuid4() for n in ("a", "b")}
    team = uuid.uuid4()
    with engine.begin() as c:
        for n, u in uids.items():
            _insert_user(c, u, f"{n}@example.com")
        for n in ("a", "b"):
            _insert_workspace(c, pw[n], f"P-{n}", True, uids[n])
            _insert_membership(c, pw[n], uids[n], "owner")
        _insert_workspace(c, team, "Team")
        _insert_membership(c, team, uids["a"], "owner")
        for n in ("b", "m1", "m2", "m3", "m4"):
            _insert_membership(c, team, uids[n], "member")
        c.execute(text("INSERT INTO agent_memories (agent_name, user_email, summary, created_at) "
                       "VALUES ('ceo','a@example.com','legacy-email-match',now()),('ceo','voice-call@agentforge.local','voice',now())"))

    def P(n):
        return Principal(user_id=uids[n], provider="google", provider_subject=f"s-{n}", email=f"{n}@example.com", is_active=True)

    try:
        sa, sb = authorize_memory_scope(sf, P("a"), pw["a"]), authorize_memory_scope(sf, P("b"), pw["b"])
        with pytest.raises(AuthorizationError):
            authorize_memory_scope(sf, P("b"), pw["a"])
        with pytest.raises(AuthorizationError):
            authorize_memory_scope(sf, P("a"), uuid.uuid4())
        repo = MemoryRepository(sf)
        mine, theirs = repo.save(sa, "ceo", "A"), repo.save(sb, "ceo", "B")
        assert [m["summary"] for m in repo.get_history(sa)] == ["A"] and [m["summary"] for m in repo.get(sb, "ceo")] == ["B"]
        assert repo.update_summary(sa, theirs, "x") is False and repo.delete(sa, theirs) is False
        assert repo.update_summary(sa, mine, "A2") is True and repo.get_history(sa)[0]["summary"] == "A2"
        legacy_ids = [r[0] for r in pg_db.rows("SELECT id FROM agent_memories WHERE user_id IS NULL")]
        assert len(legacy_ids) == 2
        for i in legacy_ids:
            assert repo.update_summary(sa, i, "x") is False and repo.delete(sa, i) is False
        assert pg_db.rows("SELECT count(*) FROM agent_memories WHERE user_id IS NULL AND workspace_id IS NULL") == [(2,)]
        assert pg_db.rows("SELECT user_email FROM agent_memories WHERE user_id IS NOT NULL") == [(None,), (None,)]
        with pytest.raises(ScopeError):
            repo.get_history("a@example.com")

        # owner invariant under concurrency: five threads race to remove/demote; the owner is untouchable
        errors, lock = [], threading.Lock()

        def attempt(actor, target, op):
            try:
                if op == "remove":
                    remove_membership(sf, P(actor), team, uids[target])
                else:
                    change_role(sf, P(actor), team, uids[target], "owner" if op == "promote" else "admin")
            except AuthorizationError as e:
                with lock:
                    errors.append(e.reason)

        jobs = [("a", "m1", "remove"), ("a", "m1", "remove"), ("m2", "a", "remove"), ("b", "a", "demote"), ("a", "a", "remove"),
                ("a", "m3", "promote"), ("a", "m4", "demote"), ("m3", "m4", "remove")]
        threads = [threading.Thread(target=attempt, args=j) for j in jobs]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert pg_db.rows("SELECT count(*) FROM workspace_memberships WHERE workspace_id = :w AND role='owner'".replace(":w", f"'{team}'")) == [(1,)]
        assert pg_db.rows(f"SELECT user_id FROM workspace_memberships WHERE workspace_id='{team}' AND role='owner'") == [(uids["a"],)]
        with sf() as s:
            check_workspace_invariants(s, team)
        assert {"owner_required", "sole_owner_protected", "owner_transfer_unsupported"} <= set(errors)
        assert "target_not_a_member" in errors  # exactly one of the two racing removals of m1 won
        assert pg_db.rows(f"SELECT count(*) FROM workspace_memberships WHERE workspace_id='{team}' AND user_id='{uids['m1']}'") == [(0,)]
        assert pg_db.rows("SELECT count(*) FROM users") == [(6,)]  # no cascading deletes
    finally:
        engine.dispose()


def test_history_keyset_pagination_and_task_scope_on_postgresql(pg_db, alembic_env):
    """M1-F on a real PostgreSQL: keyset pagination over (created_at, id) with equal timestamps, ownership predicates, cursor."""
    from datetime import datetime, timedelta

    from sqlalchemy.orm import sessionmaker

    from auth.principal import Principal
    from authz.workspaces import authorize_personal_scope
    from memory.cursor import CursorError
    from memory.repository import MemoryRepository

    command.upgrade(alembic_env(pg_db), "head")
    engine = create_engine(pg_db.url)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    ua, ub, wa, wb, other = (uuid.uuid4() for _ in range(5))
    base = datetime(2026, 3, 1, 12, 0, 0, 123456)
    with engine.begin() as c:
        for u, e in ((ua, "a@example.com"), (ub, "b@example.com")):
            _insert_user(c, u, e)
        for u, w in ((ua, wa), (ub, wb)):
            _insert_workspace(c, w, "P", True, u)
            _insert_membership(c, w, u, "owner")
        _insert_workspace(c, other, "Team")
        _insert_membership(c, other, ua, "owner")
        for i in range(53):  # four rows share each timestamp
            c.execute(text("INSERT INTO agent_memories (agent_name, user_email, summary, created_at, user_id, workspace_id) "
                           "VALUES ('ceo', NULL, :s, :t, :u, :w)"), {"s": f"a{i}", "t": base + timedelta(seconds=i // 4), "u": ua, "w": wa})
        c.execute(text("INSERT INTO agent_memories (agent_name, user_email, summary, created_at, user_id, workspace_id) "
                       "VALUES ('ceo', NULL, 'b', :t, :u, :w), ('ceo', NULL, 'team', :t, :ua, :o), "
                       "('ceo', 'a@example.com', 'legacy', :t, NULL, NULL)"), {"t": base, "u": ub, "w": wb, "ua": ua, "o": other})
    principal = Principal(user_id=ua, provider="google", provider_subject="s", email="a@example.com", is_active=True)
    try:
        scope = authorize_personal_scope(sf, principal)
        assert scope.workspace_id == wa
        repo = MemoryRepository(sf)
        collected, cursor, pages = [], None, 0
        while True:
            page = repo.get_history_page(scope, 7, cursor)
            collected += [i["id"] for i in page["items"]]
            cursor, pages = page["next_cursor"], pages + 1
            if cursor is None:
                break
        expected = [r[0] for r in pg_db.rows(f"SELECT id FROM agent_memories WHERE user_id = '{ua}' AND workspace_id = '{wa}' "
                                             "ORDER BY created_at DESC, id DESC")]
        assert collected == expected and len(set(collected)) == 53 and pages == 8
        assert len(repo.get_history_page(scope)["items"]) == 50
        with pytest.raises(CursorError):
            repo.get_history_page(scope, 5, "v1.garbage.00000000")
        with pytest.raises(ValueError):
            repo.get_history_page(scope, 101)
    finally:
        engine.dispose()


def test_custom_agents_migration_triggers_service_and_races_on_postgresql(pg_db, alembic_env):
    """M2-A on a real PostgreSQL: named constraints, insert-only version rows (trigger), compare_metadata, concurrency,
    reversibility with data in the pre-existing tables preserved."""
    import threading

    from sqlalchemy.orm import sessionmaker

    from agentplatform.schema import AgentDefinition
    from agentplatform.service import AgentService, SlugTaken, VersionConflict
    from auth.principal import Principal
    from authz.workspaces import authorize_personal_scope
    from memory.models import Base

    command.upgrade(alembic_env(pg_db), "head")
    engine = create_engine(pg_db.url, pool_size=12)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    uid, uid2, ws = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as c:
        _insert_user(c, uid, "a@example.com")
        _insert_user(c, uid2, "b@example.com")
        _insert_workspace(c, ws, "P", True, uid)
        _insert_membership(c, ws, uid, "owner")
        c.execute(text("INSERT INTO agent_memories (agent_name, user_email, summary, created_at) VALUES ('ceo','legacy@example.com','kept',now())"))

    def agent_row(c, slug="my-agent", **o):
        v = {"id": uuid.uuid4(), "user_id": uid, "workspace_id": ws, "slug": slug, "status": "active", "latest_version": 1,
             "current_version": 1, "template_id": None, "template_version": None, **o}
        c.execute(text(f"INSERT INTO custom_agents ({', '.join(v)}, created_at, updated_at) VALUES ({', '.join(':' + k for k in v)}, now(), now())"), v)
        return v["id"]

    def version_row(c, agent_id, version=1, **o):
        v = {"id": uuid.uuid4(), "agent_id": agent_id, "version": version, "content_hash": "h" * 64, "restored_from_version": None, "created_by": uid, **o}
        c.execute(text(f"INSERT INTO custom_agent_versions ({', '.join(v)}, definition, created_at) VALUES ({', '.join(':' + k for k in v)}, CAST('{{}}' AS json), now())"), v)

    try:
        with engine.begin() as c:
            first = agent_row(c)
            version_row(c, first)
        _rejected(engine, "uq_custom_agents_workspace_id_slug", lambda c: agent_row(c))
        _rejected(engine, "ck_custom_agents_status", lambda c: agent_row(c, slug="other", status="deleted"))
        # latest_version >= 1 is implied by the range check (current >= 1 AND current <= latest), so PostgreSQL names the range check
        _rejected(engine, "ck_custom_agents_current_version_range", lambda c: agent_row(c, slug="other", latest_version=0, current_version=0))
        _rejected(engine, "ck_custom_agents_current_version_range", lambda c: agent_row(c, slug="other", latest_version=1, current_version=2))
        _rejected(engine, "ck_custom_agents_template_pair", lambda c: agent_row(c, slug="other", template_id="sales-outreach"))
        _rejected(engine, "fk_custom_agents_user_id_users", lambda c: agent_row(c, slug="orphan", user_id=uuid.uuid4()))
        _rejected(engine, "fk_custom_agents_workspace_id_workspaces", lambda c: agent_row(c, slug="orphan2", workspace_id=uuid.uuid4()))
        _rejected(engine, "uq_custom_agent_versions_agent_id_version", lambda c: version_row(c, first, 1))
        _rejected(engine, "ck_custom_agent_versions_version_positive", lambda c: version_row(c, first, 0))
        _rejected(engine, "ck_custom_agent_versions_restored_from", lambda c: version_row(c, first, 2, restored_from_version=2))
        _rejected(engine, "fk_custom_agent_versions_agent_id_custom_agents", lambda c: version_row(c, uuid.uuid4(), 1))
        _rejected(engine, "fk_custom_agent_versions_created_by_users", lambda c: version_row(c, first, 2, created_by=uuid.uuid4()))
        with engine.begin() as c:
            version_row(c, first, 2, restored_from_version=1)  # inserts work

        for statement in ("UPDATE custom_agent_versions SET change_note = 'x'", "DELETE FROM custom_agent_versions"):
            with pytest.raises(DBAPIError) as exc:
                with engine.begin() as conn:
                    conn.execute(text(statement))
            assert "custom_agent_versions rows are immutable" in str(exc.value)
        assert pg_db.rows("SELECT count(*) FROM custom_agent_versions") == [(2,)]
        assert pg_db.rows("SELECT tgname, tgenabled FROM pg_trigger WHERE tgrelid = 'custom_agent_versions'::regclass AND NOT tgisinternal") == [
            ("custom_agent_versions_immutable", "O")]
        assert pg_db.rows("SELECT count(*) FROM pg_proc WHERE proname = 'custom_agent_versions_immutable'") == [(1,)]
        assert pg_db.rows("SELECT data_type FROM information_schema.columns WHERE table_name = 'custom_agents' AND column_name = 'id'") == [("uuid",)]
        assert pg_db.rows("SELECT data_type FROM information_schema.columns WHERE table_name = 'custom_agent_versions' AND column_name = 'definition'") == [("json",)]
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts=dict(compare_type=True, compare_server_default=True))
            assert [d for d in compare_metadata(ctx, Base.metadata) if "alembic_version" not in str(d)] == []

        # service-level races on the real server
        principal = Principal(user_id=uid, provider="google", provider_subject="s", email="a@example.com", is_active=True)
        scope = authorize_personal_scope(sf, principal)
        service = AgentService(sf)
        base = {"name": "Race Agent", "description": "An agent used to test concurrent updates.", "role": "Tester",
                "purpose": "Exercise optimistic concurrency on PostgreSQL.", "instructions": "You are a test agent used only by the test-suite.",
                "model": {"provider": "groq", "name": "llama-3.3-70b-versatile"}}
        agent = service.create(scope, "race-agent", AgentDefinition.model_validate(base))
        outcomes, barrier = [], threading.Barrier(8)

        def edit(i):
            barrier.wait()
            try:
                service.update(scope, agent.id, 1, AgentDefinition.model_validate({**base, "role": f"Editor {i}"}))
                outcomes.append("won")
            except VersionConflict:
                outcomes.append("conflict")

        threads = [threading.Thread(target=edit, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sorted(outcomes) == ["conflict"] * 7 + ["won"]
        assert pg_db.rows(f"SELECT version FROM custom_agent_versions WHERE agent_id = '{agent.id}' ORDER BY version") == [(1,), (2,)]

        taken = []

        def create_contested(i):
            try:
                service.create(scope, "contested", AgentDefinition.model_validate({**base, "name": f"Contender {i}"}))
                taken.append("won")
            except SlugTaken:
                taken.append("taken")

        threads = [threading.Thread(target=create_contested, args=(i,)) for i in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sorted(taken) == ["taken"] * 5 + ["won"]
        assert pg_db.rows("SELECT count(*) FROM custom_agent_versions v JOIN custom_agents a ON a.id = v.agent_id WHERE a.slug = 'contested'") == [(1,)]
        assert service.get(scope, agent.id).definition.role.startswith("Editor")
        assert pg_db.rows("SELECT count(*) FROM custom_agents") == [(3,)]  # my-agent, race-agent, contested
    finally:
        engine.dispose()

    # reversibility: downgrade removes exactly the 0005 objects (tables, trigger, function) and keeps every other row
    command.downgrade(alembic_env(pg_db), "0004_memory_ownership")
    assert pg_db.rows("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name LIKE 'custom_agent%'") == []
    assert pg_db.rows("SELECT count(*) FROM pg_proc WHERE proname = 'custom_agent_versions_immutable'") == [(0,)]
    assert pg_db.rows("SELECT summary FROM agent_memories") == [("kept",)] and pg_db.rows("SELECT count(*) FROM users") == [(2,)]
    assert pg_db.rows("SELECT version_num FROM alembic_version") == [("0004_memory_ownership",)]
    command.upgrade(alembic_env(pg_db), "head")
    assert pg_db.rows("SELECT version_num FROM alembic_version") == [(HEAD,)]
    assert pg_db.rows("SELECT count(*) FROM custom_agents") == [(0,)]


def test_agent_executions_pinning_guards_ownership_and_concurrency_on_postgresql(pg_db, alembic_env):
    """M2-B on a real PostgreSQL: version-pinned records, named constraints, the append-only guard trigger, ownership
    isolation, concurrent executions, an edit during an execution, the orphan sweep and reversibility."""
    import asyncio
    import threading

    from sqlalchemy.orm import sessionmaker

    from agentplatform.execution import ExecutionService, ExecutionStore, Limits
    from agentplatform.providers import ProviderRegistry
    from agentplatform.schema import AgentDefinition
    from agentplatform.service import AgentService
    from auth.principal import Principal
    from authz.workspaces import authorize_personal_scope
    from memory.models import Base
    from memory.repository import MemoryRepository
    from memory.short_term import ShortTermMemory
    from tests.api_support import FakeProvider, FakeRedis

    command.upgrade(alembic_env(pg_db), "head")
    engine = create_engine(pg_db.url, pool_size=20)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    uid, uid2, ws, ws2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as c:
        for u, e, w in ((uid, "a@example.com", ws), (uid2, "b@example.com", ws2)):
            _insert_user(c, u, e)
            _insert_workspace(c, w, "P", True, u)
            _insert_membership(c, w, u, "owner")

    def scope_for(user, email):
        return authorize_personal_scope(sf, Principal(user_id=user, provider="google", provider_subject=str(user), email=email, is_active=True))

    class Orchestrator:
        async def run_agent(self, name, task, scope):
            return f"builtin {name}: {task}"

        run_with_delegation = run_agent

    base = {"name": "PG Agent", "description": "An agent used to test execution records on PostgreSQL.", "role": "Tester",
            "purpose": "Exercise version pinning on a real server.", "instructions": "You are a test agent used only by the test-suite.",
            "model": {"provider": "groq", "name": "llama-3.3-70b-versatile"}}
    provider = FakeProvider()
    redis = FakeRedis()

    def service(limits=None):
        return ExecutionService(sf, orchestrator=Orchestrator(), providers=ProviderRegistry({"groq": lambda: provider}),
                                short_term=ShortTermMemory(client=redis), long_term=MemoryRepository(sf), limits=limits or Limits(total=50, per_user=50))

    try:
        sa, sb = scope_for(uid, "a@example.com"), scope_for(uid2, "b@example.com")
        agents = AgentService(sf)
        agent = agents.create(sa, "pg-agent", AgentDefinition.model_validate(base))
        v1 = agent.version.version_id

        built = asyncio.run(service().run(sa, "sales", "built-in task"))
        custom = asyncio.run(service().run(sa, "pg-agent", "custom task"))
        assert (built.agent_source, built.agent_version, custom.agent_source, custom.agent_version) == ("builtin", None, "custom", 1)
        assert pg_db.rows("SELECT agent_source, agent_version_id IS NULL, agent_version, status, provider FROM agent_executions ORDER BY agent_source") == [
            ("builtin", True, None, "succeeded", "groq"), ("custom", False, 1, "succeeded", "groq")]
        assert pg_db.rows(f"SELECT agent_version_id FROM agent_executions WHERE id = '{custom.execution_id}'") == [(v1,)]  # native uuid, the exact version row
        assert pg_db.rows("SELECT data_type FROM information_schema.columns WHERE table_name = 'agent_executions' AND column_name IN ('id', 'agent_version_id') ORDER BY 1") == [("uuid",), ("uuid",)]

        # an edit while an execution is in flight: the execution stays on version 1, the next one uses version 2
        started, gate = threading.Event(), threading.Event()
        provider.started, provider.gate = started, gate
        outcome = {}
        worker = threading.Thread(target=lambda: outcome.update(o=asyncio.run(service().run(sa, "pg-agent", "slow task"))))
        worker.start()
        assert started.wait(10)
        edited = agents.update(sa, agent.id, 1, AgentDefinition.model_validate({**base, "role": "Edited while running"}))
        assert edited.latest_version == 2
        gate.set()
        worker.join(10)
        provider.gate = None
        assert outcome["o"].agent_version == 1 and "Tester" in provider.requests[-1].system and "Edited while running" not in provider.requests[-1].system
        second = asyncio.run(service().run(sa, "pg-agent", "next task"))
        assert second.agent_version == 2 and "Edited while running" in provider.requests[-1].system
        assert pg_db.rows(f"SELECT agent_version_id FROM agent_executions WHERE id = '{outcome['o'].execution_id}'") == [(v1,)]

        # constraints name themselves
        store = ExecutionStore(sf)
        resolved = agents.resolve(sa, "pg-agent")

        def row(c, **o):
            v = {"id": uuid.uuid4(), "user_id": uid, "workspace_id": ws, "agent_source": "builtin", "agent_key": "sales", "agent_name": "Sales",
                 "agent_version_id": None, "agent_version": None, "status": "running", "task": "t", "result": None, "error_code": None, **o}
            done = "NULL" if v["status"] == "running" else "now()"
            c.execute(text(f"INSERT INTO agent_executions ({', '.join(v)}, created_at, started_at, completed_at) VALUES "
                           f"({', '.join(':' + k for k in v)}, now(), now(), {done})"), v)

        _rejected(engine, "ck_agent_executions_source", lambda c: row(c, agent_source="robot"))
        _rejected(engine, "ck_agent_executions_status", lambda c: row(c, status="queued"))
        _rejected(engine, "ck_agent_executions_version_pin", lambda c: row(c, agent_source="custom", agent_key="pg-agent"))
        _rejected(engine, "ck_agent_executions_version_pin", lambda c: row(c, agent_version_id=resolved.version_id, agent_version=2))
        _rejected(engine, "ck_agent_executions_outcome", lambda c: row(c, status="failed"))
        _rejected(engine, "ck_agent_executions_outcome", lambda c: row(c, status="succeeded", error_code="timeout"))
        _rejected(engine, "fk_agent_executions_user_id_users", lambda c: row(c, user_id=uuid.uuid4()))
        _rejected(engine, "fk_agent_executions_workspace_id_workspaces", lambda c: row(c, workspace_id=uuid.uuid4()))
        _rejected(engine, "fk_agent_executions_agent_version_id_custom_agent_versions",
                  lambda c: row(c, agent_source="custom", agent_key="x", agent_version_id=uuid.uuid4(), agent_version=1))

        # guards: finished rows are frozen, the pin and identity are fixed from the start, nothing is deleted
        for statement in ("UPDATE agent_executions SET result = 'tampered'", "UPDATE agent_executions SET status = 'running', completed_at = NULL",
                          "UPDATE agent_executions SET agent_version = 9", "DELETE FROM agent_executions"):
            with pytest.raises(DBAPIError) as exc:
                with engine.begin() as conn:
                    conn.execute(text(statement))
            assert "append-only after completion" in str(exc.value), statement
        running = store.start(sa, resolved, "PG Agent", "in flight", provider="groq", model="m")
        for statement in (f"UPDATE agent_executions SET agent_version_id = '{v1}' WHERE id = '{running}'",
                          f"UPDATE agent_executions SET agent_key = 'other' WHERE id = '{running}'",
                          f"UPDATE agent_executions SET user_id = '{uid2}' WHERE id = '{running}'"):
            with pytest.raises(DBAPIError) as exc:
                with engine.begin() as conn:
                    conn.execute(text(statement))
            assert "append-only after completion" in str(exc.value), statement
        assert pg_db.rows("SELECT tgname, tgenabled FROM pg_trigger WHERE tgrelid = 'agent_executions'::regclass AND NOT tgisinternal") == [("agent_executions_guard", "O")]

        # orphan sweep, ownership
        assert store.fail_orphaned() == 1 and store.get(sa, running).error_code == "interrupted"
        assert [v.agent_key for v in store.list(sb)["items"]] == []
        from agentplatform.execution import ExecutionNotFound

        with pytest.raises(ExecutionNotFound):
            store.get(sb, custom.execution_id)
        assert store.get(sa, custom.execution_id).agent_version == 1

        # concurrent executions: all succeed, distinct ids, nothing left running
        results = []

        def many(i):
            results.append(asyncio.run(service().run(sa if i % 2 else sb, "sales", f"parallel {i}")).execution_id)

        threads = [threading.Thread(target=many, args=(i,)) for i in range(12)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        assert len(set(results)) == 12 and store.count_running() == 0
        assert pg_db.rows("SELECT count(*) FROM agent_executions WHERE task LIKE 'parallel %'") == [(12,)]

        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts=dict(compare_type=True, compare_server_default=True))
            assert [d for d in compare_metadata(ctx, Base.metadata) if "alembic_version" not in str(d)] == []
    finally:
        engine.dispose()

    command.downgrade(alembic_env(pg_db), "0005_custom_agents")
    assert pg_db.rows("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'agent_executions'") == []
    assert pg_db.rows("SELECT count(*) FROM pg_proc WHERE proname = 'agent_executions_guard'") == [(0,)]
    assert pg_db.rows("SELECT count(*) FROM custom_agents") == [(1,)] and pg_db.rows("SELECT count(*) FROM custom_agent_versions") == [(2,)]
    command.upgrade(alembic_env(pg_db), "head")
    assert pg_db.rows("SELECT version_num FROM alembic_version") == [(HEAD,)] and pg_db.rows("SELECT count(*) FROM agent_executions") == [(0,)]



def test_plans_revisions_pinning_ownership_and_concurrency_on_postgresql(pg_db, alembic_env):
    """M3-A on a real PostgreSQL: native uuids, named constraints, the insert-only trigger, composite foreign keys,
    version pins, ownership isolation, one winner among concurrent revisions and reversibility."""
    import asyncio
    import json
    import threading

    from sqlalchemy.orm import sessionmaker

    from agentplatform.execution import Limits
    from agentplatform.planning import ProposedTask
    from agentplatform.plans import PlanConflict, PlanNotFound, PlanningService
    from agentplatform.providers import ProviderRegistry
    from agentplatform.schema import AgentDefinition
    from agentplatform.service import AgentService
    from auth.principal import Principal
    from authz.workspaces import authorize_personal_scope
    from memory.models import Base
    from tests.api_support import FakeProvider

    command.upgrade(alembic_env(pg_db), "head")
    engine = create_engine(pg_db.url, pool_size=20)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    uid, uid2, ws, ws2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as c:
        for u, e, w in ((uid, "a@example.com", ws), (uid2, "b@example.com", ws2)):
            _insert_user(c, u, e)
            _insert_workspace(c, w, "P", True, u)
            _insert_membership(c, w, u, "owner")

    def scope_for(user, email):
        return authorize_personal_scope(sf, Principal(user_id=user, provider="google", provider_subject=str(user), email=email, is_active=True))

    def task(id, agent, deps=()):
        return {"id": id, "title": f"Task {id}", "description": f"Do the work for task {id} carefully.", "expected_output": "A short result.",
                "agent": agent, "rationale": "Fits.", "depends_on": list(deps)}

    provider = FakeProvider()
    provider.reply = lambda request: json.dumps({"feasible": True, "interpretation": "A launch plan for the product.", "unsupported": [],
                                                 "tasks": [task("t1", "research"), task("t2", "pg-agent", ["t1"]), task("t3", "sales", ["t2"])]})
    service = PlanningService(sf, providers=ProviderRegistry({"groq": lambda: provider}), limits=Limits(total=50, per_user=50))
    base = {"name": "PG Agent", "description": "An agent used to test plans on PostgreSQL.", "role": "Tester",
            "purpose": "Exercise version pinning on a real server.", "instructions": "You are a test agent used only by the test-suite.",
            "model": {"provider": "groq", "name": "llama-3.3-70b-versatile"}}
    try:
        sa, sb = scope_for(uid, "a@example.com"), scope_for(uid2, "b@example.com")
        agents = AgentService(sf)
        agent = agents.create(sa, "pg-agent", AgentDefinition.model_validate(base))
        plan = asyncio.run(service.create_plan(sa, "Launch the product to the first customers within a month."))
        assert (plan.status, plan.latest_revision) == ("draft", 1) and plan.current.levels == (("t1",), ("t2",), ("t3",))
        v1 = agent.version.version_id
        assert pg_db.rows("SELECT task_key, agent_version_id, agent_version FROM plan_tasks ORDER BY position") == [("t1", None, None), ("t2", v1, 1), ("t3", None, None)]
        assert pg_db.rows("SELECT data_type FROM information_schema.columns WHERE table_name = 'plan_tasks' AND column_name IN ('id', 'revision_id', 'agent_version_id') ORDER BY 1") == [("uuid",)] * 3
        assert sorted(r[0] for r in pg_db.rows("SELECT conname FROM pg_constraint WHERE conrelid IN ('plans'::regclass, 'plan_revisions'::regclass, 'plan_tasks'::regclass, "
                                               "'plan_task_dependencies'::regclass) AND contype IN ('c', 'f', 'u')")) == sorted([
            "ck_plans_status", "ck_plans_latest_revision_positive", "ck_plans_approval", "fk_plans_user_id_users", "fk_plans_workspace_id_workspaces",
            "ck_plan_revisions_revision_positive", "ck_plan_revisions_origin", "fk_plan_revisions_plan_id_plans", "uq_plan_revisions_plan_id_revision",
            "ck_plan_tasks_source", "ck_plan_tasks_complexity", "ck_plan_tasks_priority", "ck_plan_tasks_position", "ck_plan_tasks_version_pin",
            "fk_plan_tasks_revision_id_plan_revisions", "fk_plan_tasks_agent_version_id_custom_agent_versions", "uq_plan_tasks_revision_id_task_key",
            "uq_plan_tasks_revision_id_position", "ck_plan_task_dependencies_not_self", "fk_plan_task_dependencies_task", "fk_plan_task_dependencies_depends_on"])

        # insert-only guard trigger on the three revision tables, mutable header
        for statement in ("UPDATE plan_revisions SET interpretation = 'x'", "DELETE FROM plan_revisions", "UPDATE plan_tasks SET title = 'x'", "DELETE FROM plan_tasks",
                          "UPDATE plan_task_dependencies SET depends_on_key = 't3'", "DELETE FROM plan_task_dependencies"):
            with pytest.raises(DBAPIError) as exc:
                with engine.begin() as conn:
                    conn.execute(text(statement))
            assert "insert-only" in str(exc.value), statement
        assert pg_db.rows("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'plan%_immutable' AND NOT tgisinternal") == [(3,)]

        rid = pg_db.rows("SELECT id FROM plan_revisions")[0][0]

        def bad(sql, constraint):
            with pytest.raises(DBAPIError) as exc:
                with engine.begin() as conn:
                    conn.execute(text(sql))
            assert constraint in str(exc.value), (sql, str(exc.value)[:200])

        bad(f"INSERT INTO plan_task_dependencies VALUES ('{rid}', 't1', 't1')", "ck_plan_task_dependencies_not_self")
        bad(f"INSERT INTO plan_task_dependencies VALUES ('{rid}', 't1', 'ghost')", "fk_plan_task_dependencies_depends_on")
        bad(f"INSERT INTO plan_task_dependencies VALUES ('{rid}', 't2', 't1')", "plan_task_dependencies_pkey")
        bad("UPDATE plans SET status = 'approved'", "ck_plans_approval")

        # ownership
        with pytest.raises(PlanNotFound):
            service.store.get(sb, plan.id)
        assert service.store.list_plans(sb)["items"] == [] and [p.id for p, _ in service.store.list_plans(sa)["items"]] == [plan.id]

        # an agent edit never changes the existing plan; an approval pins its revision
        agents.update(sa, agent.id, 1, AgentDefinition.model_validate({**base, "role": "Second role"}))
        assert service.store.get(sa, plan.id).current.tasks[1].agent_version == 1
        assert service.approve(sa, plan.id, 1).approved_revision == 1

        # concurrent revisions from one base: exactly one winner
        outcomes, barrier = [], threading.Barrier(10)

        def revise(i):
            barrier.wait()
            try:
                service.revise(sa, plan.id, 1, None, [ProposedTask.model_validate(task(f"r{i}", "research"))])
                outcomes.append("won")
            except PlanConflict:
                outcomes.append("conflict")

        threads = [threading.Thread(target=revise, args=(i,)) for i in range(10)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        assert sorted(outcomes) == ["conflict"] * 9 + ["won"]
        assert pg_db.rows("SELECT revision FROM plan_revisions ORDER BY revision") == [(1,), (2,)]
        assert pg_db.rows("SELECT latest_revision, status, approved_revision FROM plans") == [(2, "draft", None)]
        assert pg_db.rows("SELECT count(*) FROM plan_tasks") == [(4,)]

        # concurrent creation across users (only built-in agents: user b has no custom agent)
        provider.reply = lambda request: json.dumps({"feasible": True, "interpretation": "A launch plan for the product.", "unsupported": [],
                                                     "tasks": [task("t1", "research"), task("t2", "sales", ["t1"])]})
        ids = []
        creators = [threading.Thread(target=lambda i=i: ids.append(asyncio.run(service.create_plan(sa if i % 2 else sb, f"Plan number {i} for the launch.")).id)) for i in range(8)]
        [t.start() for t in creators]
        [t.join(30) for t in creators]
        assert len(set(ids)) == 8

        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts=dict(compare_type=True, compare_server_default=True))
            assert [d for d in compare_metadata(ctx, Base.metadata) if "alembic_version" not in str(d)] == []
    finally:
        engine.dispose()

    command.downgrade(alembic_env(pg_db), "0006_agent_executions")
    assert pg_db.rows("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name LIKE 'plan%'") == []
    assert pg_db.rows("SELECT count(*) FROM pg_proc WHERE proname = 'plan_rows_immutable'") == [(0,)]
    assert pg_db.rows("SELECT count(*) FROM custom_agents") == [(1,)] and pg_db.rows("SELECT count(*) FROM custom_agent_versions") == [(2,)]
    command.upgrade(alembic_env(pg_db), "head")
    assert pg_db.rows("SELECT version_num FROM alembic_version") == [(HEAD,)] and pg_db.rows("SELECT count(*) FROM plans") == [(0,)]


def test_plan_runs_gate_state_machine_and_concurrency_on_postgresql(pg_db, alembic_env):
    """M3-B on a real PostgreSQL: native uuids, named constraints, partial unique indexes, the transition triggers, the
    composite references, concurrent starts (exactly one run), a real run through the M2-B runtime, the orphan sweep and
    reversibility."""
    import asyncio
    import json
    import threading

    from sqlalchemy.orm import sessionmaker

    from agentplatform.execution import ExecutionService, ExecutionStore, Limits
    from agentplatform.plan_runs import AlreadyExecuted, PlanRunService, PlanRunStore, RunInProgress, RunRegistry
    from agentplatform.plans import PlanningService
    from agentplatform.providers import ProviderRegistry
    from agentplatform.schema import AgentDefinition
    from agentplatform.service import AgentService, ResolvedAgent
    from auth.principal import Principal
    from authz.workspaces import authorize_personal_scope
    from memory.models import Base
    from memory.repository import MemoryRepository
    from memory.short_term import ShortTermMemory
    from tests.api_support import FakeProvider, FakeRedis

    command.upgrade(alembic_env(pg_db), "head")
    engine = create_engine(pg_db.url, pool_size=30)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    uid, uid2, ws, ws2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as c:
        for u, e, w in ((uid, "a@example.com", ws), (uid2, "b@example.com", ws2)):
            _insert_user(c, u, e)
            _insert_workspace(c, w, "P", True, u)
            _insert_membership(c, w, u, "owner")

    def scope_for(user, email):
        return authorize_personal_scope(sf, Principal(user_id=user, provider="google", provider_subject=str(user), email=email, is_active=True))

    class Orchestrator:
        def __init__(self):
            self.gate = None
            self.calls = []

        async def run_agent(self, name, task, scope):
            self.calls.append(name)
            if self.gate is not None:
                await self.gate.wait()
            return f"builtin {name}"

        run_with_delegation = run_agent

    def task(id, agent, deps=()):
        return {"id": id, "title": f"Task {id}", "description": f"Do the work for task {id} carefully.", "expected_output": "A short result.",
                "agent": agent, "rationale": "Fits.", "depends_on": list(deps)}

    provider, orch, limits = FakeProvider(), Orchestrator(), Limits(total=50, per_user=50)
    providers = ProviderRegistry({"groq": lambda: provider})
    executions = ExecutionService(sf, orchestrator=orch, providers=providers, short_term=ShortTermMemory(client=FakeRedis()), long_term=MemoryRepository(sf), limits=limits)
    svc = PlanRunService(sf, executions=executions, registry=RunRegistry(), busy_poll=0.01)
    planning = PlanningService(sf, providers=providers, limits=limits)
    base = {"name": "PG Agent", "description": "An agent used to test plan runs on PostgreSQL.", "role": "Tester",
            "purpose": "Exercise version pinning on a real server.", "instructions": "You are a test agent used only by the test-suite.",
            "model": {"provider": "groq", "name": "llama-3.3-70b-versatile"}}

    def make_plan(scope, tasks):
        provider.reply = lambda request: json.dumps({"feasible": True, "interpretation": "A launch plan for the product.", "unsupported": [], "tasks": tasks})
        plan = asyncio.run(planning.create_plan(scope, "Launch the product to the first customers within a month."))
        planning.approve(scope, plan.id, 1)
        return plan.id

    try:
        sa, sb = scope_for(uid, "a@example.com"), scope_for(uid2, "b@example.com")
        agents = AgentService(sf)
        agent = agents.create(sa, "pg-agent", AgentDefinition.model_validate(base))
        provider.reply = lambda request: "custom reply"

        # a real run: dependency order, pinned custom version, links to the M2-B records
        plan_id = make_plan(sa, [task("t1", "research"), task("t2", "pg-agent", ["t1"]), task("t3", "sales", ["t2"])])
        agents.update(sa, agent.id, 1, AgentDefinition.model_validate({**base, "role": "Second role"}))  # edited after approval

        async def run_once(plan, scope):
            started = await svc.start(scope, plan)
            await svc.registry.wait(started.id, 30)
            return svc.store.get(scope, started.id)

        run = asyncio.run(run_once(plan_id, sa))
        assert run.status == "succeeded" and [t.status for t in run.tasks] == ["succeeded"] * 3 and orch.calls == ["research", "sales"]
        assert [(t.agent_version, t.agent_version_id) for t in run.tasks if t.agent_source == "custom"] == [(1, agent.version.version_id)]
        assert pg_db.rows("SELECT data_type FROM information_schema.columns WHERE table_name = 'plan_task_runs' AND column_name IN ('id', 'run_id', 'revision_id', 'execution_id') ORDER BY 1") == [("uuid",)] * 4
        assert pg_db.rows("SELECT count(*) FROM plan_task_runs t JOIN agent_executions e ON e.id = t.execution_id WHERE e.status = 'succeeded'") == [(3,)]
        assert sorted(r[0] for r in pg_db.rows("SELECT indexname FROM pg_indexes WHERE tablename = 'plan_runs'")) == sorted([
            "plan_runs_pkey", "uq_plan_runs_plan_id_attempt", "uq_plan_runs_id_revision_id", "uq_plan_runs_one_active", "uq_plan_runs_one_success", "ix_plan_runs_plan_id_created_at"])
        assert pg_db.rows("SELECT count(*) FROM pg_indexes WHERE indexname IN ('uq_plan_runs_one_active', 'uq_plan_runs_one_success') AND indexdef LIKE '%WHERE%'") == [(2,)]
        with pytest.raises(AlreadyExecuted):
            asyncio.run(svc.start(sa, plan_id))

        # state-machine triggers
        def bad(sql, text_):
            with pytest.raises(DBAPIError) as exc:
                with engine.begin() as conn:
                    conn.execute(text(sql))
            assert text_ in str(exc.value), (sql, str(exc.value)[:200])

        bad("DELETE FROM plan_runs", "never deleted")
        bad("DELETE FROM plan_task_runs", "never deleted")
        bad("UPDATE plan_runs SET status = 'failed'", "frozen")
        bad("UPDATE plan_runs SET attempt = 7", "frozen")
        bad("UPDATE plan_task_runs SET status = 'running'", "legal state transitions")
        bad("UPDATE plan_task_runs SET agent_key = 'ceo'", "legal state transitions")
        assert pg_db.rows("SELECT count(*) FROM pg_trigger WHERE tgname IN ('plan_runs_guard', 'plan_task_runs_guard') AND NOT tgisinternal") == [(2,)]
        bad(f"INSERT INTO plan_task_runs (id, run_id, revision_id, task_key, position, level, agent_source, agent_key, agent_name, status, created_at) "
            f"VALUES ('{uuid.uuid4()}', '{run.id}', '{pg_db.rows('SELECT revision_id FROM plan_runs')[0][0]}', 'ghost', 9, 0, 'builtin', 'sales', 'Sales', 'pending', now())", "fk_plan_task_runs_task")

        # concurrent starts of one plan: exactly one run
        provider.reply = lambda request: "custom reply"
        orch.gate = None
        plan2 = make_plan(sa, [task("a", "research"), task("b", "sales", ["a"])])

        async def concurrent():
            orch.gate = asyncio.Event()
            results = await asyncio.gather(*[svc.start(sa, plan2) for _ in range(8)], return_exceptions=True)
            wins = [r for r in results if not isinstance(r, Exception)]
            assert len(wins) == 1 and all(isinstance(r, RunInProgress) for r in results if isinstance(r, Exception)), results
            orch.gate.set()
            await svc.registry.wait(wins[0].id, 30)
            return svc.store.get(sa, wins[0].id)

        second = asyncio.run(concurrent())
        assert second.status == "succeeded" and pg_db.rows(f"SELECT count(*) FROM plan_runs WHERE plan_id = '{second.plan_id}'") == [(1,)]
        assert orch.calls.count("research") == 2 and pg_db.rows("SELECT count(*) FROM plan_task_runs") == [(5,)]  # each task of each run dispatched once

        # ownership
        from agentplatform.plan_runs import RunNotFound

        with pytest.raises(RunNotFound):
            svc.store.get(sb, run.id)

        # orphan sweep after a "crash"
        orch.gate = None
        plan3 = make_plan(sa, [task("a", "research"), task("b", "sales", ["a"])])
        store, exec_store = PlanRunStore(sf), ExecutionStore(sf)
        run_id = store.create(sa, plan3)
        _, tasks = store.load_for_driver(run_id)
        eid = exec_store.start(sa, ResolvedAgent("builtin", "research", None, None, None), "Research", "t", provider="groq", model=None)
        store.mark_running(tasks[0].id, eid)
        assert exec_store.fail_orphaned() == 0 and store.recover_expired() == []  # M4-A1: the owner's lease is live, nothing is touched
        pg_db.run("UPDATE run_leases SET expires_at = (now() AT TIME ZONE 'utc') - interval '1 minute'")  # the owner stopped renewing
        assert store.recover_expired() == [run_id]
        swept = store.get(sa, run_id)
        assert (swept.status, swept.error_code, [t.status for t in swept.tasks]) == ("failed", "interrupted", ["failed", "skipped"])

        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts=dict(compare_type=True, compare_server_default=True))
            assert [d for d in compare_metadata(ctx, Base.metadata) if "alembic_version" not in str(d)] == []
    finally:
        engine.dispose()

    command.downgrade(alembic_env(pg_db), "0007_plans")
    assert pg_db.rows("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('plan_runs', 'plan_task_runs')") == []
    assert pg_db.rows("SELECT count(*) FROM pg_proc WHERE proname IN ('plan_runs_guard', 'plan_task_runs_guard')") == [(0,)]
    assert pg_db.rows("SELECT count(*) FROM plans") [0][0] >= 3 and pg_db.rows("SELECT count(*) FROM agent_executions")[0][0] >= 5  # earlier data is kept
    command.upgrade(alembic_env(pg_db), "head")
    assert pg_db.rows("SELECT version_num FROM alembic_version") == [(HEAD,)] and pg_db.rows("SELECT count(*) FROM plan_runs") == [(0,)]


# ------------------------------------------------------------------------------------------------ M4-A1: run ownership

def _lease_runtime(pg_db, alembic_env):
    """A migrated disposable database with two users, a fake built-in orchestrator and the REAL M2-B runtime (no network)."""
    import asyncio
    import json

    from sqlalchemy.orm import sessionmaker

    from agentplatform.execution import ExecutionService, Limits
    from agentplatform.plans import PlanningService
    from agentplatform.providers import ProviderRegistry
    from auth.principal import Principal
    from authz.workspaces import authorize_personal_scope
    from memory.repository import MemoryRepository
    from memory.short_term import ShortTermMemory
    from tests.api_support import FakeProvider, FakeRedis

    command.upgrade(alembic_env(pg_db), "head")
    engine = create_engine(pg_db.url, pool_size=40, max_overflow=20)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    uid, uid2, ws, ws2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as c:
        for u, e, w in ((uid, "a@example.com", ws), (uid2, "b@example.com", ws2)):
            _insert_user(c, u, e)
            _insert_workspace(c, w, "P", True, u)
            _insert_membership(c, w, u, "owner")

    def scope_for(user, email):
        return authorize_personal_scope(sf, Principal(user_id=user, provider="google", provider_subject=str(user), email=email, is_active=True))

    class Orchestrator:
        agent_names = ("ceo", "sales", "research", "marketing", "finance", "hiring", "support")

        def __init__(self):
            self.calls, self.behaviour = [], None

        async def run_agent(self, name, task, scope):
            self.calls.append(name)
            return await self.behaviour(name, task, scope) if self.behaviour else f"builtin {name}"

        run_with_delegation = run_agent

    def task(id, agent, deps=()):
        return {"id": id, "title": f"Task {id}", "description": f"Do the work for task {id} carefully.", "expected_output": "A short result.",
                "agent": agent, "rationale": "Fits.", "depends_on": list(deps)}

    provider, orch, limits = FakeProvider(), Orchestrator(), Limits(total=50, per_user=50)
    providers = ProviderRegistry({"groq": lambda: provider})
    executions = ExecutionService(sf, orchestrator=orch, providers=providers, short_term=ShortTermMemory(client=FakeRedis()), long_term=MemoryRepository(sf), limits=limits)
    planning = PlanningService(sf, providers=providers, limits=limits)

    def make_plan(scope, tasks, objective="Launch the product to the first customers within a month."):
        provider.reply = lambda request: json.dumps({"feasible": True, "interpretation": "A launch plan for the product.", "unsupported": [], "tasks": tasks})
        plan = asyncio.run(planning.create_plan(scope, objective))
        planning.approve(scope, plan.id, 1)
        return plan.id

    chain = [task("a", "research"), task("b", "sales", ["a"]), task("c", "ceo", ["b"])]
    return SimpleNamespace(engine=engine, sf=sf, sa=scope_for(uid, "a@example.com"), sb=scope_for(uid2, "b@example.com"), uid=uid, orch=orch, executions=executions,
                           make_plan=make_plan, chain=chain, task=task)


def test_run_leases_on_postgresql_schema_database_clock_fencing_and_concurrent_takeover(pg_db, alembic_env):
    """M4-A1 on a real PostgreSQL: the lease table and its constraints; expiry decided by the DATABASE clock (a wildly wrong
    application clock changes nothing); the row lock of a fenced writer holds back a takeover; exactly one of many concurrent
    takeovers/recoveries wins; stale writers are fenced; the accepted plan_runs triggers are untouched; the migration is reversible."""
    import threading
    from datetime import datetime

    from agentplatform.plan_runs import PlanRunStore
    from agentplatform.run_leases import Fence, LeaseConfig, LeaseLost, LeaseStore, Renewal

    rt = _lease_runtime(pg_db, alembic_env)
    try:
        # -- schema
        assert pg_db.rows("SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name = 'run_leases' ORDER BY ordinal_position") == [
            ("run_id", "uuid", "NO"), ("owner_id", "character varying", "NO"), ("generation", "integer", "NO"), ("expires_at", "timestamp without time zone", "NO"),
            ("cancel_requested_at", "timestamp without time zone", "YES"), ("created_at", "timestamp without time zone", "NO"), ("updated_at", "timestamp without time zone", "NO")]
        assert sorted(r[0] for r in pg_db.rows("SELECT conname FROM pg_constraint WHERE conrelid = 'run_leases'::regclass")) == [
            "ck_run_leases_generation_positive", "fk_run_leases_run_id_plan_runs", "run_leases_pkey"]
        assert pg_db.rows("SELECT indexname FROM pg_indexes WHERE tablename = 'run_leases' AND indexname = 'ix_run_leases_expires_at'") == [("ix_run_leases_expires_at",)]
        assert pg_db.rows("SELECT tgname FROM pg_trigger WHERE tgrelid = 'run_leases'::regclass AND NOT tgisinternal") == []  # a lease is mutable: no trigger
        assert pg_db.rows("SELECT tgname FROM pg_trigger WHERE tgrelid = 'plan_runs'::regclass AND NOT tgisinternal") == [("plan_runs_guard",)]  # the accepted trigger is intact

        store = PlanRunStore(rt.sf, owner_id="owner-a", config=LeaseConfig())
        plan_id = rt.make_plan(rt.sa, rt.chain)
        run_id = store.create(rt.sa, plan_id)
        assert pg_db.rows(f"SELECT owner_id, generation, cancel_requested_at IS NULL FROM run_leases WHERE run_id = '{run_id}'") == [("owner-a", 1, True)]
        with pytest.raises(DBAPIError):
            pg_db.run(f"UPDATE run_leases SET generation = 0 WHERE run_id = '{run_id}'")  # the CHECK
        # the term was written with DATABASE time: within seconds of the server's own clock, whatever the application clock says
        gap = pg_db.rows(f"SELECT extract(epoch FROM (expires_at - (now() AT TIME ZONE 'utc'))) FROM run_leases WHERE run_id = '{run_id}'")[0][0]
        assert 25 < float(gap) <= 30

        # -- expiry is decided by the database clock, not by the application's
        wrong_clock = PlanRunStore(rt.sf, owner_id="owner-wrong-clock", config=LeaseConfig(), clock=lambda: datetime(2100, 1, 1))
        assert wrong_clock.leases.expired_run_ids() == [] and wrong_clock.recover_expired() == []  # the app thinks it is 2100: irrelevant
        pg_db.run(f"UPDATE run_leases SET expires_at = (now() AT TIME ZONE 'utc') - interval '1 second' WHERE run_id = '{run_id}'")
        assert LeaseStore(rt.sf, clock=lambda: datetime(1999, 1, 1)).expired_run_ids() == [run_id]  # ... and in the other direction

        # -- a fenced writer holds the lease row: a takeover has to wait for it, then wins; the old owner is fenced afterwards
        fence_old = Fence(run_id, "owner-a", 1)
        assert LeaseStore(rt.sf).renew(fence_old, 30) == Renewal(False)  # expired by the database clock: the owner cannot resurrect it
        taker = LeaseStore(rt.sf)
        order, result = [], {}
        locked, writer_may_commit = threading.Event(), threading.Event()

        def writer():
            with rt.sf() as session, session.begin():
                taker.touch(session, fence_old)  # the fencing check = UPDATE of the lease row: it takes the row lock
                locked.set()
                writer_may_commit.wait(10)
                order.append("writer committed")

        def takeover():
            locked.wait(10)
            result["fence"] = taker.takeover(run_id, "owner-b", 30)
            order.append("takeover finished")

        tw, tt = threading.Thread(target=writer), threading.Thread(target=takeover)
        tw.start()
        tt.start()
        time.sleep(1.0)
        assert "takeover finished" not in order and tt.is_alive()  # blocked behind the writer's row lock
        writer_may_commit.set()
        tw.join(15)
        tt.join(15)
        assert order == ["writer committed", "takeover finished"] and result["fence"] == Fence(run_id, "owner-b", 2)
        with pytest.raises(LeaseLost):
            with rt.sf() as session, session.begin():
                taker.touch(session, fence_old)  # taken over first: the stale writer's check matches no row
        assert LeaseStore(rt.sf).renew(fence_old, 30) == Renewal(False) and LeaseStore(rt.sf).release(fence_old) is False

        # -- many concurrent takeovers and recoveries: exactly one winner
        pg_db.run(f"UPDATE run_leases SET expires_at = (now() AT TIME ZONE 'utc') - interval '1 second' WHERE run_id = '{run_id}'")
        stores = [PlanRunStore(rt.sf, owner_id=f"racer-{i}", config=LeaseConfig()) for i in range(12)]
        barrier, fences, errors = threading.Barrier(12), [], []

        def race(s):
            try:
                barrier.wait()
                fences.append(s.leases.takeover(run_id, s.owner_id, 30))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=race, args=(s,)) for s in stores]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        winners = [f for f in fences if f is not None]
        assert errors == [] and len(winners) == 1 and winners[0].generation == 3
        assert pg_db.rows(f"SELECT owner_id, generation FROM run_leases WHERE run_id = '{run_id}'") == [(winners[0].owner_id, 3)]

        pg_db.run(f"UPDATE run_leases SET expires_at = (now() AT TIME ZONE 'utc') - interval '1 second' WHERE run_id = '{run_id}'")
        outcomes, errors = [], []
        barrier = threading.Barrier(12)

        def recover(s):
            try:
                barrier.wait()
                outcomes.append(s.recover_run(run_id))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=recover, args=(s,)) for s in stores]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        assert errors == [] and [o for o in outcomes if o is not None] == ["failed"]  # recovered exactly once
        run = store.get(rt.sa, run_id)
        assert (run.status, run.error_code, [t.status for t in run.tasks]) == ("failed", "interrupted", ["skipped"] * 3)
        assert all(s.recover_expired() == [] for s in stores) and rt.orch.calls == []  # idempotent; nothing was dispatched

        # -- reversible
        before = pg_db.rows("SELECT count(*) FROM plan_runs")
        command.downgrade(alembic_env(pg_db), "0008_plan_runs")
        assert pg_db.rows("SELECT count(*) FROM information_schema.tables WHERE table_name = 'run_leases'") == [(0,)] and pg_db.rows("SELECT count(*) FROM plan_runs") == before
        command.upgrade(alembic_env(pg_db), "head")
        assert pg_db.rows("SELECT version_num FROM alembic_version") == [(HEAD,)] and pg_db.rows("SELECT count(*) FROM run_leases") == [(0,)]
    finally:
        rt.engine.dispose()


def test_run_ownership_cancellation_and_a_killed_process_on_postgresql(pg_db, alembic_env):
    """M4-A1 on PostgreSQL through the real service: competing instances, a live owner untouched by another process, cancellation
    requested by a non-owner reaching the owner, and a REAL second OS process hard-killed mid-run and recovered after its lease
    expired (nothing dispatched again)."""
    import asyncio
    import queue
    import subprocess
    import threading

    from agentplatform.execution import ExecutionStore
    from agentplatform.plan_runs import PlanRunService, PlanRunStore, RunInProgress, RunNotFound, RunRegistry
    from agentplatform.run_leases import LeaseConfig

    rt = _lease_runtime(pg_db, alembic_env)
    fast = LeaseConfig(ttl_seconds=1.5, heartbeat_seconds=0.5, sweep_seconds=0.5, shutdown_grace_seconds=2.0)

    def service(registry):
        return PlanRunService(rt.sf, executions=rt.executions, registry=registry, busy_poll=0.01)

    try:
        plan_id = rt.make_plan(rt.sa, rt.chain)
        reg_a, reg_b = RunRegistry(config=fast), RunRegistry(config=fast)
        svc_a, svc_b = service(reg_a), service(reg_b)
        cancelled = []

        async def scenario():
            async def stuck(name, text, scope):
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancelled.append(name)
                    raise

            rt.orch.behaviour = stuck
            results = await asyncio.gather(svc_a.start(rt.sa, plan_id), svc_b.start(rt.sa, plan_id), return_exceptions=True)
            started = [r for r in results if not isinstance(r, BaseException)]
            assert len(started) == 1 and sum(isinstance(r, RunInProgress) for r in results) == 1  # competing starts: one owner
            run_id = started[0].id
            owner, other = (reg_a, svc_b) if reg_a.get(run_id) else (reg_b, svc_a)
            assert pg_db.rows(f"SELECT owner_id, generation FROM run_leases WHERE run_id = '{run_id}'") == [(owner.owner_id, 1)]
            for _ in range(200):
                if pg_db.rows("SELECT count(*) FROM plan_task_runs WHERE status = 'running'") == [(1,)]:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(fast.ttl_seconds * 1.6)  # more than a lease term: only the heartbeat (DB-clock lease) keeps it owned
            assert ExecutionStore(rt.sf).fail_orphaned() == 0 and PlanRunStore(rt.sf, config=fast).recover_expired() == []  # a second process changes nothing
            assert svc_a.store.get(rt.sa, run_id).status == "running"
            started_at = time.monotonic()
            view = await other.cancel(rt.sa, run_id)  # a non-owner requests the cancellation
            assert time.monotonic() - started_at < fast.heartbeat_seconds * 1.5 + 2
            await owner.wait(run_id, 20)
            return run_id, view

        run_id, view = asyncio.run(scenario())
        assert (view.status, view.error_code) == ("cancelled", "cancelled") and cancelled == ["research"] and rt.orch.calls == ["research"]
        final = svc_a.store.get(rt.sa, run_id)
        assert [t.status for t in final.tasks] == ["cancelled"] * 3
        assert pg_db.rows("SELECT status, error_code FROM agent_executions") == [("cancelled", "cancelled")]

        # foreign scope: the other user cannot cancel it (404 semantics)
        with pytest.raises(RunNotFound):
            asyncio.run(svc_a.cancel(rt.sb, run_id))

        # -- a REAL process killed mid-run
        second_plan = rt.make_plan(rt.sa, rt.chain)
        child = subprocess.Popen([sys.executable, str(BACKEND / "tests" / "crash_child.py"), pg_db.url, str(rt.uid), str(second_plan), "2.0", "0.5", "pg-child-1"],
                                 cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines = queue.Queue()
        threading.Thread(target=lambda: [lines.put(line.strip()) for line in child.stdout], daemon=True).start()

        def wait_line(prefix, timeout=60.0):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line.startswith(prefix):
                    return line
            raise AssertionError(f"the child never printed {prefix!r}")

        other = PlanRunStore(rt.sf, config=LeaseConfig(ttl_seconds=2.0, heartbeat_seconds=0.5))
        try:
            crashed_run = uuid.UUID(wait_line("RUN ").split()[1])
            wait_line("CALL research")
            time.sleep(2.6)  # longer than the lease term: the child's heartbeat keeps the run owned
            assert other.recover_expired() == [] and child.poll() is None
            assert pg_db.rows(f"SELECT owner_id FROM run_leases WHERE run_id = '{crashed_run}'") == [("pg-child-1",)]
            child.kill()  # a hard crash: no cleanup, no release
            child.wait(timeout=20)
            deadline, closed = time.monotonic() + 20, []
            while not closed and time.monotonic() < deadline:
                closed = other.recover_expired()
                time.sleep(0.2)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=20)
        assert closed == [crashed_run]
        crashed = svc_a.store.get(rt.sa, crashed_run)
        assert (crashed.status, crashed.error_code, [t.status for t in crashed.tasks]) == ("failed", "interrupted", ["failed", "skipped", "skipped"])
        assert pg_db.rows(f"SELECT e.status, e.error_code FROM plan_task_runs t JOIN agent_executions e ON e.id = t.execution_id WHERE t.run_id = '{crashed_run}'") == [("failed", "interrupted")]
        assert rt.orch.calls == ["research"] and other.recover_expired() == []  # the parent never dispatched it again; recovery is idempotent
    finally:
        rt.engine.dispose()


# ------------------------------------------------------------------------------------------------ M4-A2: durable orchestration

def test_orchestration_schema_events_retry_and_atomic_task_commits_on_postgresql(pg_db, alembic_env, monkeypatch):
    """M4-A2 on a real PostgreSQL: the five new tables, constraints and append-only triggers; events ordered under concurrent writers and
    written in the same transaction as their transition; an explicit retry reusing succeeded work, with one-retry-per-run under 12 racing
    threads; the atomic task start/finish rolled back by an injected failure; and reversibility of migration 0010."""
    import asyncio
    import threading

    from agentplatform import run_events
    from agentplatform.plan_runs import AlreadyExecuted, PlanRunService, PlanRunStore, RetryNotAllowed, RunInProgress, RunRegistry
    from agentplatform.providers import ProviderError
    from agentplatform.run_leases import LeaseConfig

    rt = _lease_runtime(pg_db, alembic_env)
    fast = LeaseConfig(ttl_seconds=5.0, heartbeat_seconds=1.0, sweep_seconds=1.0, shutdown_grace_seconds=2.0)
    try:
        # -- schema
        assert pg_db.rows("SELECT data_type FROM information_schema.columns WHERE table_name = 'execution_events' AND column_name = 'id'") == [("bigint",)]
        assert pg_db.rows("SELECT is_identity FROM information_schema.columns WHERE table_name = 'execution_events' AND column_name = 'id'") == [("YES",)] or pg_db.rows(
            "SELECT column_default FROM information_schema.columns WHERE table_name = 'execution_events' AND column_name = 'id'")[0][0] is not None
        assert sorted(r[0] for r in pg_db.rows("SELECT conname FROM pg_constraint WHERE conrelid = 'plan_run_retries'::regclass")) == [
            "fk_plan_run_retries_authorized_by_users", "fk_plan_run_retries_retry_of_run_id_plan_runs", "fk_plan_run_retries_run_id_plan_runs", "plan_run_retries_pkey",
            "uq_plan_run_retries_retry_of_run_id"]
        for table, trigger in (("execution_events", "execution_events_guard"), ("plan_run_retries", "plan_run_retries_guard"), ("plan_task_carryovers", "plan_task_carryovers_guard"),
                               ("execution_owners", "execution_owners_guard")):
            assert pg_db.rows(f"SELECT tgname FROM pg_trigger WHERE tgrelid = '{table}'::regclass AND NOT tgisinternal") == [(trigger,)]
        assert pg_db.rows("SELECT tgname FROM pg_trigger WHERE tgrelid = 'worker_processes'::regclass AND NOT tgisinternal") == []  # the heartbeat row is mutable

        chain = rt.chain
        plan_id = rt.make_plan(rt.sa, chain)

        async def failing(**errors):
            async def behaviour(name, text, scope):
                if name in errors:
                    raise errors[name]
                return f"result of {name}"

            rt.orch.behaviour = behaviour

        svc = PlanRunService(rt.sf, executions=rt.executions, registry=RunRegistry(config=fast), busy_poll=0.01)

        async def run_and_wait(start):
            started = await start()
            await svc.registry.wait(started.id, 60)
            return await asyncio.to_thread(svc.store.get, rt.sa, started.id)

        asyncio.run(failing(sales=ProviderError("rate_limited")))
        first = asyncio.run(run_and_wait(lambda: svc.start(rt.sa, plan_id)))
        assert (first.status, [t.status for t in first.tasks]) == ("partial", ["succeeded", "failed", "skipped"])
        assert [t.retry_class for t in first.tasks] == [None, "safe", "safe"]
        asyncio.run(failing())

        # -- twelve racing retries: exactly one attempt, one lineage row
        stores = [PlanRunStore(rt.sf, config=fast) for _ in range(12)]
        barrier, outcomes = threading.Barrier(12), []

        def attempt(store):
            barrier.wait()
            try:
                outcomes.append(store.create(rt.sa, plan_id, retry_of=first.id, claim=False))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

        threads = [threading.Thread(target=attempt, args=(s,)) for s in stores]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        winners = [o for o in outcomes if isinstance(o, uuid.UUID)]
        assert len(winners) == 1 and all(isinstance(o, (RetryNotAllowed, RunInProgress, AlreadyExecuted)) for o in outcomes if o not in winners)
        assert pg_db.rows("SELECT count(*) FROM plan_run_retries") == [(1,)] and pg_db.rows("SELECT count(*) FROM plan_runs") == [(2,)]
        # the unclaimed retry run is cancelled to make room; then retry for real through the service
        asyncio.run(svc.cancel(rt.sa, winners[0]))
        assert pg_db.rows("SELECT status FROM plan_runs WHERE attempt = 2") == [("cancelled",)]
        third_source = svc.store.get(rt.sa, winners[0])
        assert third_source.retry_of["attempt"] == 1 and third_source.retry["allowed"] is True  # cancelled before anything started: nothing uncertain
        second = asyncio.run(run_and_wait(lambda: svc.retry(rt.sa, winners[0])))
        assert (second.attempt, second.status) == (3, "succeeded")
        by = {t.key: t for t in second.tasks}
        assert by["a"].reused and by["a"].reused_attempt == 1 and not by["b"].reused
        assert rt.orch.calls.count("research") == 1  # the succeeded task ran exactly once across all attempts
        assert pg_db.rows(f"SELECT source_task_run_id::text = (SELECT id::text FROM plan_task_runs WHERE task_key = 'a' AND status = 'succeeded') FROM plan_task_carryovers WHERE run_id = '{second.id}' AND task_key = 'a'") == [(True,)]

        # -- append-only history, in order, written with its transition
        events = svc.store.events(rt.sa, second.id)["items"]
        assert [e.event_type for e in events][:4] == ["retry_authorized", "task_reused", "run_created", "run_claimed"] and events[-1].event_type == "run_completed"
        for table in ("execution_events", "plan_run_retries", "plan_task_carryovers"):
            for statement in (f"UPDATE {table} SET created_at = created_at", f"DELETE FROM {table}"):
                with pytest.raises(DBAPIError) as exc:
                    pg_db.run(statement)
                assert "append-only" in str(exc.value), statement
        run_id = first.id
        scope = rt.sa
        ids = []
        barrier = threading.Barrier(16)

        def write_event(i):
            barrier.wait()
            with rt.sf() as session, session.begin():
                run_events.add(session, run_id, "task_failed", actor_type="recovery", task_key=f"k{i}", code="timeout")
        threads = [threading.Thread(target=write_event, args=(i,)) for i in range(16)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        ids = [r[0] for r in pg_db.rows(f"SELECT id FROM execution_events WHERE run_id = '{run_id}' AND task_key LIKE 'k%' ORDER BY id")]
        assert len(ids) == 16 and ids == sorted(set(ids))  # a strict total order under concurrent writers
        before = pg_db.rows("SELECT count(*) FROM execution_events")
        with pytest.raises(RuntimeError):
            with rt.sf() as session, session.begin():
                run_events.add(session, run_id, "task_failed", actor_type="recovery", task_key="rolled-back")
                raise RuntimeError("the transition failed after its event was written")
        assert pg_db.rows("SELECT count(*) FROM execution_events") == before  # an event exists only if its transition committed

        # -- the atomic task commits: a failure inside the transaction takes BOTH records back
        plan2 = rt.make_plan(rt.sb, [rt.task("a", "research")])
        original = PlanRunStore._event
        fired = []

        def injected(session, rid, sc, kind, actor, **kw):
            if kind == "task_succeeded" and not fired:
                fired.append(kind)
                raise RuntimeError("injected failure inside the transaction")
            return original(session, rid, sc, kind, actor, **kw)

        monkeypatch.setattr(PlanRunStore, "_event", staticmethod(injected))
        svc_b = PlanRunService(rt.sf, executions=rt.executions, registry=RunRegistry(config=fast), busy_poll=0.01)

        async def run_b():
            started = await svc_b.start(rt.sb, plan2)
            await svc_b.registry.wait(started.id, 60)
            return await asyncio.to_thread(svc_b.store.get, rt.sb, started.id)

        broken = asyncio.run(run_b())
        assert fired == ["task_succeeded"] and (broken.status, broken.tasks[0].status, broken.tasks[0].error_code) == ("failed", "failed", "internal_error")
        assert pg_db.rows(f"SELECT e.status, e.error_code, e.result IS NULL FROM agent_executions e JOIN plan_task_runs t ON t.execution_id = e.id WHERE t.run_id = '{broken.id}'") == [
            ("failed", "internal_error", True)]  # never one succeeded and the other not
        monkeypatch.setattr(PlanRunStore, "_event", staticmethod(original))

        # -- reversible
        runs_before = pg_db.rows("SELECT count(*) FROM plan_runs")
        command.downgrade(alembic_env(pg_db), "0009_run_leases")
        assert pg_db.rows("SELECT count(*) FROM information_schema.tables WHERE table_name IN ('execution_events', 'plan_run_retries', 'plan_task_carryovers', 'worker_processes', 'execution_owners')") == [(0,)]
        assert pg_db.rows("SELECT count(*) FROM pg_proc WHERE proname IN ('execution_events_guard', 'plan_run_retries_guard', 'plan_task_carryovers_guard', 'execution_owners_guard')") == [(0,)]
        assert pg_db.rows("SELECT count(*) FROM plan_runs") == runs_before and pg_db.rows("SELECT count(*) FROM agent_executions")[0][0] > 0  # runs and executions are kept
        command.upgrade(alembic_env(pg_db), "head")
        assert pg_db.rows("SELECT version_num FROM alembic_version") == [(HEAD,)] and pg_db.rows("SELECT count(*) FROM execution_events") == [(0,)]
    finally:
        rt.engine.dispose()


def test_database_wide_limits_process_ownership_claiming_and_the_active_run_cap_on_postgresql(pg_db, alembic_env):
    """M4-A2 on PostgreSQL with real concurrent connections: the advisory-lock admission holds the global and the per-user limit across
    processes; standalone executions of a dead process are recovered by the database clock while a live process is untouched; the per-user
    active-run cap holds under concurrency; of many racing claimers exactly one claims an unclaimed run."""
    import threading

    from agentplatform.execution import Busy, DbLimits, ExecutionStore
    from agentplatform.plan_runs import MAX_ACTIVE_RUNS_PER_USER, PlanRunStore, RunLimit
    from agentplatform.processes import ProcessRegistry
    from agentplatform.run_leases import LeaseConfig
    from agentplatform.service import ResolvedAgent

    rt = _lease_runtime(pg_db, alembic_env)
    cfg = LeaseConfig(ttl_seconds=30.0, heartbeat_seconds=10.0)
    resolved = ResolvedAgent("builtin", "research", None, None, None)
    try:
        for owner in ("proc-a", "proc-b"):
            ProcessRegistry(rt.sf, owner, "api", cfg).heartbeat()
        assert pg_db.rows("SELECT owner_id, kind FROM worker_processes ORDER BY owner_id") == [("proc-a", "api"), ("proc-b", "api")]
        gap = pg_db.rows("SELECT extract(epoch FROM (expires_at - (now() AT TIME ZONE 'utc'))) FROM worker_processes WHERE owner_id = 'proc-a'")[0][0]
        assert 25 < float(gap) <= 30  # the heartbeat row is stamped with DATABASE time

        # -- admission: 24 concurrent admissions over two processes, per-user 3 < total 5
        limits = DbLimits(total=5, per_user=3)
        stores = [ExecutionStore(rt.sf, owner_id=o, limits=limits) for o in ("proc-a", "proc-b") for _ in range(12)]
        barrier, outcomes = threading.Barrier(len(stores)), []

        def admit(store):
            barrier.wait()
            try:
                store.start(rt.sa, resolved, "Research", "work", provider="groq", model=None)
                outcomes.append("ok")
            except Busy:
                outcomes.append("busy")
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

        threads = [threading.Thread(target=admit, args=(s,)) for s in stores]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        assert outcomes.count("ok") == 3 and outcomes.count("busy") == len(stores) - 3  # never more than the per-user limit, whatever the interleaving
        assert pg_db.rows("SELECT count(*) FROM agent_executions WHERE status = 'running'") == [(3,)] and pg_db.rows("SELECT count(*) FROM execution_owners") == [(3,)]
        other = ExecutionStore(rt.sf, owner_id="proc-b", limits=limits)
        other.start(rt.sb, resolved, "Research", "work", provider="groq", model=None)
        other.start(rt.sb, resolved, "Research", "work", provider="groq", model=None)  # the total (5) is reached with the second user's two
        with pytest.raises(Busy):
            other.start(rt.sb, resolved, "Research", "work", provider="groq", model=None)

        # -- a live process is never swept; a dead one is, by the database clock
        sweeper = ExecutionStore(rt.sf, owner_id="proc-b")
        assert sweeper.fail_orphaned() == 0
        owned_by_a = pg_db.rows("SELECT count(*) FROM agent_executions e JOIN execution_owners o ON o.execution_id = e.id WHERE o.owner_id = 'proc-a' AND e.status = 'running'")[0][0]
        pg_db.run("UPDATE worker_processes SET expires_at = (now() AT TIME ZONE 'utc') - interval '1 minute' WHERE owner_id = 'proc-a'")
        closed = sweeper.fail_orphaned()
        assert closed == owned_by_a
        assert pg_db.rows("SELECT count(*) FROM agent_executions e JOIN execution_owners o ON o.execution_id = e.id WHERE o.owner_id = 'proc-b' AND e.status = 'running'")[0][0] >= 2
        assert sweeper.fail_orphaned() == 0

        # -- the per-user active-run cap under 8 racing creators
        plans = [rt.make_plan(rt.sa, [rt.task("a", "research")]) for _ in range(8)]
        runners = [PlanRunStore(rt.sf, config=cfg) for _ in plans]
        barrier, created = threading.Barrier(len(plans)), []

        def create(store, plan_id):
            barrier.wait()
            try:
                created.append(store.create(rt.sa, plan_id, claim=False))
            except Exception as exc:  # noqa: BLE001
                created.append(exc)

        threads = [threading.Thread(target=create, args=(s, p)) for s, p in zip(runners, plans)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        made = [c for c in created if isinstance(c, uuid.UUID)]
        assert len(made) == MAX_ACTIVE_RUNS_PER_USER and all(isinstance(c, RunLimit) for c in created if c not in made)

        # -- twelve claimers race for one unclaimed run: exactly one claims it
        target = made[0]
        claimers = [PlanRunStore(rt.sf, config=cfg) for _ in range(12)]
        barrier, claims = threading.Barrier(12), []

        def claim(store):
            barrier.wait()
            try:
                claims.append(store.claim_or_recover(target, resume=True))
            except Exception as exc:  # noqa: BLE001
                claims.append(exc)

        threads = [threading.Thread(target=claim, args=(s,)) for s in claimers]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        winners = [c for c in claims if c is not None and not isinstance(c, Exception)]
        assert [w[0] for w in winners] == ["resume"] and not [c for c in claims if isinstance(c, Exception)]
        assert pg_db.rows(f"SELECT generation FROM run_leases WHERE run_id = '{target}'") == [(2,)]
        assert pg_db.rows(f"SELECT count(*) FROM execution_events WHERE run_id = '{target}' AND event_type = 'run_claimed'") == [(1,)]
    finally:
        rt.engine.dispose()


def test_real_worker_processes_on_postgresql_claim_race_crash_recovery_and_graceful_stop(pg_db, alembic_env):
    """M4-A2 on PostgreSQL with REAL worker processes: two workers racing for four runs recorded by an api-only service execute every task
    exactly once; a worker hard-killed mid-task is recovered (nothing re-dispatched) and the acknowledged retry completes the run; a worker
    asked to stop mid-task records the interruption and releases everything."""
    import asyncio

    from agentplatform.plan_runs import PlanRunService, PlanRunStore, RunRegistry, UncertainOutcome
    from agentplatform.run_leases import LeaseConfig
    from tests.test_m4a2_worker import Child

    rt = _lease_runtime(pg_db, alembic_env)
    api = PlanRunService(rt.sf, executions=rt.executions, registry=RunRegistry(config=LeaseConfig(ttl_seconds=30, heartbeat_seconds=10), embedded=False), busy_poll=0.01)
    children = []
    try:
        plans = [(rt.sa, rt.make_plan(rt.sa, rt.chain, objective=f"Launch the product to early customers this month. marker:P{i}")) for i in range(2)]
        plans += [(rt.sb, rt.make_plan(rt.sb, rt.chain, objective=f"Launch the product to early customers this month. marker:P{i}")) for i in range(2, 4)]
        runs = [asyncio.run(api.start(scope, plan_id)) for scope, plan_id in plans]
        assert {r.ownership for r in runs} == {"unclaimed"} and pg_db.rows("SELECT count(*) FROM agent_executions") == [(0,)]
        children = [Child(None, f"pg-race-{i}", 0.3, url=pg_db.url) for i in range(2)]
        for child in children:
            child.wait_line("READY")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and pg_db.rows("SELECT count(*) FROM plan_runs WHERE status = 'running'")[0][0] > 0:
            time.sleep(0.3)
        assert pg_db.rows("SELECT status, count(*) FROM plan_runs GROUP BY status") == [("succeeded", 4)]
        every_call = [line for child in children for line in child.calls]
        assert len(every_call) == 12 and len(set(every_call)) == 12  # 4 runs x 3 tasks, each dispatched exactly once across both processes
        assert pg_db.rows("SELECT count(*) FROM agent_executions") == [(12,)] and pg_db.rows("SELECT count(*) FROM execution_owners WHERE owner_id LIKE 'pg-race-%'") == [(12,)]
        assert pg_db.rows("SELECT count(*) FROM execution_events WHERE event_type = 'run_claimed'") == [(4,)]
        for child in children:
            child.stop_gracefully()
        for child in children:
            child.wait_line("STOPPED", 30)
            child.proc.wait(timeout=30)
            assert child.proc.returncode == 0
        assert pg_db.rows("SELECT count(*) FROM worker_processes WHERE kind = 'worker' AND expires_at > (now() AT TIME ZONE 'utc')") == [(0,)]  # both declared gone, by the DB clock

        # -- a worker killed while a provider call is in flight
        crash_plan = rt.make_plan(rt.sa, rt.chain, objective="Launch the product to early customers this month. marker:CRASH")
        victim = Child(None, "pg-victim", 60, url=pg_db.url)
        children.append(victim)
        victim.wait_line("READY")
        run = asyncio.run(api.start(rt.sa, crash_plan))
        victim.wait_line("CALL research CRASH")
        time.sleep(0.5)
        victim.kill()
        sweeper = PlanRunStore(rt.sf, config=LeaseConfig(ttl_seconds=2.0, heartbeat_seconds=0.5))
        deadline, closed = time.monotonic() + 40, []
        while not closed and time.monotonic() < deadline:
            closed = sweeper.recover_expired()
            time.sleep(0.3)
        assert closed == [run.id]
        crashed = api.store.get(rt.sa, run.id)
        assert (crashed.status, crashed.error_code, [t.status for t in crashed.tasks]) == ("failed", "interrupted", ["failed", "skipped", "skipped"])
        assert pg_db.rows(f"SELECT e.status, e.error_code FROM plan_task_runs t JOIN agent_executions e ON e.id = t.execution_id WHERE t.run_id = '{run.id}'") == [("failed", "interrupted")]
        assert victim.calls == ["CALL research CRASH"] and rt.orch.calls == []  # never dispatched again
        embedded = PlanRunService(rt.sf, executions=rt.executions, registry=RunRegistry(config=LeaseConfig(ttl_seconds=30, heartbeat_seconds=10)), busy_poll=0.01)

        async def retry_it(acknowledge):
            started = await embedded.retry(rt.sa, run.id, acknowledge)
            await embedded.registry.wait(started.id, 60)
            return await asyncio.to_thread(embedded.store.get, rt.sa, started.id)

        with pytest.raises(UncertainOutcome):
            asyncio.run(retry_it(()))  # an in-flight task whose outcome is unknown is never retried silently
        done = asyncio.run(retry_it(("a",)))
        assert done.status == "succeeded" and rt.orch.calls == ["research", "sales", "ceo"]

        # -- a worker asked to stop while a task is in flight
        stop_plan = rt.make_plan(rt.sb, rt.chain, objective="Launch the product to early customers this month. marker:STOP")
        stopper = Child(None, "pg-stopper", 60, url=pg_db.url)
        children.append(stopper)
        stopper.wait_line("READY")
        stop_run = asyncio.run(api.start(rt.sb, stop_plan))
        stopper.wait_line("CALL research STOP")
        time.sleep(0.3)
        stopper.stop_gracefully()
        stopper.wait_line("STOPPED", 30)
        stopper.proc.wait(timeout=30)
        stopped = api.store.get(rt.sb, stop_run.id)
        assert (stopped.status, stopped.error_code) == ("failed", "interrupted") and stopper.proc.returncode == 0
        assert pg_db.rows(f"SELECT expires_at <= (now() AT TIME ZONE 'utc') FROM run_leases WHERE run_id = '{stop_run.id}'") == [(True,)]
        assert pg_db.rows("SELECT expires_at <= (now() AT TIME ZONE 'utc') FROM worker_processes WHERE owner_id = 'pg-stopper'") == [(True,)]
        assert PlanRunStore(rt.sf, config=LeaseConfig(ttl_seconds=2.0, heartbeat_seconds=0.5)).recover_expired() == []  # a clean stop leaves nothing to recover
    finally:
        for child in children:
            child.kill()
        rt.engine.dispose()


def test_tool_invocations_schema_triggers_duplicate_dispatch_recovery_and_reversibility_on_postgresql(pg_db, alembic_env):
    """M4-B on a real PostgreSQL: the invocation table, its constraints and guard trigger; the intent row is written before the handler; six
    racing dispatches of one (execution, seq) leave exactly one row; a call finishes once; recovery closes in-flight calls (uncertain for
    side-effectful tools); the table's own rules reject tampering; and migration 0011 is reversible without touching executions."""
    import asyncio
    import threading

    from agentplatform.execution import ExecutionStore
    from agentplatform.service import ResolvedAgent
    from agentplatform.tools import default_registry
    from agentplatform.tools.invoker import InvocationContext, ToolInvoker, ToolStore, close_open

    rt = _lease_runtime(pg_db, alembic_env)
    try:
        assert pg_db.rows("SELECT tgname FROM pg_trigger WHERE tgrelid = 'tool_invocations'::regclass AND NOT tgisinternal") == [("tool_invocations_guard",)]
        constraints = {r[0] for r in pg_db.rows("SELECT conname FROM pg_constraint WHERE conrelid = 'tool_invocations'::regclass")}
        assert {"tool_invocations_pkey"} <= constraints and any("idempotency" in c for c in constraints) and any("execution_id" in c and c.startswith("uq_") for c in constraints)

        reg = default_registry()
        calc = reg.get("math.calculate")
        store = ToolStore(rt.sf)
        agent = ResolvedAgent("builtin", "research", None, None, None)

        def context():
            eid = ExecutionStore(rt.sf).start(rt.sa, agent, "Research", "t", provider="groq", model=None)
            return InvocationContext(scope=rt.sa, execution_id=eid, agent_key="research", agent_version_id=None, allowed={calc.wire_name: calc})

        # -- a real call through the invoker, recorded with its intent and outcome
        ctx = context()
        out = asyncio.run(ToolInvoker(store, reg).invoke(ctx, calc.wire_name, '{"expression": "6 * 7"}', 1))
        assert (out.ok, out.status) == (True, "succeeded")
        assert pg_db.rows("SELECT status, tool_id, tool_version, side_effect, completed_at >= started_at FROM tool_invocations") == [("succeeded", "math.calculate", 1, "none", True)]

        # -- six racing dispatches of the same (execution, seq): exactly one row
        barrier, outcomes = threading.Barrier(6), []

        def race(n):
            barrier.wait()
            try:
                store.begin(ctx, calc, 2, '{"expression":"2+2"}', "a" * 64, f"{n:064d}")
                outcomes.append("ok")
            except Exception as exc:  # noqa: BLE001
                outcomes.append(type(exc).__name__)

        threads = [threading.Thread(target=race, args=(i,)) for i in range(6)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        assert outcomes.count("ok") == 1 and outcomes.count("IntegrityError") == 5
        assert pg_db.rows(f"SELECT count(*) FROM tool_invocations WHERE execution_id = '{ctx.execution_id}' AND seq = 2") == [(1,)]
        running = pg_db.rows(f"SELECT id FROM tool_invocations WHERE execution_id = '{ctx.execution_id}' AND seq = 2")[0][0]

        # -- one transition, once; a finished row is immutable; fixed columns and deletes are refused
        assert store.finish(running, "succeeded", output="{}") is True and store.finish(running, "failed", error_code="tool_error") is False
        for statement in (f"UPDATE tool_invocations SET output = 'x' WHERE id = '{running}'", "DELETE FROM tool_invocations",
                          f"UPDATE tool_invocations SET tool_id = 'other.tool' WHERE id = '{running}'", f"UPDATE tool_invocations SET seq = 9 WHERE id = '{running}'"):
            with pytest.raises(DBAPIError):
                pg_db.run(statement)

        # -- recovery: an in-flight call is closed as interrupted (the 'uncertain' class for side effects is covered on SQLite)
        ctx2 = context()
        pending = store.begin(ctx2, calc, 1, '{"expression":"1+1"}', "b" * 64, "c" * 64)
        with pytest.raises(DBAPIError):  # a failed call must carry its error code (CHECK)
            pg_db.run(f"UPDATE tool_invocations SET status = 'failed', completed_at = now(), error_code = NULL WHERE id = '{pending}'")
        with rt.sf() as session, session.begin():
            assert close_open(session, [ctx2.execution_id]) == 1
        assert pg_db.rows(f"SELECT status, error_code FROM tool_invocations WHERE id = '{pending}'") == [("interrupted", "interrupted")]
        with rt.sf() as session, session.begin():
            assert close_open(session, [ctx2.execution_id]) == 0

        # -- reversible: the table goes, executions stay; upgrading again restores an empty table
        executions = pg_db.rows("SELECT count(*) FROM agent_executions")
        command.downgrade(alembic_env(pg_db), "0010_orchestration")
        assert pg_db.rows("SELECT count(*) FROM information_schema.tables WHERE table_name = 'tool_invocations'") == [(0,)]
        assert pg_db.rows("SELECT count(*) FROM pg_proc WHERE proname = 'tool_invocations_guard'") == [(0,)]
        assert pg_db.rows("SELECT count(*) FROM agent_executions") == executions
        command.upgrade(alembic_env(pg_db), "head")
        assert pg_db.rows("SELECT version_num FROM alembic_version") == [(HEAD,)] and pg_db.rows("SELECT count(*) FROM tool_invocations") == [(0,)]
    finally:
        rt.engine.dispose()


def test_tool_approvals_schema_triggers_races_recovery_and_reversibility_on_postgresql(pg_db, alembic_env):
    """M4-C on a real PostgreSQL: the approval table, its constraints and guard trigger; twelve racing human decisions on one approval leave exactly one
    winner; six racing consumptions of an approved approval link exactly one invocation; an approval cannot be changed, reused or deleted; recovery
    invalidates open approvals; and migration 0012 is reversible without touching invocations."""
    import threading

    from agentplatform.execution import ExecutionStore
    from agentplatform.service import ResolvedAgent
    from agentplatform.tools import ToolRegistry
    from agentplatform.tools.approvals import ApprovalConflict, ApprovalStore
    from agentplatform.tools.fixtures import DEMO_SEND_NOTE
    from agentplatform.tools.invoker import ApprovalUnusable, InvocationContext, ToolStore, close_open

    rt = _lease_runtime(pg_db, alembic_env)
    try:
        assert pg_db.rows("SELECT tgname FROM pg_trigger WHERE tgrelid = 'tool_approvals'::regclass AND NOT tgisinternal") == [("tool_approvals_guard",)]
        constraints = {r[0] for r in pg_db.rows("SELECT conname FROM pg_constraint WHERE conrelid = 'tool_approvals'::regclass")}
        assert {"tool_approvals_pkey", "uq_tool_approvals_execution_id_seq", "uq_tool_approvals_invocation_id", "ck_tool_approvals_consumed_link", "ck_tool_approvals_status"} <= constraints

        spec = DEMO_SEND_NOTE
        agent = ResolvedAgent("builtin", "research", None, None, None)
        store = ApprovalStore(rt.sf)
        tool_store = ToolStore(rt.sf)

        def context():
            eid = ExecutionStore(rt.sf).start(rt.sa, agent, "Research", "t", provider="groq", model=None)
            return InvocationContext(scope=rt.sa, execution_id=eid, agent_key="research", agent_version_id=None, allowed={spec.wire_name: spec})

        # -- twelve racing decisions: exactly one winner
        ctx = context()
        approval_id = store.request(ctx, spec, 1, "{}", "h" * 64, "reason")
        barrier, outcomes = threading.Barrier(12), []

        def decide(i):
            barrier.wait()
            try:
                store.decide(rt.sa, approval_id, "approve" if i % 2 == 0 else "reject")
                outcomes.append("won")
            except ApprovalConflict:
                outcomes.append("conflict")
            except Exception as exc:  # noqa: BLE001
                outcomes.append(type(exc).__name__)

        threads = [threading.Thread(target=decide, args=(i,)) for i in range(12)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        assert outcomes.count("won") == 1 and outcomes.count("conflict") == 11, outcomes
        # -- another workspace can neither read nor decide it
        from agentplatform.tools.approvals import ApprovalNotFound

        with pytest.raises(ApprovalNotFound):
            store.decide(rt.sb, approval_id, "approve")

        # -- racing consumption of an APPROVED approval: exactly one invocation is linked
        ctx2 = context()
        second = store.request(ctx2, spec, 1, "{}", "h" * 64, None)
        store.decide(rt.sa, second, "approve")
        barrier2, results = threading.Barrier(6), []

        def consume():
            barrier2.wait()
            try:
                tool_store.begin(ctx2, spec, 1, "{}", "h" * 64, uuid.uuid4().hex * 2, second)
                results.append("ok")
            except Exception as exc:  # noqa: BLE001
                results.append(type(exc).__name__)

        threads = [threading.Thread(target=consume) for _ in range(6)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        assert results.count("ok") == 1, results
        assert pg_db.rows(f"SELECT status, invocation_id IS NOT NULL FROM tool_approvals WHERE id = '{second}'") == [("consumed", True)]
        assert pg_db.rows(f"SELECT count(*) FROM tool_invocations WHERE execution_id = '{ctx2.execution_id}'") == [(1,)]
        # -- changed arguments and a replay cannot use it
        ctx3 = context()
        third = store.request(ctx3, spec, 1, "{}", "h" * 64, None)
        store.decide(rt.sa, third, "approve")
        with pytest.raises(ApprovalUnusable):
            tool_store.begin(ctx3, spec, 1, "{}", "DIFFERENT" + "h" * 55, uuid.uuid4().hex * 2, third)
        from sqlalchemy.exc import IntegrityError as _IE
        with pytest.raises((ApprovalUnusable, _IE)):  # a consumed approval cannot be replayed: the unique step (or the approval itself) refuses it
            tool_store.begin(ctx2, spec, 1, "{}", "h" * 64, uuid.uuid4().hex * 2, second)

        # -- the table's own rules
        for statement in (f"UPDATE tool_approvals SET tool_id = 'other.tool' WHERE id = '{second}'", f"UPDATE tool_approvals SET payload_hash = '{'z' * 64}' WHERE id = '{second}'",
                          f"UPDATE tool_approvals SET status = 'approved' WHERE id = '{second}'", "DELETE FROM tool_approvals"):
            with pytest.raises(DBAPIError):
                pg_db.run(statement)

        # -- recovery invalidates what nobody is waiting for
        ctx4 = context()
        fourth = store.request(ctx4, spec, 1, "{}", "h" * 64, None)
        store.decide(rt.sa, fourth, "approve")
        with rt.sf() as session, session.begin():
            close_open(session, [ctx4.execution_id])
        assert pg_db.rows(f"SELECT status FROM tool_approvals WHERE id = '{fourth}'") == [("invalidated",)]

        # -- reversible: the table goes, invocations and executions stay
        invocations = pg_db.rows("SELECT count(*) FROM tool_invocations")
        command.downgrade(alembic_env(pg_db), "0011_tools")
        assert pg_db.rows("SELECT count(*) FROM information_schema.tables WHERE table_name = 'tool_approvals'") == [(0,)]
        assert pg_db.rows("SELECT count(*) FROM pg_proc WHERE proname = 'tool_approvals_guard'") == [(0,)]
        assert pg_db.rows("SELECT count(*) FROM tool_invocations") == invocations
        command.upgrade(alembic_env(pg_db), "head")
        assert pg_db.rows("SELECT version_num FROM alembic_version") == [(HEAD,)] and pg_db.rows("SELECT count(*) FROM tool_approvals") == [(0,)]
    finally:
        rt.engine.dispose()


def test_durable_suspension_schema_events_and_a_real_killed_worker_resumed_by_another_on_postgresql(pg_db, alembic_env, tmp_path):
    """M4-C finalization on a real PostgreSQL: migration 0013 (the suspension table and its guard trigger, the extended event vocabulary, reversibility), the approval
    lifecycle in the run history, and REAL worker processes: one is killed while a task is suspended on an approval and another is in flight; a second worker
    process recovers the run, the pending approval survives, a human approves, and the exact call is dispatched once, by the second process."""
    import asyncio
    import importlib
    from types import SimpleNamespace

    from agentplatform.plan_runs import PlanRunService, RunRegistry
    from agentplatform.service import AgentService
    from agentplatform.tools.approvals import ApprovalStore
    from tests.test_m4c_worker_recovery import Child, dispatches, until
    from tests.test_plan_runs import definition

    rt = _lease_runtime(pg_db, alembic_env)
    tools = importlib.import_module("agentplatform.tools")
    fx = importlib.import_module("agentplatform.tools.fixtures")
    registry = tools.default_registry()
    registry.register(fx.DEMO_SEND_NOTE, fx.send_note)
    log = tmp_path / "dispatch.log"
    first = second = None
    try:
        # -- schema
        assert pg_db.rows("SELECT tgname FROM pg_trigger WHERE tgrelid = 'tool_suspensions'::regclass AND NOT tgisinternal") == [("tool_suspensions_guard",)]
        check = pg_db.rows("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_execution_events_type'")[0][0]
        for event_type in ("approval_requested", "approval_consumed", "task_suspended", "task_resumed", "run_completed"):
            assert f"'{event_type}'" in check, event_type

        # -- a suspension and its guard trigger
        AgentService(rt.sf).create(rt.sa, "notifier", definition(allowed_tools=["demo.send_note"]))
        plan_id = rt.make_plan(rt.sa, [rt.task("a", "notifier"), rt.task("b", "research"), rt.task("c", "ceo", ["a", "b"])])
        api = PlanRunService(rt.sf, executions=rt.executions, registry=RunRegistry(embedded=False, kind="api"))
        store = ApprovalStore(rt.sf)
        world = {"db": SimpleNamespace(db=SimpleNamespace(url=pg_db.url))}
        first = Child(world, "pg-worker-1", 60, log)
        first_pid = int(first.wait_line("READY").split()[1])
        run = asyncio.run(api.start(rt.sa, plan_id))
        first.wait_line("CALL research")
        pending = until(lambda: store.list(rt.sa, status="pending"), what="the approval request")[0]
        assert (pending.task_key, pending.suspended) == ("a", True) and dispatches(log) == []
        for statement in ("UPDATE tool_suspensions SET transcript = 'x'", "DELETE FROM tool_suspensions", "UPDATE tool_suspensions SET call_index = 5"):
            with pytest.raises(DBAPIError):
                pg_db.run(statement)

        # -- the first worker is killed; a second, unrelated worker recovers the run
        first.kill()
        second = Child(world, "pg-worker-2", 0.2, log)
        second_pid = int(second.wait_line("READY").split()[1])
        until(lambda: dict(pg_db.rows("SELECT task_key, status FROM plan_task_runs")).get("b") == "failed" and dict(pg_db.rows("SELECT task_key, status FROM plan_task_runs")).get("c") == "skipped",
              what="recovery of the in-flight task")
        assert dict(pg_db.rows("SELECT task_key, status FROM plan_task_runs"))["a"] == "running"
        assert pg_db.rows("SELECT status FROM tool_approvals") == [("pending",)] and pg_db.rows("SELECT resumed_at IS NULL FROM tool_suspensions") == [(True,)]
        assert dispatches(log) == []
        assert store.decide(rt.sa, pending.id, "approve", submitted_hash=pending.payload_hash).status == "approved"
        until(lambda: pg_db.rows("SELECT status FROM plan_runs")[0][0] != "running", what="the run to finish")
    finally:
        for child in (first, second):
            if child is not None:
                child.kill()
        registry._tools.pop(("demo.send_note", 1), None)
        rt.engine.dispose()
    assert pg_db.rows("SELECT status FROM plan_runs") == [("partial",)]
    assert dict(pg_db.rows("SELECT task_key, status FROM plan_task_runs")) == {"a": "succeeded", "b": "failed", "c": "skipped"}
    sent = dispatches(log)
    assert len(sent) == 1 and int(sent[0][1]) == second_pid != first_pid  # exactly once, by the second process
    assert pg_db.rows("SELECT status FROM tool_approvals") == [("consumed",)] and pg_db.rows("SELECT resumed_at IS NOT NULL FROM tool_suspensions") == [(True,)]
    types = [r[0] for r in pg_db.rows("SELECT event_type FROM execution_events ORDER BY id")]
    assert types.count("task_suspended") == 1 and types.count("task_resumed") == 1 and types.count("approval_consumed") == 1
    assert types.index("approval_requested") < types.index("approval_approved") < types.index("approval_consumed") < types.index("task_resumed") < types.index("run_completed")

    # -- reversible: the table goes, the old vocabulary returns, executions and runs stay
    engine = create_engine(pg_db.url)
    try:
        runs_before = pg_db.rows("SELECT count(*) FROM plan_runs")
        command.downgrade(alembic_env(pg_db), "0012_approvals")
        assert pg_db.rows("SELECT count(*) FROM information_schema.tables WHERE table_name = 'tool_suspensions'") == [(0,)]
        assert "'approval_requested'" not in pg_db.rows("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_execution_events_type'")[0][0]
        assert pg_db.rows("SELECT count(*) FROM plan_runs") == runs_before
        command.upgrade(alembic_env(pg_db), "head")
        assert pg_db.rows("SELECT version_num FROM alembic_version") == [(HEAD,)]
    finally:
        engine.dispose()


def test_the_decision_compare_and_swap_alone_stops_a_double_decision_even_without_the_table_trigger_on_postgresql(pg_db, alembic_env):
    """Defence in depth, layer by layer: the table trigger forbids a second transition, and the compare-and-swap in ``decide`` makes the loser get a clean conflict. This test
    DROPS the trigger so that only the compare-and-swap is left, then races 16 deciders on one approval: exactly one may win and the stored state must be consistent."""
    import threading

    from agentplatform.execution import ExecutionStore
    from agentplatform.service import ResolvedAgent
    from agentplatform.tools.approvals import ApprovalConflict, ApprovalStore
    from agentplatform.tools.fixtures import DEMO_SEND_NOTE
    from agentplatform.tools.invoker import InvocationContext

    rt = _lease_runtime(pg_db, alembic_env)
    try:
        agent = ResolvedAgent("builtin", "research", None, None, None)
        store = ApprovalStore(rt.sf)
        winners_by_round = []
        for _ in range(6):
            eid = ExecutionStore(rt.sf).start(rt.sa, agent, "Research", "t", provider="groq", model=None)
            ctx = InvocationContext(scope=rt.sa, execution_id=eid, agent_key="research", agent_version_id=None, allowed={DEMO_SEND_NOTE.wire_name: DEMO_SEND_NOTE})
            approval_id = store.request(ctx, DEMO_SEND_NOTE, 1, "{}", "h" * 64, None)
            if not winners_by_round:
                pg_db.run("DROP TRIGGER tool_approvals_guard ON tool_approvals")  # only the compare-and-swap is left from here on
            barrier, outcomes = threading.Barrier(16), []

            def decide(i):
                barrier.wait()
                try:
                    store.decide(rt.sa, approval_id, "approve" if i % 2 == 0 else "reject")
                    outcomes.append("won")
                except ApprovalConflict:
                    outcomes.append("conflict")
                except Exception as exc:  # noqa: BLE001
                    outcomes.append(type(exc).__name__)

            threads = [threading.Thread(target=decide, args=(i,)) for i in range(16)]
            [t.start() for t in threads]
            [t.join(60) for t in threads]
            winners_by_round.append((outcomes.count("won"), outcomes.count("conflict")))
        assert winners_by_round == [(1, 15)] * 6, winners_by_round
    finally:
        rt.engine.dispose()


def test_m6_usage_and_notifications_guards_checks_and_a_clean_downgrade_on_postgresql(pg_db, alembic_env):
    """Migration 0014 on real PostgreSQL: model_usage is append-only, a cost is only ever a calculated one, a notification is unique per (user, kind, ref), is read once and never deleted,
    and the downgrade removes exactly these objects (tables, triggers and functions)."""
    cfg = alembic_env(pg_db)
    command.upgrade(cfg, "head")
    u, w, ref = (str(uuid.uuid4()) for _ in range(3))
    pg_db.run(
        f"INSERT INTO users (id, email, email_normalized, is_active, created_at) VALUES ('{u}','p@example.com','p@example.com',true,now())",
        f"INSERT INTO workspaces (id, name, is_personal, personal_for_user_id, created_at) VALUES ('{w}','P',true,'{u}',now())",
        f"INSERT INTO workspace_memberships (workspace_id, user_id, role, created_at) VALUES ('{w}','{u}','owner',now())",
        f"INSERT INTO model_usage (id, user_id, workspace_id, purpose, provider, model, input_tokens, output_tokens, usage_known, cost_micros, cost_basis, pricing_version, created_at) "
        f"VALUES ('{uuid.uuid4()}','{u}','{w}','agent','groq','m',10,5,true,3,'calculated','2026-11-19',now())",
        f"INSERT INTO notifications (id, workspace_id, user_id, kind, ref_id, created_at) VALUES ('{uuid.uuid4()}','{w}','{u}','approval_requested','{ref}',now())",
    )

    def refused(sql):
        with pytest.raises(Exception):
            pg_db.run(sql)

    refused("UPDATE model_usage SET model = 'x'")
    refused("DELETE FROM model_usage")
    refused(f"INSERT INTO model_usage (id, user_id, workspace_id, purpose, provider, model, usage_known, cost_basis, created_at) VALUES ('{uuid.uuid4()}','{u}','{w}','agent','groq','m',true,'calculated',now())")  # calculated without a cost
    refused(f"INSERT INTO notifications (id, workspace_id, user_id, kind, ref_id, created_at) VALUES ('{uuid.uuid4()}','{w}','{u}','approval_requested','{ref}',now())")  # a retry cannot notify twice
    refused("DELETE FROM notifications")
    refused("UPDATE notifications SET read_at = NULL")
    pg_db.run("UPDATE notifications SET read_at = now()")
    refused("UPDATE notifications SET read_at = now() + interval '1 day'")  # read once
    command.downgrade(cfg, "0013_suspension")
    assert pg_db.rows("SELECT count(*) FROM information_schema.tables WHERE table_name IN ('model_usage', 'notifications')") == [(0,)]
    assert pg_db.rows("SELECT count(*) FROM pg_proc WHERE proname IN ('model_usage_guard', 'notifications_guard')") == [(0,)]
    command.upgrade(cfg, "head")


def test_m6b_provider_events_guard_required_tool_column_and_a_clean_downgrade_on_postgresql(pg_db, alembic_env):
    """Migration 0015 on real PostgreSQL: provider_events is append-only and checked, plan_tasks gains the nullable required_tool column, and the downgrade removes exactly these."""
    cfg = alembic_env(pg_db)
    command.upgrade(cfg, "head")
    u, w = str(uuid.uuid4()), str(uuid.uuid4())
    pg_db.run(
        f"INSERT INTO users (id, email, email_normalized, is_active, created_at) VALUES ('{u}','q@example.com','q@example.com',true,now())",
        f"INSERT INTO workspaces (id, name, is_personal, personal_for_user_id, created_at) VALUES ('{w}','P',true,'{u}',now())",
        f"INSERT INTO workspace_memberships (workspace_id, user_id, role, created_at) VALUES ('{w}','{u}','owner',now())",
        f"INSERT INTO provider_events (id, user_id, workspace_id, provider, kind, attempt, wait_ms, created_at) VALUES ('{uuid.uuid4()}','{u}','{w}','groq','waiting',1,1500,now())",
    )
    assert pg_db.rows("SELECT is_nullable FROM information_schema.columns WHERE table_name = 'plan_tasks' AND column_name = 'required_tool'") == [("YES",)]

    def refused(sql):
        with pytest.raises(Exception):
            pg_db.run(sql)

    refused("UPDATE provider_events SET attempt = 2")
    refused("DELETE FROM provider_events")
    refused(f"INSERT INTO provider_events (id, user_id, workspace_id, provider, kind, attempt, created_at) VALUES ('{uuid.uuid4()}','{u}','{w}','groq','panic',1,now())")
    refused(f"INSERT INTO provider_events (id, user_id, workspace_id, provider, kind, attempt, created_at) VALUES ('{uuid.uuid4()}','{u}','{w}','groq','waiting',0,now())")
    command.downgrade(cfg, "0014_usage_notifications")
    assert pg_db.rows("SELECT count(*) FROM information_schema.columns WHERE table_name = 'plan_tasks' AND column_name = 'required_tool'") == [(0,)]
    assert pg_db.rows("SELECT count(*) FROM information_schema.tables WHERE table_name = 'provider_events'") == [(0,)]
    assert pg_db.rows("SELECT count(*) FROM pg_proc WHERE proname = 'provider_events_guard'") == [(0,)]
    command.upgrade(cfg, "head")


def test_durable_dispatch_state_machine_gate_and_concurrency_on_postgresql(pg_db, alembic_env):
    """P0 safety sprint on a real PostgreSQL: the dispatch ledger and its guard trigger, the legal transitions, the lineage gate under REAL concurrent transactions (the advisory lock closes the
    READ COMMITTED race the unique key cannot see), finish-versus-recovery and reconcile-versus-reconcile races (compare-and-swap: one winner), and a reversible migration."""
    import threading
    from datetime import datetime

    from pydantic import BaseModel, ConfigDict

    from agentplatform.execution import ExecutionStore
    from agentplatform.plan_runs import PlanRunStore
    from agentplatform.run_leases import Fence
    from agentplatform.tools import ToolSpec
    from agentplatform.tools import dispatch as dp
    from agentplatform.tools.invoker import InvocationContext, ToolRunContext, ToolStore, close_open
    from tests.test_run_recovery import BUILTIN

    class In(BaseModel):
        model_config = ConfigDict(extra="forbid")
        n: int = 0

    spec = ToolSpec(id="remote.send", version=1, title="Send", summary="s", description="d", category="Test", input_model=In, output_model=In, risk="external", side_effect="non_idempotent")
    rt = _lease_runtime(pg_db, alembic_env)
    store = ToolStore(rt.sf)

    def scalar(sql):
        rows = pg_db.rows(sql)
        return rows[0][0] if rows else None

    try:
        # -- schema
        assert pg_db.rows("SELECT tgname FROM pg_trigger WHERE tgrelid = 'tool_dispatches'::regclass AND NOT tgisinternal") == [("tool_dispatches_guard",)]
        assert pg_db.rows("SELECT tgname FROM pg_trigger WHERE tgrelid = 'tool_reconciliations'::regclass AND NOT tgisinternal") == [("tool_reconciliations_guard",)]
        check = pg_db.rows("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_execution_events_type'")[0][0]
        for event_type in ("tool_dispatch_authorized", "tool_dispatch_started", "tool_dispatch_unknown", "tool_dispatch_reconciled", "approval_consumed", "run_completed"):
            assert f"'{event_type}'" in check, event_type

        # -- a plan task lineage with a fenced owner
        plan_id = rt.make_plan(rt.sa, [rt.task("a", "research")])
        runs = PlanRunStore(rt.sf, owner_id="pg-owner")
        run_id = runs.create(rt.sa, plan_id)
        fence = Fence(run_id, "pg-owner", 1)
        _, tasks = runs.load_for_driver(run_id)
        eid = runs.start_task(tasks[0].id, rt.sa, BUILTIN, "Research", "t", "groq", None, executions=rt.executions.store, fence=fence)
        ctx = InvocationContext(scope=rt.sa, execution_id=eid, agent_key="research", agent_version_id=None, allowed={spec.wire_name: spec}, run=ToolRunContext(run_id, "a", fence))

        # -- the legal path, with events written in the same transactions
        inv = store.begin(ctx, spec, 1, '{"n":1}', "a" * 64, uuid.uuid4().hex * 2)
        assert pg_db.rows("SELECT state, generation FROM tool_dispatches") == [("authorized", 1)]
        info = store.start_dispatch(ctx, inv)
        assert pg_db.rows("SELECT state FROM tool_dispatches") == [("dispatching",)] and info.idempotency_key.endswith("-g1")
        assert store.finish(inv, "succeeded", output="{}", run=ctx.run, dispatch=(info.dispatch_id, "succeeded", "REF-1", None))
        assert pg_db.rows("SELECT state, external_ref FROM tool_dispatches") == [("succeeded", "REF-1")]
        assert [r[0] for r in pg_db.rows("SELECT event_type FROM execution_events WHERE event_type LIKE 'tool_dispatch_%' ORDER BY id")] == ["tool_dispatch_authorized", "tool_dispatch_started", "tool_dispatch_succeeded"]

        # -- the database refuses every illegal move and any change to the identity
        for statement in ("UPDATE tool_dispatches SET state = 'failed'", "UPDATE tool_dispatches SET state = 'unknown'", "UPDATE tool_dispatches SET state = 'authorized'", "DELETE FROM tool_dispatches",
                          "UPDATE tool_dispatches SET idempotency_key = 'other'", "UPDATE tool_dispatches SET external_ref = 'tampered'"):
            with pytest.raises(Exception):
                pg_db.run(statement)
        assert pg_db.rows("SELECT state FROM tool_dispatches") == [("succeeded",)]

        # -- the gate: the same operation again is a REPLAY, never a second authorisation
        gate = store.precheck(ctx, spec, 2, "a" * 64)
        assert gate.action == "replay" and gate.replay_of == inv

        # -- REAL concurrent authorisations in one lineage: same arguments and different arguments, 16 transactions: exactly ONE may authorise (the lineage already has a success, so a NEW operation
        #    is allowed once; the rest must be refused as in flight)
        rt2_plan = rt.make_plan(rt.sb, [rt.task("a", "research")])  # a second, independent lineage owned by the other user
        runs2 = PlanRunStore(rt.sf, owner_id="pg-owner2")
        run2 = runs2.create(rt.sb, rt2_plan)
        fence2 = Fence(run2, "pg-owner2", 1)
        _, tasks2 = runs2.load_for_driver(run2)
        eid2 = runs2.start_task(tasks2[0].id, rt.sb, BUILTIN, "Research", "t", "groq", None, executions=rt.executions.store, fence=fence2)
        ctx2 = InvocationContext(scope=rt.sb, execution_id=eid2, agent_key="research", agent_version_id=None, allowed={spec.wire_name: spec}, run=ToolRunContext(run2, "a", fence2))
        outcomes, barrier = [], threading.Barrier(16)

        def authorise(i):
            barrier.wait()
            try:
                store.begin(ctx2, spec, i + 1, f'{{"n":{i % 2}}}', (f"{i % 2}" * 64), uuid.uuid4().hex * 2)  # alternating two different argument hashes
                outcomes.append("authorized")
            except dp.DispatchRefused as refused:
                outcomes.append(refused.code)
            except Exception as exc:  # noqa: BLE001
                outcomes.append(type(exc).__name__)

        threads = [threading.Thread(target=authorise, args=(i,)) for i in range(16)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        assert outcomes.count("authorized") == 1 and all(o in ("authorized", "operation_in_flight", "IntegrityError") for o in outcomes), outcomes
        assert scalar("SELECT count(*) FROM tool_dispatches WHERE workspace_id = (SELECT workspace_id FROM plan_runs WHERE id = '%s')" % run2) == 1

        # -- finish versus recovery: exactly one terminal state, and the invocation always agrees with it (20 rounds, real concurrency)
        results = []
        for round_number in range(20):
            e = ExecutionStore(rt.sf).start(rt.sa, BUILTIN, "Research", "t", provider="groq", model=None)
            c = InvocationContext(scope=rt.sa, execution_id=e, agent_key="research", agent_version_id=None, allowed={spec.wire_name: spec})
            invocation = store.begin(c, spec, 1, "{}", "b" * 64, uuid.uuid4().hex * 2)
            started = store.start_dispatch(c, invocation)
            barrier = threading.Barrier(2)

            def finish():
                barrier.wait()
                store.finish(invocation, "succeeded", output="{}", dispatch=(started.dispatch_id, "succeeded", "R", None))

            def recover():
                barrier.wait()
                with rt.sf() as session, session.begin():
                    close_open(session, [e])

            threads = [threading.Thread(target=finish), threading.Thread(target=recover)]
            [t.start() for t in threads]
            [t.join(30) for t in threads]
            state = scalar("SELECT state FROM tool_dispatches WHERE invocation_id = '%s'" % invocation)
            status = scalar("SELECT status FROM tool_invocations WHERE id = '%s'" % invocation)
            assert (state, status) in (("succeeded", "succeeded"), ("unknown", "uncertain")), (state, status)  # never one without the other
            results.append(state)
        assert set(results) <= {"succeeded", "unknown"}

        # -- reconcile versus reconcile on real concurrent transactions: ONE winner, ONE record
        unknown_inv = scalar("SELECT invocation_id FROM tool_dispatches WHERE state = 'unknown' LIMIT 1") or None
        if unknown_inv is None:
            e = ExecutionStore(rt.sf).start(rt.sa, BUILTIN, "Research", "t", provider="groq", model=None)
            c = InvocationContext(scope=rt.sa, execution_id=e, agent_key="research", agent_version_id=None, allowed={spec.wire_name: spec})
            invocation = store.begin(c, spec, 1, "{}", "c" * 64, uuid.uuid4().hex * 2)
            store.start_dispatch(c, invocation)
            with rt.sf() as session, session.begin():
                close_open(session, [e])
            unknown_inv = str(invocation)
        target = uuid.UUID(str(unknown_inv))
        wins, barrier = [], threading.Barrier(12)

        def reconcile(i):
            barrier.wait()
            try:
                dp.reconcile(rt.sf, rt.sa, target, decision="confirmed_succeeded" if i % 2 else "confirmed_failed", evidence_kind="operator_attestation", evidence=f"checked {i}", reason=f"operator {i}")
                wins.append(i)
            except dp.ReconcileError as exc:
                assert exc.code == "already_resolved", exc.code

        threads = [threading.Thread(target=reconcile, args=(i,)) for i in range(12)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        assert len(wins) == 1 and scalar("SELECT count(*) FROM tool_reconciliations WHERE invocation_id = '%s'" % target) == 1
        assert scalar("SELECT state FROM tool_dispatches WHERE invocation_id = '%s'" % target) in ("reconciled_succeeded", "reconciled_failed")
        for statement in ("UPDATE tool_reconciliations SET reason = 'edited'", "DELETE FROM tool_reconciliations"):
            with pytest.raises(Exception):
                pg_db.run(statement)

        # -- reversible
        command.downgrade(alembic_env(pg_db), "0015_resilience_required_action")
        assert pg_db.rows("SELECT count(*) FROM information_schema.tables WHERE table_name IN ('tool_dispatches', 'tool_reconciliations')") == [(0,)]
        assert pg_db.rows("SELECT count(*) FROM pg_proc WHERE proname IN ('tool_dispatches_guard', 'tool_reconciliations_guard')") == [(0,)]
        assert "'tool_dispatch_authorized'" not in pg_db.rows("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_execution_events_type'")[0][0]
        command.upgrade(alembic_env(pg_db), "head")
    finally:
        rt.engine.dispose()



def test_sprint3_completion_race_retry_race_stale_fencing_and_injected_failures_on_postgresql(pg_db, alembic_env, monkeypatch):
    """Sprint 3 on a real PostgreSQL: (1) two simultaneous completions of ONE task have exactly one winner and the task and the execution agree; (2) concurrent explicit retries of a failed run create exactly ONE
    attempt; (3) a worker that lost its lease cannot start, finish or re-open a dispatch (and the takeover owner can); (4) a failure injected in the middle of a transaction leaves NOTHING half written."""
    import threading

    from pydantic import BaseModel, ConfigDict

    from agentplatform.plan_runs import PlanRunStore
    from agentplatform.run_leases import Fence, LeaseLost
    from agentplatform.tools import ToolSpec
    from agentplatform.tools import dispatch as dp
    from agentplatform.tools.invoker import InvocationContext, ToolRunContext, ToolStore
    from tests.test_run_recovery import BUILTIN

    class In(BaseModel):
        model_config = ConfigDict(extra="forbid")
        n: int = 0

    spec = ToolSpec(id="remote.send", version=1, title="Send", summary="s", description="d", category="Test", input_model=In, output_model=In, risk="external", side_effect="non_idempotent", timeout_seconds=5,
                    max_output_bytes=100, retry_policy="never", remote_idempotency=True, external_ref_field="n", delivery="test")
    rt = _lease_runtime(pg_db, alembic_env)
    store = ToolStore(rt.sf)

    def scalar(sql):
        rows = pg_db.rows(sql)
        return rows[0][0] if rows else None

    def new_task(owner, scope=None):
        scope = scope or rt.sa
        plan_id = rt.make_plan(scope, [rt.task("a", "research")])
        runs = PlanRunStore(rt.sf, owner_id=owner)
        run_id = runs.create(scope, plan_id)
        fence = Fence(run_id, owner, 1)
        _, tasks = runs.load_for_driver(run_id)
        eid = runs.start_task(tasks[0].id, scope, BUILTIN, "Research", "t", "groq", None, executions=rt.executions.store, fence=fence)
        return plan_id, runs, run_id, fence, tasks[0].id, eid

    try:
        # -- (1) simultaneous completion: 8 rounds; whichever the database serialises first wins, never both
        for round_number in range(8):
            plan_id, runs, run_id, fence, task_id, eid = new_task(f"pg-comp-{round_number}")
            barrier, won = threading.Barrier(2), []

            def complete(kind):
                barrier.wait()
                ok_kind = kind == "ok"
                done = runs.finish_task_execution(task_id, eid, "succeeded" if ok_kind else "failed", "succeeded" if ok_kind else "failed", None if ok_kind else "internal_error", fence=fence,
                                                  result="r" if ok_kind else None, error_code=None if ok_kind else "internal_error")
                if done:
                    won.append(kind)

            threads = [threading.Thread(target=complete, args=(k,)) for k in ("ok", "fail")]
            [t.start() for t in threads]
            [t.join(30) for t in threads]
            assert len(won) == 1, won
            task_status = scalar(f"SELECT status FROM plan_task_runs WHERE id = '{task_id}'")
            exec_status = scalar(f"SELECT status FROM agent_executions WHERE id = '{eid}'")
            assert (task_status, exec_status) == (("succeeded", "succeeded") if won[0] == "ok" else ("failed", "failed")), (won, task_status, exec_status)  # the two records always agree
            assert runs.finish_run(run_id, "succeeded" if won[0] == "ok" else "failed", None if won[0] == "ok" else "internal_error", fence=fence)  # free the user's active-run slot

        # -- (2) concurrent retries of a failed run: exactly one new attempt
        plan_id, runs, run_id, fence, task_id, eid = new_task("pg-retry")
        assert runs.finish_task_execution(task_id, eid, "failed", "failed", "internal_error", fence=fence, error_code="internal_error")
        assert runs.finish_run(run_id, "failed", "internal_error", fence=fence)
        attempts, errors, barrier = [], [], threading.Barrier(8)

        def retry(i):
            barrier.wait()
            try:
                attempts.append(PlanRunStore(rt.sf, owner_id=f"pg-retry-{i}").create(rt.sa, plan_id, retry_of=run_id, acknowledged=("a",), claim=False))
            except Exception as exc:  # noqa: BLE001 - the losers
                errors.append(type(exc).__name__)

        threads = [threading.Thread(target=retry, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        assert len(attempts) == 1 and len(errors) == 7, (attempts, errors)
        assert scalar(f"SELECT count(*) FROM plan_runs WHERE plan_id = '{plan_id}'") == 2

        # -- (3) stale-worker fencing for the dispatch ledger
        plan_id, runs, run_id, fence, task_id, eid = new_task("pg-stale", rt.sb)
        ctx = InvocationContext(scope=rt.sb, execution_id=eid, agent_key="research", agent_version_id=None, allowed={spec.wire_name: spec}, run=ToolRunContext(run_id, "a", fence))
        inv = store.begin(ctx, spec, 1, '{"n":1}', "d" * 64, uuid.uuid4().hex * 2)
        assert scalar(f"SELECT state FROM tool_dispatches WHERE invocation_id = '{inv}'") == "authorized"
        pg_db.run(f"UPDATE run_leases SET expires_at = '2000-01-01' WHERE run_id = '{run_id}'")  # the first worker's lease lapses
        new_fence = runs.leases.takeover(run_id, "pg-takeover", 60)
        assert new_fence is not None and new_fence.generation == 2
        with pytest.raises(LeaseLost):
            store.start_dispatch(ctx, inv)  # the stale worker cannot begin sending
        assert scalar(f"SELECT state FROM tool_dispatches WHERE invocation_id = '{inv}'") == "authorized"
        new_ctx = InvocationContext(scope=rt.sb, execution_id=eid, agent_key="research", agent_version_id=None, allowed={spec.wire_name: spec}, run=ToolRunContext(run_id, "a", new_fence))
        started = store.start_dispatch(new_ctx, inv)  # the new owner can
        with pytest.raises(LeaseLost):
            store.finish(inv, "succeeded", output="{}", run=ctx.run, dispatch=(started.dispatch_id, "succeeded", "X", None))  # and the stale one cannot finish it
        assert (scalar(f"SELECT state FROM tool_dispatches WHERE invocation_id = '{inv}'"), scalar(f"SELECT status FROM tool_invocations WHERE id = '{inv}'")) == ("dispatching", "running")
        assert runs.finish_run(run_id, "failed", "internal_error", fence=new_fence)  # free the slot

        # -- (4) injected failures: nothing half written
        plan_id, runs, run_id, fence, task_id, eid = new_task("pg-inject", rt.sb)
        ctx = InvocationContext(scope=rt.sb, execution_id=eid, agent_key="research", agent_version_id=None, allowed={spec.wire_name: spec}, run=ToolRunContext(run_id, "a", fence))
        real_authorize = dp.authorize_in

        def boom(*a, **k):
            real_authorize(*a, **k)  # the dispatch row IS inserted inside the transaction...
            raise RuntimeError("injected failure after the dispatch row was written")

        monkeypatch.setattr(dp, "authorize_in", boom)
        with pytest.raises(RuntimeError):
            store.begin(ctx, spec, 1, '{"n":2}', "e" * 64, uuid.uuid4().hex * 2)
        monkeypatch.setattr(dp, "authorize_in", real_authorize)
        assert scalar(f"SELECT count(*) FROM tool_invocations WHERE execution_id = '{eid}'") == 0 and scalar(f"SELECT count(*) FROM tool_dispatches WHERE execution_id = '{eid}'") == 0  # ...and rolled back with the intent
        inv2 = store.begin(ctx, spec, 1, '{"n":2}', "e" * 64, uuid.uuid4().hex * 2)
        started2 = store.start_dispatch(ctx, inv2)
        real_finish = dp.finish_in

        def boom_finish(*a, **k):
            raise RuntimeError("injected failure while closing the dispatch")

        monkeypatch.setattr(dp, "finish_in", boom_finish)
        with pytest.raises(RuntimeError):
            store.finish(inv2, "succeeded", output="{}", run=ctx.run, dispatch=(started2.dispatch_id, "succeeded", "R", None))
        monkeypatch.setattr(dp, "finish_in", real_finish)
        assert (scalar(f"SELECT state FROM tool_dispatches WHERE invocation_id = '{inv2}'"), scalar(f"SELECT status FROM tool_invocations WHERE id = '{inv2}'")) == ("dispatching", "running")  # both unchanged
    finally:
        rt.engine.dispose()
