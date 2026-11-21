"""Workspace authorization and the owner invariant (D025).

Every decision reads membership FROM THE DATABASE for the Principal's internal user id; a workspace id supplied
by a client is only a requested resource. Email is never used. Unknown workspaces and non-members are
indistinguishable (``not_a_member``), so existence is not leaked.

Roles are exactly ``owner``, ``admin``, ``member`` (no viewer). Membership in any of them authorizes a
``MemoryScope``: the member's OWN memory in that workspace. No role grants access to another user's memory.

PERMISSIONS. The approved decisions define the roles but no permission matrix, so nothing role-specific is
invented: ``admin`` and ``member`` have NO capability beyond the above. The only operations that change
memberships (``remove_membership``, ``change_role``) are therefore deny-by-default: only an owner may call
them. There is no add/invite operation and no owner transfer (nothing requires one yet); promoting anyone to
``owner`` is refused. Who else may manage members is an open question for the owner.

OWNER INVARIANT. A workspace has exactly one ``owner`` membership; a personal workspace additionally has
exactly one membership in total, owned by ``personal_for_user_id``. It is checked on every authorization
decision (an inconsistent workspace is refused, fail closed) and, for any membership change, inside the same
transaction under a row lock on the workspace, before commit: a violating change is rolled back. The sole owner
can be neither removed nor demoted by anyone, including themselves. Foreign keys stay NO ACTION: nothing here
deletes users or workspaces.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import aliased

from auth.principal import Principal
from authz.errors import AuthorizationError
from authz.scope import _ISSUE, MemoryScope, _issue_scope
from auth.provisioning import normalize_email
from memory.models import User, Workspace, WorkspaceMembership

OWNER = "owner"
ROLES = ("owner", "admin", "member")  # fixed; there is no viewer role


@dataclass(frozen=True, slots=True)
class Membership:
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    is_personal: bool


def _validate(principal, workspace_id) -> None:
    if not isinstance(principal, Principal) or principal.is_active is not True or not isinstance(principal.user_id, uuid.UUID):
        raise AuthorizationError("untrusted_principal")
    if not isinstance(workspace_id, uuid.UUID):  # a client-supplied string must be parsed by the caller first
        raise AuthorizationError("invalid_workspace_id")


def _state(workspace_id):
    """Owner-structure columns as correlated subqueries over an ALIAS (so they survive a join on memberships)."""
    m = aliased(WorkspaceMembership)
    owners = select(func.count()).select_from(m).where(m.workspace_id == Workspace.id, m.role == OWNER).scalar_subquery()
    members = select(func.count()).select_from(m).where(m.workspace_id == Workspace.id).scalar_subquery()
    owner_user = (select(m.user_id).where(m.workspace_id == Workspace.id, m.role == OWNER).limit(1).scalar_subquery())
    return owners, members, owner_user


def _consistent(is_personal, personal_for, owners, members, owner_user) -> bool:
    if bool(is_personal) != (personal_for is not None):
        return False
    if owners != 1:
        return False  # exactly one owner, always
    if is_personal:
        return members == 1 and owner_user == personal_for  # the personal owner is the sole member
    return True


def check_workspace_invariants(session, workspace_id: uuid.UUID) -> None:
    """Raise AuthorizationError unless the workspace satisfies the owner invariant. Reads inside ``session``."""
    owners, members, owner_user = _state(workspace_id)
    row = session.execute(
        select(Workspace.is_personal, Workspace.personal_for_user_id, owners, members, owner_user).where(Workspace.id == workspace_id)
    ).first()
    if row is None:
        raise AuthorizationError("not_a_member")
    if not _consistent(*row):
        raise AuthorizationError("inconsistent_workspace")


def get_membership(session_factory, principal: Principal, workspace_id: uuid.UUID) -> Membership:
    """The Principal's membership of ``workspace_id``, read from the database now, or AuthorizationError."""
    _validate(principal, workspace_id)
    owners, members, owner_user = _state(workspace_id)
    with session_factory() as session:
        row = session.execute(
            select(Workspace.is_personal, Workspace.personal_for_user_id, WorkspaceMembership.role, owners, members, owner_user)
            .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
            .where(Workspace.id == workspace_id, WorkspaceMembership.user_id == principal.user_id)
        ).first()
    if row is None:
        raise AuthorizationError("not_a_member")  # unknown workspace and non-member look the same
    is_personal, personal_for, role, owner_count, member_count, owner_id = row
    if role not in ROLES:
        raise AuthorizationError("invalid_role")
    if not _consistent(is_personal, personal_for, owner_count, member_count, owner_id):
        raise AuthorizationError("inconsistent_workspace")
    if is_personal and personal_for != principal.user_id:
        raise AuthorizationError("inconsistent_workspace")  # someone else's personal workspace
    return Membership(workspace_id=workspace_id, user_id=principal.user_id, role=role, is_personal=bool(is_personal))


