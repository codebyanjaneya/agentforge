"""Sprint 5: workspace invitations and membership management (SQLite)."""

from datetime import timedelta

import pytest

from auth.principal import Principal
from auth.provisioning import resolve_principal  # noqa: F401  (module top: purges would otherwise create a second Principal class)
from authz.errors import AuthorizationError
from authz.invitations import (TTL, _now, accept_invitation, change_role_by_email, create_invitation, leave_workspace, list_invitations,
                               remove_member_by_email, revoke_invitation)
from tests.auth_support import auth_db  # noqa: F401  (fixture)
from tests.test_authz_memory import make_user, team_workspace


def reason(call):
    with pytest.raises(AuthorizationError) as e:
        call()
    return e.value.reason


@pytest.fixture
def w(auth_db):  # noqa: F811
    owner, _ = make_user(auth_db, "owner@example.com")
    admin, _ = make_user(auth_db, "admin@example.com")
    member, _ = make_user(auth_db, "member@example.com")
    newbie, _ = make_user(auth_db, "newbie@example.com")  # exists as a user, so the token (not a sign-in) is what grants access
    team = team_workspace(auth_db, owner, [(admin, "admin"), (member, "member")])
    return dict(db=auth_db, sf=auth_db.session_factory, owner=owner, admin=admin, member=member, newbie=newbie, team=team)


def invite(w, email="newbie@example.com", role="member", **kw):
    return create_invitation(w["sf"], w["owner"], w["team"], email, role, **kw)


def count(w, where="1=1"):
    return w["db"].scalar(f"SELECT count(*) FROM workspace_invitations WHERE {where}")


def test_token_is_returned_once_stored_only_as_hash_and_never_listed(w):
    inv = invite(w)
    assert len(inv["token"]) >= 40 and inv["email"] == "newbie@example.com"
    stored = w["db"].rows("SELECT token_hash FROM workspace_invitations")[0][0]
    assert stored != inv["token"] and inv["token"] not in stored and len(stored) == 64
    listed = list_invitations(w["sf"], w["owner"], w["team"])
    assert [i["email"] for i in listed] == ["newbie@example.com"] and all("token" not in i and "token_hash" not in i for i in listed)


def test_accept_creates_membership_once_and_the_token_is_single_use(w):
    token = invite(w, role="admin")["token"]
    got = accept_invitation(w["sf"], w["newbie"], token)
    assert got["role"] == "admin" and got["workspace_id"] == str(w["team"])
    assert w["db"].scalar("SELECT role FROM workspace_memberships WHERE workspace_id=:w AND user_id=:u", w=w["team"].hex, u=w["newbie"].user_id.hex) == "admin"
    assert reason(lambda: accept_invitation(w["sf"], w["newbie"], token)) == "invalid_invitation"  # consumed
    assert list_invitations(w["sf"], w["owner"], w["team"]) == []


def test_every_accept_failure_is_the_same_error(w):
    token = invite(w)["token"]
    other = Principal(user_id=w["member"].user_id, provider="x", provider_subject="m", email="member@example.com", is_active=True)
    assert reason(lambda: accept_invitation(w["sf"], other, token)) == "invalid_invitation"  # wrong e-mail: a forwarded link is not enough
    assert reason(lambda: accept_invitation(w["sf"], w["newbie"], "x" * 43)) == "invalid_invitation"  # unknown token
    assert reason(lambda: accept_invitation(w["sf"], w["newbie"], token, now=_now() + TTL + timedelta(seconds=1))) == "invalid_invitation"  # expired
    revoke_id = list_invitations(w["sf"], w["owner"], w["team"])[0]["id"]
    import uuid
    revoke_invitation(w["sf"], w["owner"], w["team"], uuid.UUID(revoke_id))
    assert reason(lambda: accept_invitation(w["sf"], w["newbie"], token)) == "invalid_invitation"  # revoked
    assert w["db"].scalar("SELECT count(*) FROM workspace_memberships WHERE user_id=:u AND workspace_id=:w", u=w["newbie"].user_id.hex, w=w["team"].hex) == 0


