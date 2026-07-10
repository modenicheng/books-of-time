"""account Cookie refresh scheduled job kind

Revision ID: 0003_account_cookie_refresh_job
Revises: 0002_event_archive
Create Date: 2026-07-10 20:02:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_account_cookie_refresh_job"
down_revision: str | Sequence[str] | None = "0002_event_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE scheduledjobkind "
                "ADD VALUE IF NOT EXISTS 'account_cookie_refresh'"
            )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM scheduled_jobs WHERE job_kind = 'account_cookie_refresh'")
    )
    # PostgreSQL cannot remove one enum value without rebuilding the type. The
    # unused value is intentionally retained so downgrade remains data-safe.
