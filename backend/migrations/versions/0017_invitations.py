"""workspace invitations: a secure way to add people who have not signed in yet (sprint 5)

Revision ID: 0017_invitations
Revises: 0016_durable_dispatch
Create Date: 2026-11-21

THE PROBLEM. Only an EXISTING user could be added to a workspace (the add-member route looked the e-mail up among people who had already signed in once). A new teammate could not be invited.

``workspace_invitations``: one row per invitation. The secret is a 256-bit random token that is shown ONCE to the inviter (and optionally e-mailed); only its SHA-256 (``token_hash``, unique) is stored, so a database
read never yields a usable link. ``email_normalized`` is who it is for: accepting requires a signed-in person whose VERIFIED e-mail equals it (a forwarded link alone is not enough). ``role`` is admin or member
(never owner). An invitation expires (``expires_at`` > ``created_at``), can be revoked (``revoked_at``) and is single-use (``accepted_at``/``accepted_by``, set by a compare-and-swap in the same transaction that
creates the membership); an accepted invitation can never also be revoked. At most ONE open invitation per workspace and e-mail (a partial unique index). Nothing here changes existing tables.

``downgrade()`` drops the table (pending invitations are lost). Production policy is forward-only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0017_invitations"
down_revision: Union[str, None] = "0016_durable_dispatch"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OPEN = "accepted_at IS NULL AND revoked_at IS NULL"


def upgrade() -> None:
    op.create_table(
        "workspace_invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("email_normalized", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("accepted_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="workspace_invitations_pkey"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_workspace_invitations_workspace_id_workspaces"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], name="fk_workspace_invitations_invited_by_users"),
        sa.ForeignKeyConstraint(["accepted_by"], ["users.id"], name="fk_workspace_invitations_accepted_by_users"),
        sa.UniqueConstraint("token_hash", name="uq_workspace_invitations_token_hash"),
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_workspace_invitations_role"),
        sa.CheckConstraint("expires_at > created_at", name="ck_workspace_invitations_expiry"),
        sa.CheckConstraint("NOT (accepted_at IS NOT NULL AND revoked_at IS NOT NULL)", name="ck_workspace_invitations_single_outcome"),
        sa.CheckConstraint("(accepted_at IS NULL) = (accepted_by IS NULL)", name="ck_workspace_invitations_acceptor"),
    )
    op.create_index("uq_workspace_invitations_open", "workspace_invitations", ["workspace_id", "email_normalized"], unique=True, sqlite_where=sa.text(OPEN), postgresql_where=sa.text(OPEN))
    op.create_index("ix_workspace_invitations_workspace_id", "workspace_invitations", ["workspace_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_workspace_invitations_workspace_id", table_name="workspace_invitations")
    op.drop_index("uq_workspace_invitations_open", table_name="workspace_invitations")
    op.drop_table("workspace_invitations")
