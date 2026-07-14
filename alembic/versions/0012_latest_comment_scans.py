"""latest comment scan ownership and multi-anchor frontiers

Revision ID: 0012_latest_comment_scans
Revises: 0011_hot_comment_scans
Create Date: 2026-07-14 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

import books_of_time.db.types
from alembic import op

revision: str = "0012_latest_comment_scans"
down_revision: str | Sequence[str] | None = "0011_hot_comment_scans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_LATEST_PREDICATE = """
mode IN (
    'baseline_tail',
    'baseline_head_sweep',
    'incremental',
    'full_reconciliation',
    'segmented_reconciliation'
)
AND status IN ('planned', 'running', 'paused')
"""


def upgrade() -> None:
    with op.batch_alter_table("frontier_states") as batch_op:
        batch_op.add_column(
            sa.Column("active_scan_run_id", sa.BigInteger(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "version",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "frontier_anchor_set",
                books_of_time.db.types.json_dict_type,
                server_default=sa.text("'[]'"),
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_frontier_states_version",
            "version >= 0",
        )
        batch_op.create_foreign_key(
            "fk_frontier_states_active_scan_run",
            "comment_scan_runs",
            ["active_scan_run_id"],
            ["id"],
            ondelete="SET NULL",
        )

    _backfill_legacy_frontier_anchors()

    op.create_index(
        "idx_frontier_states_active_scan",
        "frontier_states",
        ["active_scan_run_id"],
        unique=False,
    )
    op.create_index(
        "uq_comment_scan_runs_active_latest_bvid",
        "comment_scan_runs",
        ["bvid"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_LATEST_PREDICATE),
        postgresql_where=sa.text(_ACTIVE_LATEST_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_comment_scan_runs_active_latest_bvid",
        table_name="comment_scan_runs",
    )
    op.drop_index(
        "idx_frontier_states_active_scan",
        table_name="frontier_states",
    )

    with op.batch_alter_table("frontier_states") as batch_op:
        batch_op.drop_constraint(
            "fk_frontier_states_active_scan_run",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "ck_frontier_states_version",
            type_="check",
        )
        batch_op.drop_column("frontier_anchor_set")
        batch_op.drop_column("version")
        batch_op.drop_column("active_scan_run_id")


def _backfill_legacy_frontier_anchors() -> None:
    frontier_states = sa.table(
        "frontier_states",
        sa.column("id", sa.BigInteger()),
        sa.column("frontier_rpid", sa.BigInteger()),
        sa.column(
            "frontier_anchor_set",
            books_of_time.db.types.json_dict_type,
        ),
    )
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.select(frontier_states.c.id, frontier_states.c.frontier_rpid).where(
                frontier_states.c.frontier_rpid.is_not(None)
            )
        ).mappings()
    )
    for row in rows:
        connection.execute(
            frontier_states.update()
            .where(frontier_states.c.id == row["id"])
            .values(
                frontier_anchor_set=[
                    {
                        "rpid": int(row["frontier_rpid"]),
                        "platform_created_at": None,
                    }
                ]
            )
        )
