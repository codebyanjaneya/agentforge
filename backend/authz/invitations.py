"""Workspace invitations (sprint 5, migration 0017): add someone who has not signed in yet.

The token is 256 random bits, returned ONCE to the inviter; only its SHA-256 is stored. Accepting needs a signed-in person whose verified e-mail equals the invited address (a forwarded link alone is
not enough), an open (not accepted, not revoked, not expired) invitation, and consumes it with a compare-and-swap in the SAME transaction that creates the membership (single use, race-safe).
Every accept failure is one indistinguishable error (``invalid_invitation``). Creating/revoking/listing and member management stay owner-only (deny by default), like ``add_member``.
No e-mail is sent here: the inviter shares the link (shortcut: add a Resend send behind EMAIL_ALLOWED_DOMAINS once a sandbox key exists)."""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from auth.principal import Principal
from auth.provisioning import normalize_email
from authz.errors import AuthorizationError
from authz.workspaces import OWNER, _owner_operation, _target_role, _validate, check_workspace_invariants
from memory.models import User, Workspace, WorkspaceInvitation, WorkspaceMembership

TTL = timedelta(days=7)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_invitation(sf, actor: Principal, workspace_id: uuid.UUID, email: str, role: str, now: datetime | None = None) -> dict:
    if role not in ("admin", "member"):
        raise AuthorizationError("unsupported_role")
    if not isinstance(email, str) or "@" not in email or len(email) > 320:
        raise AuthorizationError("invalid_target")
    norm, now = normalize_email(email), now or _now()
    with sf() as session, session.begin():
        _owner_operation(session, actor, workspace_id)
        if session.execute(select(Workspace.is_personal).where(Workspace.id == workspace_id)).scalar():
            raise AuthorizationError("personal_workspace")
        if session.execute(select(WorkspaceMembership.user_id).join(User, User.id == WorkspaceMembership.user_id)
                           .where(WorkspaceMembership.workspace_id == workspace_id, User.email_normalized == norm)).first():
            raise AuthorizationError("already_a_member")
        session.execute(update(WorkspaceInvitation).where(WorkspaceInvitation.workspace_id == workspace_id, WorkspaceInvitation.email_normalized == norm,
                                                          WorkspaceInvitation.accepted_at.is_(None), WorkspaceInvitation.revoked_at.is_(None)).values(revoked_at=now))
        token = secrets.token_urlsafe(32)
        inv = WorkspaceInvitation(workspace_id=workspace_id, email_normalized=norm, role=role, token_hash=_hash(token), invited_by=actor.user_id, created_at=now, expires_at=now + TTL)
        session.add(inv)
        session.flush()
        return {"id": str(inv.id), "email": norm, "role": role, "expires_at": inv.expires_at.isoformat(), "token": token}


def list_invitations(sf, actor: Principal, workspace_id: uuid.UUID, now: datetime | None = None) -> list[dict]:
    now = now or _now()
    with sf() as session, session.begin():
        _owner_operation(session, actor, workspace_id)
        rows = session.execute(select(WorkspaceInvitation).where(WorkspaceInvitation.workspace_id == workspace_id, WorkspaceInvitation.accepted_at.is_(None),
                                                                 WorkspaceInvitation.revoked_at.is_(None), WorkspaceInvitation.expires_at > now)
                               .order_by(WorkspaceInvitation.created_at)).scalars().all()
        return [{"id": str(r.id), "email": r.email_normalized, "role": r.role, "expires_at": r.expires_at.isoformat()} for r in rows]  # never the token or its hash


def revoke_invitation(sf, actor: Principal, workspace_id: uuid.UUID, invitation_id: uuid.UUID, now: datetime | None = None) -> None:
    with sf() as session, session.begin():
        _owner_operation(session, actor, workspace_id)
        n = session.execute(update(WorkspaceInvitation).where(WorkspaceInvitation.id == invitation_id, WorkspaceInvitation.workspace_id == workspace_id,
                                                              WorkspaceInvitation.accepted_at.is_(None), WorkspaceInvitation.revoked_at.is_(None))
                            .values(revoked_at=now or _now())).rowcount
        if n != 1:
            raise AuthorizationError("target_unknown")


