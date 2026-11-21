"""Model metadata separation (T1-C): completeness, isolation, source guard, DDL preservation.

No database or network is used: DDL is only compiled, never executed.
"""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from baseline_support import BASELINE_TABLES, baseline_metadata

BACKEND = Path(__file__).resolve().parent.parent
PLAN = BACKEND.parent / "docs" / "database" / "T1_ALEMBIC_BASELINE_PLAN.md"
IDENTITY_TABLES = {"users", "auth_identities"}  # added by migration 0002_identity (M1-A)
WORKSPACE_TABLES = {"workspaces", "workspace_memberships"}  # added by migration 0003_workspaces (M1-B)
# The live metadata is the baseline tables plus every table added by later migrations. The baseline
# tables themselves stay frozen (see baseline_support.py and the DDL tests below).
AGENT_TABLES = {"custom_agents", "custom_agent_versions", "agent_executions", "plans", "plan_revisions", "plan_tasks", "plan_task_dependencies", "plan_runs", "plan_task_runs", "run_leases", "execution_events", "plan_run_retries", "plan_task_carryovers", "worker_processes", "execution_owners", "tool_invocations", "tool_approvals", "tool_suspensions", "model_usage", "notifications", "provider_events", "tool_dispatches", "tool_reconciliations", "workspace_invitations"}  # 0005 (M2-A), 0006 (M2-B), 0007 (M3-A), 0008 (M3-B)
EXPECTED_TABLES = set(BASELINE_TABLES) | IDENTITY_TABLES | WORKSPACE_TABLES | AGENT_TABLES


# ---------------------------------------------------------------- completeness


def test_metadata_contains_exactly_the_expected_tables_and_no_naming_convention(import_fresh):
    long_term = import_fresh("memory.long_term")
    models = sys.modules["memory.models"]
    assert set(models.Base.metadata.tables) == EXPECTED_TABLES
    # T1-C must not introduce a naming convention (SQLAlchemy's default is index-only).
    assert dict(models.Base.metadata.naming_convention) == {"ix": "ix_%(column_0_label)s"}
    assert long_term.Base is models.Base


def test_long_term_reexports_the_same_objects(import_fresh):
    long_term = import_fresh("memory.long_term")
    models = sys.modules["memory.models"]
    assert long_term.AgentMemory is models.AgentMemory
    assert long_term.TelegramLink is models.TelegramLink
    # Behaviour that depends on the re-exports is unchanged.
    assert long_term.AgentMemory.__table__ is models.Base.metadata.tables["agent_memories"]
    assert long_term.TelegramLink.__table__ is models.Base.metadata.tables["telegram_links"]


def test_every_mapped_class_is_registered_in_the_same_metadata(import_fresh):
    import_fresh("memory.long_term")
    models = sys.modules["memory.models"]
    mapped = {m.class_ for m in models.Base.registry.mappers}
    assert mapped == {models.AgentMemory, models.TelegramLink, models.User, models.AuthIdentity,
                      models.Workspace, models.WorkspaceMembership, models.CustomAgent, models.CustomAgentVersion,
                      models.AgentExecution, models.Plan, models.PlanRevision, models.PlanTask, models.PlanTaskDependency, models.PlanRun, models.PlanTaskRun, models.RunLease, models.ExecutionEvent, models.PlanRunRetry, models.PlanTaskCarryover, models.WorkerProcess, models.ExecutionOwner, models.ToolInvocation, models.ToolApproval, models.ToolSuspension, models.ModelUsage, models.Notification, models.ProviderEvent, models.ToolDispatch, models.ToolReconciliation, models.WorkspaceInvitation}
    assert all(cls.metadata is models.Base.metadata for cls in mapped)
    assert {cls.__table__.name for cls in mapped} == set(models.Base.metadata.tables)


# ------------------------------------------------------------- import isolation


