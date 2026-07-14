"""snapshot cohort planning scheduled job kind

Revision ID: 0010_snapshot_cohort_planning_job
Revises: 0009_cohort_state_and_policy
Create Date: 2026-07-14 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_snapshot_cohort_planning_job"
down_revision: str | Sequence[str] | None = "0009_cohort_state_and_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE scheduledjobkind "
                "ADD VALUE IF NOT EXISTS 'snapshot_cohort_planning'"
            )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM scheduled_jobs WHERE job_kind = 'snapshot_cohort_planning'"
        )
    )
    # PostgreSQL cannot remove one enum value without rebuilding the type. The
    # unused value is intentionally retained so downgrade remains data-safe.
