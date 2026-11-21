"""M1-E: workspace authorization, roles and the owner invariant (SQLite; PostgreSQL in test_postgres_integration)."""

import uuid

import pytest

from auth.principal import Principal
from auth.provisioning import resolve_principal  # module top: purges would otherwise create a second Principal class
from authz.errors import AuthorizationError
from authz.workspaces import (ROLES, authorize_memory_scope, change_role, check_workspace_invariants, get_membership,
                              reject_workspace_header, remove_membership)
from tests.auth_support import auth_db, claims  # noqa: F401  (fixture)
from tests.test_authz_memory import make_user, team_workspace


def reason(call):
    with pytest.raises(AuthorizationError) as e:
        call()
    return e.value.reason


@pytest.fixture
def w(auth_db):  # noqa: F811
    sf = auth_db.session_factory
    owner, owner_ws = make_user(auth_db, "owner@example.com")
    admin, _ = make_user(auth_db, "admin@example.com")
    member, _ = make_user(auth_db, "member@example.com")
    outsider, outsider_ws = make_user(auth_db, "out@example.com")
    team = team_workspace(auth_db, owner, [(admin, "admin"), (member, "member")])
    return dict(db=auth_db, sf=sf, owner=owner, admin=admin, member=member, outsider=outsider, team=team,
                owner_ws=owner_ws, outsider_ws=outsider_ws)


def roles(db, ws):
    return dict(db.rows("SELECT user_id, role FROM workspace_memberships WHERE workspace_id=:w", w=ws.hex))


def test_roles_are_exactly_owner_admin_member():
    assert ROLES == ("owner", "admin", "member")


def test_every_role_gets_only_its_own_scope(w):
    for who, role in [("owner", "owner"), ("admin", "admin"), ("member", "member")]:
        p = w[who]
        assert get_membership(w["sf"], p, w["team"]).role == role
        s = authorize_memory_scope(w["sf"], p, w["team"])
        assert (s.user_id, s.workspace_id) == (p.user_id, w["team"])  # always the caller's OWN id


def test_unknown_workspace_and_non_member_rejected_identically(w):
    r1 = reason(lambda: authorize_memory_scope(w["sf"], w["outsider"], w["team"]))
    r2 = reason(lambda: authorize_memory_scope(w["sf"], w["owner"], uuid.uuid4()))
    assert r1 == r2 == "not_a_member"


def test_personal_workspace_only_for_its_user(w):
    assert authorize_memory_scope(w["sf"], w["owner"], w["owner_ws"]).workspace_id == w["owner_ws"]
    assert reason(lambda: authorize_memory_scope(w["sf"], w["outsider"], w["owner_ws"])) == "not_a_member"


def test_personal_workspace_with_mismatched_owner_membership_is_refused(w):
    db = w["db"]
    # corrupt: the personal workspace's owner membership belongs to somebody else
    db.run("DELETE FROM workspace_memberships WHERE workspace_id=:w", w=w["owner_ws"].hex)
    db.run("INSERT INTO workspace_memberships (workspace_id,user_id,role,created_at) VALUES (:w,:u,'owner',CURRENT_TIMESTAMP)",
           w=w["owner_ws"].hex, u=w["outsider"].user_id.hex)
    assert reason(lambda: authorize_memory_scope(w["sf"], w["outsider"], w["owner_ws"])) == "inconsistent_workspace"
    assert reason(lambda: authorize_memory_scope(w["sf"], w["owner"], w["owner_ws"])) == "not_a_member"


def test_workspace_without_exactly_one_owner_is_refused(w):
    db = w["db"]
    db.run("UPDATE workspace_memberships SET role='member' WHERE workspace_id=:w AND role='owner'", w=w["team"].hex)
    assert reason(lambda: authorize_memory_scope(w["sf"], w["member"], w["team"])) == "inconsistent_workspace"
    db.run("UPDATE workspace_memberships SET role='owner' WHERE workspace_id=:w", w=w["team"].hex)  # now three owners
    assert reason(lambda: authorize_memory_scope(w["sf"], w["member"], w["team"])) == "inconsistent_workspace"


def test_membership_is_read_from_the_database_every_time(w):
    authorize_memory_scope(w["sf"], w["member"], w["team"])
    w["db"].run("DELETE FROM workspace_memberships WHERE workspace_id=:w AND user_id=:u", w=w["team"].hex, u=w["member"].user_id.hex)
    assert reason(lambda: authorize_memory_scope(w["sf"], w["member"], w["team"])) == "not_a_member"  # no cache


def test_untrusted_inputs_rejected(w):
    for bad in (None, "owner@example.com", {"user_id": w["owner"].user_id}, object()):
        assert reason(lambda: authorize_memory_scope(w["sf"], bad, w["team"])) == "untrusted_principal"
    inactive = Principal(user_id=w["owner"].user_id, provider="google", provider_subject="s", email="x", is_active=False)
    assert reason(lambda: authorize_memory_scope(w["sf"], inactive, w["team"])) == "untrusted_principal"
    for bad in (None, str(w["team"]), "default", 1):
        assert reason(lambda: authorize_memory_scope(w["sf"], w["owner"], bad)) == "invalid_workspace_id"


