"""ops metrics stage 5: daily missions, manager progress, xp ledger

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-06

Этап 5: миссии и геймификация. Новые таблицы daily_missions, manager_progress,
xp_ledger. Существующие данные не затрагиваются.
"""
import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "daily_missions",
        sa.Column("manager_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("targets", sa.JSON(), nullable=True),
        sa.Column("results", sa.JSON(), nullable=True),
        sa.Column("tier_reached", sa.String(length=10), nullable=False, server_default="none"),
        sa.Column("xp_awarded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("finalized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("manager_id", "date"),
    )
    op.create_index("ix_daily_missions_date", "daily_missions", ["date"])

    op.create_table(
        "manager_progress",
        sa.Column("manager_id", sa.Integer(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("streak_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("streak_best", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("freezes_available", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("manager_id"),
    )

    op.create_table(
        "xp_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("manager_id", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ref", sa.String(length=80), nullable=False),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "ref", name="uq_xp_ledger_source_ref"),
    )
    op.create_index("ix_xp_ledger_manager_id", "xp_ledger", ["manager_id"])
    op.create_index("ix_xp_ledger_occurred_at", "xp_ledger", ["occurred_at"])


def downgrade():
    op.drop_index("ix_xp_ledger_occurred_at", table_name="xp_ledger")
    op.drop_index("ix_xp_ledger_manager_id", table_name="xp_ledger")
    op.drop_table("xp_ledger")
    op.drop_table("manager_progress")
    op.drop_index("ix_daily_missions_date", table_name="daily_missions")
    op.drop_table("daily_missions")
