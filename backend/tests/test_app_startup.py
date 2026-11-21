"""Real ``main:app`` startup/shutdown with isolated SQLite, plus isolation self-checks."""

import logging
import os
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from config import ConfigError

SENTINEL = "SENTINEL-must-never-appear"


def test_startup_on_an_unmigrated_database_fails_closed_and_creates_nothing(import_main, monkeypatch, caplog):
    main = import_main(TELEGRAM_BOT_TOKEN="dummy-token")  # bot enabled, so we can prove it never launches
    monkeypatch.setattr(main, "start_telegram_bot", lambda: pytest.fail("Telegram bot must not start"))
    from memory import long_term
    from migrations.schema_guard import SchemaVersionError

    assert not hasattr(long_term, "init_db")  # the transitional create_all path is gone
    # Import alone must not touch the database.
    assert inspect(long_term.engine).get_table_names() == []
    with caplog.at_level(logging.ERROR):
        with pytest.raises(SchemaVersionError) as exc:
            with TestClient(main.app):
                pass
    assert exc.value.code == "NO_SCHEMA_VERSION"
    assert "Startup aborted" in caplog.text and SENTINEL not in caplog.text
    # Startup created nothing (no application tables, no alembic_version).
    assert inspect(long_term.engine).get_table_names() == []