def authorize_memory_scope(session_factory, principal: Principal, workspace_id: uuid.UUID) -> MemoryScope:
    """The ONLY way to obtain a ``MemoryScope``: trusted Principal + a membership verified now."""
    membership = get_membership(session_factory, principal, workspace_id)
    return _issue_scope(principal.user_id, membership.workspace_id, _proof=_ISSUE)


def authorize_run_scope(session_factory, user_id: uuid.UUID, workspace_id: uuid.UUID) -> MemoryScope:
    """The scope a persisted plan run executes under when no request (and so no token) exists: a worker claiming it, or recovery resuming
    it. The user must still exist and be active, and the membership of that user in that workspace is verified in the database NOW, exactly
    as for a request (a user removed from the workspace since the run was created can no longer have it executed; the caller then closes the
    run). The Principal built here is an internal, non-authenticating stand-in: it carries no provider identity and cannot be presented anywhere."""
    with session_factory() as session:
        user = session.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthorizationError("not_a_member")
    principal = Principal(user_id=user.id, provider="system", provider_subject=f"run:{user.id}", email=user.email, is_active=True)
    return authorize_memory_scope(session_factory, principal, workspace_id)


def authorize_personal_scope(session_factory, principal: Principal) -> MemoryScope:
    """M1 API/WebSocket workspace: the Principal's OWN personal workspace (``X-Workspace-Id`` stays rejected, D025).

    The workspace id is looked up by the Principal's internal user id (never supplied by a client) and is then
    authorized like any other: membership, owner and personal-workspace invariants are verified in the database.
    """
    _validate(principal, uuid.uuid4())  # validates the Principal only
    with session_factory() as session:
        workspace_id = session.execute(
            select(Workspace.id).where(Workspace.personal_for_user_id == principal.user_id, Workspace.is_personal.is_(True))
        ).scalar()
    if workspace_id is None:
        raise AuthorizationError("no_personal_workspace")
    return authorize_memory_scope(session_factory, principal, workspace_id)


def _owner_operation(session, actor: Principal, workspace_id: uuid.UUID) -> None:
    """Common start of a membership change: lock, verify the actor FROM THE DATABASE, require an owner."""
    _validate(actor, workspace_id)
    if session.execute(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()).first() is None:
        raise AuthorizationError("not_a_member")
    actor_role = session.execute(
        select(WorkspaceMembership.role).where(
            WorkspaceMembership.workspace_id == workspace_id, WorkspaceMembership.user_id == actor.user_id)
    ).scalar()
    if actor_role is None:
        raise AuthorizationError("not_a_member")
    if actor_role not in ROLES:
        raise AuthorizationError("invalid_role")
    check_workspace_invariants(session, workspace_id)  # the state we start from must itself be consistent
    if actor_role != OWNER:
        raise AuthorizationError("owner_required")  # deny by default: no admin/member capability is defined


def _target_role(session, workspace_id: uuid.UUID, target_user_id: uuid.UUID) -> str:
    if not isinstance(target_user_id, uuid.UUID):
        raise AuthorizationError("invalid_target")
    role = session.execute(
        select(WorkspaceMembership.role).where(
            WorkspaceMembership.workspace_id == workspace_id, WorkspaceMembership.user_id == target_user_id)
    ).scalar()
    if role is None:
        raise AuthorizationError("target_not_a_member")
    if role == OWNER:
        raise AuthorizationError("sole_owner_protected")  # never removed or demoted (use of a transfer is unsupported)
    return role


def remove_membership(session_factory, actor: Principal, workspace_id: uuid.UUID, target_user_id: uuid.UUID) -> None:
    """Owner-only. Removes a NON-owner member. The invariant is re-checked before commit."""
    with session_factory() as session:
        with session.begin():
            _owner_operation(session, actor, workspace_id)
            _target_role(session, workspace_id, target_user_id)
            session.execute(delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id, WorkspaceMembership.user_id == target_user_id))
            session.flush()
            check_workspace_invariants(session, workspace_id)


