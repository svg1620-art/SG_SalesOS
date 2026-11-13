"""renewal cycles: журнал исходов продлений (ретроспектива)

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-11-13

Таблица renewal_cycles — история «кто продлил / отвалился» по циклам, чтобы
ретроспектива по месяцам работала, когда дата окончания съезжает вперёд.
"""
import sqlalchemy as sa
from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "renewal_cycles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("amo_lead_id", sa.BigInteger(), nullable=False),
        sa.Column("cycle_end", sa.Date(), nullable=False),
        sa.Column("outcome", sa.String(length=10), nullable=False),
        sa.Column("price", sa.Integer(), nullable=True),
        sa.Column("manager_id", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("amo_lead_id", "cycle_end", name="uq_renewal_cycle_lead_end"),
    )
    op.create_index("ix_renewal_cycles_amo_lead_id", "renewal_cycles", ["amo_lead_id"])
    op.create_index("ix_renewal_cycles_cycle_end", "renewal_cycles", ["cycle_end"])
    op.create_index("ix_renewal_cycles_manager_id", "renewal_cycles", ["manager_id"])


def downgrade():
    op.drop_index("ix_renewal_cycles_manager_id", table_name="renewal_cycles")
    op.drop_index("ix_renewal_cycles_cycle_end", table_name="renewal_cycles")
    op.drop_index("ix_renewal_cycles_amo_lead_id", table_name="renewal_cycles")
    op.drop_table("renewal_cycles")
