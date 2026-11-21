"""Workspace invitations and membership management on a real PostgreSQL (disposable server only; same guards as test_postgres_integration).

Proves the BEHAVIOUR on PostgreSQL (lifecycle, identity rules, token secrecy, membership rules, races, rollback, migration), not just that the schema migrates.
Run: AGENTFORGE_TEST_PG_ADMIN_URL=postgresql://postgres:...@127.0.0.1:<port>/postgres AGENTFORGE_TEST_PG_TOKEN=<token> python -m pytest tests/test_invitations_postgres.py
"""

import logging
import os
import threading
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from auth.principal import Principal
from authz.errors import AuthorizationError
from authz import invitations as inv
from authz.invitations import (TTL, accept_invitation, change_role_by_email, create_invitation, leave_workspace, list_invitations,
                               remove_member_by_email, revoke_invitation)
from tests.test_authz_memory import make_user, team_workspace
from tests.test_postgres_integration import ADMIN_ENV, alembic_env, pg_admin, pg_db  # noqa: F401  (fixtures)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.environ.get(ADMIN_ENV), reason=f"{ADMIN_ENV} not set (no disposable PostgreSQL)"),
]


@pytest.fixture
def w(pg_db, alembic_env):  # noqa: F811
    command.upgrade(alembic_env(pg_db), "head")
    engine = create_engine(pg_db.url, pool_size=20, max_overflow=20)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    db = SimpleNamespace(session_factory=sf)
    owner, _ = make_user(db, "owner@example.com")
    admin, _ = make_user(db, "admin@example.com")
    member, _ = make_user(db, "member@example.com")
    nina, _ = make_user(db, "nina@example.com")
    other, _ = make_user(db, "other@example.com")
    team = team_workspace(db, owner, [(admin, "admin"), (member, "member")])
    yield SimpleNamespace(pg=pg_db, sf=sf, engine=engine, owner=owner, admin=admin, member=member, nina=nina, other=other, team=team, db=db)
    engine.dispose()


def reason(call):
    with pytest.raises(AuthorizationError) as e:
        call()
    return e.value.reason


def invite(w, email="nina@example.com", role="member", **kw):
    return create_invitation(w.sf, w.owner, w.team, email, role, **kw)


def scalar(world, sql, **p):
    with world.engine.connect() as c:
        return c.execute(text(sql), p).scalar()


def membership_count(w, user, ws=None):
    return scalar(w, "SELECT count(*) FROM workspace_memberships WHERE user_id = :u AND workspace_id = :w", u=user.user_id, w=ws or w.team)


# ------------------------------------------------------------------ A. lifecycle


def test_token_once_hash_only_seven_day_expiry_single_use_and_the_unusable_states(w):
    made = invite(w)
    token = made["token"]
    row = w.pg.rows("SELECT token_hash, created_at, expires_at, accepted_at, revoked_at FROM workspace_invitations")[0]
    assert row[0] != token and len(row[0]) == 64 and token not in row[0]
    assert row[2] - row[1] == TTL == timedelta(days=7)
    assert all("token" not in i and "token_hash" not in i for i in list_invitations(w.sf, w.owner, w.team))  # never listed again

    assert accept_invitation(w.sf, w.nina, token)["role"] == "member"
    assert membership_count(w, w.nina) == 1
    assert reason(lambda: accept_invitation(w.sf, w.nina, token)) == "invalid_invitation"  # single use

    # withdraw a pending one, then try it
    second = invite(w, "other@example.com", "admin")
    revoke_invitation(w.sf, w.owner, w.team, uuid.UUID(second["id"]))
    assert reason(lambda: accept_invitation(w.sf, w.other, second["token"])) == "invalid_invitation"
    assert reason(lambda: revoke_invitation(w.sf, w.owner, w.team, uuid.UUID(second["id"]))) == "target_unknown"  # nothing left to withdraw
    assert membership_count(w, w.other) == 0

    # expiry: the last valid instant works, the boundary and later do not
    third = invite(w, "other@example.com")
    created = scalar(w, "SELECT created_at FROM workspace_invitations WHERE id = :i", i=uuid.UUID(third["id"]))
    assert reason(lambda: accept_invitation(w.sf, w.other, third["token"], now=created + TTL)) == "invalid_invitation"  # expires_at > now must hold
    assert reason(lambda: accept_invitation(w.sf, w.other, third["token"], now=created + TTL + timedelta(days=1))) == "invalid_invitation"
    assert accept_invitation(w.sf, w.other, third["token"], now=created + TTL - timedelta(seconds=1))["role"] == "member"


