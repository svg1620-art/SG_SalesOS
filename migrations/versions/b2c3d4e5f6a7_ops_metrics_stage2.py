"""ops metrics stage 2: day/hour stats, plans, absences, users.hire_date

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-05

Этап 2 модуля «Операционный пульт»: материализованные агрегаты + планы +
отсутствия + дата найма. Существующие таблицы не затрагиваются (кроме
аддитивной колонки users.hire_date).
"""
import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "manager_day_stats",
        sa.Column("manager_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("calls_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("calls_connected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("talk_time_sec", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("messages_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("touches", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_leads_contacted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("speed_to_lead_median_min", sa.Float(), nullable=True),
        sa.Column("first_action_at", sa.DateTime(), nullable=True),
        sa.Column("last_action_at", sa.DateTime(), nullable=True),
        sa.Column("idle_gaps_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tasks_overdue", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("qualified", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("meetings_set", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("meetings_held", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invoices", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invoices_sum", sa.Numeric(), nullable=False, server_default="0"),
        sa.Column("payments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payments_sum", sa.Numeric(), nullable=False, server_default="0"),
        sa.Column("avg_call_score", sa.Float(), nullable=True),
        sa.Column("is_working_day", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("manager_id", "date"),
    )
    op.create_index("ix_manager_day_stats_date", "manager_day_stats", ["date"])

    op.create_table(
        "manager_hour_stats",
        sa.Column("manager_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("hour", sa.Integer(), nullable=False),
        sa.Column("calls_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("calls_connected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("messages_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("talk_time_sec", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("manager_id", "date", "hour"),
    )

    op.create_table(
        "manager_plans",
        sa.Column("manager_id", sa.Integer(), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("revenue_plan", sa.Numeric(), nullable=True),
        sa.Column("qualified_plan", sa.Integer(), nullable=True),
        sa.Column("meetings_plan", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("manager_id", "month"),
    )

    op.create_table(
        "manager_absences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("manager_id", sa.Integer(), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="vacation"),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_manager_absences_manager_id", "manager_absences", ["manager_id"])

    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("hire_date", sa.Date(), nullable=True))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("hire_date")
    op.drop_index("ix_manager_absences_manager_id", table_name="manager_absences")
    op.drop_table("manager_absences")
    op.drop_table("manager_plans")
    op.drop_table("manager_hour_stats")
    op.drop_index("ix_manager_day_stats_date", table_name="manager_day_stats")
    op.drop_table("manager_day_stats")
