"""ops metrics stage 1: events, funnel map/events, settings, sync log, calendar

Revision ID: a1b2c3d4e5f6
Revises: f6b8d0c2e4a7
Create Date: 2026-10-05

Этап 1 модуля «Операционный пульт». Только таблицы сбора данных и маппинга.
Существующие таблицы не затрагиваются. Таблица сырых действий называется
ops_activity_events (имя activity_events занято существующей ActivityEvent).
"""
import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "f6b8d0c2e4a7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ops_activity_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("amo_event_id", sa.String(length=64), nullable=False),
        sa.Column("manager_id", sa.Integer(), nullable=True),
        sa.Column("amo_user_id", sa.BigInteger(), nullable=True),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=True),
        sa.Column("lead_id", sa.BigInteger(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.Column("call_status", sa.Integer(), nullable=True),
        sa.Column("is_connected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("call_id", sa.Integer(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("amo_event_id", name="uq_ops_activity_amo_event_id"),
    )
    op.create_index("ix_ops_activity_events_amo_event_id", "ops_activity_events", ["amo_event_id"])
    op.create_index("ix_ops_activity_events_manager_id", "ops_activity_events", ["manager_id"])
    op.create_index("ix_ops_activity_events_amo_user_id", "ops_activity_events", ["amo_user_id"])
    op.create_index("ix_ops_activity_events_type", "ops_activity_events", ["type"])
    op.create_index("ix_ops_activity_events_contact_id", "ops_activity_events", ["contact_id"])
    op.create_index("ix_ops_activity_events_lead_id", "ops_activity_events", ["lead_id"])
    op.create_index("ix_ops_activity_events_occurred_at", "ops_activity_events", ["occurred_at"])

    op.create_table(
        "funnel_stage_map",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pipeline_id", sa.BigInteger(), nullable=False),
        sa.Column("status_id", sa.Integer(), nullable=False),
        sa.Column("step", sa.String(length=20), nullable=False, server_default="none"),
        sa.Column("step_order", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pipeline_id", "status_id", name="uq_funnel_stage_map_pl_st"),
    )

    op.create_table(
        "funnel_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.BigInteger(), nullable=False),
        sa.Column("step", sa.String(length=20), nullable=False),
        sa.Column("manager_id", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=True),
        sa.Column("pipeline_id", sa.BigInteger(), nullable=True),
        sa.Column("client_key", sa.String(length=64), nullable=True),
        sa.Column("is_first_revenue", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lead_id", "step", name="uq_funnel_events_lead_step"),
    )
    op.create_index("ix_funnel_events_lead_id", "funnel_events", ["lead_id"])
    op.create_index("ix_funnel_events_step", "funnel_events", ["step"])
    op.create_index("ix_funnel_events_manager_id", "funnel_events", ["manager_id"])
    op.create_index("ix_funnel_events_occurred_at", "funnel_events", ["occurred_at"])
    op.create_index("ix_funnel_events_client_key", "funnel_events", ["client_key"])

    op.create_table(
        "ops_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_ops_settings_key"),
    )
    op.create_index("ix_ops_settings_key", "ops_settings", ["key"])

    op.create_table(
        "ops_sync_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("cursor_from", sa.DateTime(), nullable=True),
        sa.Column("cursor_to", sa.DateTime(), nullable=True),
        sa.Column("counts", sa.JSON(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ops_sync_log_started_at", "ops_sync_log", ["started_at"])

    op.create_table(
        "work_calendar",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("is_working_day", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("date"),
    )


def downgrade():
    op.drop_table("work_calendar")
    op.drop_index("ix_ops_sync_log_started_at", table_name="ops_sync_log")
    op.drop_table("ops_sync_log")
    op.drop_index("ix_ops_settings_key", table_name="ops_settings")
    op.drop_table("ops_settings")
    for ix in (
        "ix_funnel_events_client_key", "ix_funnel_events_occurred_at",
        "ix_funnel_events_manager_id", "ix_funnel_events_step", "ix_funnel_events_lead_id",
    ):
        op.drop_index(ix, table_name="funnel_events")
    op.drop_table("funnel_events")
    op.drop_table("funnel_stage_map")
    for ix in (
        "ix_ops_activity_events_occurred_at", "ix_ops_activity_events_lead_id",
        "ix_ops_activity_events_contact_id", "ix_ops_activity_events_type",
        "ix_ops_activity_events_amo_user_id", "ix_ops_activity_events_manager_id",
        "ix_ops_activity_events_amo_event_id",
    ):
        op.drop_index(ix, table_name="ops_activity_events")
    op.drop_table("ops_activity_events")
