"""persistent operational alert state

Revision ID: 0007_operational_alert_states
Revises: 0006_request_budget_states
Create Date: 2026-07-11 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

import books_of_time.db.types
from alembic import op

revision: str = "0007_operational_alert_states"
down_revision: str | Sequence[str] | None = "0006_request_budget_states"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE scheduledjobkind "
                "ADD VALUE IF NOT EXISTS 'operational_alert_evaluation'"
            )
    op.create_table(
        "operational_alert_states",
        sa.Column("alert_key", sa.Text(), nullable=False),
        sa.Column("alert_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("details", books_of_time.db.types.json_dict_type, nullable=False),
        sa.Column(
            "first_triggered_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=False,
        ),
        sa.Column(
            "last_evaluated_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=False,
        ),
        sa.Column(
            "last_triggered_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=False,
        ),
        sa.Column(
            "last_notified_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
        sa.Column("resolved_at", books_of_time.db.types.UTCDateTime(), nullable=True),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=False,
            comment="记录创建时间",
        ),
        sa.Column(
            "updated_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=False,
            comment="记录最后更新时间",
        ),
        sa.PrimaryKeyConstraint("alert_key"),
    )
    op.create_index(
        "idx_operational_alert_states_status_severity",
        "operational_alert_states",
        ["status", "severity"],
        unique=False,
    )
    op.create_index(
        "idx_operational_alert_states_type",
        "operational_alert_states",
        ["alert_type"],
        unique=False,
    )
    op.drop_index(
        "idx_comment_analysis_flags_event_type",
        table_name="comment_analysis_flags",
    )
    op.create_index(
        "idx_comment_analysis_flags_event_type",
        "comment_analysis_flags",
        ["event_id", "flag_type", sa.text("detected_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM scheduled_jobs WHERE job_kind = 'operational_alert_evaluation'"
        )
    )
    op.drop_index(
        "idx_comment_analysis_flags_event_type",
        table_name="comment_analysis_flags",
    )
    op.create_index(
        "idx_comment_analysis_flags_event_type",
        "comment_analysis_flags",
        ["event_id", "flag_type", "detected_at"],
        unique=False,
    )
    op.drop_index(
        "idx_operational_alert_states_type",
        table_name="operational_alert_states",
    )
    op.drop_index(
        "idx_operational_alert_states_status_severity",
        table_name="operational_alert_states",
    )
    op.drop_table("operational_alert_states")
    # PostgreSQL cannot remove one enum value without rebuilding the type.
