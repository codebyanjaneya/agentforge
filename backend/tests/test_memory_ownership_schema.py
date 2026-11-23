"""M1-C: memory ownership references, migration 0004_memory_ownership (SQLite files in tmp_path).

SQLite is NOT PostgreSQL: it proves structure, constraint behaviour, legacy-row safety and reversibility
only; PostgreSQL runtime behaviour is checked separately (tests/test_postgres_integration.py, opt-in against
a disposable server). Offline SQL tests compare compiled PostgreSQL DDL text only. Nothing here exercises
query filtering or authorization: this task adds schema only.
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
from sqlalchemy.schema import AddConstraint, CreateIndex

from baseline_support import baseline_metadata
from migration_support import BACKEND, Db, make_cfg
from migrations import preflight as pf

VERSION_FILE = BACKEND / "migrations" / "versions" / "0004_memory_ownership.py"
BASELINE, IDENTITY, WORKSPACES, OWNERSHIP = "0001_baseline", "0002_identity", "0003_workspaces", "0004_memory_ownership"
INDEX = "ix_agent_memories_user_id_created_at_id"
FK_USER, FK_WORKSPACE = "fk_agent_memories_user_id_users", "fk_agent_memories_workspace_id_workspaces"
USER_A = "00000000000000000000000000000a01"
WORKSPACE_A = "00000000000000000000000000000b01"


@pytest.fixture
def db(tmp_path, monkeypatch):
    return Db(tmp_path / "ownership.db", monkeypatch)


def fresh_models(import_fresh):
    import_fresh("memory.models")
    return sys.modules["memory.models"]


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def seed_legacy(db: Db):
    """Three legacy rows as production could hold them: with an email, orphaned, and 'voice-call'."""
    db.run(
        "INSERT INTO agent_memories (agent_name, user_email, summary, created_at) "
        "VALUES ('ceo', 'legacy@example.com', 'legacy summary', '2026-01-02 03:04:05.000000')",
        "INSERT INTO agent_memories (agent_name, user_email, summary, created_at) "
        "VALUES ('sales', NULL, 'orphan', '2026-02-03 04:05:06.000000')",
        "INSERT INTO agent_memories (agent_name, user_email, summary, created_at) "
        "VALUES ('ceo', 'voice-call@agentforge.local', 'a phone call', '2026-03-04 05:06:07.000000')",
    )


LEGACY_SQL = "SELECT id, agent_name, user_email, summary, created_at FROM agent_memories ORDER BY id"


def add_user_and_workspace(db: Db):
    db.run(
        f"INSERT INTO users (id, email, email_normalized, is_active, created_at) "
        f"VALUES ('{USER_A}', 'owner@example.com', 'owner@example.com', 1, '2026-01-01 00:00:00.000000')",
        f"INSERT INTO workspaces (id, name, is_personal, personal_for_user_id, created_at) "
        f"VALUES ('{WORKSPACE_A}', 'w', 0, NULL, '2026-01-01 00:00:00.000000')",
    )


# ------------------------------------------------------------------- revision graph


def test_revision_graph_ends_with_memory_ownership():
    script = ScriptDirectory.from_config(make_cfg())
    assert script.get_heads() == ["0018_agent_slug_per_user"]  # M2-A, M2-B, M3-A, M3-B and M4-A1 sit on top of 0004_memory_ownership
    assert script.get_bases() == [BASELINE]
    rev = script.get_revision(OWNERSHIP)
    assert rev.revision == "0004_memory_ownership" and rev.down_revision == "0003_workspaces"
    assert rev.branch_labels == set() and rev.dependencies is None
    assert [r.revision for r in script.walk_revisions()] == ["0018_agent_slug_per_user", "0017_invitations", "0016_durable_dispatch", "0015_resilience_required_action", "0014_usage_notifications", "0013_suspension", "0012_approvals", "0011_tools", "0010_orchestration", "0009_run_leases", "0008_plan_runs", "0007_plans", "0006_agent_executions", "0005_custom_agents", OWNERSHIP, WORKSPACES, IDENTITY, BASELINE]
    assert Path(rev.path).resolve() == VERSION_FILE.resolve()
    assert len(OWNERSHIP) <= 32  # alembic_version.version_num is VARCHAR(32)


# --------------------------------------------------- upgrade from 0003_workspaces


def test_upgrade_from_workspaces_changes_only_agent_memories(db):
    db.upgrade(WORKSPACES)
    before = db.master()
    db.upgrade(OWNERSHIP)
    after = db.master()
    assert db.rows("SELECT version_num FROM alembic_version") == [(OWNERSHIP,)]
    # Every object except agent_memories' own DDL is byte-for-byte unchanged ...
    untouched = [r for r in after if r in before]
    changed_before = [r for r in before if r not in after]
    assert [r[1] for r in changed_before] == ["agent_memories"]
    # ... and the only additions are the table's rewritten DDL and the new index.
    added = {(r[0], r[1]) for r in after if r not in before}
    assert added == {("table", "agent_memories"), ("index", INDEX)}
    assert len(untouched) == len(before) - 1
    # The pre-existing columns and both pre-existing indexes are untouched.
    assert [r[1] for r in db.rows("PRAGMA table_info(agent_memories)")] == [
        "id", "agent_name", "user_email", "summary", "created_at", "user_id", "workspace_id"]
    assert {r[1] for r in after if r[0] == "index" and r[2] == "agent_memories"} == {
        "ix_agent_memories_agent_name", "ix_agent_memories_user_email", INDEX}
    for name in ("ix_agent_memories_agent_name", "ix_agent_memories_user_email"):
        assert next(r for r in after if r[1] == name) == next(r for r in before if r[1] == name)


def test_exact_columns_types_nullability_and_foreign_keys_on_sqlite(db):
    db.upgrade(OWNERSHIP)
    engine = db.engine()
    try:
        insp = inspect(engine)
        cols = {c["name"]: (str(c["type"]), c["nullable"], c["default"]) for c in insp.get_columns("agent_memories")}
        assert cols == {
            "id": ("INTEGER", False, None),
            "agent_name": ("VARCHAR(100)", False, None),
            "user_email": ("VARCHAR(255)", True, None),
            "summary": ("TEXT", False, None),
            "created_at": ("DATETIME", False, None),
            "user_id": ("CHAR(32)", True, None),
            "workspace_id": ("CHAR(32)", True, None),
        }
        assert insp.get_pk_constraint("agent_memories")["constrained_columns"] == ["id"]
        fks = sorted((tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]))
                     for f in insp.get_foreign_keys("agent_memories"))
        assert fks == [(("user_id",), "users", ("id",)), (("workspace_id",), "workspaces", ("id",))]
        assert insp.get_unique_constraints("agent_memories") == [] and insp.get_check_constraints("agent_memories") == []
    finally:
        engine.dispose()
    # SQLite dialect difference: the inspector does not report the names of inline column-level foreign keys
    # (they are part of the stored DDL, which is what we pin here).
    sql = squash(next(r[3] for r in db.master() if r[1] == "agent_memories"))
    assert f"user_idCHAR(32)CONSTRAINT{FK_USER}REFERENCESusers(id)" in sql
    assert f"workspace_idCHAR(32)CONSTRAINT{FK_WORKSPACE}REFERENCESworkspaces(id)" in sql


def test_history_index_columns_order_and_desc_semantics_on_sqlite(db):
    db.upgrade(OWNERSHIP)
    stored = next(r[3] for r in db.master() if r[1] == INDEX)
    assert stored == f"CREATE INDEX {INDEX} ON agent_memories (user_id, created_at DESC, id DESC)"
    # SQLite's own catalog confirms column order and the DESC flag (index_xinfo: seqno, cid, name, desc, ...).
    info = [(r[0], r[2], r[3]) for r in db.rows(f'PRAGMA index_xinfo("{INDEX}")') if r[5] == 1]  # key columns only
    assert info == [(0, "user_id", 0), (1, "created_at", 1), (2, "id", 1)]
    # SQLAlchemy's SQLite reflection reports names and order but not the sort direction (dialect difference).
    engine = db.engine()
    try:
        reflected = next(i for i in inspect(engine).get_indexes("agent_memories") if i["name"] == INDEX)
        assert reflected["column_names"] == ["user_id", "created_at", "id"] and not reflected["unique"]
        assert "column_sorting" not in reflected
    finally:
        engine.dispose()
    # The index is used for the history query shape (newest first, id as the tie-breaker).
    plan = " ".join(str(r[3]) for r in db.rows(
        f"EXPLAIN QUERY PLAN SELECT id FROM agent_memories WHERE user_id = '{USER_A}' ORDER BY created_at DESC, id DESC"))
    assert INDEX in plan and "TEMP B-TREE" not in plan


def test_schema_matches_the_models_metadata_on_sqlite(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade()
    assert db.diffs(models.Base.metadata) == []  # Alembic compares columns, FKs and index columns (not sort order or CHECKs)


# ------------------------------------- offline SQL: exact PostgreSQL DDL text (not runtime)


def _statements(sql: str) -> list[str]:
    code = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
    return [s for s in (squash(x) for x in code.split(";") if x.strip()) if s not in ("BEGIN", "COMMIT")]


def test_offline_upgrade_sql_for_postgresql_is_exactly_the_ownership_ddl(import_fresh):
    models = fresh_models(import_fresh)
    table = models.Base.metadata.tables["agent_memories"]
    dialect = postgresql.dialect()
    out = io.StringIO()
    command.upgrade(make_cfg(out), f"{WORKSPACES}:{OWNERSHIP}", sql=True)
    statements = _statements(out.getvalue())

    fks = {fk.name: fk for fk in table.foreign_key_constraints}
    index = next(ix for ix in table.indexes if ix.name == INDEX)
    assert statements == [
        "ALTERTABLEagent_memoriesADDCOLUMNuser_idUUID",
        "ALTERTABLEagent_memoriesADDCOLUMNworkspace_idUUID",
        squash(str(AddConstraint(fks[FK_USER]).compile(dialect=dialect))),
        squash(str(AddConstraint(fks[FK_WORKSPACE]).compile(dialect=dialect))),
        squash(str(CreateIndex(index).compile(dialect=dialect))),
        "UPDATEalembic_versionSETversion_num='0004_memory_ownership'WHEREalembic_version.version_num='0003_workspaces'",
    ]
    flat = squash(out.getvalue())
    assert f"CREATEINDEX{INDEX}ONagent_memories(user_id,created_atDESC,idDESC)" in flat
    assert f"ADDCONSTRAINT{FK_USER}FOREIGNKEY(user_id)REFERENCESusers(id)" in flat
    assert f"ADDCONSTRAINT{FK_WORKSPACE}FOREIGNKEY(workspace_id)REFERENCESworkspaces(id)" in flat
    assert not re.search(r"\b(DROP|DELETE|TRUNCATE|INSERT|UPDATE agent_memories|SET)\b", out.getvalue().replace(
        "UPDATE alembic_version SET", ""))  # no data statements: nothing is backfilled
    assert "NOTNULL" not in flat.replace("version_numVARCHAR(32)NOTNULL", "")  # both new columns stay nullable
    assert "DEFAULT" not in flat


def test_offline_downgrade_sql_for_postgresql_drops_only_the_ownership_objects():
    out = io.StringIO()
    command.downgrade(make_cfg(out), f"{OWNERSHIP}:{WORKSPACES}", sql=True)
    assert _statements(out.getvalue()) == [
        f"DROPINDEX{INDEX}",
        f"ALTERTABLEagent_memoriesDROPCONSTRAINT{FK_WORKSPACE}",
        f"ALTERTABLEagent_memoriesDROPCONSTRAINT{FK_USER}",
        "ALTERTABLEagent_memoriesDROPCOLUMNworkspace_id",
        "ALTERTABLEagent_memoriesDROPCOLUMNuser_id",
        "UPDATEalembic_versionSETversion_num='0003_workspaces'WHEREalembic_version.version_num='0004_memory_ownership'",
    ]


# ------------------------------------------- legacy data: survives, stays NULL, no backfill


def test_legacy_rows_survive_the_upgrade_unchanged_with_null_ownership_and_no_backfill(db):
    db.upgrade(WORKSPACES)
    seed_legacy(db)
    add_user_and_workspace(db)  # a user and a workspace exist: a backfill would have something to attach to
    legacy = db.rows(LEGACY_SQL)
    users, workspaces = db.rows("SELECT * FROM users"), db.rows("SELECT * FROM workspaces")

    db.upgrade(OWNERSHIP)
    assert db.rows(LEGACY_SQL) == legacy  # byte-for-byte the same legacy values
    assert db.rows("SELECT user_id, workspace_id FROM agent_memories ORDER BY id") == [(None, None)] * 3
    assert db.rows("SELECT count(*) FROM agent_memories WHERE user_id IS NOT NULL OR workspace_id IS NOT NULL") == [(0,)]
    # No identity or workspace data was created or changed: nothing is inferred from email, content or time.
    assert db.rows("SELECT * FROM users") == users and db.rows("SELECT * FROM workspaces") == workspaces
    assert db.rows("SELECT count(*) FROM auth_identities") == [(0,)]
    assert db.rows("SELECT count(*) FROM workspace_memberships") == [(0,)]


def test_a_legacy_row_whose_email_matches_a_real_user_is_still_not_assigned(db):
    """The one case where a backfill would be tempting: the owner's email equals a legacy user_email."""
    db.upgrade(WORKSPACES)
    db.run(
        "INSERT INTO agent_memories (agent_name, user_email, summary, created_at) "
        "VALUES ('ceo', 'owner@example.com', 'looks like the owner', '2026-01-02 03:04:05.000000')")
    add_user_and_workspace(db)  # users.email = owner@example.com
    db.upgrade(OWNERSHIP)
    assert db.rows("SELECT user_email, user_id, workspace_id FROM agent_memories") == [("owner@example.com", None, None)]