def change_role(session_factory, actor: Principal, workspace_id: uuid.UUID, target_user_id: uuid.UUID, new_role: str) -> None:
    """Owner-only. Changes a NON-owner member between ``admin`` and ``member``. No viewer, no owner transfer."""
    if not isinstance(new_role, str) or new_role not in ROLES:
        raise AuthorizationError("unsupported_role")
    with session_factory() as session:
        with session.begin():
            _owner_operation(session, actor, workspace_id)
            if new_role == OWNER:
                raise AuthorizationError("owner_transfer_unsupported")  # would create a second owner
            _target_role(session, workspace_id, target_user_id)
            session.execute(update(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id, WorkspaceMembership.user_id == target_user_id
            ).values(role=new_role))
            session.flush()
            check_workspace_invariants(session, workspace_id)


def create_team_workspace(session_factory, principal: Principal, name: str) -> uuid.UUID:
    """M6: a shared (non-personal) workspace; the caller becomes its sole owner. The owner invariant is verified before commit."""
    _validate(principal, uuid.uuid4())
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise AuthorizationError("invalid_name")
    with session_factory() as session:
        with session.begin():
            workspace = Workspace(name=name.strip(), is_personal=False, personal_for_user_id=None)
            session.add(workspace)
            session.flush()
            session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=principal.user_id, role=OWNER))
            session.flush()
            check_workspace_invariants(session, workspace.id)
            return workspace.id


def add_member(session_factory, actor: Principal, workspace_id: uuid.UUID, email: str, role: str) -> uuid.UUID:
    """M6, owner-only (deny by default, like every membership change): add an EXISTING user (one who has signed in once) to a SHARED workspace as ``admin`` or
    ``member``. Never owner (one owner, no transfer), never into a personal workspace. The email is only a lookup aid; the membership is of the internal user id."""
    if role not in ("admin", "member"):
        raise AuthorizationError("unsupported_role")
    if not isinstance(email, str) or not email.strip() or len(email) > 320:
        raise AuthorizationError("invalid_target")
    with session_factory() as session:
        with session.begin():
            _owner_operation(session, actor, workspace_id)
            if session.execute(select(Workspace.is_personal).where(Workspace.id == workspace_id)).scalar():
                raise AuthorizationError("personal_workspace")
            target = session.execute(select(User).where(User.email_normalized == normalize_email(email), User.is_active.is_(True))).scalar_one_or_none()
            if target is None:
                raise AuthorizationError("target_unknown")
            if session.execute(select(WorkspaceMembership.user_id).where(WorkspaceMembership.workspace_id == workspace_id, WorkspaceMembership.user_id == target.id)).first() is not None:
                raise AuthorizationError("already_a_member")
            session.add(WorkspaceMembership(workspace_id=workspace_id, user_id=target.id, role=role))
            session.flush()
            check_workspace_invariants(session, workspace_id)
            return target.id


def list_memberships(session_factory, principal: Principal) -> list[dict]:
    """The caller's own workspaces with their role (never anyone else's)."""
    _validate(principal, uuid.uuid4())
    with session_factory() as session:
        rows = session.execute(select(Workspace.id, Workspace.name, Workspace.is_personal, WorkspaceMembership.role, _state(Workspace.id)[1])
                               .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id).where(WorkspaceMembership.user_id == principal.user_id)
                               .order_by(Workspace.is_personal.desc(), Workspace.created_at, Workspace.id)).all()
    return [{"id": str(r[0]), "name": r[1], "personal": bool(r[2]), "role": r[3], "members": r[4]} for r in rows]


def list_members(session_factory, principal: Principal, workspace_id: uuid.UUID) -> list[dict]:
    """The members of a workspace the caller belongs to (any role may see who shares it)."""
    get_membership(session_factory, principal, workspace_id)
    with session_factory() as session:
        rows = session.execute(select(User.email, WorkspaceMembership.role).join(User, User.id == WorkspaceMembership.user_id)
                               .where(WorkspaceMembership.workspace_id == workspace_id).order_by(WorkspaceMembership.created_at, User.email)).all()
    return [{"email": r[0], "role": r[1]} for r in rows]


def reject_workspace_header(value) -> None:
    """``X-Workspace-Id`` stays reserved: any non-empty value is refused until M1-F wires the approved behaviour."""
    if value is not None and value != "":
        raise AuthorizationError("workspace_header_not_supported")
