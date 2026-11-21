"""Identity resolution and first-valid-token provisioning (D019).

A verified token is resolved ONLY through ``(provider, provider_subject)`` (the unique constraint
``uq_auth_identities_provider_provider_subject``). Email is an attribute: it never selects, links or merges
users, and an email change never moves an identity. An inactive user is rejected and never reactivated.

When the identity does not exist, ONE transaction creates, together: the ``users`` row, its personal
``workspaces`` row (``is_personal`` true, ``personal_for_user_id`` set), the ``workspace_memberships`` owner row
and the ``auth_identities`` row (inserted last, so a lost race fails at the unique constraint). If anything
fails, all four are rolled back.

Concurrency: the unique constraint is the final arbiter. A losing transaction is rolled back, and the identity
is re-read in a FRESH session (a failed transaction is never reused). Only a violation of that specific
constraint is treated as "someone else provisioned it"; every other integrity error propagates unchanged.

There are no writes to refresh activity timestamps. The only write on an existing identity is updating the
stored email attribute when a verified token carries a different one (best effort: a failure there never blocks
authentication and never affects identity ownership).
"""

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from auth.errors import InactiveUserError
from auth.principal import Principal
from auth.tokens import VerifiedClaims
from memory.models import AuthIdentity, User, Workspace, WorkspaceMembership

logger = logging.getLogger("agentforge.auth")

IDENTITY_UNIQUE = "uq_auth_identities_provider_provider_subject"
OWNER_ROLE = "owner"
PERSONAL_WORKSPACE_NAME = "Personal workspace"


def normalize_email(email: str) -> str:
    """Lookup aid only (users.email_normalized); never an identity key."""
    return email.strip().casefold()


def _is_identity_conflict(exc: IntegrityError) -> bool:
    """True only for a violation of the (provider, provider_subject) unique constraint."""
    orig = exc.orig
    constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)  # PostgreSQL (psycopg2)
    if constraint is not None:
        return constraint == IDENTITY_UNIQUE
    # SQLite reports the columns instead of the constraint name.
    return "UNIQUE constraint failed: auth_identities.provider, auth_identities.provider_subject" in str(orig)


def _find(session_factory, claims: VerifiedClaims):
    """Return ``(Principal, is_active)`` for the identity, or None. Reads in its own session."""
    with session_factory() as session:
        row = session.execute(
            select(AuthIdentity, User)
            .join(User, User.id == AuthIdentity.user_id)
            .where(AuthIdentity.provider == claims.provider, AuthIdentity.provider_subject == claims.subject)
        ).first()
        if row is None:
            return None
        _identity, user = row
        active = bool(user.is_active)
        if active and user.email != claims.email:  # attribute only: ownership of the identity never changes
            _refresh_email_attribute(session, user, claims.email)
        return Principal(
            user_id=user.id,
            provider=claims.provider,
            provider_subject=claims.subject,
            email=claims.email,  # the verified attribute from the token, never a request field
            is_active=active,
        ), active


def _refresh_email_attribute(session, user: User, email: str) -> None:
    try:
        user.email = email
        user.email_normalized = normalize_email(email)
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        logger.warning("Could not refresh a stored email attribute (%s)", type(exc).__name__)


def _provision(session_factory, claims: VerifiedClaims) -> None:
    """Create user, personal workspace, owner membership and identity in ONE transaction."""
    with session_factory() as session:
        with session.begin():
            user = User(email=claims.email, email_normalized=normalize_email(claims.email), is_active=True)
            session.add(user)
            session.flush()
            workspace = Workspace(name=PERSONAL_WORKSPACE_NAME, is_personal=True, personal_for_user_id=user.id)
            session.add(workspace)
            session.flush()
            session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role=OWNER_ROLE))
            session.flush()
            session.add(
                AuthIdentity(
                    user_id=user.id,
                    provider=claims.provider,
                    provider_subject=claims.subject,
                    email_at_link=claims.email,
                )
            )
            session.flush()  # a lost race fails here, at the unique constraint; the block then rolls back


def resolve_principal(session_factory, claims: VerifiedClaims) -> Principal:
    """Resolve (provisioning on first use) the internal user for verified ``claims``.

    Raises InactiveUserError for a deactivated user. Any integrity error other than a lost
    (provider, provider_subject) race propagates unchanged (after its transaction was rolled back).
    """
    found = _find(session_factory, claims)
    if found is None:
        try:
            _provision(session_factory, claims)
        except IntegrityError as exc:
            if not _is_identity_conflict(exc):
                raise
            # Lost the race: the winner's committed identity is re-read in a fresh session below.
        found = _find(session_factory, claims)
        if found is None:  # cannot happen unless the winner's row vanished: never pretend success
            raise RuntimeError("identity could not be resolved after provisioning")
    principal, active = found
    if not active:
        raise InactiveUserError("inactive_user")
    return principal
