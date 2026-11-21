"""M1-D: identity resolution and first-valid-token provisioning (isolated migrated SQLite; no network)."""

import dataclasses
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from auth import provisioning
from auth.errors import InactiveUserError
from auth.principal import Principal
from auth.provisioning import IDENTITY_UNIQUE, resolve_principal
from auth_support import ONE_OF_EACH, ZERO, auth_db, claims  # noqa: F401  (auth_db is a fixture)
from memory.models import AuthIdentity, User, Workspace, WorkspaceMembership


def orm(auth_db):
    return auth_db.session_factory()


# ------------------------------------------------------------ 1-3. resolution and creation


def test_first_valid_token_creates_user_personal_workspace_owner_membership_and_identity(auth_db):
    principal = resolve_principal(auth_db.session_factory, claims(subject="sub-A", email="Alice@Example.com"))
    assert auth_db.counts() == ONE_OF_EACH  # all four, and exactly one of each
    with orm(auth_db) as s:
        user = s.scalars(select(User)).one()
        workspace = s.scalars(select(Workspace)).one()
        membership = s.scalars(select(WorkspaceMembership)).one()
        identity = s.scalars(select(AuthIdentity)).one()
    assert (user.email, user.email_normalized, user.is_active) == ("Alice@Example.com", "alice@example.com", True)
    assert user.id == principal.user_id  # the application identity is the internal UUID
    assert (workspace.is_personal, workspace.personal_for_user_id, workspace.name) == (True, user.id, "Personal workspace")
    assert (membership.workspace_id, membership.user_id, membership.role) == (workspace.id, user.id, "owner")
    assert (identity.user_id, identity.provider, identity.provider_subject, identity.email_at_link) == (
        user.id, "google", "sub-A", "Alice@Example.com")


def test_existing_identity_resolves_to_the_same_internal_uuid_without_new_rows(auth_db):
    first = resolve_principal(auth_db.session_factory, claims(subject="sub-A"))
    second = resolve_principal(auth_db.session_factory, claims(subject="sub-A", jti="another-token"))
    assert first.user_id == second.user_id and isinstance(first.user_id, uuid.UUID)
    assert auth_db.counts() == ONE_OF_EACH


def test_the_four_records_are_linked_to_each_other_in_one_consistent_set(auth_db):
    resolve_principal(auth_db.session_factory, claims(subject="sub-A"))
    assert auth_db.rows(
        "SELECT count(*) FROM users u JOIN workspaces w ON w.personal_for_user_id = u.id AND w.is_personal = 1 "
        "JOIN workspace_memberships m ON m.workspace_id = w.id AND m.user_id = u.id AND m.role = 'owner' "
        "JOIN auth_identities i ON i.user_id = u.id") == [(1,)]


# ------------------------------------------------------------------ 4. atomic rollback


def test_a_failure_during_provisioning_rolls_back_all_four_records(auth_db, monkeypatch):
    def failing_identity(**kwargs):
        raise RuntimeError("simulated failure after user, workspace and membership were flushed")

    monkeypatch.setattr(provisioning, "AuthIdentity", failing_identity)
    with pytest.raises(RuntimeError, match="simulated failure"):
        resolve_principal(auth_db.session_factory, claims(subject="sub-A"))
    assert auth_db.counts() == ZERO  # nothing of the partial work survived
    monkeypatch.undo()  # the identical request now succeeds cleanly (no poisoned state)
    resolve_principal(auth_db.session_factory, claims(subject="sub-A"))
    assert auth_db.counts() == ONE_OF_EACH


# ------------------------------------------------------------------- 5. real concurrency


def test_concurrent_first_provisioning_creates_exactly_one_of_each(auth_db, monkeypatch):
    threads = 4
    barrier = threading.Barrier(threads, timeout=30)
    real_find, local, conflicts = provisioning._find, threading.local(), []

    def synced_find(session_factory, verified):
        result = real_find(session_factory, verified)
        if result is None and not getattr(local, "waited", False):
            local.waited = True
            barrier.wait()  # every thread has seen "no identity" before any of them provisions
        return result

    real_is_conflict = provisioning._is_identity_conflict
    monkeypatch.setattr(provisioning, "_find", synced_find)
    monkeypatch.setattr(provisioning, "_is_identity_conflict", lambda exc: conflicts.append(1) or real_is_conflict(exc))

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(resolve_principal, auth_db.session_factory, claims(subject="racing-sub")) for _ in range(threads)]
        principals = [f.result(timeout=60) for f in futures]

    assert len({p.user_id for p in principals}) == 1  # every caller got the same internal user
    assert auth_db.counts() == ONE_OF_EACH  # exactly one user, personal workspace, owner membership, identity
    assert len(conflicts) >= 1  # the unique constraint really arbitrated at least one lost race
    assert auth_db.scalar("SELECT count(*) FROM workspace_memberships WHERE role = 'owner'") == 1


