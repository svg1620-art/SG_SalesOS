"""add activity tracking: user.last_seen_at + activity_events

Revision ID: d4f6a8b0c2e5
Revises: c3e5f7a9b2d4
Create Date: 2026-07-08 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4f6a8b0c2e5'
down_revision = 'c3e5f7a9b2d4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_seen_at', sa.DateTime(), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_users_last_seen_at'), ['last_seen_at'], unique=False
        )

    op.create_table(
        'activity_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('call_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                                name='fk_activity_events_user_id'),
        sa.ForeignKeyConstraint(['call_id'], ['calls.id'],
                                name='fk_activity_events_call_id'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('activity_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_activity_events_user_id'),
                              ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_activity_events_created_at'),
                              ['created_at'], unique=False)


def downgrade():
    with op.batch_alter_table('activity_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_activity_events_created_at'))
        batch_op.drop_index(batch_op.f('ix_activity_events_user_id'))
    op.drop_table('activity_events')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_last_seen_at'))
        batch_op.drop_column('last_seen_at')