def accept_invitation(sf, principal: Principal, token: str, now: datetime | None = None) -> dict:
    _validate(principal, uuid.uuid4())
    now = now or _now()
    if not isinstance(token, str) or not 20 <= len(token) <= 200 or not principal.email:
        raise AuthorizationError("invalid_invitation")
    with sf() as session, session.begin():
        inv = session.execute(select(WorkspaceInvitation).where(WorkspaceInvitation.token_hash == _hash(token))).scalar_one_or_none()
        if inv is None or normalize_email(principal.email) != inv.email_normalized:
            raise AuthorizationError("invalid_invitation")
        session.execute(select(Workspace.id).where(Workspace.id == inv.workspace_id).with_for_update())  # one membership change at a time
        won = session.execute(update(WorkspaceInvitation).where(WorkspaceInvitation.id == inv.id, WorkspaceInvitation.accepted_at.is_(None), WorkspaceInvitation.revoked_at.is_(None),
                                                                WorkspaceInvitation.expires_at > now).values(accepted_at=now, accepted_by=principal.user_id)).rowcount
        if won != 1:
            raise AuthorizationError("invalid_invitation")
        if session.execute(select(WorkspaceMembership.user_id).where(WorkspaceMembership.workspace_id == inv.workspace_id, WorkspaceMembership.user_id == principal.user_id)).first():
            raise AuthorizationError("invalid_invitation")  # already inside: nothing changes
        session.add(WorkspaceMembership(workspace_id=inv.workspace_id, user_id=principal.user_id, role=inv.role))
        session.flush()
        check_workspace_invariants(session, inv.workspace_id)
        name = session.execute(select(Workspace.name).where(Workspace.id == inv.workspace_id)).scalar()
        return {"workspace_id": str(inv.workspace_id), "name": name, "role": inv.role}


def _user_id(session, email: str) -> uuid.UUID:
    uid = session.execute(select(User.id).where(User.email_normalized == normalize_email(email))).scalar()
    if uid is None:
        raise AuthorizationError("target_unknown")
    return uid


def remove_member_by_email(sf, actor: Principal, workspace_id: uuid.UUID, email: str) -> None:
    with sf() as session, session.begin():
        _owner_operation(session, actor, workspace_id)
        uid = _user_id(session, email)
        _target_role(session, workspace_id, uid)
        session.execute(delete(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace_id, WorkspaceMembership.user_id == uid))
        session.flush()
        check_workspace_invariants(session, workspace_id)


def change_role_by_email(sf, actor: Principal, workspace_id: uuid.UUID, email: str, role: str) -> None:
    if role not in ("admin", "member"):
        raise AuthorizationError("unsupported_role")
    with sf() as session, session.begin():
        _owner_operation(session, actor, workspace_id)
        uid = _user_id(session, email)
        _target_role(session, workspace_id, uid)
        session.execute(update(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace_id, WorkspaceMembership.user_id == uid).values(role=role))
        session.flush()
        check_workspace_invariants(session, workspace_id)


def leave_workspace(sf, principal: Principal, workspace_id: uuid.UUID) -> None:
    """Any non-owner member may leave a shared workspace; the owner can not (one owner, no transfer)."""
    _validate(principal, workspace_id)
    with sf() as session, session.begin():
        if session.execute(select(Workspace.id).where(Workspace.id == workspace_id, Workspace.is_personal.is_(False)).with_for_update()).first() is None:
            raise AuthorizationError("not_a_member")
        role = session.execute(select(WorkspaceMembership.role).where(WorkspaceMembership.workspace_id == workspace_id, WorkspaceMembership.user_id == principal.user_id)).scalar()
        if role is None:
            raise AuthorizationError("not_a_member")
        if role == OWNER:
            raise AuthorizationError("sole_owner_protected")
        session.execute(delete(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace_id, WorkspaceMembership.user_id == principal.user_id))
        session.flush()
        check_workspace_invariants(session, workspace_id)