def test_a_lost_race_recovers_in_a_fresh_session_and_leaves_no_loser_rows(auth_db, monkeypatch):
    real_provision, fired = provisioning._provision, []

    def racing_provision(session_factory, verified):
        if not fired:  # the winner commits between our lookup and our provisioning
            fired.append(1)
            real_provision(session_factory, verified)
        return real_provision(session_factory, verified)  # the loser now violates the unique constraint

    monkeypatch.setattr(provisioning, "_provision", racing_provision)
    principal = resolve_principal(auth_db.session_factory, claims(subject="raced", email="same@example.com"))
    assert auth_db.counts() == ONE_OF_EACH  # the loser's user, workspace and membership were rolled back
    assert principal.user_id == uuid.UUID(auth_db.scalar("SELECT id FROM users"))


# ------------------------------------------------- 6-8. no merging, email is an attribute


def test_same_email_with_different_subjects_never_merges_identities(auth_db):
    a = resolve_principal(auth_db.session_factory, claims(subject="sub-A", email="same@example.com"))
    b = resolve_principal(auth_db.session_factory, claims(subject="sub-B", email="same@example.com"))
    assert a.user_id != b.user_id
    assert auth_db.counts() == {"users": 2, "workspaces": 2, "memberships": 2, "identities": 2}
    assert auth_db.scalar("SELECT count(DISTINCT user_id) FROM auth_identities") == 2


def test_same_email_across_different_providers_never_merges_identities(auth_db):
    # The token layer accepts only supported providers; the identity layer itself is provider-agnostic.
    a = resolve_principal(auth_db.session_factory, claims(provider="google", subject="42", email="same@example.com"))
    b = resolve_principal(auth_db.session_factory, claims(provider="github", subject="42", email="same@example.com"))
    assert a.user_id != b.user_id  # even the same subject string under another provider is another identity
    assert auth_db.counts() == {"users": 2, "workspaces": 2, "memberships": 2, "identities": 2}


def test_subject_and_provider_are_stored_and_matched_exactly_without_normalization(auth_db):
    ids = {s: resolve_principal(auth_db.session_factory, claims(subject=s)).user_id for s in ("AbC", "abc", "ünï-sub")}
    assert len(set(ids.values())) == 3  # case differs: different identities
    assert resolve_principal(auth_db.session_factory, claims(subject="ünï-sub")).user_id == ids["ünï-sub"]
    assert sorted(r[0] for r in auth_db.rows("SELECT provider_subject FROM auth_identities")) == sorted(["AbC", "abc", "ünï-sub"])
    assert resolve_principal(auth_db.session_factory, claims(provider="Google", subject="AbC")).user_id != ids["AbC"]


def test_an_email_change_never_transfers_identity_ownership(auth_db):
    first = resolve_principal(auth_db.session_factory, claims(subject="sub-A", email="old@example.com"))
    later = resolve_principal(auth_db.session_factory, claims(subject="sub-A", email="new@example.com"))
    assert first.user_id == later.user_id and later.email == "new@example.com"  # same user, current email attribute
    assert auth_db.counts() == ONE_OF_EACH
    assert auth_db.rows("SELECT email, email_normalized FROM users") == [("new@example.com", "new@example.com")]
    assert auth_db.scalar("SELECT email_at_link FROM auth_identities") == "old@example.com"  # what was observed at link time
    # The old address does not stay attached to anything: a NEW subject using it is a NEW user, not a takeover.
    other = resolve_principal(auth_db.session_factory, claims(subject="sub-B", email="old@example.com"))
    assert other.user_id != first.user_id and auth_db.counts()["users"] == 2


def test_an_unchanged_email_causes_no_write_and_a_failing_refresh_never_blocks_authentication(auth_db, monkeypatch, caplog):
    first = resolve_principal(auth_db.session_factory, claims(subject="sub-A", email="a@example.com"))
    statements = []
    from sqlalchemy import event

    event.listen(auth_db.engine, "before_cursor_execute", lambda c, cur, s, *a: statements.append(s.lstrip()[:6].upper()))
    resolve_principal(auth_db.session_factory, claims(subject="sub-A", email="a@example.com"))
    assert set(statements) <= {"SELECT", "PRAGMA"}  # no UPDATE / INSERT merely to refresh anything

    monkeypatch.setattr(provisioning, "normalize_email", lambda e: (_ for _ in ()).throw(SQLAlchemyError("SENTINEL-driver-text")))
    with caplog.at_level(logging.DEBUG):
        again = resolve_principal(auth_db.session_factory, claims(subject="sub-A", email="b@example.com"))
    assert again.user_id == first.user_id  # authentication still succeeds
    assert again.email == "b@example.com"  # the Principal carries the VERIFIED token email, not the stale stored one
    assert auth_db.scalar("SELECT email FROM users") == "a@example.com"  # the attribute was not changed
    assert "SENTINEL-driver-text" not in caplog.text and "SQLAlchemyError" in caplog.text  # class name only


# ------------------------------------------------------------------- 9. inactive users


