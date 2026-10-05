"""ops metrics stage 4: staff costs, rop allocations, first revenue, deactivated_at

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-10-05

Этап 4: окупаемость. Новые таблицы staff_costs, rop_cost_allocations;
аддитивные колонки deals.(amo_company_id, client_key, is_first_revenue) и
users.deactivated_at. Существующие данные не затрагиваются.
"""
import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "staff_costs",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("cost_role", sa.String(length=10), nullable=False, server_default="manager"),
        sa.Column("salary_fixed", sa.Numeric(), nullable=False, server_default="0"),
        sa.Column("bonus_paid", sa.Numeric(), nullable=False, server_default="0"),
        sa.Column("payroll_tax_rate", sa.Numeric(), nullable=False, server_default="0.302"),
        sa.Column("overhead", sa.Numeric(), nullable=False, server_default="0"),
        sa.Column("lead_cost", sa.Numeric(), nullable=True),
        sa.Column("comment", sa.String(length=500), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id", "month"),
    )

    op.create_table(
        "rop_cost_allocations",
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("rop_user_id", sa.Integer(), nullable=False),
        sa.Column("manager_id", sa.Integer(), nullable=False),
        sa.Column("weight", sa.Numeric(), nullable=False, server_default="0"),
        sa.Column("allocated_cost", sa.Numeric(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["rop_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("month", "rop_user_id", "manager_id"),
    )

    with op.batch_alter_table("deals") as batch:
        batch.add_column(sa.Column("amo_company_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("client_key", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("is_first_revenue", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))
        batch.create_index("ix_deals_amo_company_id", ["amo_company_id"])
        batch.create_index("ix_deals_client_key", ["client_key"])
        batch.create_index("ix_deals_is_first_revenue", ["is_first_revenue"])

    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("deactivated_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("deactivated_at")
    with op.batch_alter_table("deals") as batch:
        batch.drop_index("ix_deals_is_first_revenue")
        batch.drop_index("ix_deals_client_key")
        batch.drop_index("ix_deals_amo_company_id")
        batch.drop_column("is_first_revenue")
        batch.drop_column("client_key")
        batch.drop_column("amo_company_id")
    op.drop_table("rop_cost_allocations")
    op.drop_table("staff_costs")