def test_startup_on_a_migrated_database_serves_health(import_main, migrate_test_db, monkeypatch):
    main = import_main()
    monkeypatch.setattr(main, "start_telegram_bot", lambda: pytest.fail("Telegram bot must not start"))
    from memory import long_term

    migrate_test_db()  # the real `alembic upgrade head` on the temporary SQLite file
    tables = set(inspect(long_term.engine).get_table_names())
    assert tables == {"agent_memories", "telegram_links", "users", "auth_identities", "workspaces",
                      "workspace_memberships", "custom_agents", "custom_agent_versions", "agent_executions", "plans", "plan_revisions",
                      "plan_tasks", "plan_task_dependencies", "plan_runs", "plan_task_runs", "run_leases", "execution_events", "plan_run_retries", "plan_task_carryovers", "worker_processes", "execution_owners", "tool_invocations", "tool_approvals", "tool_suspensions", "model_usage", "notifications", "provider_events", "tool_dispatches", "tool_reconciliations", "workspace_invitations", "alembic_version"}
    with TestClient(main.app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").json() == {"status": "ready"}
    assert set(inspect(long_term.engine).get_table_names()) == tables  # startup changed nothing
    # Existing behaviour through the real module graph still works against SQLite.
    from authz.errors import ScopeError  # M1-E: the email-keyed memory API is gone and now fails closed

    with pytest.raises(ScopeError):
        long_term.LongTermMemory().save("a@example.com", "ceo", "hello")
    with pytest.raises(ScopeError):
        long_term.LongTermMemory().get_history("a@example.com")
    long_term.set_email(42, "a@example.com")
    assert long_term.get_link(42)["email"] == "a@example.com"


def test_startup_refuses_a_database_at_an_unknown_revision(import_main, migrate_test_db):
    main = import_main()
    migrate_test_db()
    from memory import long_term
    from migrations.schema_guard import SchemaVersionError

    with long_term.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE alembic_version SET version_num = 'ffff'")
    with pytest.raises(SchemaVersionError) as exc:
        with TestClient(main.app):
            pass
    assert exc.value.code == "UNKNOWN_REVISION"


def test_startup_sources_contain_no_create_all_or_init_db():
    backend = Path(__file__).resolve().parent.parent
    for rel in ("main.py", "memory/long_term.py"):
        text = (backend / rel).read_text(encoding="utf-8")
        assert "create_all" not in text and "init_db" not in text, rel


def test_test_environment_has_no_telegram_token_and_dotenv_disabled(import_main):
    assert "TELEGRAM_BOT_TOKEN" not in os.environ
    assert os.environ["PYTHON_DOTENV_DISABLED"] == "1"
    assert import_main().settings.telegram_enabled is False


def test_dotenv_files_are_not_loaded(tmp_path, monkeypatch):
    from dotenv import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("DOTENV_PROBE=loaded\n")
    monkeypatch.delenv("DOTENV_PROBE", raising=False)
    load_dotenv(env_file)
    assert "DOTENV_PROBE" not in os.environ


def test_non_production_policy_on_real_app(import_main, migrate_test_db):
    # SQLite is only valid with APP_ENV=test, which shares development's docs/CORS defaults.
    main = import_main(APP_ENV="test")
    migrate_test_db()
    with TestClient(main.app) as c:
        assert c.get("/docs").status_code == 200
        r = c.options("/health", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"})
        assert r.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_production_config_rejected_before_serving(import_main):
    with pytest.raises(ConfigError) as exc:
        import_main(APP_ENV="production", API_DOCS_ENABLED="true", DATABASE_URL="postgresql://u:p@h/db")
    assert "API_DOCS_ENABLED" in str(exc.value)


def test_production_real_app_hides_docs(import_main):
    # The Postgres URL is only parsed (lazy engine); lifespan is not run, so nothing connects.
    main = import_main(APP_ENV="production", DATABASE_URL="postgresql://u:p@db.invalid/db")
    c = TestClient(main.app)
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404


def test_error_bodies_and_logs_do_not_contain_secrets(import_main, migrate_test_db, caplog, capsys):
    main = import_main(GROQ_API_KEY=f"{SENTINEL}-groq", TWILIO_AUTH_TOKEN=f"{SENTINEL}-twilio")
    migrate_test_db()

    def boom():
        raise RuntimeError("internal failure")

    main.app.add_api_route("/boom", boom, methods=["GET"])
    with caplog.at_level(logging.DEBUG):
        with TestClient(main.app, raise_server_exceptions=False) as c:
            bodies = [c.get("/missing").text, c.get("/boom").text, c.get("/health").text]
    out = capsys.readouterr()
    assert SENTINEL not in "\n".join(bodies + [caplog.text, out.out, out.err])
    assert any("Configuration loaded" in r.getMessage() for r in caplog.records)


def test_network_guard_blocks_outbound_but_not_in_process_asgi():
    with pytest.raises(Exception, match="blocked"):
        socket.create_connection(("example.com", 80), timeout=1)
    for port in (5432, 6379):  # a developer's local Postgres/Redis must be unreachable too
        s = socket.socket()
        with pytest.raises(Exception, match="blocked"):
            s.connect(("127.0.0.1", port))
        s.close()
    from fastapi import FastAPI

    app = FastAPI()
    app.add_api_route("/ok", lambda: {"ok": True}, methods=["GET"])
    with TestClient(app) as c:  # creates an event loop and portal inside the guard
        assert c.get("/ok").json() == {"ok": True}


class _Engine:
    """Lets the shared SQL builders run against the app's own temporary SQLite database."""

    def __init__(self, engine):
        self.engine = engine

    def run(self, *statements):
        with self.engine.begin() as conn:
            for statement in statements:
                conn.exec_driver_sql(statement)

    def rows(self, sql):
        with self.engine.begin() as conn:
            return [tuple(r) for r in conn.exec_driver_sql(sql).fetchall()]


def test_startup_recovery_closes_runs_without_a_live_owner_and_is_idempotent(import_main, migrate_test_db, monkeypatch, caplog):
    """The REAL lifespan (M4-A1 replaced the unconditional sweep): an orphaned run WITHOUT a lease (it predates leases or its owner is
    gone; a task running, one pending) is closed as interrupted, a finished run and the completed work of the orphaned run stay byte
    for byte unchanged, and a second startup changes nothing."""
    from tests.test_agent_executions_schema import execution_sql
    from tests.test_plan_runs_schema import run_sql, trun_sql
    from tests.test_plans_schema import STAMP, U, W, dep_sql, plan_sql, revision_sql, seed, task_sql

    main = import_main()
    monkeypatch.setattr(main, "start_telegram_bot", lambda: pytest.fail("Telegram bot must not start"))
    from memory import long_term

    migrate_test_db()
    db = _Engine(long_term.engine)
    seed(db)
    plan, pid = plan_sql(status="approved", approved_revision=1, approved_at="2026-01-01 00:00:00.000000")
    rev, rid = revision_sql(pid)
    db.run(plan, rev, task_sql(rid, "t1", 0), task_sql(rid, "t2", 1), task_sql(rid, "t3", 2), dep_sql(rid, "t2", "t1"), dep_sql(rid, "t3", "t2"))

    def execution(**kw):
        sql, eid = execution_sql(user_id=U, workspace_id=W, **kw)
        db.run(sql)
        return eid

    # a finished run (history): partial, with a failed and a skipped task
    finished_sql, finished = run_sql(pid, rid, 1, "partial")
    done_ok, done_bad = execution(status="succeeded", result="kept"), execution(status="failed", error_code="timeout")
    db.run(finished_sql, trun_sql(finished, rid, "t1", 0, status="succeeded", execution_id=done_ok)[0],
           trun_sql(finished, rid, "t2", 1, status="failed", execution_id=done_bad, error_code="timeout", error_message="The agent took too long to answer.")[0],
           trun_sql(finished, rid, "t3", 2, status="skipped", error_code="dependency_failed", error_message="skipped")[0])
    # the orphaned run: t1 succeeded, t2 in flight when the process died, t3 never started
    orphan_sql, orphan = run_sql(pid, rid, 2, "running")
    ok_exec, live_exec = execution(status="succeeded", result="also kept"), execution(status="running")
    db.run(orphan_sql, trun_sql(orphan, rid, "t1", 0, status="succeeded", execution_id=ok_exec)[0],
           trun_sql(orphan, rid, "t2", 1, status="running", execution_id=live_exec)[0], trun_sql(orphan, rid, "t3", 2)[0])

    def state():
        return {t: db.rows(f"SELECT * FROM {t} ORDER BY 1") for t in ("plan_runs", "plan_task_runs", "agent_executions", "plans", "plan_revisions", "plan_tasks")}

    before = state()
    with caplog.at_level(logging.WARNING):
        with TestClient(main.app) as client:  # the real lifespan runs the sweeps
            assert client.get("/health").json() == {"status": "ok"}
    assert "Recovered 1 plan run(s) without a live owner" in caplog.text and "execution(s) left running" not in caplog.text  # no standalone execution existed
    run = db.rows(f"SELECT status, error_code, error_message, completed_at IS NOT NULL FROM plan_runs WHERE id = '{orphan}'")
    assert run == [("partial", "interrupted", "The server restarted before this task finished.", 1)]
    assert db.rows(f"SELECT task_key, status, error_code, execution_id IS NOT NULL FROM plan_task_runs WHERE run_id = '{orphan}' ORDER BY position") == [
        ("t1", "succeeded", None, 1), ("t2", "failed", "interrupted", 1), ("t3", "skipped", "interrupted", 0)]
    assert db.rows(f"SELECT status, error_code FROM agent_executions WHERE id = '{live_exec}'") == [("failed", "interrupted")]  # closed together with its run
    after = state()
    for table in ("plans", "plan_revisions", "plan_tasks"):
        assert after[table] == before[table]
    assert [r for r in after["plan_runs"] if r[0] == finished] == [r for r in before["plan_runs"] if r[0] == finished]  # the finished run is untouched
    assert [r for r in after["plan_task_runs"] if r[1] == finished] == [r for r in before["plan_task_runs"] if r[1] == finished]
    for eid in (done_ok, done_bad, ok_exec):
        assert [r for r in after["agent_executions"] if r[0] == eid] == [r for r in before["agent_executions"] if r[0] == eid]  # completed work is unchanged
    assert [r for r in after["plan_task_runs"] if r[2] == "t1" and r[1] == orphan] == [r for r in before["plan_task_runs"] if r[2] == "t1" and r[1] == orphan]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        with TestClient(main.app) as client:  # a second startup is safe
            assert client.get("/health").status_code == 200
    assert "Recovered" not in caplog.text and state()["plan_runs"] == after["plan_runs"] and state()["plan_task_runs"] == after["plan_task_runs"] and state()["agent_executions"] == after["agent_executions"]  # nothing is re-processed or corrupted


def test_a_second_process_starting_up_leaves_a_live_owners_run_alone_and_recovers_it_once_the_lease_expires(import_main, migrate_test_db, monkeypatch, caplog):
    """The REAL lifespan of a SECOND process: a run whose owner still renews its lease (and the execution of its running task) is not
    touched at startup; after the lease has expired the next startup closes it."""
    from tests.test_agent_executions_schema import execution_sql
    from tests.test_plan_runs_schema import run_sql, trun_sql
    from tests.test_plans_schema import STAMP, U, W, dep_sql, plan_sql, revision_sql, seed, task_sql

    main = import_main()
    monkeypatch.setattr(main, "start_telegram_bot", lambda: pytest.fail("Telegram bot must not start"))
    from memory import long_term

    migrate_test_db()
    db = _Engine(long_term.engine)
    seed(db)
    plan, pid = plan_sql(status="approved", approved_revision=1, approved_at="2026-01-01 00:00:00.000000")
    rev, rid = revision_sql(pid)
    db.run(plan, rev, task_sql(rid, "t1", 0), task_sql(rid, "t2", 1), dep_sql(rid, "t2", "t1"))
    exec_sql, live_exec = execution_sql(user_id=U, workspace_id=W, status="running")
    run_statement, run_id = run_sql(pid, rid, 1, "running")
    db.run(exec_sql, run_statement, trun_sql(run_id, rid, "t1", 0, status="running", execution_id=live_exec)[0], trun_sql(run_id, rid, "t2", 1)[0],
           f"INSERT INTO run_leases (run_id, owner_id, generation, expires_at, created_at, updated_at) VALUES ('{run_id}', 'another-process', 1, '2999-01-01 00:00:00.000000', {STAMP}, {STAMP})")

    def state():
        return {t: db.rows(f"SELECT * FROM {t} ORDER BY 1") for t in ("plan_runs", "plan_task_runs", "agent_executions", "run_leases")}

    before = state()
    with caplog.at_level(logging.WARNING):
        with TestClient(main.app) as client:
            assert client.get("/health").json() == {"status": "ok"}
    assert state() == before and "Recovered" not in caplog.text and "left running" not in caplog.text  # a live owner's run is untouched
    db.run(f"UPDATE run_leases SET expires_at = '2000-01-01 00:00:00.000000' WHERE run_id = '{run_id}'")  # its owner stopped renewing
    with caplog.at_level(logging.WARNING):
        with TestClient(main.app) as client:
            assert client.get("/health").status_code == 200
    assert "Recovered 1 plan run(s) without a live owner" in caplog.text
    assert db.rows(f"SELECT status, error_code FROM plan_runs WHERE id = '{run_id}'") == [("failed", "interrupted")]
    assert db.rows(f"SELECT status, error_code FROM agent_executions WHERE id = '{live_exec}'") == [("failed", "interrupted")]


def test_the_lifespan_stops_the_recovery_loop_and_closes_the_run_registry_on_shutdown(import_main, migrate_test_db, monkeypatch):
    """M4-A1: graceful shutdown stops accepting new run work (and, with live drivers, stops them before their leases are released)."""
    main = import_main()
    monkeypatch.setattr(main, "start_telegram_bot", lambda: pytest.fail("Telegram bot must not start"))
    migrate_test_db()
    assert main.plan_run_registry.closing is False
    with TestClient(main.app) as client:
        assert client.get("/health").json() == {"status": "ok"} and main.plan_run_registry.closing is False
    assert main.plan_run_registry.closing is True and main.plan_run_registry.active() == 0
