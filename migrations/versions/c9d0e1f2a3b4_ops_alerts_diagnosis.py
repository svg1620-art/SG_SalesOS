"""ops stage 6: системные алерты, AI-диагностика, Telegram-привязка

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-11-13

Таблицы ops_alerts, manager_diagnosis + поля users.telegram_*/notify_enabled.
"""
import sqlalchemy as sa
from alembic import op

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("telegram_chat_id", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("telegram_code", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("notify_enabled", sa.Boolean(), nullable=False,
                                   server_default=sa.true()))
        batch.create_index("ix_users_telegram_chat_id", ["telegram_chat_id"])

    op.create_table(
        "ops_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("atype", sa.String(length=30), nullable=False),
        sa.Column("manager_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("severity", sa.String(length=10), nullable=False, server_default="warn"),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column("data_json", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_ops_alerts_atype", "ops_alerts", ["atype"])
    op.create_index("ix_ops_alerts_manager_id", "ops_alerts", ["manager_id"])
    op.create_index("ix_ops_alerts_is_active", "ops_alerts", ["is_active"])
    op.create_index("ix_ops_alerts_active", "ops_alerts",
                    ["is_active", "atype", "manager_id"])

    op.create_table(
        "manager_diagnosis",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("manager_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("main_cause", sa.String(length=600), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("action_for_manager", sa.String(length=600), nullable=True),
        sa.Column("question_for_rop", sa.String(length=600), nullable=True),
        sa.Column("risk_level", sa.String(length=10), nullable=True),
        sa.Column("provider", sa.String(length=20), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("manager_id", "week_start", name="uq_diagnosis_manager_week"),
    )
    op.create_index("ix_manager_diagnosis_manager_id", "manager_diagnosis", ["manager_id"])
    op.create_index("ix_manager_diagnosis_week_start", "manager_diagnosis", ["week_start"])


def downgrade():
    op.drop_index("ix_manager_diagnosis_week_start", table_name="manager_diagnosis")
    op.drop_index("ix_manager_diagnosis_manager_id", table_name="manager_diagnosis")
    op.drop_table("manager_diagnosis")
    op.drop_index("ix_ops_alerts_active", table_name="ops_alerts")
    op.drop_index("ix_ops_alerts_is_active", table_name="ops_alerts")
    op.drop_index("ix_ops_alerts_manager_id", table_name="ops_alerts")
    op.drop_index("ix_ops_alerts_atype", table_name="ops_alerts")
    op.drop_table("ops_alerts")
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_telegram_chat_id")
        batch.drop_column("notify_enabled")
        batch.drop_column("telegram_code")
        batch.drop_column("telegram_chat_id")
