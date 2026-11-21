"""run leases: durable ownership of running plan runs (M4-A1)

Revision ID: 0009_run_leases
Revises: 0008_plan_runs
Create Date: 2026-11-17

Adds ONE table, ``run_leases``; no existing table, index, constraint or trigger is touched (the ``plan_runs`` triggers of 0008
freeze a running run, which is why ownership cannot live there):

* ``run_id``               primary key and foreign key to ``plan_runs.id`` (NO ACTION; nothing is cascaded or deleted);
* ``owner_id``             the process that drives the run;
* ``generation``           fencing token (>= 1), increased by every takeover;
* ``expires_at``           the lease deadline (compared against database time on PostgreSQL);
* ``cancel_requested_at``  the persisted cancellation request;
* ``created_at`` / ``updated_at``.

Existing data is preserved. A run that is ``running`` and has NO lease row (it predates this migration, or its process died
before the lease was written) is treated as an expired lease by recovery: the same outcome the old startup sweep produced.

``downgrade()`` drops the table and discards the lease rows only. Runs and task runs are not touched; with the 0008 code a
run left ``running`` is closed by that version's startup sweep. Production policy is forward-only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0009_run_leases"
down_revision: Union[str, None] = "0008_plan_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "run_leases",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.String(length=100), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", name="run_leases_pkey"),
        sa.ForeignKeyConstraint(["run_id"], ["plan_runs.id"], name="fk_run_leases_run_id_plan_runs"),
        sa.CheckConstraint("generation >= 1", name="ck_run_leases_generation_positive"),
    )
    op.create_index("ix_run_leases_expires_at", "run_leases", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_run_leases_expires_at", table_name="run_leases")
    op.drop_table("run_leases")
