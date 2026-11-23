"""agent slugs are unique per (workspace, user), not per workspace (starter workforce for every member)

Revision ID: 0018_agent_slug_per_user
Revises: 0017_invitations
Create Date: 2026-11-22

THE DEFECT. ``custom_agents`` rows are PRIVATE to the (user, workspace) that created them (every read and every resolve filters on both), yet the slug was unique per
WORKSPACE. In a shared workspace the first member to add the standard starter workforce took ``unit-economics-analyst``, ``launch-planner`` and ``launch-comms``; for everyone
else the insert hit the unique constraint, the route reported ``created: false`` and they were left with no agents (planning then failed).

THE CHANGE. ``UNIQUE (workspace_id, slug)`` becomes ``UNIQUE (workspace_id, user_id, slug)``. This only RELAXES a constraint: every existing row still satisfies it, nothing is
rewritten, no agent changes owner and no personal agent becomes visible to anyone else (visibility is the unchanged ``_owned`` filter). Two members may now each own an agent
with the same slug; a slug still identifies exactly one agent per (workspace, user).

``downgrade()`` restores the old constraint and REFUSES (changes nothing) when two users of one workspace already share a slug, because that data cannot satisfy it.
SQLite (tests and the throw-away E2E stack only; production is PostgreSQL) rebuilds the table in a batch with foreign keys deferred.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0018_agent_slug_per_user"
down_revision: Union[str, None] = "0017_invitations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = "uq_custom_agents_workspace_id_slug"
NEW = "uq_custom_agents_workspace_id_user_id_slug"


def _swap(drop: str, create: str, columns: list[str]) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("PRAGMA defer_foreign_keys=ON")
        with op.batch_alter_table("custom_agents", recreate="always") as batch:
            batch.drop_constraint(drop, type_="unique")
            batch.create_unique_constraint(create, columns)
    else:
        op.drop_constraint(drop, "custom_agents", type_="unique")
        op.create_unique_constraint(create, "custom_agents", columns)


def upgrade() -> None:
    _swap(OLD, NEW, ["workspace_id", "user_id", "slug"])


def downgrade() -> None:
    clash = op.get_bind().execute(sa.text("SELECT 1 FROM custom_agents GROUP BY workspace_id, slug HAVING COUNT(*) > 1 LIMIT 1")).first()
    if clash is not None:
        raise RuntimeError("cannot downgrade 0018: two users of one workspace own an agent with the same slug; rename or archive-and-recreate one first")
    _swap(NEW, OLD, ["workspace_id", "slug"])
