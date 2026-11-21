"""M1-A: identity schema, migration 0002_identity (SQLite files in tmp_path; no PostgreSQL here).

SQLite is NOT PostgreSQL: it proves structure, constraint behaviour and migration reversibility
only. PostgreSQL runtime behaviour is checked separately (tests/test_postgres_integration.py,
opt-in against a disposable server). Offline SQL tests compare compiled PostgreSQL DDL text only.
"""

import ast
import io
import re
import sys
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex, CreateTable

from baseline_support import BASELINE_REVISION, BASELINE_TABLES, baseline_metadata

BACKEND = Path(__file__).resolve().parent.parent
INI = BACKEND / "alembic.ini"
VERSION_FILE = BACKEND / "migrations" / "versions" / "0002_identity.py"
IDENTITY = "0002_identity"
NEW_TABLES = {"users", "auth_identities"}


def make_cfg(output=None) -> Config:
    cfg = Config(str(INI), output_buffer=output)
    cfg.attributes["configure_logger"] = False
    return cfg


class Db:
    """A throw-away SQLite file migrated through the real env.py online path."""

    def __init__(self, path: Path, monkeypatch):
        self.path, self.monkeypatch = path, monkeypatch
        self.url = f"sqlite:///{path.as_posix()}"

    def _env(self):
        self.monkeypatch.setenv("APP_ENV", "test")
        self.monkeypatch.setenv("DATABASE_URL", self.url)
        self.monkeypatch.delenv("GROQ_API_KEY", raising=False)

    def upgrade(self, revision="head"):
        self._env()
        command.upgrade(make_cfg(), revision)

    def downgrade(self, revision):
        self._env()
        command.downgrade(make_cfg(), revision)

    def engine(self, fk=False):
        engine = create_engine(self.url)
        if fk:
            event.listen(engine, "connect", lambda conn, _rec: conn.execute("PRAGMA foreign_keys=ON"))
        return engine

    def master(self):
        """Every schema object exactly as SQLite stores it (name, owning table, DDL text)."""
        engine = self.engine()
        try:
            with engine.connect() as conn:
                return [tuple(r) for r in conn.exec_driver_sql(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()]
        finally:
            engine.dispose()

    def rows(self, sql: str):
        engine = self.engine()
        try:
            with engine.connect() as conn:
                return [tuple(r) for r in conn.exec_driver_sql(sql).fetchall()]
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


@pytest.fixture
def db(tmp_path, monkeypatch):
    return Db(tmp_path / "identity.db", monkeypatch)


def fresh_models(import_fresh):
    import_fresh("memory.models")
    return sys.modules["memory.models"]


def parity_diffs(db: Db, metadata):
    engine = db.engine()
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts=dict(
                compare_type=True, compare_server_default=True,
                include_object=lambda o, n, t, r, c: not (t == "table" and n == "alembic_version")))
            return compare_metadata(ctx, metadata)
    finally:
        engine.dispose()


# ------------------------------------------------------------------- revision graph


def test_revision_graph_is_baseline_then_identity():
    script = ScriptDirectory.from_config(make_cfg())
    assert script.get_heads() == ["0017_invitations"]  # later revisions sit on top of 0002_identity
    assert script.get_bases() == [BASELINE_REVISION]
    rev = script.get_revision(IDENTITY)
    assert rev.revision == "0002_identity" and rev.down_revision == "0001_baseline"
    assert rev.branch_labels == set() and rev.dependencies is None
    assert [r.revision for r in script.walk_revisions()] == [
        "0017_invitations", "0016_durable_dispatch", "0015_resilience_required_action", "0014_usage_notifications", "0013_suspension", "0012_approvals", "0011_tools", "0010_orchestration", "0009_run_leases", "0008_plan_runs", "0007_plans", "0006_agent_executions", "0005_custom_agents", "0004_memory_ownership", "0003_workspaces", IDENTITY, BASELINE_REVISION]
    assert Path(rev.path).resolve() == VERSION_FILE.resolve()
    assert len(IDENTITY) <= 32  # alembic_version.version_num is VARCHAR(32)


