"""M1-B: workspace schema, migration 0003_workspaces (SQLite files in tmp_path; no PostgreSQL here).

SQLite is NOT PostgreSQL: it proves structure, constraint behaviour and migration reversibility only.
PostgreSQL runtime behaviour is checked separately (tests/test_postgres_integration.py, opt-in against a
disposable server). Offline SQL tests compare compiled PostgreSQL DDL text only.
"""

import ast
import io
import re
import sys
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex, CreateTable

from baseline_support import baseline_metadata
from migration_support import BACKEND, Db, make_cfg
from migrations import preflight as pf

VERSION_FILE = BACKEND / "migrations" / "versions" / "0003_workspaces.py"
BASELINE, IDENTITY, WORKSPACES = "0001_baseline", "0002_identity", "0003_workspaces"
NEW_TABLES = {"workspaces", "workspace_memberships"}
PERSONAL_CHECK = (
    "(is_personal AND personal_for_user_id IS NOT NULL) OR (NOT is_personal AND personal_for_user_id IS NULL)"
)
ROLE_CHECK = "role IN ('owner', 'admin', 'member')"


@pytest.fixture
def db(tmp_path, monkeypatch):
    return Db(tmp_path / "workspaces.db", monkeypatch)


def fresh_models(import_fresh):
    import_fresh("memory.models")
    return sys.modules["memory.models"]


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


# ------------------------------------------------------------------- revision graph


def test_revision_graph_is_baseline_identity_workspaces():
    script = ScriptDirectory.from_config(make_cfg())
    assert script.get_heads() == ["0017_invitations"]  # later revisions (M1-C, M2-A, M2-B, M3-A, M3-B, M4-A1) sit on top of 0003_workspaces
    assert script.get_bases() == [BASELINE]
    rev = script.get_revision(WORKSPACES)
    assert rev.revision == "0003_workspaces" and rev.down_revision == "0002_identity"
    assert rev.branch_labels == set() and rev.dependencies is None
    assert [r.revision for r in script.walk_revisions()] == ["0017_invitations", "0016_durable_dispatch", "0015_resilience_required_action", "0014_usage_notifications", "0013_suspension", "0012_approvals", "0011_tools", "0010_orchestration", "0009_run_leases", "0008_plan_runs", "0007_plans", "0006_agent_executions", "0005_custom_agents", "0004_memory_ownership", WORKSPACES, IDENTITY, BASELINE]
    assert Path(rev.path).resolve() == VERSION_FILE.resolve()
    assert len(WORKSPACES) <= 32  # alembic_version.version_num is VARCHAR(32)


# ------------------------------------------------------- upgrade from 0002_identity


def test_upgrade_from_identity_creates_only_the_workspace_objects(db):
    db.upgrade(IDENTITY)
    before = db.master()
    db.upgrade(WORKSPACES)
    after = db.master()
    assert db.rows("SELECT version_num FROM alembic_version") == [(WORKSPACES,)]
    assert {r[1] for r in after if r[0] == "table"} == {
        "agent_memories", "telegram_links", "users", "auth_identities", "alembic_version"} | NEW_TABLES
    untouched = [r for r in after if r in before]
    assert untouched == before  # every baseline and identity object is byte-for-byte unchanged
    added = {(r[0], r[1]) for r in after if r not in before}
    assert added == {
        ("table", "workspaces"), ("table", "workspace_memberships"), ("index", "ix_workspace_memberships_user_id"),
        # SQLite's own indexes behind the two non-integer PKs and the UNIQUE constraint:
        ("index", "sqlite_autoindex_workspaces_1"), ("index", "sqlite_autoindex_workspaces_2"),
        ("index", "sqlite_autoindex_workspace_memberships_1"),
    }


def test_migration_creates_no_rows_and_preserves_existing_rows(db):
    db.upgrade(IDENTITY)
    db.run(
        "INSERT INTO users (id, email, email_normalized, is_active, created_at) "
        "VALUES ('00000000000000000000000000000001', 'a@example.com', 'a@example.com', 1, '2026-01-01 00:00:00.000000')",
        "INSERT INTO auth_identities (id, user_id, provider, provider_subject, email_at_link, created_at) "
        "VALUES ('00000000000000000000000000000002', '00000000000000000000000000000001', 'google', 's', "
        "'a@example.com', '2026-01-01 00:00:00.000000')",
    )
    existing = db.rows("SELECT * FROM users") + db.rows("SELECT * FROM auth_identities")
    db.upgrade(WORKSPACES)
    assert db.rows("SELECT (SELECT count(*) FROM workspaces), (SELECT count(*) FROM workspace_memberships)") == [(0, 0)]
    assert db.rows("SELECT * FROM users") + db.rows("SELECT * FROM auth_identities") == existing  # no personal workspace made


