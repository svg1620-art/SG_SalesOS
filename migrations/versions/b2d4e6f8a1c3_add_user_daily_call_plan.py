"""add user.daily_call_plan

Revision ID: b2d4e6f8a1c3
Revises: a1c2d3e4f5a6
Create Date: 2026-07-08 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2d4e6f8a1c3'
down_revision = 'a1c2d3e4f5a6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('daily_call_plan', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('daily_call_plan')