# ------------------------------------------------------------------ B. identity and secrecy


def test_email_match_normalisation_generic_failure_and_the_token_never_appears_anywhere(w, caplog):
    caplog.set_level(logging.DEBUG)
    made = invite(w, "Nina@Example.COM")
    token = made["token"]
    assert made["email"] == "nina@example.com"
    assert reason(lambda: accept_invitation(w.sf, w.other, token)) == "invalid_invitation"  # a forwarded link is not enough
    assert reason(lambda: accept_invitation(w.sf, w.nina, "x" * 43)) == "invalid_invitation"  # unknown token: the same error
    padded = Principal(user_id=w.nina.user_id, provider="google", provider_subject="s", email="  NINA@example.com ", is_active=True)
    assert accept_invitation(w.sf, padded, token)["role"] == "member"  # case and surrounding spaces normalise

    # the raw token is in NO column of NO table and in no log record
    tables = [r[0] for r in w.pg.rows("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
    for table in tables:
        assert token not in (scalar(w, f'SELECT coalesce(string_agg(t::text, \'|\'), \'\') FROM "{table}" t') or ""), table
    assert token not in caplog.text


def test_a_token_of_one_workspace_never_grants_another_and_cross_workspace_revoke_fails(w):
    other_team = team_workspace(w.db, w.owner, [])
    a = create_invitation(w.sf, w.owner, w.team, "nina@example.com", "member")
    b = create_invitation(w.sf, w.owner, other_team, "nina@example.com", "admin")
    assert accept_invitation(w.sf, w.nina, a["token"])["workspace_id"] == str(w.team)
    assert membership_count(w, w.nina, w.team) == 1 and membership_count(w, w.nina, other_team) == 0
    assert reason(lambda: revoke_invitation(w.sf, w.owner, w.team, uuid.UUID(b["id"]))) == "target_unknown"  # b belongs to the other workspace
    assert scalar(w, "SELECT revoked_at IS NULL FROM workspace_invitations WHERE id = :i", i=uuid.UUID(b["id"])) is True
    assert accept_invitation(w.sf, w.nina, b["token"])["role"] == "admin"  # each token only its own workspace and role
    assert membership_count(w, w.nina, other_team) == 1


# ------------------------------------------------------------------ C. membership


def test_role_change_remove_leave_sole_owner_rules_and_pending_invitations_stay_consistent(w):
    pending = invite(w, "other@example.com")
    change_role_by_email(w.sf, w.owner, w.team, "member@example.com", "admin")
    assert scalar(w, "SELECT role FROM workspace_memberships WHERE user_id = :u AND workspace_id = :w", u=w.member.user_id, w=w.team) == "admin"
    assert reason(lambda: change_role_by_email(w.sf, w.member, w.team, "admin@example.com", "member")) == "owner_required"  # an admin, even a promoted one, gains no owner power
    assert reason(lambda: create_invitation(w.sf, w.member, w.team, "z@example.com", "member")) == "owner_required"
    assert reason(lambda: change_role_by_email(w.sf, w.owner, w.team, "owner@example.com", "member")) == "sole_owner_protected"
    assert reason(lambda: remove_member_by_email(w.sf, w.owner, w.team, "owner@example.com")) == "sole_owner_protected"
    assert reason(lambda: leave_workspace(w.sf, w.owner, w.team)) == "sole_owner_protected"
    assert reason(lambda: change_role_by_email(w.sf, w.owner, w.team, "owner@example.com", "owner")) == "unsupported_role"

    remove_member_by_email(w.sf, w.owner, w.team, "admin@example.com")
    leave_workspace(w.sf, w.member, w.team)
    assert membership_count(w, w.admin) == 0 and membership_count(w, w.member) == 0
    assert scalar(w, "SELECT count(*) FROM workspace_memberships WHERE workspace_id = :w AND role = 'owner'", w=w.team) == 1
    # the pending invitation was untouched by all of that and still works; a removed person can be invited again
    assert [i["email"] for i in list_invitations(w.sf, w.owner, w.team)] == ["other@example.com"]
    assert accept_invitation(w.sf, w.other, pending["token"])["role"] == "member"
    again = invite(w, "admin@example.com")
    assert accept_invitation(w.sf, w.admin, again["token"])["role"] == "member"


# ------------------------------------------------------------------ D. concurrency and transactions


def run_threads(fns):
    out, errs, barrier = [None] * len(fns), [None] * len(fns), threading.Barrier(len(fns))

    def go(i):
        barrier.wait()
        try:
            out[i] = fns[i]()
        except AuthorizationError as exc:
            errs[i] = exc.reason

    ts = [threading.Thread(target=go, args=(i,)) for i in range(len(fns))]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    return out, errs


def test_simultaneous_and_duplicate_acceptance_creates_exactly_one_membership(w):
    for round_ in range(5):
        email = f"racer{round_}@example.com"
        racer, _ = make_user(w.db, email)
        token = invite(w, email)["token"]
        out, errs = run_threads([lambda: accept_invitation(w.sf, racer, token)] * 6)
        assert sum(o is not None for o in out) == 1 and errs.count("invalid_invitation") == 5
        assert membership_count(w, racer) == 1
        assert scalar(w, "SELECT count(*) FROM workspace_invitations WHERE email_normalized = :e AND accepted_at IS NOT NULL", e=email) == 1


def test_accept_versus_withdraw_race_always_ends_in_one_consistent_state(w):
    outcomes = set()
    for round_ in range(10):
        email = f"rr{round_}@example.com"
        racer, _ = make_user(w.db, email)
        made = invite(w, email)
        out, errs = run_threads([lambda: accept_invitation(w.sf, racer, made["token"]),
                                 lambda: revoke_invitation(w.sf, w.owner, w.team, uuid.UUID(made["id"]))])
        accepted, revoked = membership_count(w, racer) == 1, scalar(w, "SELECT revoked_at IS NOT NULL FROM workspace_invitations WHERE id = :i", i=uuid.UUID(made["id"]))
        assert accepted != revoked, "exactly one of accepted / withdrawn wins (never both, never neither)"
        assert scalar(w, "SELECT accepted_at IS NOT NULL FROM workspace_invitations WHERE id = :i", i=uuid.UUID(made["id"])) == accepted
        outcomes.add(accepted)
    assert outcomes <= {True, False}


def test_concurrent_invitations_for_one_email_leave_exactly_one_open_and_unique_index_holds(w):
    out, errs = run_threads([lambda: invite(w)] * 6)
    assert sum(o is not None for o in out) == 6 and not any(errs)  # each succeeds by revoking the previous open one (serialised by the workspace row lock)
    assert scalar(w, "SELECT count(*) FROM workspace_invitations WHERE workspace_id = :w AND email_normalized = 'nina@example.com' AND accepted_at IS NULL AND revoked_at IS NULL", w=w.team) == 1
    open_token = [o for o in out if o][-1]["token"]
    live = [o["token"] for o in out if o and scalar(w, "SELECT count(*) FROM workspace_invitations WHERE token_hash = :h AND revoked_at IS NULL", h=inv._hash(o["token"])) == 1]
    assert len(live) == 1 and open_token in {o["token"] for o in out if o}
    assert accept_invitation(w.sf, w.nina, live[0])["role"] == "member"


def test_concurrent_role_updates_end_in_one_valid_role_and_one_row(w):
    out, errs = run_threads([lambda: change_role_by_email(w.sf, w.owner, w.team, "member@example.com", "admin"),
                             lambda: change_role_by_email(w.sf, w.owner, w.team, "member@example.com", "member")] * 3)
    assert not any(errs)
    rows = w.pg.rows("SELECT role FROM workspace_memberships WHERE user_id = :u AND workspace_id = :w", u=w.member.user_id, w=w.team)
    assert len(rows) == 1 and rows[0][0] in ("admin", "member")
    assert scalar(w, "SELECT count(*) FROM workspace_memberships WHERE workspace_id = :w AND role = 'owner'", w=w.team) == 1


def test_a_failure_in_the_accept_transaction_rolls_back_the_consumed_token_and_the_membership(w, monkeypatch):
    made = invite(w)

    def boom(*a, **k):
        raise RuntimeError("injected failure after the membership insert")

    monkeypatch.setattr(inv, "check_workspace_invariants", boom)
    with pytest.raises(RuntimeError):
        accept_invitation(w.sf, w.nina, made["token"])
    monkeypatch.undo()
    assert membership_count(w, w.nina) == 0
    assert scalar(w, "SELECT accepted_at IS NULL AND accepted_by IS NULL FROM workspace_invitations WHERE id = :i", i=uuid.UUID(made["id"])) is True  # the token is NOT consumed
    assert accept_invitation(w.sf, w.nina, made["token"])["role"] == "member"  # and still works afterwards


def test_database_constraints_refuse_what_the_application_never_writes(w):
    made = invite(w)
    base = dict(w=w.team, u=w.owner.user_id)
    cases = {
        "second open invitation": "INSERT INTO workspace_invitations (id,workspace_id,email_normalized,role,token_hash,invited_by,created_at,expires_at) VALUES (gen_random_uuid(),:w,'nina@example.com','member',md5(random()::text)||md5(random()::text),:u,now(),now()+interval '1 day')",
        "owner role": "INSERT INTO workspace_invitations (id,workspace_id,email_normalized,role,token_hash,invited_by,created_at,expires_at) VALUES (gen_random_uuid(),:w,'x@example.com','owner',md5(random()::text)||md5(random()::text),:u,now(),now()+interval '1 day')",
        "expiry before creation": "INSERT INTO workspace_invitations (id,workspace_id,email_normalized,role,token_hash,invited_by,created_at,expires_at) VALUES (gen_random_uuid(),:w,'y@example.com','member',md5(random()::text)||md5(random()::text),:u,now(),now())",
        "accepted and revoked": "UPDATE workspace_invitations SET accepted_at = now(), accepted_by = :u, revoked_at = now() WHERE token_hash = :h",
        "accepted without acceptor": "UPDATE workspace_invitations SET accepted_at = now() WHERE token_hash = :h",
    }
    h = inv._hash(made["token"])
    for name, sql in cases.items():
        with pytest.raises(DBAPIError, match="violates"):
            with w.engine.begin() as c:
                c.execute(text(sql), {**base, "h": h})
    # unique token hash
    other = invite(w, "other@example.com")
    with pytest.raises(DBAPIError, match="uq_workspace_invitations_token_hash"):
        with w.engine.begin() as c:
            c.execute(text("UPDATE workspace_invitations SET token_hash = :a WHERE token_hash = :b"), {"a": h, "b": inv._hash(other["token"])})


# ------------------------------------------------------------------ E. migration


def test_migration_0017_chain_schema_indexes_downgrade_data_preservation_and_reupgrade(pg_db, alembic_env):  # noqa: F811
    cfg = alembic_env(pg_db)
    command.upgrade(cfg, "0016_durable_dispatch")
    assert pg_db.rows("SELECT to_regclass('workspace_invitations')") == [(None,)]
    sf = sessionmaker(bind=create_engine(pg_db.url), expire_on_commit=False)
    db = SimpleNamespace(session_factory=sf)
    owner, ows = make_user(db, "owner@example.com")
    team = team_workspace(db, owner, [])
    before = {t: pg_db.rows(f"SELECT count(*) FROM {t}")[0][0] for t in ("users", "workspaces", "workspace_memberships")}

    command.upgrade(cfg, "head")  # 0016 -> 0017
    assert pg_db.rows("SELECT version_num FROM alembic_version") == [("0017_invitations",)]
    assert {r[0] for r in pg_db.rows("SELECT conname FROM pg_constraint WHERE conrelid = 'workspace_invitations'::regclass")} >= {
        "workspace_invitations_pkey", "uq_workspace_invitations_token_hash", "ck_workspace_invitations_role", "ck_workspace_invitations_expiry",
        "ck_workspace_invitations_single_outcome", "ck_workspace_invitations_acceptor", "fk_workspace_invitations_workspace_id_workspaces",
        "fk_workspace_invitations_invited_by_users", "fk_workspace_invitations_accepted_by_users"}
    idx = dict(pg_db.rows("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'workspace_invitations'"))
    assert "accepted_at IS NULL" in idx["uq_workspace_invitations_open"] and "UNIQUE" in idx["uq_workspace_invitations_open"] and "ix_workspace_invitations_workspace_id" in idx
    assert {t: pg_db.rows(f"SELECT count(*) FROM {t}")[0][0] for t in before} == before  # existing data untouched

    made = create_invitation(sf, owner, team, "nina@example.com", "member")
    command.downgrade(cfg, "-1")  # drops only the new table (pending invitations are lost: documented)
    assert pg_db.rows("SELECT to_regclass('workspace_invitations')") == [(None,)]
    assert {t: pg_db.rows(f"SELECT count(*) FROM {t}")[0][0] for t in before} == before
    command.upgrade(cfg, "head")
    assert pg_db.rows("SELECT count(*) FROM workspace_invitations") == [(0,)]
    again = create_invitation(sf, owner, team, "nina@example.com", "member")
    assert again["token"] != made["token"]