def test_models_import_needs_no_credentials_database_or_settings():
    """In a clean interpreter with no app variables: no settings, engine, DB module or sockets."""
    code = (
        "import socket, sys\n"
        "def blocked(*a, **k): raise RuntimeError('network attempted')\n"
        "socket.socket.connect = blocked; socket.socket.connect_ex = blocked\n"
        "import memory.models as m\n"
        "assert set(m.Base.metadata.tables) == {'agent_memories', 'telegram_links', 'users', 'auth_identities', 'workspaces', 'workspace_memberships', 'custom_agents', 'custom_agent_versions', 'agent_executions', 'plans', 'plan_revisions', 'plan_tasks', 'plan_task_dependencies', 'plan_runs', 'plan_task_runs', 'run_leases', 'execution_events', 'plan_run_retries', 'plan_task_carryovers', 'worker_processes', 'execution_owners', 'tool_invocations', 'tool_approvals', 'tool_suspensions', 'model_usage', 'notifications', 'provider_events', 'tool_dispatches', 'tool_reconciliations', 'workspace_invitations'}\n"
        "import os\n"
        "assert 'GROQ_API_KEY' not in os.environ and 'DATABASE_URL' not in os.environ\n"
        "loaded = [n for n in ('config', 'memory.long_term', 'main', 'redis', 'groq', 'telegram') if n in sys.modules]\n"
        "print('UNEXPECTED:', loaded) if loaded else print('OK')\n"
        "sys.exit(1 if loaded else 0)\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("GROQ_API_KEY", "DATABASE_URL", "REDIS_URL")}
    env.update(PYTHONPATH=str(BACKEND), PYTHONDONTWRITEBYTECODE="1", PYTHON_DOTENV_DISABLED="1")
    # Inherits the normal environment (a stripped one makes Windows Store Python create stray dirs).
    r = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr


def test_models_module_has_no_environment_or_engine_access():
    tree = ast.parse((BACKEND / "memory" / "models.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert imported <= {"datetime", "sqlalchemy", "sqlalchemy.orm", "uuid"}, imported  # uuid: pure stdlib, no env/engine access


# ----------------------------------------------------------------- source guard

_BASE_NAMES = {"Base", "DeclarativeBase", "MappedAsDataclass"}


def declarative_violations(source: str) -> list[str]:
    """Return reasons why ``source`` defines declarative models/bases."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
                if name in _BASE_NAMES:
                    found.append(f"class {node.name}({name})")
            for stmt in node.body:
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [getattr(stmt, "target", None)]
                if any(isinstance(t, ast.Name) and t.id in ("__tablename__", "__table__") for t in targets):
                    found.append(f"class {node.name} declares a table")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in ("declarative_base", "registry"):
                found.append(f"call to {name}()")
    return found


def test_guard_detects_violations_and_ignores_clean_code():
    assert declarative_violations("class A(Base):\n    pass\n")
    assert declarative_violations("class A(DeclarativeBase):\n    pass\n")
    assert declarative_violations("import sqlalchemy.orm as o\nclass A(o.DeclarativeBase):\n    pass\n")
    assert declarative_violations("class A(Other):\n    __tablename__ = 'x'\n")
    assert declarative_violations("Base = declarative_base()\n")
    assert not declarative_violations("class A:\n    pass\nBase.metadata.create_all(engine)\n")
    assert not declarative_violations("from memory.models import Base\n")


def test_declarative_models_exist_only_in_memory_models():
    offenders = {}
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND)
        if rel.parts[0] in ("venv", "tests", "migrations", "__pycache__") or rel.as_posix() == "memory/models.py":
            continue
        found = declarative_violations(path.read_text(encoding="utf-8"))
        if found:
            offenders[rel.as_posix()] = found
    assert offenders == {}, "declarative models must live in memory/models.py"
    # The guard is not vacuous: it does flag the real models module.
    assert len(declarative_violations((BACKEND / "memory" / "models.py").read_text(encoding="utf-8"))) >= 4


# ------------------------------------------------------------ DDL preservation


def _compiled_statements(metadata, only=None) -> set[str]:
    """Compiled PostgreSQL DDL; ``only`` restricts it to the named tables (the frozen baseline)."""
    dialect = postgresql.dialect()
    statements = set()
    for table in metadata.tables.values():
        if only is not None and table.name not in only:
            continue
        statements.add(str(CreateTable(table).compile(dialect=dialect)))
        statements.update(str(CreateIndex(ix).compile(dialect=dialect)) for ix in table.indexes)
    return {re.sub(r"\s+", "", s) for s in statements}


def _plan_statements() -> set[str]:
    """Statements from the first ```sql block of the T1-A plan (section 3, verified schema)."""
    block = re.search(r"```sql\n(.*?)```", PLAN.read_text(encoding="utf-8"), re.S).group(1)
    return {re.sub(r"\s+", "", s) for s in block.split(";") if s.strip()}


def test_postgresql_ddl_matches_t1a_plan_schema(import_fresh):
    import_fresh("memory.models")
    models = sys.modules["memory.models"]
    # The BASELINE tables are exactly the T1-A plan schema (unchanged by later migrations) ...
    compiled = _compiled_statements(models.Base.metadata, BASELINE_TABLES)
    plan = _plan_statements()
    # M1-C changes exactly ONE statement of the baseline tables: agent_memories gains two nullable ownership
    # columns, their named foreign keys and the history index. The expected DDL is derived from the frozen
    # plan statement, so that "purely additive" is itself asserted (every other baseline statement is equal).
    old_table = [s for s in plan if s.startswith("CREATETABLEagent_memories(")]
    assert len(old_table) == 1
    tail = "created_atTIMESTAMPWITHOUTTIMEZONENOTNULL,PRIMARYKEY(id))"
    assert old_table[0].endswith(tail)
    new_table = old_table[0][: -len(tail)] + (
        "created_atTIMESTAMPWITHOUTTIMEZONENOTNULL,user_idUUID,workspace_idUUID,PRIMARYKEY(id),"
        "CONSTRAINTfk_agent_memories_user_id_usersFOREIGNKEY(user_id)REFERENCESusers(id),"
        "CONSTRAINTfk_agent_memories_workspace_id_workspacesFOREIGNKEY(workspace_id)REFERENCESworkspaces(id))")
    history_index = "".join(
        "CREATE INDEX ix_agent_memories_user_id_created_at_id ON agent_memories (user_id, created_at DESC, id DESC)".split())
    assert compiled == (plan - set(old_table)) | {new_table, history_index}
    # ... and so is the frozen copy that the other T1 tests use as "baseline metadata".
    assert _compiled_statements(baseline_metadata()) == _plan_statements()
    # SERIAL / BIGSERIAL behaviour is part of that DDL.
    joined = "".join(compiled)
    assert "idSERIALNOTNULL" in joined and "chat_idBIGSERIALNOTNULL" in joined


def test_column_attributes_are_preserved(import_fresh):
    import_fresh("memory.models")
    models = sys.modules["memory.models"]
    dialect = postgresql.dialect()
    got = {
        f"{t.name}.{c.name}": (c.type.compile(dialect), c.nullable, c.primary_key, c.server_default is None, c.index)
        for t in models.Base.metadata.tables.values()
        if t.name in BASELINE_TABLES  # the baseline tables' column attributes stay frozen
        for c in t.columns
        if not (t.name == "agent_memories" and c.name in ("user_id", "workspace_id"))  # added by M1-C, tested there
    }
    assert got == {
        "agent_memories.id": ("INTEGER", False, True, True, None),
        "agent_memories.agent_name": ("VARCHAR(100)", False, False, True, True),
        "agent_memories.user_email": ("VARCHAR(255)", True, False, True, True),
        "agent_memories.summary": ("TEXT", False, False, True, None),
        "agent_memories.created_at": ("TIMESTAMP WITHOUT TIME ZONE", False, False, True, None),
        "telegram_links.chat_id": ("BIGINT", False, True, True, None),
        "telegram_links.email": ("VARCHAR(255)", True, False, True, None),
        "telegram_links.active_agent": ("VARCHAR(100)", False, False, True, None),
        "telegram_links.updated_at": ("TIMESTAMP WITHOUT TIME ZONE", False, False, True, None),
    }
    # Python-side defaults are unchanged (timestamps and the telegram default agent).
    am, tl = models.AgentMemory.__table__, models.TelegramLink.__table__
    assert am.c.created_at.default.arg.__qualname__ == "datetime.utcnow"
    assert tl.c.active_agent.default.arg == "ceo"
    assert tl.c.updated_at.default.arg.__qualname__ == "datetime.utcnow"
    assert tl.c.updated_at.onupdate.arg.__qualname__ == "datetime.utcnow"
    # Constraints: telegram_links still has only its primary key. agent_memories has its primary key plus
    # EXACTLY the two named, nullable ownership foreign keys added by M1-C (nothing else).
    assert {type(c).__name__ for c in tl.constraints} == {"PrimaryKeyConstraint"}
    assert sorted((type(c).__name__, c.name) for c in am.constraints) == [
        ("ForeignKeyConstraint", "fk_agent_memories_user_id_users"),
        ("ForeignKeyConstraint", "fk_agent_memories_workspace_id_workspaces"),
        ("PrimaryKeyConstraint", None),
    ]
    assert am.c.user_id.nullable and am.c.workspace_id.nullable
    assert am.c.user_id.server_default is None and am.c.workspace_id.server_default is None
    assert am.c.user_id.default is None and am.c.workspace_id.default is None  # never assigned by the model
