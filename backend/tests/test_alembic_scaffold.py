"""Alembic scaffolding (T1-D): config, metadata target, offline SQL, import isolation, no leaks.

No migration is ever applied: the online tests only run ``alembic current`` against a
throw-away SQLite file in tmp_path. Offline tests use a toy revision in tmp_path, never a
real one. Nothing connects to PostgreSQL or the network.
"""

import os
import shutil
import subprocess
import sys
import textwrap
import traceback
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory

BACKEND = Path(__file__).resolve().parent.parent
INI = BACKEND / "alembic.ini"
MIGRATIONS = BACKEND / "migrations"
SENTINEL = "s3ntinel-pw-9f2c"

TOY_REVISION = textwrap.dedent(
    '''
    import sqlalchemy as sa
    from alembic import op

    revision = "0001"
    down_revision = None
    branch_labels = None
    depends_on = None


    def upgrade():
        op.create_table("toy", sa.Column("id", sa.Integer(), primary_key=True))


    def downgrade():
        pass
    '''
)


def make_config(script_location: Path | None = None, output=None) -> Config:
    cfg = Config(str(INI), output_buffer=output)
    cfg.attributes["configure_logger"] = False  # keep pytest's logging (caplog) intact
    if script_location is not None:
        cfg.set_main_option("script_location", script_location.as_posix())
    return cfg


def make_toy_scripts(tmp_path: Path) -> Path:
    """The real env.py and template, with one throw-away revision."""
    d = tmp_path / "toy_migrations"
    (d / "versions").mkdir(parents=True)
    shutil.copy(MIGRATIONS / "env.py", d / "env.py")
    shutil.copy(MIGRATIONS / "script.py.mako", d / "script.py.mako")
    (d / "versions" / "0001_toy.py").write_text(TOY_REVISION, encoding="utf-8")
    return d


# ----------------------------------------------------------------- configuration


def test_ini_has_no_database_url_or_credentials():
    text = INI.read_text(encoding="utf-8")
    assert "sqlalchemy.url" not in text
    assert "://" not in text and "password" not in text.lower()
    cfg = make_config()
    assert cfg.get_main_option("sqlalchemy.url") is None
    assert cfg.get_main_option("script_location").endswith("/migrations")


def test_script_directory_loads_with_a_single_linear_head():
    script = ScriptDirectory.from_config(make_config())
    assert Path(script.dir).resolve() == MIGRATIONS.resolve()
    assert script.get_bases() == ["0001_baseline"]  # the T1-E baseline is the root of the chain
    assert script.get_heads() == ["0017_invitations"]  # M4-A1 (0008: M3-B; 0007: M3-A; 0006: M2-B; 0005: M2-A; 0004: M1-C; 0003: M1-B; 0002: M1-A; baseline: T1-E)
    assert (MIGRATIONS / "script.py.mako").is_file()


def test_ini_logging_is_not_verbose():
    import configparser

    parser = configparser.RawConfigParser()
    parser.read(INI, encoding="utf-8")
    levels = {parser.get(s, "level") for s in ("logger_root", "logger_sqlalchemy", "logger_alembic")}
    assert levels == {"WARN"}  # INFO would echo SQL


# --------------------------------------------------------------- metadata target


def _run_env_offline(cfg):
    script = ScriptDirectory.from_config(cfg)
    with EnvironmentContext(cfg, script, fn=lambda rev, ctx: [], as_sql=True) as env:
        script.run_env()
        return env.get_context()


def test_metadata_target_is_memory_models_base_metadata(import_fresh):
    import_fresh("memory.models")
    ctx = _run_env_offline(make_config())
    models = sys.modules["memory.models"]
    assert ctx.opts["target_metadata"] is models.Base.metadata
    assert ctx.opts["compare_type"] is True and ctx.opts["compare_server_default"] is True
    assert ctx.dialect.name == "postgresql"  # placeholder URL: dialect only


def test_include_object_ignores_version_table_and_never_drops_unmanaged_tables(import_fresh):
    import_fresh("memory.models")
    include = _run_env_offline(make_config()).opts["include_object"]
    assert include(None, "alembic_version", "table", True, None) is False
    assert include(None, "legacy_notes", "table", True, None) is False  # DB-only: never a drop
    assert include(None, "agent_memories", "table", True, object()) is True  # in metadata
    assert include(None, "agent_memories", "table", False, None) is True  # metadata-only: create
    assert include(None, "ix_agent_memories_agent_name", "index", True, None) is True


