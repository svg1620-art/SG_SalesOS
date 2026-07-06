"""add checklist.department_id

Revision ID: a1c2d3e4f5a6
Revises: 74f73daefcb2
Create Date: 2026-07-06 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1c2d3e4f5a6'
down_revision = '74f73daefcb2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('checklists', schema=None) as batch_op:
        batch_op.add_column(sa.Column('department_id', sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_checklists_department_id'), ['department_id'], unique=False
        )
        batch_op.create_foreign_key(
            'fk_checklists_department_id', 'departments', ['department_id'], ['id']
        )


def downgrade():
    with op.batch_alter_table('checklists', schema=None) as batch_op:
        batch_op.drop_constraint('fk_checklists_department_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_checklists_department_id'))
        batch_op.drop_column('department_id')