def test_membership_is_never_resolved_by_email(w):
    # a Principal carrying an owner's email but a different internal id gets nothing
    spoof = Principal(user_id=uuid.uuid4(), provider="google", provider_subject="s", email="owner@example.com", is_active=True)
    assert reason(lambda: authorize_memory_scope(w["sf"], spoof, w["team"])) == "not_a_member"


def test_workspace_header_rejected_when_non_empty():
    reject_workspace_header(None)
    reject_workspace_header("")
    for v in ("abc", str(uuid.uuid4()), " "):
        assert reason(lambda: reject_workspace_header(v)) == "workspace_header_not_supported"


# ---------------------------------------------------------------- owner invariant

def test_non_owner_cannot_manage_members(w):
    for actor in (w["admin"], w["member"]):
        assert reason(lambda: remove_membership(w["sf"], actor, w["team"], w["member"].user_id)) == "owner_required"
        assert reason(lambda: change_role(w["sf"], actor, w["team"], w["member"].user_id, "admin")) == "owner_required"
    assert reason(lambda: remove_membership(w["sf"], w["outsider"], w["team"], w["member"].user_id)) == "not_a_member"
    assert len(roles(w["db"], w["team"])) == 3


def test_sole_owner_cannot_be_removed_or_demoted_by_anyone(w):
    owner_id = w["owner"].user_id
    for actor in (w["owner"], w["admin"], w["member"]):
        assert reason(lambda: remove_membership(w["sf"], actor, w["team"], owner_id)) in ("sole_owner_protected", "owner_required")
        assert reason(lambda: change_role(w["sf"], actor, w["team"], owner_id, "member")) in ("sole_owner_protected", "owner_required")
    assert roles(w["db"], w["team"])[owner_id.hex] == "owner"


def test_no_second_owner_and_no_viewer(w):
    assert reason(lambda: change_role(w["sf"], w["owner"], w["team"], w["admin"].user_id, "owner")) == "owner_transfer_unsupported"
    for bad in ("viewer", "", None, "OWNER"):
        assert reason(lambda: change_role(w["sf"], w["owner"], w["team"], w["admin"].user_id, bad)) == "unsupported_role"
    assert list(roles(w["db"], w["team"]).values()).count("owner") == 1


def test_owner_can_manage_non_owners_and_invariant_holds(w):
    change_role(w["sf"], w["owner"], w["team"], w["member"].user_id, "admin")
    assert roles(w["db"], w["team"])[w["member"].user_id.hex] == "admin"
    remove_membership(w["sf"], w["owner"], w["team"], w["admin"].user_id)
    assert w["admin"].user_id.hex not in roles(w["db"], w["team"])
    assert reason(lambda: remove_membership(w["sf"], w["owner"], w["team"], w["admin"].user_id)) == "target_not_a_member"
    with w["sf"]() as s:
        check_workspace_invariants(s, w["team"])
    assert reason(lambda: authorize_memory_scope(w["sf"], w["admin"], w["team"])) == "not_a_member"  # removed


def test_personal_workspace_owner_cannot_be_removed_or_given_members(w):
    assert reason(lambda: remove_membership(w["sf"], w["owner"], w["owner_ws"], w["owner"].user_id)) == "sole_owner_protected"
    assert reason(lambda: change_role(w["sf"], w["owner"], w["owner_ws"], w["outsider"].user_id, "member")) == "target_not_a_member"


def test_violating_change_is_rolled_back_before_commit(w, monkeypatch):
    import authz.workspaces as aw
    real = aw.check_workspace_invariants
    calls = []

    def failing(session, wid):
        calls.append(1)
        if len(calls) >= 2:  # the post-change check (the first is the pre-change check)
            raise AuthorizationError("inconsistent_workspace")
        return real(session, wid)

    monkeypatch.setattr(aw, "check_workspace_invariants", failing)
    assert reason(lambda: remove_membership(w["sf"], w["owner"], w["team"], w["member"].user_id)) == "inconsistent_workspace"
    assert w["member"].user_id.hex in roles(w["db"], w["team"])  # rolled back
    calls.clear()
    assert reason(lambda: change_role(w["sf"], w["owner"], w["team"], w["member"].user_id, "admin")) == "inconsistent_workspace"
    assert roles(w["db"], w["team"])[w["member"].user_id.hex] == "member"


def test_nothing_is_deleted_besides_the_membership_row(w):
    before = w["db"].counts()
    remove_membership(w["sf"], w["owner"], w["team"], w["member"].user_id)
    after = w["db"].counts()
    assert after["memberships"] == before["memberships"] - 1
    assert (after["users"], after["workspaces"], after["identities"]) == (before["users"], before["workspaces"], before["identities"])


def test_m1d_provisioning_still_produces_an_authorizable_workspace(w):
    p = resolve_principal(w["sf"], claims(subject="brand-new", email="new@example.com"))
    ws = uuid.UUID(w["db"].rows("SELECT id FROM workspaces WHERE personal_for_user_id=:u", u=p.user_id.hex)[0][0])
    s = authorize_memory_scope(w["sf"], p, ws)
    assert (s.user_id, s.workspace_id) == (p.user_id, ws)
    assert get_membership(w["sf"], p, ws).role == "owner"