# ------------------------------------------------------------- upgrade from the baseline


def test_upgrade_from_baseline_creates_only_the_identity_objects(db):
    db.upgrade(BASELINE_REVISION)
    assert db.rows("SELECT version_num FROM alembic_version") == [(BASELINE_REVISION,)]
    before = db.master()

    db.upgrade(IDENTITY)
    after = db.master()
    assert db.rows("SELECT version_num FROM alembic_version") == [(IDENTITY,)]
    assert {r[1] for r in after if r[0] == "table"} == BASELINE_TABLES | NEW_TABLES | {"alembic_version"}

    # Every baseline object is byte-for-byte unchanged; the only additions are the identity objects.
    untouched = [r for r in after if r in before]
    assert untouched == before
    added = {(r[0], r[1]) for r in after if r not in before}
    assert added == {
        ("table", "users"), ("table", "auth_identities"),
        ("index", "ix_users_email_normalized"), ("index", "ix_auth_identities_user_id"),
        # SQLite's own indexes behind the two (non-integer) primary keys and the UNIQUE constraint:
        ("index", "sqlite_autoindex_users_1"),
        ("index", "sqlite_autoindex_auth_identities_1"),
        ("index", "sqlite_autoindex_auth_identities_2"),
    }


def test_exact_schema_on_sqlite(db):
    db.upgrade()
    engine = db.engine()
    try:
        insp = inspect(engine)
        users = {c["name"]: (str(c["type"]), c["nullable"], c["default"]) for c in insp.get_columns("users")}
        assert users == {
            "id": ("CHAR(32)", False, None),
            "email": ("VARCHAR(320)", False, None),
            "email_normalized": ("VARCHAR(320)", False, None),
            "display_name": ("VARCHAR(255)", True, None),
            "is_active": ("BOOLEAN", False, None),
            "created_at": ("DATETIME", False, None),
        }
        assert [c["name"] for c in insp.get_columns("users")] == [
            "id", "email", "email_normalized", "display_name", "is_active", "created_at"]
        identities = {c["name"]: (str(c["type"]), c["nullable"], c["default"]) for c in insp.get_columns("auth_identities")}
        assert identities == {
            "id": ("CHAR(32)", False, None),
            "user_id": ("CHAR(32)", False, None),
            "provider": ("VARCHAR(32)", False, None),
            "provider_subject": ("VARCHAR(255)", False, None),
            "email_at_link": ("VARCHAR(320)", False, None),
            "created_at": ("DATETIME", False, None),
        }
        assert insp.get_pk_constraint("users") == {"constrained_columns": ["id"], "name": "users_pkey"}
        assert insp.get_pk_constraint("auth_identities") == {"constrained_columns": ["id"], "name": "auth_identities_pkey"}
        fks = insp.get_foreign_keys("auth_identities")
        assert [(f["name"], f["constrained_columns"], f["referred_table"], f["referred_columns"]) for f in fks] == [
            ("fk_auth_identities_user_id_users", ["user_id"], "users", ["id"])]
        assert insp.get_foreign_keys("users") == []
        uniques = insp.get_unique_constraints("auth_identities")
        assert [(u["name"], u["column_names"]) for u in uniques] == [
            ("uq_auth_identities_provider_provider_subject", ["provider", "provider_subject"])]
        assert insp.get_unique_constraints("users") == []
        users_ix = [(i["name"], tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes("users")]
        assert users_ix == [("ix_users_email_normalized", ("email_normalized",), False)]
        ident_ix = [(i["name"], tuple(i["column_names"]), bool(i["unique"]))
                    for i in insp.get_indexes("auth_identities") if not i["name"].startswith("sqlite_")]
        assert ident_ix == [("ix_auth_identities_user_id", ("user_id",), False)]
        assert insp.get_check_constraints("users") == [] and insp.get_check_constraints("auth_identities") == []
    finally:
        engine.dispose()


def test_schema_matches_the_models_metadata_on_sqlite(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade()
    assert parity_diffs(db, models.Base.metadata) == []


# ----------------------------------------- offline SQL: exact PostgreSQL DDL text (not runtime)


def _statements(sql: str) -> list[str]:
    code = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
    squashed = [re.sub(r"\s+", "", s) for s in code.split(";") if s.strip()]
    return [s for s in squashed if s not in ("BEGIN", "COMMIT")]  # transaction wrappers only


def test_offline_upgrade_sql_equals_compiled_postgresql_ddl_and_does_nothing_else(import_fresh):
    models = fresh_models(import_fresh)
    out = io.StringIO()
    command.upgrade(make_cfg(out), f"{BASELINE_REVISION}:{IDENTITY}", sql=True)
    statements = _statements(out.getvalue())

    dialect = postgresql.dialect()
    expected = set()
    for name in NEW_TABLES:
        table = models.Base.metadata.tables[name]
        expected.add(re.sub(r"\s+", "", str(CreateTable(table).compile(dialect=dialect))))
        expected.update(re.sub(r"\s+", "", str(CreateIndex(ix).compile(dialect=dialect))) for ix in table.indexes)
    creates = {s for s in statements if s.startswith("CREATE")}
    assert creates == expected, "migration DDL differs from the models' compiled PostgreSQL DDL"
    others = [s for s in statements if not s.startswith("CREATE")]
    assert others == ["UPDATEalembic_versionSETversion_num='0002_identity'WHEREalembic_version.version_num='0001_baseline'"]
    sql = out.getvalue()
    assert not re.search(r"\b(DROP|DELETE|TRUNCATE|ALTER|INSERT)\b", sql)
    assert "agent_memories" not in sql and "telegram_links" not in sql  # baseline tables are not touched
    # Spot-check the PostgreSQL types and names (not only equality with the models).
    squashed = re.sub(r"\s+", "", sql)
    for fragment in ("idUUIDNOTNULL", "emailVARCHAR(320)NOTNULL", "is_activeBOOLEANNOTNULL",
                     "created_atTIMESTAMPWITHOUTTIMEZONENOTNULL", "CONSTRAINTusers_pkeyPRIMARYKEY(id)",
                     "CONSTRAINTfk_auth_identities_user_id_usersFOREIGNKEY(user_id)REFERENCESusers(id)",
                     "CONSTRAINTuq_auth_identities_provider_provider_subjectUNIQUE(provider,provider_subject)",
                     "CREATEINDEXix_users_email_normalizedONusers(email_normalized)",
                     "CREATEINDEXix_auth_identities_user_idONauth_identities(user_id)"):
        assert fragment in squashed, fragment
    assert "UNIQUEINDEX" not in squashed and "DEFAULT" not in squashed  # email index is not unique; no server defaults


def test_offline_downgrade_sql_drops_only_the_identity_objects():
    out = io.StringIO()
    command.downgrade(make_cfg(out), f"{IDENTITY}:{BASELINE_REVISION}", sql=True)
    statements = _statements(out.getvalue())
    assert statements[:4] == [
        "DROPINDEXix_auth_identities_user_id", "DROPTABLEauth_identities",
        "DROPINDEXix_users_email_normalized", "DROPTABLEusers"]
    assert statements[4:] == ["UPDATEalembic_versionSETversion_num='0001_baseline'WHEREalembic_version.version_num='0002_identity'"]
    assert "agent_memories" not in out.getvalue() and "telegram_links" not in out.getvalue()


# ---------------------------------------------------------------- identity rules (SQLite)


def _user(models, email, normalized=None, **kw):
    return models.User(email=email, email_normalized=normalized or email.casefold(), **kw)


def test_email_is_not_unique_and_identities_with_one_email_are_never_merged(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade()
    engine = db.engine(fk=True)
    try:
        with Session(engine) as s:
            first, second = _user(models, "same@example.com"), _user(models, "same@example.com")
            s.add_all([first, second])
            s.flush()
            s.add_all([
                models.AuthIdentity(user_id=first.id, provider="google", provider_subject="subject-A", email_at_link="same@example.com"),
                models.AuthIdentity(user_id=second.id, provider="google", provider_subject="subject-B", email_at_link="same@example.com"),
            ])
            s.commit()
            assert first.id != second.id  # UUIDs generated in Python, distinct
        with Session(engine) as s:
            users = s.query(models.User).all()
            assert len(users) == 2 and {u.email for u in users} == {"same@example.com"}
            owner = {i.provider_subject: i.user_id for i in s.query(models.AuthIdentity).all()}
            assert owner["subject-A"] != owner["subject-B"]  # no automatic linking by email
    finally:
        engine.dispose()
    # Nothing in the schema makes email or the normalized email unique.
    # (the two partial unique indexes of 0008 are about plan runs: one active run per plan, one success per revision)
    unique_sql = [r for r in db.master() if r[0] == "index" and "UNIQUE" in (r[3] or "").upper() and r[1] not in ("uq_plan_runs_one_active", "uq_plan_runs_one_success", "uq_workspace_invitations_open")]
    assert [r[1] for r in unique_sql] == []


def test_duplicate_provider_and_subject_is_rejected_but_neighbours_are_allowed(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade()
    engine = db.engine(fk=True)

    def identity(user, provider, subject):
        return models.AuthIdentity(user_id=user.id, provider=provider, provider_subject=subject, email_at_link="a@example.com")

    try:
        with Session(engine) as s:
            user, other = _user(models, "a@example.com"), _user(models, "b@example.com")
            s.add_all([user, other])
            s.flush()
            s.add(identity(user, "google", "sub-1"))
            s.commit()
            for allowed in (identity(user, "google", "sub-2"),   # same provider, other subject
                            identity(user, "github", "sub-1")):  # other provider, same subject
                s.add(allowed)
                s.commit()
            s.add(identity(other, "google", "sub-1"))  # the same external account, even for another user
            with pytest.raises(IntegrityError):
                s.commit()
            s.rollback()
            assert s.query(models.AuthIdentity).count() == 3
    finally:
        engine.dispose()


def test_foreign_key_rejects_an_unknown_user(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade()
    engine = db.engine(fk=True)
    try:
        with Session(engine) as s:
            s.add(models.AuthIdentity(user_id=uuid.uuid4(), provider="google", provider_subject="x", email_at_link="a@example.com"))
            with pytest.raises(IntegrityError):
                s.commit()
    finally:
        engine.dispose()


def test_models_generate_uuids_and_defaults_in_python_with_no_server_defaults(import_fresh):
    models = fresh_models(import_fresh)
    for name in NEW_TABLES:
        table = models.Base.metadata.tables[name]
        assert all(c.server_default is None for c in table.columns), name
    users = models.Base.metadata.tables["users"]
    assert isinstance(users.c.id.default.arg(None), uuid.UUID)  # generated by a Python-side callable
    assert users.c.is_active.default.arg is True
    assert [c.name for c in users.primary_key] == ["id"]


# ------------------------------------------------------------ data safety and reversibility


def _seed_baseline(db: Db):
    db.run(
        "INSERT INTO agent_memories (agent_name, user_email, summary, created_at) "
        "VALUES ('ceo', 'legacy@example.com', 'legacy summary', '2026-01-02 03:04:05.000000')",
        "INSERT INTO agent_memories (agent_name, user_email, summary, created_at) "
        "VALUES ('sales', NULL, 'orphan', '2026-02-03 04:05:06.000000')",
        "INSERT INTO telegram_links (chat_id, email, active_agent, updated_at) "
        "VALUES (42, 'legacy@example.com', 'ceo', '2026-03-04 05:06:07.000000')",
    )


def test_migration_creates_no_rows_and_never_backfills_from_email(db):
    db.upgrade(BASELINE_REVISION)
    _seed_baseline(db)
    legacy = db.rows("SELECT * FROM agent_memories ORDER BY id") + db.rows("SELECT * FROM telegram_links")
    db.upgrade(IDENTITY)
    assert db.rows("SELECT count(*) FROM users") == [(0,)]
    assert db.rows("SELECT count(*) FROM auth_identities") == [(0,)]  # no user derived from legacy emails
    assert db.rows("SELECT * FROM agent_memories ORDER BY id") + db.rows("SELECT * FROM telegram_links") == legacy
    assert [r[1] for r in db.rows("PRAGMA table_info(agent_memories)")] == [
        "id", "agent_name", "user_email", "summary", "created_at"]  # no ownership column added


def test_downgrade_restores_the_exact_baseline_and_reupgrade_succeeds(db):
    db.upgrade(BASELINE_REVISION)
    _seed_baseline(db)
    baseline_objects = db.master()
    baseline_rows = db.rows("SELECT * FROM agent_memories ORDER BY id") + db.rows("SELECT * FROM telegram_links")

    db.upgrade(IDENTITY)
    upgraded_objects = db.master()
    assert upgraded_objects != baseline_objects

    db.downgrade(BASELINE_REVISION)
    assert db.master() == baseline_objects  # every object, byte for byte, as at the baseline
    assert db.rows("SELECT version_num FROM alembic_version") == [(BASELINE_REVISION,)]
    assert db.rows("SELECT * FROM agent_memories ORDER BY id") + db.rows("SELECT * FROM telegram_links") == baseline_rows
    assert parity_diffs(db, baseline_metadata()) == []  # frozen baseline metadata: no diffs either way

    db.upgrade(IDENTITY)  # re-upgrade (explicitly to 0002; later revisions have their own tests)
    assert db.master() == upgraded_objects
    assert db.rows("SELECT version_num FROM alembic_version") == [(IDENTITY,)]
    assert db.rows("SELECT * FROM agent_memories ORDER BY id") + db.rows("SELECT * FROM telegram_links") == baseline_rows


def test_downgrade_removes_identity_data_it_owns_and_nothing_else(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade(BASELINE_REVISION)
    _seed_baseline(db)
    db.upgrade(IDENTITY)
    engine = db.engine(fk=True)
    try:
        with Session(engine) as s:
            user = _user(models, "x@example.com")
            s.add(user)
            s.flush()
            s.add(models.AuthIdentity(user_id=user.id, provider="google", provider_subject="s", email_at_link="x@example.com"))
            s.commit()
    finally:
        engine.dispose()
    db.downgrade(BASELINE_REVISION)  # development/test behaviour; production is forward-only
    assert {r[1] for r in db.master() if r[0] == "table"} == BASELINE_TABLES | {"alembic_version"}
    assert db.rows("SELECT count(*) FROM agent_memories") == [(2,)]


# ---------------------------------------------------------------------- source guards


def test_migration_source_uses_only_identity_ddl_operations():
    tree = ast.parse(VERSION_FILE.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert set(funcs) == {"upgrade", "downgrade"}

    def called(fn):
        return [c.func.attr for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)]

    assert {n for n in called(funcs["upgrade"]) if n.startswith(("create_", "drop_", "execute", "bulk", "alter", "add_"))} == {
        "create_table", "create_index"}
    assert {n for n in called(funcs["downgrade"]) if n.startswith(("create_", "drop_", "execute", "bulk", "alter", "add_"))} == {
        "drop_index", "drop_table"}
    source = VERSION_FILE.read_text(encoding="utf-8")
    for forbidden in ("agent_memories", "telegram_links", "execute(", "bulk_insert", "server_default", "uuid_generate", "gen_random_uuid"):
        assert forbidden not in source, forbidden
    assert "revision: str = \"0002_identity\"" in source and "down_revision: Union[str, None] = \"0001_baseline\"" in source
