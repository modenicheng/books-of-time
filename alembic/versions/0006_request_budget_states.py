"""database-coordinated request budget states

Revision ID: 0006_request_budget_states
Revises: 0005_brin_time_indexes
Create Date: 2026-07-11 10:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

import books_of_time.db.types
from alembic import op

revision: str = "0006_request_budget_states"
down_revision: str | Sequence[str] | None = "0005_brin_time_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "request_budget_states",
        sa.Column("budget_key", sa.Text(), nullable=False),
        sa.Column("tokens", sa.Float(), nullable=False),
        sa.Column("refill_rate", sa.Float(), nullable=False),
        sa.Column("burst", sa.Integer(), nullable=False),
        sa.Column(
            "last_refill_at", books_of_time.db.types.UTCDateTime(), nullable=False
        ),
        sa.Column("created_at", books_of_time.db.types.UTCDateTime(), nullable=False),
        sa.Column("updated_at", books_of_time.db.types.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("budget_key"),
    )


def downgrade() -> None:
    op.drop_table("request_budget_states")
