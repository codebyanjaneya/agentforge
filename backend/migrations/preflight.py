"""Read-only pre-flight classifier for adopting an existing database (T1-F).

    python -m migrations.preflight        (run from backend/)

Classifies the database named by DATABASE_URL and exits with a code. It performs inspector and
SELECT reads ONLY: no DDL, no DML, no create_all, no upgrade/stamp/downgrade. The exit codes are
classification results; they do not authorize any migration, stamping or adoption.

    0 EMPTY   10 MATCH   11 ALREADY VERSIONED (known revision)   20 DIVERGENT
    21 ALREADY VERSIONED (unknown or malformed version state)   30 UNINSPECTABLE
    2 usage/configuration error

The database URL and raw driver exception text are never printed: failures are reported as an
exception class name only. The schema comparison deliberately does NOT use env.py's object
filter, so unmanaged tables and columns are reported as drift.

What an UNVERSIONED database is compared with: the frozen ``0001_baseline`` schema
(``migrations/baseline_schema.py``), never the live models. A legacy database is adopted by matching
the baseline, stamping ``0001_baseline`` and upgrading to the head, so MATCH means "matches the
baseline", not "matches the current head"; every report names the schema it was compared with. A
database that matches the head schema but has no ``alembic_version`` is DIVERGENT (it is not the
baseline and must not be stamped as such). Versioned databases are classified by revision only.

Verified only on SQLite. PostgreSQL runtime behaviour (reflection, sequences, PK names) is
unverified; the PostgreSQL-only catalog checks below are exercised with mocks only.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from memory.models import Base
from migrations.baseline_schema import BASELINE_REVISION, baseline_metadata

VERSION_TABLE = "alembic_version"
INI = Path(__file__).resolve().parent.parent / "alembic.ini"

EMPTY = "EMPTY"
MATCH = "MATCH"
DIVERGENT = "DIVERGENT"
UNINSPECTABLE = "UNINSPECTABLE"
VERSIONED_CURRENT = "ALREADY_VERSIONED_CURRENT"  # recorded revision is a script head
VERSIONED_BEHIND = "ALREADY_VERSIONED_BEHIND"  # known revision, but not a head
VERSIONED_UNKNOWN = "ALREADY_VERSIONED_UNKNOWN"  # unknown or malformed version state

EXIT_CODES = {
    EMPTY: 0,
    MATCH: 10,
    VERSIONED_CURRENT: 11,
    VERSIONED_BEHIND: 11,
    DIVERGENT: 20,
    VERSIONED_UNKNOWN: 21,
    UNINSPECTABLE: 30,
}
EXIT_USAGE = 2

# Primary keys PostgreSQL's create_all produces: <table>_pkey; serial columns per table.
PG_SERIAL_COLUMNS = {"agent_memories": "id", "telegram_links": "chat_id"}


@dataclass(frozen=True)
class Report:
    state: str
    findings: tuple[str, ...] = field(default_factory=tuple)
    target: str | None = None  # what the schema was compared with, or the recorded revision
    notes: tuple[str, ...] = field(default_factory=tuple)  # context for the operator; never authorization

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.state]

    def render(self) -> str:
        lines = [f"preflight state: {self.state}", f"exit code: {self.exit_code}"]
        if self.target:
            lines.append(f"compared against: {self.target}")
        lines += [f"  - {f}" for f in self.findings]
        lines += [f"note: {n}" for n in self.notes]
        return "\n".join(lines)


def known_revisions() -> tuple[set[str], set[str]]:
    """(heads, all revisions) from the script directory; never imports env.py."""
    script = ScriptDirectory.from_config(Config(str(INI)))
    return set(script.get_heads()), {r.revision for r in script.walk_revisions()}


def _describe(ops) -> list[str]:
    """Names only (tables, columns, indexes, kinds): never values, defaults or URLs."""
    out = []
    for op in ops:
        if isinstance(op, list):
            out += _describe(op)
            continue
        kind = op[0]
        if kind in ("add_table", "remove_table"):
            out.append(f"{kind}: {op[1].name}")
        elif kind in ("add_column", "remove_column"):
            out.append(f"{kind}: {op[2]}.{op[3].name}")
        elif kind in ("add_index", "remove_index"):
            out.append(f"{kind}: {op[1].name} on {op[1].table.name}")
        elif kind.startswith("modify_"):
            out.append(f"{kind}: {op[2]}.{op[3]}")
        else:
            out.append(f"difference: {kind}")
    return out


def _version_report(conn, insp, heads, revisions) -> Report:
    cols = {c["name"] for c in insp.get_columns(VERSION_TABLE)}
    if "version_num" not in cols:
        return Report(VERSIONED_UNKNOWN, ("alembic_version has no version_num column (malformed)",))
    rows = conn.execute(text(f"SELECT version_num FROM {VERSION_TABLE}")).fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
        return Report(VERSIONED_UNKNOWN, (f"alembic_version holds {len(rows)} row(s) and not exactly one valid revision (malformed)",))
    rev = rows[0][0]
    if rev not in revisions:
        return Report(VERSIONED_UNKNOWN, ("recorded revision is not in the script directory",))
    target = f"recorded revision {rev} (revision check only; the schema is not compared)"
    if rev in heads:
        return Report(VERSIONED_CURRENT, (f"recorded revision {rev} is a head",), target)
    return Report(VERSIONED_BEHIND, (f"recorded revision {rev} is known but not a head",), target)


def pg_checks(conn, insp) -> list[str]:
    """PostgreSQL-only read-only catalog checks. Mock-tested only; PostgreSQL runtime unverified."""
    findings = []
    schema = conn.execute(text("SELECT current_schema()")).scalar()
    if schema != "public":
        findings.append("current schema is not public")
    for table, column in PG_SERIAL_COLUMNS.items():
        seq = conn.execute(text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": table, "c": column}).scalar()
        if not seq:
            findings.append(f"{table}.{column} has no owned sequence")
        default = next((c.get("default") for c in insp.get_columns(table) if c["name"] == column), None)
        if not (isinstance(default, str) and default.startswith("nextval(")):
            findings.append(f"{table}.{column} has no nextval default")
        if insp.get_pk_constraint(table).get("name") != f"{table}_pkey":
            findings.append(f"{table} primary key is not named {table}_pkey")
    return findings


def _schema_findings(conn, insp, metadata=None) -> list[str]:
    """Differences between the database and ``metadata`` (default: the frozen 0001_baseline schema)."""
    metadata = baseline_metadata() if metadata is None else metadata
    ctx = MigrationContext.configure(conn, opts=dict(compare_type=True, compare_server_default=True))
    findings = _describe(compare_metadata(ctx, metadata))  # no object filter: extras are drift
    managed = metadata.tables
    for name in managed:
        if not insp.has_table(name):
            continue
        pk = insp.get_pk_constraint(name).get("constrained_columns")
        if pk != [c.name for c in managed[name].primary_key]:
            findings.append(f"primary key differs: {name}")
        if insp.get_check_constraints(name):
            findings.append(f"unexpected check constraint: {name}")
    if conn.dialect.name == "postgresql" and all(insp.has_table(n) for n in managed):
        findings += pg_checks(conn, insp)
    return findings


def _matches_head_schema(conn) -> bool:
    """True if the database equals the CURRENT models' schema (tables, columns, types, indexes)."""
    try:
        ctx = MigrationContext.configure(conn, opts=dict(compare_type=True, compare_server_default=True))
        return not compare_metadata(ctx, Base.metadata)
    except Exception:
        return False


