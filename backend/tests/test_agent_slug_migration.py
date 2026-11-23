"""Migration 0018_agent_slug_per_user: the slug becomes unique per (workspace, user) so every member can own the standard starter workforce.

SQLite files in tmp_path (the same chain PostgreSQL runs in CI: test_postgres_integration checks the constraint by name and the up/down/up cycle on PostgreSQL).
"""

import sys
import uuid

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from migration_support import Db

PREV, HEAD = "0017_invitations", "0018_agent_slug_per_user"
U1, U2, W = (uuid.uuid4().hex for _ in range(3))
A1, A2, V1 = (uuid.uuid4().hex for _ in range(3))
TS = "'2026-01-02 03:04:05.000000'"


@pytest.fixture
def db(tmp_path, monkeypatch):
    return Db(tmp_path / "slug.db", monkeypatch)


def seed_users(db):
    db.run(
        f"INSERT INTO users (id, email, email_normalized, is_active, created_at) VALUES ('{U1}','u1@example.com','u1@example.com',1,{TS})",
        f"INSERT INTO users (id, email, email_normalized, is_active, created_at) VALUES ('{U2}','u2@example.com','u2@example.com',1,{TS})",
        f"INSERT INTO workspaces (id, name, is_personal, created_at) VALUES ('{W}','Team',0,{TS})",
    )


def agent(db, agent_id, user, slug="launch-comms"):
    db.run(f"INSERT INTO custom_agents (id, user_id, workspace_id, slug, status, latest_version, current_version, created_at, updated_at) "
           f"VALUES ('{agent_id}','{user}','{W}','{slug}','active',1,1,{TS},{TS})")


def test_upgrade_keeps_every_agent_and_version_and_lets_a_second_user_own_the_same_slug(db, import_fresh):
    db.upgrade(PREV)
    seed_users(db)
    agent(db, A1, U1)
    db.run(f"INSERT INTO custom_agent_versions (id, agent_id, version, definition, content_hash, change_note, restored_from_version, created_by, created_at) "
           f"VALUES ('{V1}','{A1}',1,'{{}}','{'h' * 64}',NULL,NULL,'{U1}',{TS})")
    before = (db.rows("SELECT * FROM custom_agents"), db.rows("SELECT * FROM custom_agent_versions"))
    db.upgrade(HEAD)
    assert (db.rows("SELECT * FROM custom_agents"), db.rows("SELECT * FROM custom_agent_versions")) == before  # nothing rewritten, no agent changed owner
    assert db.rows("SELECT version_num FROM alembic_version") == [(HEAD,)]
    agent(db, A2, U2)  # same workspace, same slug, different user: now allowed
    with pytest.raises(IntegrityError):  # still unique for ONE user
        agent(db, uuid.uuid4().hex, U1)
    names = {r[0] for r in db.rows("SELECT name FROM pragma_index_list('custom_agents')")}
    assert "ix_custom_agents_workspace_id_user_id" in names and not any("workspace_id_slug" in n for n in names)
    import_fresh("memory.models")
    assert db.diffs(sys.modules["memory.models"].Base.metadata) == []  # the migrated schema equals the models


def test_the_child_rows_still_reference_the_rebuilt_table(db):
    db.upgrade(PREV)
    seed_users(db)
    agent(db, A1, U1)
    db.upgrade(HEAD)
    engine = db.engine(fk=True)
    with pytest.raises((IntegrityError, OperationalError)):
        with engine.begin() as conn:  # a version of an agent that does not exist is still refused (the FK survived the rebuild)
            conn.exec_driver_sql(f"INSERT INTO custom_agent_versions (id, agent_id, version, definition, content_hash, created_by, created_at) "
                                 f"VALUES ('{uuid.uuid4().hex}','{uuid.uuid4().hex}',1,'{{}}','{'h' * 64}','{U1}',{TS})")


def test_downgrade_refuses_while_two_users_share_a_slug_and_works_after(db):
    db.upgrade(HEAD)
    seed_users(db)
    agent(db, A1, U1)
    agent(db, A2, U2)
    with pytest.raises(Exception) as refused:
        db.downgrade(PREV)
    assert "same slug" in str(refused.value) or "same slug" in repr(refused.value)
    assert db.rows("SELECT version_num FROM alembic_version") == [(HEAD,)] and db.rows("SELECT count(*) FROM custom_agents") == [(2,)]  # nothing changed
    db.run(f"DELETE FROM custom_agents WHERE id = '{A2}'")
    db.downgrade(PREV)
    assert db.rows("SELECT version_num FROM alembic_version") == [(PREV,)]
    with pytest.raises(IntegrityError):  # the old workspace-wide rule is back
        agent(db, A2, U2)
