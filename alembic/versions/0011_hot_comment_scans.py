"""hot comment scan runs and numbered task slices

Revision ID: 0011_hot_comment_scans
Revises: 0010_snapshot_cohort_planning_job
Create Date: 2026-07-14 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

import books_of_time.db.types
from alembic import op

revision: str = "0011_hot_comment_scans"
down_revision: str | Sequence[str] | None = "0010_snapshot_cohort_planning_job"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _bigint_pk() -> sa.types.TypeEngine:
    return sa.Integer().with_variant(sa.BigInteger(), "postgresql")


def _scan_mode_enum() -> sa.Enum:
    return sa.Enum(
        "hot_core",
        "hot_deep",
        "baseline_tail",
        "baseline_head_sweep",
        "incremental",
        "full_reconciliation",
        "segmented_reconciliation",
        "reply_refresh",
        "visibility_probe",
        name="commentscanmode",
    )


def _scan_status_enum() -> sa.Enum:
    return sa.Enum(
        "planned",
        "running",
        "paused",
        "complete",
        "partial",
        "failed",
        "corrupted",
        name="commentscanstatus",
    )


def upgrade() -> None:
    op.create_table(
        "comment_scan_runs",
        sa.Column("id", _bigint_pk(), autoincrement=True, nullable=False),
        sa.Column("scan_key", sa.Text(), nullable=False, unique=True),
        sa.Column("bvid", sa.Text(), nullable=False),
        sa.Column("oid", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_cohort_id", sa.BigInteger(), nullable=True),
        sa.Column("parent_scan_run_id", sa.BigInteger(), nullable=True),
        sa.Column("mode", _scan_mode_enum(), nullable=False),
        sa.Column(
            "status",
            _scan_status_enum(),
            server_default="planned",
            nullable=False,
        ),
        sa.Column("outcome", sa.String(length=64), nullable=True),
        sa.Column(
            "started_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
        sa.Column(
            "finished_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
        sa.Column("start_frontier_rpid", sa.BigInteger(), nullable=True),
        sa.Column("result_frontier_rpid", sa.BigInteger(), nullable=True),
        sa.Column(
            "start_anchor_set",
            books_of_time.db.types.json_dict_type,
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column(
            "result_anchor_set",
            books_of_time.db.types.json_dict_type,
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("start_cursor", sa.Text(), nullable=True),
        sa.Column("result_cursor", sa.Text(), nullable=True),
        sa.Column("target_pages", sa.Integer(), nullable=True),
        sa.Column("next_page_number", sa.Integer(), nullable=True),
        sa.Column(
            "pages_requested",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "pages_succeeded",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "items_observed",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "raw_payloads_saved",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "slice_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "truncated",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("last_error_type", sa.String(length=120), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column(
            "extra",
            books_of_time.db.types.json_dict_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            books_of_time.db.types.UTCDateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            books_of_time.db.types.UTCDateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "target_pages IS NULL OR target_pages >= 0",
            name="ck_comment_scan_runs_target_pages",
        ),
        sa.CheckConstraint(
            "next_page_number IS NULL OR next_page_number > 0",
            name="ck_comment_scan_runs_next_page",
        ),
        sa.CheckConstraint(
            "pages_requested >= 0",
            name="ck_comment_scan_runs_pages_requested",
        ),
        sa.CheckConstraint(
            "pages_succeeded >= 0",
            name="ck_comment_scan_runs_pages_succeeded",
        ),
        sa.CheckConstraint(
            "items_observed >= 0",
            name="ck_comment_scan_runs_items_observed",
        ),
        sa.CheckConstraint(
            "raw_payloads_saved >= 0",
            name="ck_comment_scan_runs_raw_payloads",
        ),
        sa.CheckConstraint(
            "slice_count >= 0",
            name="ck_comment_scan_runs_slice_count",
        ),
        sa.CheckConstraint(
            "pages_succeeded <= pages_requested",
            name="ck_comment_scan_runs_page_counts",
        ),
        sa.ForeignKeyConstraint(
            ["bvid"],
            ["known_videos.bvid"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_cohort_id"],
            ["snapshot_cohorts.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_scan_run_id"],
            ["comment_scan_runs.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["policy_version"],
            ["collection_policy_versions.version"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_comment_scan_runs_bvid_mode_status",
        "comment_scan_runs",
        ["bvid", "mode", "status"],
        unique=False,
    )
    op.create_index(
        "idx_comment_scan_runs_cohort",
        "comment_scan_runs",
        ["snapshot_cohort_id"],
        unique=False,
    )
    op.create_index(
        "idx_comment_scan_runs_status_updated",
        "comment_scan_runs",
        ["status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "idx_snapshot_cohort_components_scan_run",
        "snapshot_cohort_components",
        ["comment_scan_run_id"],
        unique=False,
    )

    with op.batch_alter_table("collection_tasks") as batch_op:
        batch_op.add_column(sa.Column("comment_scan_run_id", sa.BigInteger()))
        batch_op.add_column(sa.Column("scan_slice_no", sa.Integer()))
        batch_op.add_column(sa.Column("scan_slice_key", sa.Text()))
        batch_op.create_check_constraint(
            "ck_collection_tasks_scan_slice_no",
            "scan_slice_no IS NULL OR scan_slice_no >= 0",
        )
        batch_op.create_foreign_key(
            "fk_collection_tasks_comment_scan_run",
            "comment_scan_runs",
            ["comment_scan_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "uq_collection_tasks_scan_slice_key",
        "collection_tasks",
        ["scan_slice_key"],
        unique=True,
    )
    op.create_index(
        "idx_collection_tasks_scan_run_slice",
        "collection_tasks",
        ["comment_scan_run_id", "scan_slice_no"],
        unique=False,
    )

    with op.batch_alter_table("collection_coverage_stats") as batch_op:
        batch_op.add_column(sa.Column("comment_scan_run_id", sa.BigInteger()))
        batch_op.create_foreign_key(
            "fk_collection_coverage_comment_scan_run",
            "comment_scan_runs",
            ["comment_scan_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "idx_collection_coverage_scan_run",
        "collection_coverage_stats",
        ["comment_scan_run_id"],
        unique=False,
    )

    with op.batch_alter_table("raw_page_observations") as batch_op:
        batch_op.add_column(sa.Column("scan_run_id", sa.BigInteger()))
        batch_op.create_foreign_key(
            "fk_raw_page_observations_scan_run",
            "comment_scan_runs",
            ["scan_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "idx_raw_page_observations_scan_run",
        "raw_page_observations",
        ["scan_run_id"],
        unique=False,
    )

    with op.batch_alter_table("comment_observations") as batch_op:
        batch_op.add_column(sa.Column("scan_run_id", sa.BigInteger()))
        batch_op.create_foreign_key(
            "fk_comment_observations_scan_run",
            "comment_scan_runs",
            ["scan_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "idx_comment_observations_scan_run",
        "comment_observations",
        ["scan_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_snapshot_cohort_components_scan_run",
        table_name="snapshot_cohort_components",
    )

    op.drop_index(
        "idx_comment_observations_scan_run",
        table_name="comment_observations",
    )
    with op.batch_alter_table("comment_observations") as batch_op:
        batch_op.drop_constraint(
            "fk_comment_observations_scan_run",
            type_="foreignkey",
        )
        batch_op.drop_column("scan_run_id")

    op.drop_index(
        "idx_raw_page_observations_scan_run",
        table_name="raw_page_observations",
    )
    with op.batch_alter_table("raw_page_observations") as batch_op:
        batch_op.drop_constraint(
            "fk_raw_page_observations_scan_run",
            type_="foreignkey",
        )
        batch_op.drop_column("scan_run_id")

    op.drop_index(
        "idx_collection_coverage_scan_run",
        table_name="collection_coverage_stats",
    )
    with op.batch_alter_table("collection_coverage_stats") as batch_op:
        batch_op.drop_constraint(
            "fk_collection_coverage_comment_scan_run",
            type_="foreignkey",
        )
        batch_op.drop_column("comment_scan_run_id")

    op.drop_index(
        "idx_collection_tasks_scan_run_slice",
        table_name="collection_tasks",
    )
    op.drop_index(
        "uq_collection_tasks_scan_slice_key",
        table_name="collection_tasks",
    )
    with op.batch_alter_table("collection_tasks") as batch_op:
        batch_op.drop_constraint(
            "fk_collection_tasks_comment_scan_run",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "ck_collection_tasks_scan_slice_no",
            type_="check",
        )
        batch_op.drop_column("scan_slice_key")
        batch_op.drop_column("scan_slice_no")
        batch_op.drop_column("comment_scan_run_id")

    op.drop_index(
        "idx_comment_scan_runs_status_updated",
        table_name="comment_scan_runs",
    )
    op.drop_index("idx_comment_scan_runs_cohort", table_name="comment_scan_runs")
    op.drop_index(
        "idx_comment_scan_runs_bvid_mode_status",
        table_name="comment_scan_runs",
    )
    op.drop_table("comment_scan_runs")

    if op.get_bind().dialect.name == "postgresql":
        _scan_status_enum().drop(op.get_bind(), checkfirst=True)
        _scan_mode_enum().drop(op.get_bind(), checkfirst=True)
