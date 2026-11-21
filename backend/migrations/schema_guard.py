"""Read-only startup schema-version guard (T1-H).

Replaces the transitional ``init_db()`` / ``create_all()``. Startup is accepted ONLY when

* the script directory has exactly one head,
* the database records exactly one non-empty revision, known to the script directory, and
* that revision equals the head.

The guard never migrates, stamps, creates or writes anything: it opens its own short-lived
connection, reads (``SELECT``/inspector only) and closes it. Migrations are a separate, explicit
deployment step (see docs/database/T1_G_ADOPTION_RUNBOOK.md). Behaviour is identical in every
APP_ENV (there is deliberately no environment parameter): any mismatch or uncertainty fails closed.

Errors carry a fixed code and message only. The URL, host, user and raw driver text never reach
the exception text; the underlying exception class name is kept in ``error_class`` for the log.

It checks the recorded REVISION, not schema parity (a stamped but drifted database passes).
Verified on SQLite and with mocks only; PostgreSQL runtime behaviour is unverified.
"""

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

INI = Path(__file__).resolve().parent.parent / "alembic.ini"
CONNECT_TIMEOUT_SECONDS = 10  # PostgreSQL only; a dead host must not hang startup

MESSAGES = {
    "SCRIPT_HEADS_INVALID": "The migration scripts are unavailable or do not have exactly one head.",
    "DB_UNREACHABLE": "The database could not be reached.",
    "SCHEMA_CHECK_FAILED": "The database schema could not be checked.",
    "NON_PUBLIC_SCHEMA": "The database current schema is not 'public'.",
    "SCHEMA_VERSION_UNREADABLE": "The recorded schema version could not be read or is invalid.",
    "NO_SCHEMA_VERSION": (
        "The database has no recorded schema version. Run the migrations as a separate deployment step "
        "(alembic upgrade head, or adopt an existing database per the adoption runbook) before starting."
    ),
    "MULTIPLE_VERSION_ROWS": "The database records more than one schema version.",
    "UNKNOWN_REVISION": "The recorded schema version is not known to the migration scripts.",
    "SCHEMA_BEHIND_HEAD": (
        "The database schema is not at the expected version. Run the migrations as a separate "
        "deployment step (alembic upgrade head) before starting."
    ),
}


class SchemaVersionError(RuntimeError):
    """Startup must not proceed. ``str()`` is safe to log and show: code + fixed message only."""

    def __init__(self, code: str, error_class: str | None = None):
        super().__init__(f"{code}: {MESSAGES[code]}")
        self.code = code
        self.error_class = error_class  # exception class name of the underlying failure, if any


def _fail(code: str, exc: BaseException | None = None):
    # Always raised outside the `except` block's context chain (from None): no driver text is printed.
    raise SchemaVersionError(code, type(exc).__name__ if exc is not None else None) from None


def _script_head(ini_path) -> tuple[str, set[str]]:
    try:
        script = ScriptDirectory.from_config(Config(str(ini_path)))
        heads = script.get_heads()
        revisions = {r.revision for r in script.walk_revisions()}
    except Exception as exc:
        _fail("SCRIPT_HEADS_INVALID", exc)
    if len(heads) != 1:
        _fail("SCRIPT_HEADS_INVALID")
    return heads[0], revisions


def verify_schema_version(database_url, *, ini_path=INI, engine_factory=create_engine) -> str:
    """Return the verified head revision, or raise SchemaVersionError. Writes nothing."""
    head, revisions = _script_head(ini_path)  # no database needed; fail before connecting

    url = database_url.get_secret_value() if hasattr(database_url, "get_secret_value") else str(database_url)
    engine = None
    try:
        kwargs = {"poolclass": NullPool}
        if make_url(url).get_backend_name() == "postgresql":
            kwargs["connect_args"] = {"connect_timeout": CONNECT_TIMEOUT_SECONDS}
        engine = engine_factory(url, **kwargs)
        raw_conn = engine.connect()
    except Exception as exc:
        if engine is not None:
            engine.dispose()
        _fail("DB_UNREACHABLE", exc)

    try:
        with raw_conn:  # entered first so it is always closed
            try:
                conn = raw_conn.execution_options(postgresql_readonly=True)
                schema_ok = True
                if conn.dialect.name == "postgresql":
                    # Before anything else is read: in a non-public schema the version table (or
                    # its absence) would be looked up in the wrong place.
                    schema_ok = conn.execute(text("SELECT current_schema()")).scalar() == "public"
            except Exception as exc:
                _fail("SCHEMA_CHECK_FAILED", exc)
            if not schema_ok:
                _fail("NON_PUBLIC_SCHEMA")
            try:
                recorded = MigrationContext.configure(conn).get_current_heads()
            except Exception as exc:
                _fail("SCHEMA_VERSION_UNREADABLE", exc)
    finally:
        engine.dispose()

    if len(recorded) == 0:
        _fail("NO_SCHEMA_VERSION")
    if len(recorded) > 1:
        _fail("MULTIPLE_VERSION_ROWS")
    revision = recorded[0]
    if not isinstance(revision, str) or not revision:
        _fail("SCHEMA_VERSION_UNREADABLE")
    if revision not in revisions:
        _fail("UNKNOWN_REVISION")
    if revision != head:
        _fail("SCHEMA_BEHIND_HEAD")
    return head