def test_exact_schema_on_sqlite(db):
    db.upgrade(WORKSPACES)
    engine = db.engine()
    try:
        insp = inspect(engine)

        def columns(table):
            return {c["name"]: (str(c["type"]), c["nullable"], c["default"]) for c in insp.get_columns(table)}

        assert columns("workspaces") == {
            "id": ("CHAR(32)", False, None),
            "name": ("VARCHAR(255)", False, None),
            "is_personal": ("BOOLEAN", False, None),
            "personal_for_user_id": ("CHAR(32)", True, None),
            "created_at": ("DATETIME", False, None),
        }
        assert [c["name"] for c in insp.get_columns("workspaces")] == [
            "id", "name", "is_personal", "personal_for_user_id", "created_at"]
        assert columns("workspace_memberships") == {
            "workspace_id": ("CHAR(32)", False, None),
            "user_id": ("CHAR(32)", False, None),
            "role": ("VARCHAR(16)", False, None),
            "created_at": ("DATETIME", False, None),
        }
        assert [c["name"] for c in insp.get_columns("workspace_memberships")] == [
            "workspace_id", "user_id", "role", "created_at"]
        assert insp.get_pk_constraint("workspaces") == {"constrained_columns": ["id"], "name": "workspaces_pkey"}
        assert insp.get_pk_constraint("workspace_memberships") == {
            "constrained_columns": ["workspace_id", "user_id"], "name": "workspace_memberships_pkey"}
        fks = {t: sorted((f["name"], tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]))
                         for f in insp.get_foreign_keys(t)) for t in NEW_TABLES}
        assert fks == {
            "workspaces": [("fk_workspaces_personal_for_user_id_users", ("personal_for_user_id",), "users", ("id",))],
            "workspace_memberships": [
                ("fk_workspace_memberships_user_id_users", ("user_id",), "users", ("id",)),
                ("fk_workspace_memberships_workspace_id_workspaces", ("workspace_id",), "workspaces", ("id",)),
            ],
        }
        assert [(u["name"], u["column_names"]) for u in insp.get_unique_constraints("workspaces")] == [
            ("uq_workspaces_personal_for_user_id", ["personal_for_user_id"])]
        assert insp.get_unique_constraints("workspace_memberships") == []
        checks = {t: [(c["name"], squash(c["sqltext"])) for c in insp.get_check_constraints(t)] for t in NEW_TABLES}
        assert checks == {
            "workspaces": [("ck_workspaces_personal_consistency", squash(PERSONAL_CHECK))],
            "workspace_memberships": [("ck_workspace_memberships_role", squash(ROLE_CHECK))],
        }
        assert [(i["name"], tuple(i["column_names"]), bool(i["unique"]))
                for i in insp.get_indexes("workspace_memberships") if not i["name"].startswith("sqlite_")] == [
            ("ix_workspace_memberships_user_id", ("user_id",), False)]
        assert [i for i in insp.get_indexes("workspaces") if not i["name"].startswith("sqlite_")] == []
    finally:
        engine.dispose()