def test_email_match_is_case_insensitive_and_reinviting_revokes_the_old_token(w):
    first = invite(w, "NewBie@Example.com")["token"]
    second = invite(w)["token"]
    assert count(w, "revoked_at IS NULL") == 1
    assert reason(lambda: accept_invitation(w["sf"], w["newbie"], first)) == "invalid_invitation"
    assert accept_invitation(w["sf"], w["newbie"], second)["role"] == "member"


def test_only_the_owner_manages_invitations_and_never_owner_role_or_personal_workspace(w):
    for who in ("admin", "member"):
        assert reason(lambda: create_invitation(w["sf"], w[who], w["team"], "a@example.com", "member")) == "owner_required"
        assert reason(lambda: list_invitations(w["sf"], w[who], w["team"])) == "owner_required"
    assert reason(lambda: invite(w, role="owner")) == "unsupported_role"
    assert reason(lambda: invite(w, "owner@example.com")) == "already_a_member"
    assert reason(lambda: invite(w, "not-an-email")) == "invalid_target"
    outsider, ows = make_user(w["db"], "out@example.com")
    assert reason(lambda: create_invitation(w["sf"], outsider, w["team"], "a@example.com", "member")) == "not_a_member"
    assert reason(lambda: create_invitation(w["sf"], outsider, ows, "a@example.com", "member")) == "personal_workspace"


def test_an_invitation_of_one_workspace_cannot_be_revoked_through_another(w):
    import uuid
    inv = invite(w)
    other = team_workspace(w["db"], w["owner"], [])
    assert reason(lambda: revoke_invitation(w["sf"], w["owner"], other, uuid.UUID(inv["id"]))) == "target_unknown"
    assert count(w, "revoked_at IS NULL") == 1


def test_member_management_change_role_remove_leave_and_sole_owner_invariant(w):
    change_role_by_email(w["sf"], w["owner"], w["team"], "member@example.com", "admin")
    assert w["db"].scalar("SELECT role FROM workspace_memberships WHERE user_id=:u AND workspace_id=:w", u=w["member"].user_id.hex, w=w["team"].hex) == "admin"
    assert reason(lambda: change_role_by_email(w["sf"], w["admin"], w["team"], "member@example.com", "member")) == "owner_required"
    assert reason(lambda: change_role_by_email(w["sf"], w["owner"], w["team"], "owner@example.com", "member")) == "sole_owner_protected"
    assert reason(lambda: remove_member_by_email(w["sf"], w["owner"], w["team"], "owner@example.com")) == "sole_owner_protected"
    remove_member_by_email(w["sf"], w["owner"], w["team"], "admin@example.com")
    leave_workspace(w["sf"], w["member"], w["team"])
    assert reason(lambda: leave_workspace(w["sf"], w["owner"], w["team"])) == "sole_owner_protected"
    assert reason(lambda: leave_workspace(w["sf"], w["member"], w["team"])) == "not_a_member"
    assert w["db"].scalar("SELECT count(*) FROM workspace_memberships WHERE workspace_id=:w", w=w["team"].hex) == 1


def test_database_constraints_refuse_a_second_open_invitation_and_a_revoked_accepted_row(w):
    invite(w)
    db = w["db"]
    row = lambda tok, extra="": ("INSERT INTO workspace_invitations (id,workspace_id,email_normalized,role,token_hash,invited_by,created_at,expires_at" + (",accepted_at,accepted_by,revoked_at" if extra else "") +
                                 ") VALUES (:i,:w,'newbie@example.com','member',:t,:u,'2026-01-01','2026-01-02'" + extra + ")")
    import uuid
    for sql, extra in ((row("a"), ""), (row("b", ",'2026-01-01',:u,'2026-01-01'"), "x")):
        with pytest.raises(Exception):
            db.run(sql, i=uuid.uuid4().hex, w=w["team"].hex, t=uuid.uuid4().hex * 2, u=w["owner"].user_id.hex)
