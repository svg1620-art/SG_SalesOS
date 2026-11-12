"""renewals: воронка продлений (Этап A)

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-11-12

Модуль продлений, Этап A. Таблица renewals — открытые/терминальные сделки
воронки «Аккаунтинг» с датой окончания подписки и статусом-зоной.
Существующие данные не затрагиваются.
"""
import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "renewals",
        sa.Column("amo_lead_id", sa.BigInteger(), nullable=False),
        sa.Column("manager_id", sa.Integer(), nullable=True),
        sa.Column("amo_responsible_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=500), nullable=True),
        sa.Column("company", sa.String(length=500), nullable=True),
        sa.Column("amo_contact_id", sa.BigInteger(), nullable=True),
        sa.Column("amo_company_id", sa.BigInteger(), nullable=True),
        sa.Column("price", sa.Integer(), nullable=True),
        sa.Column("pipeline_id", sa.BigInteger(), nullable=True),
        sa.Column("status_id", sa.Integer(), nullable=True),
        sa.Column("status_name", sa.String(length=160), nullable=True),
        sa.Column("zone", sa.String(length=10), nullable=False, server_default="green"),
        sa.Column("is_open", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("renewal_end", sa.Date(), nullable=True),
        sa.Column("amo_updated_at", sa.DateTime(), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("amo_lead_id"),
    )
    op.create_index("ix_renewals_manager_id", "renewals", ["manager_id"])
    op.create_index("ix_renewals_amo_responsible_id", "renewals", ["amo_responsible_id"])
    op.create_index("ix_renewals_amo_contact_id", "renewals", ["amo_contact_id"])
    op.create_index("ix_renewals_amo_company_id", "renewals", ["amo_company_id"])
    op.create_index("ix_renewals_pipeline_id", "renewals", ["pipeline_id"])
    op.create_index("ix_renewals_zone", "renewals", ["zone"])
    op.create_index("ix_renewals_is_open", "renewals", ["is_open"])
    op.create_index("ix_renewals_renewal_end", "renewals", ["renewal_end"])


def downgrade():
    for ix in ("ix_renewals_renewal_end", "ix_renewals_is_open", "ix_renewals_zone",
               "ix_renewals_pipeline_id", "ix_renewals_amo_company_id",
               "ix_renewals_amo_contact_id", "ix_renewals_amo_responsible_id",
               "ix_renewals_manager_id"):
        op.drop_index(ix, table_name="renewals")
    op.drop_table("renewals")
