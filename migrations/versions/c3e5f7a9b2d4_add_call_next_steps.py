"""add call.next_steps_json + next_steps_at

Revision ID: c3e5f7a9b2d4
Revises: b2d4e6f8a1c3
Create Date: 2026-07-08 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3e5f7a9b2d4'
down_revision = 'b2d4e6f8a1c3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('calls', schema=None) as batch_op:
        batch_op.add_column(sa.Column('next_steps_json', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('next_steps_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('calls', schema=None) as batch_op:
        batch_op.drop_column('next_steps_at')
        batch_op.drop_column('next_steps_json')