def classify(conn, heads=None, revisions=None) -> Report:
    """Classify an open connection. Any failure is UNINSPECTABLE, never EMPTY."""
    try:
        if heads is None or revisions is None:
            heads, revisions = known_revisions()
        insp = inspect(conn)
        # PostgreSQL only, before ANY table/version reading: in a non-public current schema the
        # inspector looks at the wrong schema, so EMPTY, MATCH and ALREADY_VERSIONED would all be
        # untrustworthy. Read-only SELECT; SQLite never runs it.
        if conn.dialect.name == "postgresql" and conn.execute(text("SELECT current_schema()")).scalar() != "public":
            return Report(DIVERGENT, ("current schema is not public",))
        tables = insp.get_table_names()
        if VERSION_TABLE in tables:
            return _version_report(conn, insp, heads, revisions)
        if not tables:
            return Report(EMPTY, (), "none (the database is empty)",
                          ("an empty database is created by upgrading to the current head; there is nothing to stamp",))
        # Unversioned and non-empty: compare with the frozen BASELINE schema, never the live models.
        target = f"{BASELINE_REVISION} (frozen baseline schema, NOT the current head {', '.join(sorted(heads))})"
        findings = _schema_findings(conn, insp)
        if not findings:
            return Report(MATCH, (), target, (
                f"matches {BASELINE_REVISION} only and does NOT match the current head; this is a classification, "
                "not authorization: stamping the baseline and upgrading to the head are separate explicit operator actions",
            ))
        if set(Base.metadata.tables) - set(baseline_metadata().tables) and _matches_head_schema(conn):
            findings.append(
                f"the database matches the current head schema but has no {VERSION_TABLE}: it is not the "
                f"{BASELINE_REVISION} schema and must not be stamped as the baseline (stop for owner review)"
            )
        return Report(DIVERGENT, tuple(findings), target)
    except Exception as exc:
        return Report(UNINSPECTABLE, (f"inspection failed ({type(exc).__name__})",))


def main() -> int:
    from config import ConfigError, get_database_url  # lazy; needs only APP_ENV and DATABASE_URL

    try:
        url = get_database_url()
    except ConfigError as exc:  # names variables only, never values
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    engine = None
    try:
        engine = create_engine(url.get_secret_value(), poolclass=NullPool)
        with engine.connect() as raw_conn:  # entered first, so it is closed even if the next line raises
            conn = raw_conn.execution_options(postgresql_readonly=True)
            report = classify(conn)
    except Exception as exc:  # driver text can contain host/user: class name only
        report = Report(UNINSPECTABLE, (f"could not connect ({type(exc).__name__})",))
    finally:
        if engine is not None:
            engine.dispose()
    print(report.render())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
