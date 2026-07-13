"""add call lead scoring: lead_score + lead_score_json + lead_score_at

Revision ID: f6b8d0c2e4a7
Revises: e5a7c9b1d3f6
Create Date: 2026-07-08 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f6b8d0c2e4a7'
down_revision = 'e5a7c9b1d3f6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('calls', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lead_score', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('lead_score_json', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('lead_score_at', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_calls_lead_score'),
                              ['lead_score'], unique=False)


def downgrade():
    with op.batch_alter_table('calls', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_calls_lead_score'))
        batch_op.drop_column('lead_score_at')
        batch_op.drop_column('lead_score_json')
        batch_op.drop_column('lead_score')