def test_inactive_users_are_rejected_and_never_reactivated(auth_db):
    principal = resolve_principal(auth_db.session_factory, claims(subject="sub-A", email="a@example.com"))
    auth_db.run("UPDATE users SET is_active = 0")
    for email in ("a@example.com", "different@example.com"):
        with pytest.raises(InactiveUserError) as exc:
            resolve_principal(auth_db.session_factory, claims(subject="sub-A", email=email))
        assert exc.value.reason == "inactive_user"
    assert auth_db.scalar("SELECT is_active FROM users") == 0  # not reactivated
    assert auth_db.scalar("SELECT email FROM users") == "a@example.com"  # no attribute refresh for an inactive user
    assert auth_db.counts() == ONE_OF_EACH  # and no replacement user was provisioned
    assert principal.user_id == uuid.UUID(auth_db.scalar("SELECT id FROM users"))


# ------------------------------------------------------ 10-11. integrity error handling


def pg_style(constraint):
    return SimpleNamespace(diag=SimpleNamespace(constraint_name=constraint))


class FakeOrig(Exception):
    def __init__(self, message="x", constraint=None, has_diag=False):
        super().__init__(message)
        if has_diag:
            self.diag = SimpleNamespace(constraint_name=constraint)


def test_only_the_identity_unique_constraint_counts_as_a_lost_race():
    assert IDENTITY_UNIQUE == "uq_auth_identities_provider_provider_subject"
    is_conflict = provisioning._is_identity_conflict
    # PostgreSQL style: the constraint name is in diag.
    assert is_conflict(IntegrityError("s", {}, FakeOrig(constraint=IDENTITY_UNIQUE, has_diag=True)))
    assert not is_conflict(IntegrityError("s", {}, FakeOrig(constraint="uq_workspaces_personal_for_user_id", has_diag=True)))
    assert not is_conflict(IntegrityError("s", {}, FakeOrig(constraint="ck_workspace_memberships_role", has_diag=True)))
    assert not is_conflict(IntegrityError("s", {}, FakeOrig(constraint="users_pkey", has_diag=True)))
    # SQLite style: the message lists the columns.
    assert is_conflict(IntegrityError("s", {}, FakeOrig("UNIQUE constraint failed: auth_identities.provider, auth_identities.provider_subject")))
    assert not is_conflict(IntegrityError("s", {}, FakeOrig("UNIQUE constraint failed: workspaces.personal_for_user_id")))
    assert not is_conflict(IntegrityError("s", {}, FakeOrig("CHECK constraint failed: ck_workspace_memberships_role")))
    assert not is_conflict(IntegrityError("s", {}, FakeOrig("FOREIGN KEY constraint failed")))
    assert not is_conflict(IntegrityError("s", {}, FakeOrig("UNIQUE constraint failed: auth_identities.provider")))


def test_an_unrelated_integrity_error_propagates_after_a_full_rollback(auth_db, monkeypatch):
    monkeypatch.setattr(provisioning, "OWNER_ROLE", "viewer")  # violates ck_workspace_memberships_role
    with pytest.raises(IntegrityError) as exc:
        resolve_principal(auth_db.session_factory, claims(subject="sub-A"))
    assert not provisioning._is_identity_conflict(exc.value)  # it was not mistaken for a lost race
    assert "ck_workspace_memberships_role" in str(exc.value)
    assert auth_db.counts() == ZERO  # and nothing partial remains


def test_a_lost_race_that_leaves_no_winner_is_never_reported_as_success(auth_db, monkeypatch):
    def conflict_without_winner(session_factory, verified):
        raise IntegrityError("s", {}, FakeOrig("UNIQUE constraint failed: auth_identities.provider, auth_identities.provider_subject"))

    monkeypatch.setattr(provisioning, "_provision", conflict_without_winner)
    with pytest.raises(RuntimeError, match="could not be resolved"):
        resolve_principal(auth_db.session_factory, claims(subject="ghost"))
    assert auth_db.counts() == ZERO


# ------------------------------------------------------------ 12. Principal provenance


def test_principal_fields_come_from_the_verified_claims_and_the_database_resolution_only(auth_db):
    verified = claims(subject="sub-A", email="verified@example.com")
    principal = resolve_principal(auth_db.session_factory, verified)
    with orm(auth_db) as s:
        user = s.scalars(select(User)).one()
    assert principal == Principal(user_id=user.id, provider="google", provider_subject="sub-A",
                                  email="verified@example.com", is_active=True)
    assert {f.name for f in dataclasses.fields(Principal)} == {"user_id", "provider", "provider_subject", "email", "is_active"}
    with pytest.raises(dataclasses.FrozenInstanceError):
        principal.email = "attacker@example.com"
    with pytest.raises(dataclasses.FrozenInstanceError):
        principal.user_id = uuid.uuid4()
    # The resolver has no parameter through which a request could supply an identity.
    import inspect

    assert list(inspect.signature(resolve_principal).parameters) == ["session_factory", "claims"]
