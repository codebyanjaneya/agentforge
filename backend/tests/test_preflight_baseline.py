"""M1-A.1: an UNVERSIONED database is compared with the frozen 0001_baseline schema, not the live models.

SQLite throw-away files in tmp_path only. SQLite is NOT PostgreSQL; the PostgreSQL runtime evidence is
in tests/test_postgres_integration.py (opt-in, disposable server).
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

import baseline_support
from baseline_support import baseline_metadata
from migrations import baseline_schema
from migrations import preflight as pf
from migrations.schema_guard import SchemaVersionError, verify_schema_version

BACKEND = Path(__file__).resolve().parent.parent
BASELINE = "0001_baseline"
# The real head moves with every migration, so it is read from the script directory; the exact report
# text is still pinned (the format below) for whatever the head is.
HEAD = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini"))).get_current_head()
TARGET_PREFIX = f"0001_baseline (frozen baseline schema, NOT the current head {HEAD})"

VERSION_TABLE_DDL = "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, PRIMARY KEY (version_num))"


def make_db(tmp_path, metadata=None, statements=(), name="pf.db") -> str:
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    engine = create_engine(url)
    if metadata is not None:
        metadata.create_all(engine)
    with engine.begin() as conn:
        for s in statements:
            conn.exec_driver_sql(s)
    engine.dispose()
    return url


def classify(url) -> pf.Report:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return pf.classify(conn)  # real script directory
    finally:
        engine.dispose()


def live_metadata():
    import memory.models as models

    return models.Base.metadata


# ------------------------------------------------------------- unversioned databases


def test_empty_unversioned_database_is_empty(tmp_path):
    report = classify(make_db(tmp_path))
    assert report.state == pf.EMPTY and report.exit_code == 0 and report.findings == ()
    assert report.target.startswith("none")
    assert "compared against: none (the database is empty)" in report.render()


def test_exact_baseline_database_is_a_match_against_the_baseline_not_the_head(tmp_path):
    report = classify(make_db(tmp_path, baseline_metadata()))
    assert report.state == pf.MATCH and report.exit_code == 10 and report.findings == ()
    assert report.target == TARGET_PREFIX  # names the baseline AND says it is not the head
    text = report.render()
    assert f"compared against: {TARGET_PREFIX}" in text
    assert "matches 0001_baseline only and does NOT match the current head" in text
    assert "not authorization" in text and "separate explicit operator actions" in text


DIVERGENT_FROM_BASELINE = {
    "missing_index": (["DROP INDEX ix_agent_memories_user_email"], "add_index: ix_agent_memories_user_email"),
    "missing_column_historical_schema": (
        ["DROP INDEX ix_agent_memories_user_email", "ALTER TABLE agent_memories DROP COLUMN user_email"],
        "add_column: agent_memories.user_email"),
    "missing_table": (["DROP TABLE telegram_links"], "add_table: telegram_links"),
    "extra_unmanaged_table": (["CREATE TABLE legacy_notes (x INTEGER)"], "remove_table: legacy_notes"),
    "extra_column": (["ALTER TABLE agent_memories ADD COLUMN extra INTEGER"], "remove_column: agent_memories.extra"),
    "extra_unique_index": (["CREATE UNIQUE INDEX ux_extra ON telegram_links (email)"], "remove_index: ux_extra"),
}


@pytest.mark.parametrize("name", list(DIVERGENT_FROM_BASELINE))
def test_baseline_database_with_missing_or_extra_objects_is_divergent(tmp_path, name):
    statements, expected = DIVERGENT_FROM_BASELINE[name]
    report = classify(make_db(tmp_path, baseline_metadata(), statements))
    assert report.state == pf.DIVERGENT and report.exit_code == 20, report
    assert any(expected in f for f in report.findings), report.findings
    assert report.target == TARGET_PREFIX
    assert not any("matches the current head" in f for f in report.findings)  # it is not the head schema either


def test_altered_baseline_objects_are_divergent(tmp_path):
    drift = make_db(tmp_path, None, [
        "CREATE TABLE agent_memories (id INTEGER NOT NULL, agent_name VARCHAR(50) NOT NULL, user_email VARCHAR(255), "
        "summary TEXT NOT NULL, created_at DATETIME NOT NULL, PRIMARY KEY (id))",
        "CREATE INDEX ix_agent_memories_agent_name ON agent_memories (agent_name)",
        "CREATE INDEX ix_agent_memories_user_email ON agent_memories (user_email)",
        "CREATE TABLE telegram_links (chat_id BIGINT NOT NULL, email VARCHAR(255), active_agent VARCHAR(100), "
        "updated_at DATETIME NOT NULL, PRIMARY KEY (chat_id))",
    ])
    report = classify(drift)
    assert report.state == pf.DIVERGENT
    assert any("modify_type: agent_memories.agent_name" in f for f in report.findings), report.findings  # VARCHAR(50)
    assert any("modify_nullable: telegram_links.active_agent" in f for f in report.findings), report.findings
    assert report.target == TARGET_PREFIX


# ---------------------------------------- current-head schema without a version table


def test_head_schema_without_a_version_table_is_not_silently_treated_as_baseline(tmp_path):
    report = classify(make_db(tmp_path, live_metadata()))
    assert report.state == pf.DIVERGENT and report.exit_code == 20  # never MATCH
    assert report.target == TARGET_PREFIX
    found = report.findings
    assert "remove_table: users" in found and "remove_table: auth_identities" in found
    assert any("matches the current head schema but has no alembic_version" in f and "must not be stamped as the baseline" in f
               for f in found), found


def test_partial_head_schema_gets_no_head_hint(tmp_path):
    report = classify(make_db(tmp_path, live_metadata(), ["DROP INDEX ix_users_email_normalized"]))
    assert report.state == pf.DIVERGENT
    assert not any("matches the current head" in f for f in report.findings)  # the hint is precise


def test_schema_comparison_default_is_the_baseline_and_the_live_metadata_stays_selectable(tmp_path):
    url = make_db(tmp_path, baseline_metadata())
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            insp = pf.inspect(conn)
            assert pf._schema_findings(conn, insp) == []  # default: the frozen baseline
            live = pf._schema_findings(conn, insp, live_metadata())  # explicit: live head metadata
            assert "add_table: users" in live and "add_table: auth_identities" in live
    finally:
        engine.dispose()


# ----------------------------------------------------------- versioned databases (unchanged)


def test_versioned_baseline_database_is_known_but_behind_the_head(tmp_path):
    url = make_db(tmp_path, baseline_metadata(), [VERSION_TABLE_DDL, f"INSERT INTO alembic_version VALUES ('{BASELINE}')"])
    report = classify(url)
    assert report.state == pf.VERSIONED_BEHIND and report.exit_code == 11
    assert report.target == f"recorded revision {BASELINE} (revision check only; the schema is not compared)"
    assert "is known but not a head" in report.findings[0]


def test_versioned_database_is_classified_by_revision_only_even_when_the_schema_drifted(tmp_path):
    url = make_db(tmp_path, baseline_metadata(),
                  [VERSION_TABLE_DDL, f"INSERT INTO alembic_version VALUES ('{BASELINE}')",
                   "CREATE TABLE legacy_notes (x INTEGER)"])
    assert classify(url).state == pf.VERSIONED_BEHIND  # unchanged T1 behaviour: no schema comparison


def test_versioned_head_database_is_current(tmp_path):
    url = make_db(tmp_path, live_metadata(), [VERSION_TABLE_DDL, f"INSERT INTO alembic_version VALUES ('{HEAD}')"])
    report = classify(url)
    assert report.state == pf.VERSIONED_CURRENT and report.exit_code == 11
    assert report.target == f"recorded revision {HEAD} (revision check only; the schema is not compared)"


# ------------------------------------------------------------------- CLI output


def _main(monkeypatch, capsys, url):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    code = pf.main()
    return code, capsys.readouterr().out


def test_cli_output_states_which_schema_was_matched(tmp_path, monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, make_db(tmp_path, None, name="empty.db"))
    assert code == 0 and "preflight state: EMPTY" in out and "compared against: none (the database is empty)" in out

    code, out = _main(monkeypatch, capsys, make_db(tmp_path, baseline_metadata(), name="base.db"))
    assert code == 10 and "preflight state: MATCH" in out
    assert f"compared against: {TARGET_PREFIX}" in out and "does NOT match the current head" in out

    code, out = _main(monkeypatch, capsys, make_db(tmp_path, live_metadata(), name="head.db"))
    assert code == 20 and "preflight state: DIVERGENT" in out and "compared against: 0001_baseline" in out
    assert "must not be stamped as the baseline" in out

    code, out = _main(monkeypatch, capsys, make_db(
        tmp_path, baseline_metadata(), [VERSION_TABLE_DDL, f"INSERT INTO alembic_version VALUES ('{BASELINE}')"], "ver.db"))
    assert code == 11 and "preflight state: ALREADY_VERSIONED_BEHIND" in out
    assert "compared against: recorded revision 0001_baseline (revision check only" in out


def test_module_cli_reports_the_baseline_target(tmp_path):
    url = make_db(tmp_path, baseline_metadata())
    env = {**os.environ, "APP_ENV": "test", "DATABASE_URL": url, "PYTHON_DOTENV_DISABLED": "1", "PYTHONPATH": str(BACKEND)}
    env.pop("GROQ_API_KEY", None)
    result = subprocess.run([sys.executable, "-m", "migrations.preflight"], cwd=BACKEND, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 10, result.stdout + result.stderr
    assert "preflight state: MATCH" in result.stdout and TARGET_PREFIX in result.stdout
    assert "does NOT match the current head" in result.stdout


# --------------------------------------------- the whole adoption sequence (temp SQLite)


def test_adoption_sequence_baseline_match_then_stamp_then_upgrade_then_guard(tmp_path, monkeypatch):
    url = make_db(tmp_path, baseline_metadata())  # a legacy database exactly as create_all built it
    assert classify(url).state == pf.MATCH  # 1. verified against the frozen baseline

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.attributes["configure_logger"] = False

    command.stamp(cfg, BASELINE)  # 2. explicit operator action (here: a test on a temp file), never automatic
    assert classify(url).state == pf.VERSIONED_BEHIND
    with pytest.raises(SchemaVersionError) as exc:
        verify_schema_version(url)  # the startup guard refuses a database behind the head
    assert exc.value.code == "SCHEMA_BEHIND_HEAD"

    command.upgrade(cfg, "head")  # 3. normal upgrade to the current head
    assert classify(url).state == pf.VERSIONED_CURRENT
    assert verify_schema_version(url) == HEAD  # 4. the startup guard accepts it


def test_pre_flight_itself_never_stamps_or_writes(tmp_path):
    url = make_db(tmp_path, baseline_metadata())
    path = tmp_path / "pf.db"
    before = path.read_bytes()
    assert classify(url).state == pf.MATCH
    assert path.read_bytes() == before  # classification wrote nothing: no alembic_version, no change


# ---------------------------------------------------- production code stays test-free


def test_production_modules_do_not_import_test_code_and_the_baseline_is_self_contained():
    for name, allowed_roots in (("baseline_schema.py", {"sqlalchemy"}), ("preflight.py", None)):
        tree = ast.parse((BACKEND / "migrations" / name).read_text(encoding="utf-8"))
        imported = {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        imported |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not imported & {"tests", "baseline_support", "conftest", "pytest"}, (name, imported)
        if allowed_roots:
            assert imported <= allowed_roots, (name, imported)


def test_the_tests_use_the_production_baseline_source():
    assert baseline_support.baseline_metadata is baseline_schema.baseline_metadata
    assert baseline_schema.BASELINE_REVISION == BASELINE and set(baseline_schema.baseline_metadata().tables) == {
        "agent_memories", "telegram_links"}
