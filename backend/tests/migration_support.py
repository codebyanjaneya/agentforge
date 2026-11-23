"""Shared helpers for migration tests on throw-away SQLite files (used by the M1-B tests).

A ``Db`` is a SQLite file in ``tmp_path`` migrated through the real ``env.py`` online path, with the
URL supplied only through ``monkeypatch`` (so it exists for the test alone). SQLite is NOT
PostgreSQL: it proves structure, constraint behaviour and reversibility, not PostgreSQL runtime.
"""

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, event

BACKEND = Path(__file__).resolve().parent.parent
INI = BACKEND / "alembic.ini"


OWNERSHIP_COLUMNS = ("user_id", "workspace_id")
OWNERSHIP_INDEX = "ix_agent_memories_user_id_created_at_id"


def is_ownership_object(obj, name, type_) -> bool:
    """True for the objects migration 0004_memory_ownership adds to agent_memories."""
    if type_ == "column":
        return name in OWNERSHIP_COLUMNS and getattr(getattr(obj, "table", None), "name", None) == "agent_memories"
    if type_ == "index":
        return name == OWNERSHIP_INDEX
    if type_ == "foreign_key_constraint":  # reflected inline FKs on SQLite have no name: match on the column
        return (getattr(getattr(obj, "table", None), "name", None) == "agent_memories"
                and set(obj.column_keys) <= set(OWNERSHIP_COLUMNS))
    return False


AGENT_PLATFORM_TABLES = ("custom_agents", "custom_agent_versions", "agent_executions", "plans", "plan_revisions", "plan_tasks",
                         "plan_task_dependencies", "plan_runs", "plan_task_runs", "run_leases", "execution_events", "plan_run_retries", "plan_task_carryovers", "worker_processes", "execution_owners", "tool_invocations", "tool_approvals", "tool_suspensions", "model_usage", "notifications", "provider_events", "tool_dispatches", "tool_reconciliations", "workspace_invitations")  # added by 0005 (M2-A), 0006 (M2-B), 0007 (M3-A), 0008 (M3-B), 0009 (M4-A1) and 0010 (M4-A2) and 0011 (M4-B)


def is_agent_platform_object(obj, name, type_) -> bool:
    """True for the tables (and their indexes) that migrations 0005 to 0009 add."""
    if type_ == "table":
        return name in AGENT_PLATFORM_TABLES
    if type_ == "index":
        return getattr(getattr(obj, "table", None), "name", None) in AGENT_PLATFORM_TABLES
    return False


def make_cfg(output=None) -> Config:
    cfg = Config(str(INI), output_buffer=output)
    cfg.attributes["configure_logger"] = False
    return cfg


class Db:
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
        """Every schema object exactly as SQLite stores it (type, name, owning table, DDL text)."""
        return self.rows("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")

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

    def diffs(self, metadata, exclude_tables=(), exclude_ownership=False):
        """``compare_metadata`` differences (type and server-default comparison on); Alembic's own
        version table and any ``exclude_tables`` are left out of both sides. ``exclude_ownership`` also
        leaves out exactly the M1-C objects on agent_memories (user_id, workspace_id, their foreign keys
        and the history index) and the M2-A tables (0005), to compare a database from before 0004 with the
        live metadata."""
        engine = self.engine()
        try:
            with engine.connect() as conn:
                skip = {"alembic_version", *exclude_tables}
                try:
                    before_0018 = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() != "0018_agent_slug_per_user"
                except Exception:  # noqa: BLE001 - no version table yet
                    before_0018 = False

                def include(obj, name, type_, reflected, compare_to):
                    if type_ == "table" and name in skip:
                        return False
                    if type_ == "index" and getattr(getattr(obj, "table", None), "name", None) in skip:
                        return False  # the indexes of a skipped (not yet created) table
                    if type_ == "column" and name == "required_tool" and "provider_events" in skip:
                        return False  # 0015 adds plan_tasks.required_tool: a database from before it (provider_events excluded) does not have it yet
                    if before_0018 and type_ == "unique_constraint" and getattr(getattr(obj, "table", None), "name", None) == "custom_agents":
                        return False  # 0018 changed this constraint (workspace, slug) -> (workspace, user, slug): a database from before it legitimately differs from the live models
                    if exclude_ownership and (is_ownership_object(obj, name, type_) or is_agent_platform_object(obj, name, type_)):
                        return False
                    return True

                ctx = MigrationContext.configure(conn, opts=dict(
                    compare_type=True, compare_server_default=True, include_object=include))
                return compare_metadata(ctx, metadata)
        finally:
            engine.dispose()
