"""call review: самоконтроль менеджера (разбор звонка)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-11-13

Поля calls.manager_comment, calls.reviewed_at. Существующие данные не затрагиваются.
"""
import sqlalchemy as sa
from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("calls") as batch:
        batch.add_column(sa.Column("manager_comment", sa.Text(), nullable=True))
        batch.add_column(sa.Column("reviewed_at", sa.DateTime(), nullable=True))
    op.create_index("ix_calls_reviewed_at", "calls", ["reviewed_at"])


def downgrade():
    op.drop_index("ix_calls_reviewed_at", table_name="calls")
    with op.batch_alter_table("calls") as batch:
        batch.drop_column("reviewed_at")
        batch.drop_column("manager_comment")