def test_schema_matches_the_models_metadata_on_sqlite(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade()
    assert db.diffs(models.Base.metadata) == []  # note: Alembic does not compare CHECK constraints


def test_migration_check_text_equals_the_models_constants(import_fresh):
    models = fresh_models(import_fresh)
    assert models.WORKSPACE_PERSONAL_CHECK == PERSONAL_CHECK and models.MEMBERSHIP_ROLE_CHECK == ROLE_CHECK
    source = VERSION_FILE.read_text(encoding="utf-8")
    assert PERSONAL_CHECK in source and ROLE_CHECK in source


# ---------------------------------------- offline SQL: exact PostgreSQL DDL text (not runtime)


def _statements(sql: str) -> list[str]:
    code = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
    return [s for s in (squash(x) for x in code.split(";") if x.strip()) if s not in ("BEGIN", "COMMIT")]


def test_offline_upgrade_sql_equals_compiled_postgresql_ddl_and_does_nothing_else(import_fresh):
    models = fresh_models(import_fresh)
    out = io.StringIO()
    command.upgrade(make_cfg(out), f"{IDENTITY}:{WORKSPACES}", sql=True)
    statements = _statements(out.getvalue())

    dialect = postgresql.dialect()
    expected = set()
    for name in NEW_TABLES:
        table = models.Base.metadata.tables[name]
        expected.add(squash(str(CreateTable(table).compile(dialect=dialect))))
        expected.update(squash(str(CreateIndex(ix).compile(dialect=dialect))) for ix in table.indexes)
    assert {s for s in statements if s.startswith("CREATE")} == expected
    assert [s for s in statements if not s.startswith("CREATE")] == [
        "UPDATEalembic_versionSETversion_num='0003_workspaces'WHEREalembic_version.version_num='0002_identity'"]
    sql = out.getvalue()
    assert not re.search(r"\b(DROP|DELETE|TRUNCATE|ALTER|INSERT)\b", sql)
    for other in ("agent_memories", "telegram_links", "auth_identities", "CREATE TABLE users"):
        assert other not in sql, other
    flat = squash(sql)
    for fragment in (
        "idUUIDNOTNULL", "nameVARCHAR(255)NOTNULL", "is_personalBOOLEANNOTNULL", "personal_for_user_idUUID,",
        "roleVARCHAR(16)NOTNULL", "created_atTIMESTAMPWITHOUTTIMEZONENOTNULL",
        "CONSTRAINTworkspaces_pkeyPRIMARYKEY(id)",
        "CONSTRAINTuq_workspaces_personal_for_user_idUNIQUE(personal_for_user_id)",
        "CONSTRAINTfk_workspaces_personal_for_user_id_usersFOREIGNKEY(personal_for_user_id)REFERENCESusers(id)",
        "CONSTRAINTck_workspaces_personal_consistencyCHECK(" + squash(PERSONAL_CHECK) + ")",
        "CONSTRAINTworkspace_memberships_pkeyPRIMARYKEY(workspace_id,user_id)",
        "CONSTRAINTfk_workspace_memberships_workspace_id_workspacesFOREIGNKEY(workspace_id)REFERENCESworkspaces(id)",
        "CONSTRAINTfk_workspace_memberships_user_id_usersFOREIGNKEY(user_id)REFERENCESusers(id)",
        "CONSTRAINTck_workspace_memberships_roleCHECK(" + squash(ROLE_CHECK) + ")",
        "CREATEINDEXix_workspace_memberships_user_idONworkspace_memberships(user_id)",
    ):
        assert fragment in flat, fragment
    assert "viewer" not in flat and "DEFAULT" not in flat  # no viewer role, no server defaults


def test_offline_downgrade_sql_drops_only_the_workspace_objects():
    out = io.StringIO()
    command.downgrade(make_cfg(out), f"{WORKSPACES}:{IDENTITY}", sql=True)
    statements = _statements(out.getvalue())
    assert statements == [
        "DROPINDEXix_workspace_memberships_user_id", "DROPTABLEworkspace_memberships", "DROPTABLEworkspaces",
        "UPDATEalembic_versionSETversion_num='0002_identity'WHEREalembic_version.version_num='0003_workspaces'"]
    for other in ("agent_memories", "telegram_links", "auth_identities", "users"):
        assert other not in out.getvalue().replace("DROP TABLE workspaces", ""), other


# ---------------------------------------------------- constraint behaviour (SQLite)


@pytest.fixture
def world(db, import_fresh):
    """A migrated database (head) and helpers for inserting users, workspaces and memberships."""
    models = fresh_models(import_fresh)
    db.upgrade()
    engine = db.engine(fk=True)

    class World:
        pass

    w = World()
    w.models, w.engine = models, engine

    def user(email="u@example.com"):
        u = models.User(email=email, email_normalized=email.casefold())
        with Session(engine) as s:
            s.add(u)
            s.commit()
            return u.id

    def workspace(**kw):
        ws = models.Workspace(name=kw.pop("name", "w"), **kw)
        with Session(engine) as s:
            s.add(ws)
            s.commit()
            return ws.id

    def membership(workspace_id, user_id, role):
        with Session(engine) as s:
            s.add(models.WorkspaceMembership(workspace_id=workspace_id, user_id=user_id, role=role))
            s.commit()

    w.user, w.workspace, w.membership = user, workspace, membership
    yield w
    engine.dispose()


def test_multiple_null_personal_for_user_id_values_are_accepted(world):
    for i in range(3):
        world.workspace(name=f"team {i}", is_personal=False, personal_for_user_id=None)  # NULLs never collide
    with Session(world.engine) as s:
        assert s.query(world.models.Workspace).filter_by(personal_for_user_id=None).count() == 3


def test_one_personal_workspace_per_user(world):
    alice, bob = world.user("alice@example.com"), world.user("bob@example.com")
    world.workspace(name="alice personal", is_personal=True, personal_for_user_id=alice)
    world.workspace(name="bob personal", is_personal=True, personal_for_user_id=bob)  # another user: fine
    with pytest.raises(IntegrityError) as exc:
        world.workspace(name="alice again", is_personal=True, personal_for_user_id=alice)
    assert "personal_for_user_id" in str(exc.value)


def test_personal_row_consistency_check(world):
    owner = world.user()
    with pytest.raises(IntegrityError) as exc:
        world.workspace(name="personal without a user", is_personal=True, personal_for_user_id=None)
    assert "ck_workspaces_personal_consistency" in str(exc.value)
    with pytest.raises(IntegrityError) as exc:
        world.workspace(name="team with a personal owner", is_personal=False, personal_for_user_id=owner)
    assert "ck_workspaces_personal_consistency" in str(exc.value)
    world.workspace(name="valid personal", is_personal=True, personal_for_user_id=owner)
    world.workspace(name="valid team", is_personal=False, personal_for_user_id=None)


@pytest.mark.parametrize("role", ["owner", "admin", "member"])
def test_valid_roles_are_accepted(world, role):
    ws, user = world.workspace(), world.user()
    world.membership(ws, user, role)
    with Session(world.engine) as s:
        assert s.get(world.models.WorkspaceMembership, (ws, user)).role == role


@pytest.mark.parametrize("role", ["viewer", "superuser", "", "Owner", "OWNER", "owner ", "guest"])
def test_viewer_and_unknown_roles_are_rejected(world, role):
    ws, user = world.workspace(), world.user()
    with pytest.raises(IntegrityError) as exc:
        world.membership(ws, user, role)
    assert "ck_workspace_memberships_role" in str(exc.value)


def test_null_role_is_rejected(world):
    ws, user = world.workspace(), world.user()
    with pytest.raises(IntegrityError):
        world.membership(ws, user, None)


def test_duplicate_membership_is_rejected_whatever_the_role(world):
    ws, user = world.workspace(), world.user()
    world.membership(ws, user, "member")
    for role in ("member", "owner"):
        with pytest.raises(IntegrityError):
            world.membership(ws, user, role)
    # Different workspace or different user is fine.
    world.membership(world.workspace(name="second"), user, "member")
    world.membership(ws, world.user("other@example.com"), "member")


def test_unknown_foreign_keys_are_rejected(world):
    ws, user = world.workspace(), world.user()
    with pytest.raises(IntegrityError):
        world.membership(uuid.uuid4(), user, "member")  # unknown workspace
    with pytest.raises(IntegrityError):
        world.membership(ws, uuid.uuid4(), "member")  # unknown user
    with pytest.raises(IntegrityError):
        world.workspace(name="x", is_personal=True, personal_for_user_id=uuid.uuid4())  # unknown personal user


def test_schema_does_not_enforce_exactly_one_owner_by_design(world):
    """Owner invariants are a service-level concern: the database allows zero or several owners."""
    ws = world.workspace()
    a, b = world.user("a@example.com"), world.user("b@example.com")
    world.membership(ws, a, "owner")
    world.membership(ws, b, "owner")  # two owners: not a database matter
    empty = world.workspace(name="no owner yet")  # a workspace without any membership is representable
    with Session(world.engine) as s:
        assert s.query(world.models.WorkspaceMembership).filter_by(workspace_id=empty).count() == 0


# ------------------------------------------------------------ reversibility and safety


def test_downgrade_restores_the_exact_identity_schema_and_reupgrade_succeeds(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade(IDENTITY)
    db.run(
        "INSERT INTO users (id, email, email_normalized, is_active, created_at) "
        "VALUES ('00000000000000000000000000000001', 'a@example.com', 'a@example.com', 1, '2026-01-01 00:00:00.000000')",
        "INSERT INTO agent_memories (agent_name, user_email, summary, created_at) "
        "VALUES ('ceo', 'legacy@example.com', 'legacy', '2026-01-02 03:04:05.000000')",
    )
    identity_objects = db.master()
    identity_rows = db.rows("SELECT * FROM users") + db.rows("SELECT * FROM agent_memories")

    db.upgrade(WORKSPACES)
    workspaces_objects = db.master()
    assert workspaces_objects != identity_objects
    db.run(
        "INSERT INTO workspaces (id, name, is_personal, personal_for_user_id, created_at) "
        "VALUES ('00000000000000000000000000000009', 'w', 1, '00000000000000000000000000000001', '2026-01-03 00:00:00.000000')",
        "INSERT INTO workspace_memberships (workspace_id, user_id, role, created_at) "
        "VALUES ('00000000000000000000000000000009', '00000000000000000000000000000001', 'owner', '2026-01-03 00:00:00.000000')",
    )

    db.downgrade(IDENTITY)  # development/test behaviour; production is forward-only
    assert db.master() == identity_objects  # every object byte for byte as at 0002_identity
    assert db.rows("SELECT version_num FROM alembic_version") == [(IDENTITY,)]
    assert db.rows("SELECT * FROM users") + db.rows("SELECT * FROM agent_memories") == identity_rows
    assert db.diffs(models.Base.metadata, exclude_tables=NEW_TABLES, exclude_ownership=True) == []  # exactly the M1-A schema again

    db.upgrade(WORKSPACES)  # re-upgrade (explicitly to 0003; later revisions have their own tests)
    assert db.master() == workspaces_objects
    assert db.rows("SELECT version_num FROM alembic_version") == [(WORKSPACES,)]
    assert db.rows("SELECT count(*) FROM workspaces") == [(0,)]  # the downgrade removed the workspace data it owns
    assert db.rows("SELECT * FROM users") + db.rows("SELECT * FROM agent_memories") == identity_rows


def test_full_chain_downgrade_to_the_baseline_and_back(db):
    db.upgrade(WORKSPACES)
    db.downgrade(BASELINE)
    assert {r[1] for r in db.master() if r[0] == "table"} == {"agent_memories", "telegram_links", "alembic_version"}
    assert db.diffs(baseline_metadata()) == []
    db.upgrade(WORKSPACES)
    assert db.rows("SELECT version_num FROM alembic_version") == [(WORKSPACES,)]


# ----------------------------- pre-flight and startup behaviour at the new head (M1-A.1 intact)


def _make_unversioned(tmp_path, metadata, name):
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    engine = create_engine(url)
    metadata.create_all(engine)
    engine.dispose()
    return url


def _classify(url):
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return pf.classify(conn)
    finally:
        engine.dispose()


def test_unversioned_baseline_database_still_matches_the_frozen_baseline(tmp_path):
    report = _classify(_make_unversioned(tmp_path, baseline_metadata(), "base.db"))
    assert report.state == pf.MATCH and report.exit_code == 10 and report.findings == ()
    head = ScriptDirectory.from_config(make_cfg()).get_current_head()  # moves with every migration
    assert report.target == f"0001_baseline (frozen baseline schema, NOT the current head {head})"


def test_current_head_schema_without_a_version_table_is_still_divergent(tmp_path, import_fresh):
    models = fresh_models(import_fresh)
    report = _classify(_make_unversioned(tmp_path, models.Base.metadata, "head.db"))
    assert report.state == pf.DIVERGENT and report.exit_code == 20  # never MATCH
    for table in ("users", "auth_identities", "workspaces", "workspace_memberships"):
        assert f"remove_table: {table}" in report.findings
    assert any("matches the current head schema but has no alembic_version" in f for f in report.findings)
    # The CHECK constraints on the new tables must not produce spurious "unexpected check constraint"
    # findings: only the frozen baseline tables are managed when comparing an unversioned database.
    assert not any(f.startswith("unexpected check constraint") for f in report.findings)


# ---------------------------------------------------------------------- source guard


def test_migration_source_uses_only_workspace_ddl_operations():
    source = VERSION_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert set(funcs) == {"upgrade", "downgrade"}

    def ddl(fn):
        return [c.func.attr for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr.startswith(("create_", "drop_", "execute", "bulk", "alter", "add_"))]

    assert sorted(ddl(funcs["upgrade"])) == ["create_index", "create_table", "create_table"]
    assert sorted(ddl(funcs["downgrade"])) == ["drop_index", "drop_table", "drop_table"]
    code = source.split('"""', 2)[2]  # executable code only: the docstring legitimately says "no viewer"
    for forbidden in ("agent_memories", "telegram_links", "auth_identities", "execute(", "bulk_insert",
                      "server_default", "gen_random_uuid", "viewer"):
        assert forbidden not in code, forbidden
    assert 'revision: str = "0003_workspaces"' in source
    assert 'down_revision: Union[str, None] = "0002_identity"' in source
