"""workspaces and workspace_memberships (M1-B)

Revision ID: 0003_workspaces
Revises: 0002_identity
Create Date: 2026-11-14

Adds the workspace schema only: ``workspaces`` and ``workspace_memberships``. Nothing existing is
altered, no rows are created (no personal workspace, no membership, no backfill), and there are no
server defaults (UUIDs and timestamps come from application code).

* ``workspaces.personal_for_user_id`` is nullable and UNIQUE: one personal workspace per user,
  while any number of team workspaces (NULL) remain valid. A row-level CHECK keeps ``is_personal``
  and ``personal_for_user_id`` consistent. That every workspace has an owner membership is a
  service-level invariant (no CHECK can express it); this migration does not enforce it.
* ``workspace_memberships`` has the composite primary key ``(workspace_id, user_id)`` and a CHECK
  restricting ``role`` to owner, admin or member (no viewer).

This schema authorizes nothing: ``X-Workspace-Id`` stays rejected throughout M1.

``downgrade()`` removes exactly these objects and restores the 0002_identity schema; it is for
development and test. Production policy is forward-only (restore from a verified backup).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003_workspaces"
down_revision: Union[str, None] = "0002_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("is_personal", sa.Boolean(), nullable=False),
        sa.Column("personal_for_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="workspaces_pkey"),
        sa.UniqueConstraint("personal_for_user_id", name="uq_workspaces_personal_for_user_id"),
        sa.ForeignKeyConstraint(["personal_for_user_id"], ["users.id"], name="fk_workspaces_personal_for_user_id_users"),
        sa.CheckConstraint(
            "(is_personal AND personal_for_user_id IS NOT NULL) OR (NOT is_personal AND personal_for_user_id IS NULL)",
            name="ck_workspaces_personal_consistency",
        ),
    )

    op.create_table(
        "workspace_memberships",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", name="workspace_memberships_pkey"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_workspace_memberships_workspace_id_workspaces"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_workspace_memberships_user_id_users"),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_workspace_memberships_role"),
    )
    op.create_index("ix_workspace_memberships_user_id", "workspace_memberships", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_workspace_memberships_user_id", table_name="workspace_memberships")
    op.drop_table("workspace_memberships")
    op.drop_table("workspaces")
