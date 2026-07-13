"""add deal.outcome + amo_contact_id (won/lost history)

Revision ID: e5a7c9b1d3f6
Revises: d4f6a8b0c2e5
Create Date: 2026-07-08 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5a7c9b1d3f6'
down_revision = 'd4f6a8b0c2e5'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('deals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('amo_contact_id', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('outcome', sa.String(length=10), nullable=True))
        batch_op.create_index(batch_op.f('ix_deals_amo_contact_id'),
                              ['amo_contact_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_deals_outcome'), ['outcome'], unique=False)
    # существующие записи — все выигранные
    op.execute("UPDATE deals SET outcome = 'won' WHERE outcome IS NULL")


def downgrade():
    with op.batch_alter_table('deals', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_deals_outcome'))
        batch_op.drop_index(batch_op.f('ix_deals_amo_contact_id'))
        batch_op.drop_column('outcome')
        batch_op.drop_column('amo_contact_id')
