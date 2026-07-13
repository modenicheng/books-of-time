"""cohort state and policy

Revision ID: 0009_cohort_state_and_policy
Revises: 0008_collection_evidence_foundations
Create Date: 2026-07-14 03:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

import books_of_time.db.types
from alembic import op

revision: str = "0009_cohort_state_and_policy"
down_revision: str | Sequence[str] | None = "0008_collection_evidence_foundations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _bigint_pk() -> sa.types.TypeEngine:
    return sa.Integer().with_variant(sa.BigInteger(), "postgresql")


def _created_at_column() -> sa.Column:
    return sa.Column(
        "created_at",
        books_of_time.db.types.UTCDateTime(),
        server_default=sa.func.now(),
        nullable=False,
    )


def _updated_at_column() -> sa.Column:
    return sa.Column(
        "updated_at",
        books_of_time.db.types.UTCDateTime(),
        server_default=sa.func.now(),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "collection_policy_versions",
        sa.Column("id", _bigint_pk(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Text(), nullable=False, unique=True),
        sa.Column("policy_kind", sa.String(length=64), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column(
            "policy",
            books_of_time.db.types.json_dict_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "training_window_start",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
        sa.Column(
            "training_window_end",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
        sa.Column(
            "distinct_comment_count",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "complete_day_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "valid_exposure_minutes",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "excluded_comment_count",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "exclusion_reasons",
            books_of_time.db.types.json_dict_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("algorithm", sa.Text(), nullable=False),
        _created_at_column(),
        sa.Column(
            "activated_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
        sa.Column(
            "superseded_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope_type IN ('global', 'game')",
            name="ck_collection_policy_versions_scope_type",
        ),
        sa.CheckConstraint(
            "distinct_comment_count >= 0",
            name="ck_collection_policy_versions_distinct_comments",
        ),
        sa.CheckConstraint(
            "complete_day_count >= 0",
            name="ck_collection_policy_versions_complete_days",
        ),
        sa.CheckConstraint(
            "valid_exposure_minutes >= 0",
            name="ck_collection_policy_versions_exposure_minutes",
        ),
        sa.CheckConstraint(
            "excluded_comment_count >= 0",
            name="ck_collection_policy_versions_excluded_comments",
        ),
        sa.CheckConstraint(
            "training_window_end IS NULL OR training_window_start IS NULL "
            "OR training_window_end > training_window_start",
            name="ck_collection_policy_versions_training_window",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_collection_policy_versions_active_scope",
        "collection_policy_versions",
        ["policy_kind", "scope_type", "scope_id"],
        unique=True,
        sqlite_where=sa.text("active = 1"),
        postgresql_where=sa.text("active"),
    )

    op.create_table(
        "video_collection_states",
        sa.Column("bvid", sa.Text(), nullable=False),
        sa.Column(
            "desired_tier",
            sa.String(length=1),
            server_default="c",
            nullable=False,
        ),
        sa.Column(
            "effective_tier",
            sa.String(length=1),
            server_default="c",
            nullable=False,
        ),
        sa.Column("candidate_downgrade_tier", sa.String(length=1), nullable=True),
        sa.Column(
            "consecutive_downgrade_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("pinned_tier", sa.String(length=1), nullable=True),
        sa.Column(
            "life_stage",
            sa.String(length=16),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "schedule_anchor_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=False,
        ),
        sa.Column(
            "next_due_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
        sa.Column(
            "last_planned_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
        sa.Column(
            "last_completed_cohort_at",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
        sa.Column("last_checkpoint_hours", sa.Integer(), nullable=True),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column(
            "extra",
            books_of_time.db.types.json_dict_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        _created_at_column(),
        _updated_at_column(),
        sa.CheckConstraint(
            "desired_tier IN ('s', 'a', 'b', 'c')",
            name="ck_video_collection_states_desired_tier",
        ),
        sa.CheckConstraint(
            "effective_tier IN ('s', 'a', 'b', 'c')",
            name="ck_video_collection_states_effective_tier",
        ),
        sa.CheckConstraint(
            "candidate_downgrade_tier IS NULL "
            "OR candidate_downgrade_tier IN ('s', 'a', 'b', 'c')",
            name="ck_video_collection_states_candidate_tier",
        ),
        sa.CheckConstraint(
            "pinned_tier IS NULL OR pinned_tier IN ('s', 'a', 'b', 'c')",
            name="ck_video_collection_states_pinned_tier",
        ),
        sa.CheckConstraint(
            "life_stage IN ('active', 'dormant', 'archived')",
            name="ck_video_collection_states_life_stage",
        ),
        sa.CheckConstraint(
            "consecutive_downgrade_count >= 0",
            name="ck_video_collection_states_downgrade_count",
        ),
        sa.CheckConstraint(
            "last_checkpoint_hours IS NULL OR last_checkpoint_hours > 0",
            name="ck_video_collection_states_checkpoint_hours",
        ),
        sa.ForeignKeyConstraint(
            ["bvid"],
            ["known_videos.bvid"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["policy_version"],
            ["collection_policy_versions.version"],
        ),
        sa.PrimaryKeyConstraint("bvid"),
    )
    op.create_index(
        "idx_video_collection_states_next_due",
        "video_collection_states",
        ["next_due_at"],
        unique=False,
    )
    op.create_index(
        "idx_video_collection_states_life_stage",
        "video_collection_states",
        ["life_stage", "next_due_at"],
        unique=False,
    )

    op.create_table(
        "snapshot_cohorts",
        sa.Column("id", _bigint_pk(), autoincrement=True, nullable=False),
        sa.Column("cohort_key", sa.Text(), nullable=False, unique=True),
        sa.Column("bvid", sa.Text(), nullable=False),
        sa.Column(
            "scheduled_for",
            books_of_time.db.types.UTCDateTime(),
            nullable=False,
        ),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("age_checkpoint_hours", sa.Integer(), nullable=True),
        sa.Column("desired_tier", sa.String(length=1), nullable=False),
        sa.Column("effective_tier", sa.String(length=1), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column(
            "deadline",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="planned",
            nullable=False,
        ),
        sa.Column("status_reason", sa.Text(), nullable=True),
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
        sa.Column(
            "expected_component_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "completed_component_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "extra",
            books_of_time.db.types.json_dict_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        _created_at_column(),
        _updated_at_column(),
        sa.CheckConstraint(
            "desired_tier IN ('s', 'a', 'b', 'c')",
            name="ck_snapshot_cohorts_desired_tier",
        ),
        sa.CheckConstraint(
            "effective_tier IN ('s', 'a', 'b', 'c')",
            name="ck_snapshot_cohorts_effective_tier",
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'shadow_planned', 'running', 'complete', "
            "'partial', 'missed', 'corrupted', 'blocked', 'not_applicable')",
            name="ck_snapshot_cohorts_status",
        ),
        sa.CheckConstraint(
            "age_checkpoint_hours IS NULL OR age_checkpoint_hours > 0",
            name="ck_snapshot_cohorts_checkpoint_hours",
        ),
        sa.CheckConstraint(
            "expected_component_count >= 0",
            name="ck_snapshot_cohorts_expected_components",
        ),
        sa.CheckConstraint(
            "completed_component_count >= 0",
            name="ck_snapshot_cohorts_completed_components",
        ),
        sa.CheckConstraint(
            "completed_component_count <= expected_component_count",
            name="ck_snapshot_cohorts_component_counts",
        ),
        sa.ForeignKeyConstraint(
            ["bvid"],
            ["known_videos.bvid"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["policy_version"],
            ["collection_policy_versions.version"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_snapshot_cohorts_bvid_scheduled",
        "snapshot_cohorts",
        ["bvid", "scheduled_for"],
        unique=False,
    )
    op.create_index(
        "idx_snapshot_cohorts_status_deadline",
        "snapshot_cohorts",
        ["status", "deadline"],
        unique=False,
    )

    op.create_table(
        "snapshot_cohort_components",
        sa.Column("id", _bigint_pk(), autoincrement=True, nullable=False),
        sa.Column("cohort_id", sa.BigInteger(), nullable=False),
        sa.Column("component_kind", sa.String(length=64), nullable=False),
        sa.Column(
            "required",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "scheduled_for",
            books_of_time.db.types.UTCDateTime(),
            nullable=False,
        ),
        sa.Column(
            "deadline",
            books_of_time.db.types.UTCDateTime(),
            nullable=True,
        ),
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
        sa.Column("skew_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "planned_pages",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "requested_pages",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "succeeded_pages",
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
        sa.Column("comment_scan_run_id", sa.BigInteger(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "extra",
            books_of_time.db.types.json_dict_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'complete', 'partial', "
            "'joined_active_task', 'missed_due_to_capacity', "
            "'missed_due_to_service_gap', 'failed', 'corrupted', "
            "'not_applicable', 'blocked')",
            name="ck_snapshot_cohort_components_status",
        ),
        sa.CheckConstraint(
            "planned_pages >= 0",
            name="ck_snapshot_cohort_components_planned_pages",
        ),
        sa.CheckConstraint(
            "requested_pages >= 0",
            name="ck_snapshot_cohort_components_requested_pages",
        ),
        sa.CheckConstraint(
            "succeeded_pages >= 0",
            name="ck_snapshot_cohort_components_succeeded_pages",
        ),
        sa.CheckConstraint(
            "items_observed >= 0",
            name="ck_snapshot_cohort_components_items_observed",
        ),
        sa.CheckConstraint(
            "raw_payloads_saved >= 0",
            name="ck_snapshot_cohort_components_raw_payloads",
        ),
        sa.CheckConstraint(
            "succeeded_pages <= requested_pages",
            name="ck_snapshot_cohort_components_page_counts",
        ),
        sa.ForeignKeyConstraint(
            ["cohort_id"],
            ["snapshot_cohorts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cohort_id",
            "component_kind",
            name="uq_snapshot_cohort_components_kind",
        ),
    )
    op.create_index(
        "idx_snapshot_cohort_components_status_deadline",
        "snapshot_cohort_components",
        ["status", "deadline"],
        unique=False,
    )

    op.create_table(
        "collection_schedule_gaps",
        sa.Column("id", _bigint_pk(), autoincrement=True, nullable=False),
        sa.Column("bvid", sa.Text(), nullable=False),
        sa.Column(
            "gap_start",
            books_of_time.db.types.UTCDateTime(),
            nullable=False,
        ),
        sa.Column(
            "gap_end",
            books_of_time.db.types.UTCDateTime(),
            nullable=False,
        ),
        sa.Column("expected_cohort_count", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("service_instance_id", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.Text(), nullable=False),
        _created_at_column(),
        sa.CheckConstraint(
            "expected_cohort_count >= 0",
            name="ck_collection_schedule_gaps_expected_count",
        ),
        sa.CheckConstraint(
            "gap_end > gap_start",
            name="ck_collection_schedule_gaps_time_order",
        ),
        sa.ForeignKeyConstraint(
            ["bvid"],
            ["known_videos.bvid"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["policy_version"],
            ["collection_policy_versions.version"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bvid",
            "gap_start",
            "gap_end",
            "reason",
            "policy_version",
            name="uq_collection_schedule_gaps_identity",
        ),
    )
    op.create_index(
        "idx_collection_schedule_gaps_bvid_time",
        "collection_schedule_gaps",
        ["bvid", "gap_start", "gap_end"],
        unique=False,
    )

    op.add_column(
        "collection_tasks",
        sa.Column("snapshot_cohort_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "collection_tasks",
        sa.Column("snapshot_cohort_component_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "collection_coverage_stats",
        sa.Column("snapshot_cohort_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "collection_coverage_stats",
        sa.Column("snapshot_cohort_component_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("collection_coverage_stats", "snapshot_cohort_component_id")
    op.drop_column("collection_coverage_stats", "snapshot_cohort_id")
    op.drop_column("collection_tasks", "snapshot_cohort_component_id")
    op.drop_column("collection_tasks", "snapshot_cohort_id")

    op.drop_index(
        "idx_collection_schedule_gaps_bvid_time",
        table_name="collection_schedule_gaps",
    )
    op.drop_table("collection_schedule_gaps")

    op.drop_index(
        "idx_snapshot_cohort_components_status_deadline",
        table_name="snapshot_cohort_components",
    )
    op.drop_table("snapshot_cohort_components")

    op.drop_index(
        "idx_snapshot_cohorts_status_deadline",
        table_name="snapshot_cohorts",
    )
    op.drop_index(
        "idx_snapshot_cohorts_bvid_scheduled",
        table_name="snapshot_cohorts",
    )
    op.drop_table("snapshot_cohorts")

    op.drop_index(
        "idx_video_collection_states_life_stage",
        table_name="video_collection_states",
    )
    op.drop_index(
        "idx_video_collection_states_next_due",
        table_name="video_collection_states",
    )
    op.drop_table("video_collection_states")

    op.drop_index(
        "uq_collection_policy_versions_active_scope",
        table_name="collection_policy_versions",
    )
    op.drop_table("collection_policy_versions")
