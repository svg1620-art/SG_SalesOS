"""renewal comm: статус коммуникации (светофор по переговорам)

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-11-13

Поля renewals.comm_* — гибридный статус коммуникации (ИИ из звонка + ручной).
"""
import sqlalchemy as sa
from alembic import op

revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("renewals") as batch:
        batch.add_column(sa.Column("comm_status", sa.String(length=10), nullable=True))
        batch.add_column(sa.Column("comm_source", sa.String(length=10), nullable=True))
        batch.add_column(sa.Column("comm_note", sa.String(length=500), nullable=True))
        batch.add_column(sa.Column("comm_updated_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("comm_by", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("comm_call_id", sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table("renewals") as batch:
        batch.drop_column("comm_call_id")
        batch.drop_column("comm_by")
        batch.drop_column("comm_updated_at")
        batch.drop_column("comm_note")
        batch.drop_column("comm_source")
        batch.drop_column("comm_status")