def test_env_contains_no_hardcoded_urls():
    text = (MIGRATIONS / "env.py").read_text(encoding="utf-8")
    assert "sqlalchemy.url" not in text and "set_main_option" not in text
    assert "NullPool" in text


# ------------------------------------------------------------------- offline SQL


def test_offline_sql_generation_needs_no_database(tmp_path, capsys, monkeypatch):
    # A sentinel URL in the environment must not influence (or appear in) offline output.
    monkeypatch.setenv("DATABASE_URL", f"postgresql://user:{SENTINEL}@db.example.invalid/prod")
    import io

    out = io.StringIO()
    cfg = make_config(make_toy_scripts(tmp_path), output=out)
    command.upgrade(cfg, "base:head", sql=True)
    sql = out.getvalue()
    assert "CREATE TABLE toy" in sql and "CREATE TABLE alembic_version" in sql
    assert "INSERT INTO alembic_version" in sql
    captured = capsys.readouterr()
    for text in (sql, captured.out, captured.err):
        assert SENTINEL not in text and "db.example.invalid" not in text and "postgresql://" not in text


def test_offline_run_in_clean_interpreter_imports_no_app_modules(tmp_path):
    toy = make_toy_scripts(tmp_path)
    code = textwrap.dedent(
        """
        import io, socket, sys
        def blocked(*a, **k): raise RuntimeError('network attempted')
        socket.socket.connect = blocked; socket.socket.connect_ex = blocked
        from alembic import command
        from alembic.config import Config
        out = io.StringIO()
        cfg = Config(sys.argv[1], output_buffer=out)
        cfg.attributes['configure_logger'] = False
        cfg.set_main_option('script_location', sys.argv[2])
        command.upgrade(cfg, 'base:head', sql=True)
        loaded = [n for n in ('config', 'memory.long_term', 'main', 'orchestrator', 'redis', 'groq',
                              'telegram', 'langchain', 'psycopg2') if n in sys.modules]
        assert 'memory.models' in sys.modules
        assert not loaded, loaded
        print(out.getvalue())
        """
    )
    env = {k: v for k, v in os.environ.items() if k not in ("GROQ_API_KEY", "REDIS_URL")}
    env.update(
        PYTHONPATH=str(BACKEND),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHON_DOTENV_DISABLED="1",
        DATABASE_URL=f"postgresql://user:{SENTINEL}@db.example.invalid/prod",
    )
    r = subprocess.run(
        [sys.executable, "-c", code, str(INI), toy.as_posix()],
        cwd=tmp_path, capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "CREATE TABLE toy" in r.stdout
    assert SENTINEL not in r.stdout + r.stderr and "db.example.invalid" not in r.stdout + r.stderr


# -------------------------------------------------- online mode (SQLite, read-only)


def _sqlite_env(monkeypatch, tmp_path):
    db = tmp_path / f"{SENTINEL}.db"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    return db


def test_online_mode_uses_nullpool_and_leaks_nothing(tmp_path, monkeypatch, capsys, caplog):
    import sqlalchemy
    from sqlalchemy.pool import NullPool

    _sqlite_env(monkeypatch, tmp_path)
    seen = []
    real = sqlalchemy.create_engine
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: (seen.append(k), real(*a, **k))[1])
    caplog.set_level("DEBUG")
    command.current(make_config())  # read-only; empty database, no migration applied
    assert [k.get("poolclass") for k in seen] == [NullPool]
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err + caplog.text


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql+nosuchdriver://user:{SENTINEL}@db.example.invalid/prod",  # create_engine fails
        f"postgresql://user:{SENTINEL}@[bad-host/prod",  # unparsable URL
    ],
)
def test_online_connection_failures_do_not_leak_the_url(url, monkeypatch, capsys, caplog):
    monkeypatch.setenv("DATABASE_URL", url)
    caplog.set_level("DEBUG")
    with pytest.raises(RuntimeError) as info:
        command.current(make_config())
    full = "".join(traceback.format_exception(info.value))  # includes any chained cause
    captured = capsys.readouterr()
    for text in (full, captured.out, captured.err, caplog.text):
        assert SENTINEL not in text and "db.example.invalid" not in text
    assert "Could not connect to the database" in str(info.value)