def test_null_ownership_is_permitted_for_new_rows_too(db):
    db.upgrade(OWNERSHIP)
    seed_legacy(db)  # inserting without ownership still works (legacy-style writers keep functioning)
    assert db.rows("SELECT count(*) FROM agent_memories WHERE user_id IS NULL AND workspace_id IS NULL") == [(3,)]


# ------------------------------------------------------ foreign keys (SQLite, FKs enforced)


def _insert_memory(engine, models, **ownership):
    with Session(engine) as s:
        s.add(models.AgentMemory(agent_name="ceo", summary="s", **ownership))
        s.commit()


def test_new_rows_may_reference_valid_users_and_workspaces(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade(OWNERSHIP)
    engine = db.engine(fk=True)
    try:
        with Session(engine) as s:
            user = models.User(email="o@example.com", email_normalized="o@example.com")
            s.add(user)
            s.flush()
            workspace = models.Workspace(name="w", is_personal=True, personal_for_user_id=user.id)
            s.add(workspace)
            s.flush()
            user_id, workspace_id = user.id, workspace.id
            s.commit()
        _insert_memory(engine, models, user_id=user_id, workspace_id=workspace_id)
        _insert_memory(engine, models, user_id=user_id)  # user only
        _insert_memory(engine, models, workspace_id=workspace_id)  # workspace only
        with Session(engine) as s:
            rows = s.query(models.AgentMemory).order_by(models.AgentMemory.id).all()
            assert [(r.user_id, r.workspace_id) for r in rows] == [
                (user_id, workspace_id), (user_id, None), (None, workspace_id)]
    finally:
        engine.dispose()


def test_unknown_user_or_workspace_references_are_rejected(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade(OWNERSHIP)
    engine = db.engine(fk=True)
    try:
        for ownership in ({"user_id": uuid.uuid4()}, {"workspace_id": uuid.uuid4()},
                          {"user_id": uuid.uuid4(), "workspace_id": uuid.uuid4()}):
            with pytest.raises(IntegrityError) as exc:
                _insert_memory(engine, models, **ownership)
            assert "FOREIGN KEY" in str(exc.value).upper()
        with Session(engine) as s:
            assert s.query(models.AgentMemory).count() == 0  # nothing was written
    finally:
        engine.dispose()


# ------------------------------------------------------------ reversibility and safety


def test_downgrade_restores_the_exact_workspace_schema_keeps_rows_and_reupgrade_is_identical(db, import_fresh):
    models = fresh_models(import_fresh)
    db.upgrade(WORKSPACES)
    seed_legacy(db)
    add_user_and_workspace(db)
    workspaces_objects = db.master()
    legacy = db.rows(LEGACY_SQL)

    db.upgrade(OWNERSHIP)
    ownership_objects = db.master()
    assert ownership_objects != workspaces_objects
    # An owned row, created after the migration, is the one thing a downgrade cannot keep owned.
    db.run(f"INSERT INTO agent_memories (agent_name, user_email, summary, created_at, user_id, workspace_id) "
           f"VALUES ('ceo', NULL, 'owned', '2026-04-05 06:07:08.000000', '{USER_A}', '{WORKSPACE_A}')")

    db.downgrade(WORKSPACES)  # development/test behaviour; production is forward-only
    assert db.master() == workspaces_objects  # every object, byte for byte, as at 0003_workspaces
    assert db.rows("SELECT version_num FROM alembic_version") == [(WORKSPACES,)]
    rows = db.rows(LEGACY_SQL)
    assert rows[:3] == legacy and [r[3] for r in rows] == ["legacy summary", "orphan", "a phone call", "owned"]  # every row kept
    assert [r[0] for r in rows] == [1, 2, 3, 4]  # ids preserved
    assert db.diffs(models.Base.metadata, exclude_ownership=True) == []  # exactly the 0003 schema again ...
    assert db.diffs(models.Base.metadata) != []  # ... while the live head models differ from it

    db.upgrade(OWNERSHIP)  # re-upgrade to the same revision (later revisions, e.g. 0005, are covered by their own tests)
    assert db.master() == ownership_objects
    assert db.rows("SELECT version_num FROM alembic_version") == [(OWNERSHIP,)]
    assert db.rows("SELECT user_id, workspace_id FROM agent_memories ORDER BY id") == [(None, None)] * 4  # still no backfill
    db.upgrade("head")
    assert db.diffs(models.Base.metadata) == []


def test_full_chain_downgrade_to_the_baseline_and_back_keeps_the_rows(db):
    db.upgrade(BASELINE)
    seed_legacy(db)
    legacy = db.rows(LEGACY_SQL)
    baseline_objects = db.master()
    db.upgrade("head")
    db.downgrade(BASELINE)
    assert db.master() == baseline_objects  # the exact baseline, byte for byte
    assert db.rows(LEGACY_SQL) == legacy
    assert db.diffs(baseline_metadata()) == []
    db.upgrade("head")
    assert db.rows(LEGACY_SQL) == legacy


# ----------------------------- pre-flight behaviour at the new head (M1-A.1 / M1-B intact)


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
    assert report.target == "0001_baseline (frozen baseline schema, NOT the current head 0018_agent_slug_per_user)"


def test_current_head_schema_without_a_version_table_is_still_divergent(tmp_path, import_fresh):
    models = fresh_models(import_fresh)
    report = _classify(_make_unversioned(tmp_path, models.Base.metadata, "head.db"))
    assert report.state == pf.DIVERGENT and report.exit_code == 20  # never MATCH
    # The extra ownership columns on a baseline table are reported against the frozen baseline ...
    assert "remove_column: agent_memories.user_id" in report.findings
    assert "remove_column: agent_memories.workspace_id" in report.findings
    assert any("matches the current head schema but has no alembic_version" in f for f in report.findings)
    assert not any(f.startswith("unexpected check constraint") for f in report.findings)


def test_baseline_database_with_one_ownership_column_is_not_a_baseline_match(tmp_path):
    """Partial / legacy-adjacent drift: a baseline database that already has a stray user_id column."""
    url = _make_unversioned(tmp_path, baseline_metadata(), "stray.db")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE agent_memories ADD COLUMN user_id CHAR(32)")
    engine.dispose()
    report = _classify(url)
    assert report.state == pf.DIVERGENT and "remove_column: agent_memories.user_id" in report.findings
    assert not any("matches the current head" in f for f in report.findings)


# ---------------------------------------------------------------------- source guard


def test_migration_source_adds_only_ownership_objects_and_never_touches_data_values():
    source = VERSION_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {"upgrade", "downgrade"} <= set(funcs)
    code = source.split('"""', 2)[2]  # executable code only
    for forbidden in ("bulk_insert", "UPDATE ", "DELETE ", "user_email =", "lower(", "LIKE", "workspaces SET", "users SET",
                      "server_default", "gen_random_uuid", "SELECT user_id"):
        assert forbidden not in code, forbidden
    # The only data statement is the SQLite downgrade rebuild's verbatim row copy of the baseline columns.
    inserts = re.findall(r'"INSERT INTO[^"]*"\s*"[^"]*"', code)
    assert len(inserts) == 1 and "user_id" not in inserts[0] and "workspace_id" not in inserts[0]
    assert 'revision: str = "0004_memory_ownership"' in source
    assert 'down_revision: Union[str, None] = "0003_workspaces"' in source