@pytest.mark.parametrize(
    "env",
    [
        {},  # DATABASE_URL missing
        {"DATABASE_URL": f"mysql://user:{SENTINEL}@db.example.invalid/prod"},  # wrong scheme
        {"DATABASE_URL": f"sqlite:///{SENTINEL}.db"},  # sqlite outside APP_ENV=test (production default)
    ],
)
def test_online_config_errors_name_variables_but_not_values(env, monkeypatch, capsys):
    from config import ConfigError

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ConfigError) as info:
        command.current(make_config())
    full = "".join(traceback.format_exception(info.value))
    assert "DATABASE_URL" in str(info.value)
    assert SENTINEL not in full + capsys.readouterr().err


def test_online_mode_does_not_require_groq_key_or_other_settings(tmp_path, monkeypatch):
    _sqlite_env(monkeypatch, tmp_path)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    command.current(make_config())  # would raise ConfigError if full settings were required


def test_get_database_url_helper_is_secret_and_validated():
    from config import ConfigError, get_database_url

    url = get_database_url({"DATABASE_URL": f"postgresql://u:{SENTINEL}@h/d"})
    assert SENTINEL not in repr(url) and SENTINEL not in str(url)
    assert url.get_secret_value().endswith("@h/d")
    with pytest.raises(ConfigError):
        get_database_url({"APP_ENV": "bogus", "DATABASE_URL": "postgresql://h/d"})


# --------------------------------------- unmanaged-object protection (T1-D-R1)
# Simulation, NOT PostgreSQL integration: a throw-away SQLite file plays the "database" and a
# small controlled MetaData plays the models. It exercises Alembic's real compare_metadata with
# the real env.py include_object; PostgreSQL reflection/type/default behavior is unverified.


def _autogen_ops(tmp_path, include_object):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text

    engine = create_engine(f"sqlite:///{(tmp_path / 'sim.db').as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE managed (id INTEGER PRIMARY KEY, name VARCHAR(50), "
                          "legacy_col TEXT, qty INTEGER DEFAULT 1)"))
        conn.execute(text("CREATE TABLE legacy_table (id INTEGER PRIMARY KEY)"))
    md = MetaData()
    Table(
        "managed", md,
        Column("id", Integer, primary_key=True),
        Column("name", String(100)),  # type differs: VARCHAR(50) in the "database"
        Column("qty", Integer, server_default="2"),  # default differs: 1 in the "database"
        Column("new_col", String(10)),  # metadata-only
    )
    with engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn,
            opts=dict(target_metadata=md, compare_type=True, compare_server_default=True,
                      include_object=include_object),
        )
        diffs = compare_metadata(ctx, md)
    engine.dispose()
    flat = []
    for d in diffs:
        flat.extend(d if isinstance(d, list) else [d])
    # Tuple shapes: (op, schema, table, Column, ...) for add/remove_column, (op, Table) for
    # remove_table, (op, schema, table, column_name, ...) for modify_*.
    names = set()
    for d in flat:
        if d[0] in ("add_column", "remove_column"):
            names.add((d[0], d[3].name))
        elif d[0] == "remove_table":
            names.add((d[0], d[1].name))
        elif d[0].startswith("modify_"):
            names.add((d[0], d[3]))
    return names


def test_autogenerate_simulation_without_filter_would_propose_drops(tmp_path):
    """Negative control: proves the simulation can see the drops the filter must prevent."""
    ops = _autogen_ops(tmp_path, include_object=None)
    assert ("remove_column", "legacy_col") in ops and ("remove_table", "legacy_table") in ops


def test_autogenerate_simulation_with_env_filter(import_fresh, tmp_path):
    import_fresh("memory.models")
    include = _run_env_offline(make_config()).opts["include_object"]
    ops = _autogen_ops(tmp_path, include_object=include)
    assert ("remove_column", "legacy_col") not in ops  # unmanaged column: no DROP COLUMN
    assert ("remove_table", "legacy_table") not in ops  # unmanaged table: no DROP TABLE
    assert ("add_column", "new_col") in ops  # metadata-only column still detected
    assert ("modify_type", "name") in ops  # managed type difference still detected
    assert ("modify_default", "qty") in ops  # managed default difference still detected
    assert not {o for o in ops if o[0].startswith("remove_")}


def test_include_object_column_rules(import_fresh):
    import_fresh("memory.models")
    include = _run_env_offline(make_config()).opts["include_object"]
    assert include(None, "legacy_col", "column", True, None) is False  # DB-only column
    assert include(None, "new_col", "column", False, None) is True  # metadata-only column
    assert include(None, "name", "column", True, object()) is True  # managed column
    assert include(None, "name", "column", False, object()) is True
