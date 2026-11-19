"""calls: индексы started_at / manager_id / status (+ composite)

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-11-19

Список звонков фильтрует/сортирует по started_at и manager_id без индексов —
на больших аккаунтах полный скан + сортировка всей таблицы на каждый заход.
"""
from alembic import op

revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def _has_index(insp, table, name):
    try:
        return any(ix["name"] == name for ix in insp.get_indexes(table))
    except Exception:  # noqa: BLE001
        return False


def upgrade():
    from sqlalchemy import inspect
    insp = inspect(op.get_bind())
    if not _has_index(insp, "calls", "ix_calls_started_at"):
        op.create_index("ix_calls_started_at", "calls", ["started_at"])
    if not _has_index(insp, "calls", "ix_calls_manager_id"):
        op.create_index("ix_calls_manager_id", "calls", ["manager_id"])
    if not _has_index(insp, "calls", "ix_calls_status"):
        op.create_index("ix_calls_status", "calls", ["status"])
    if not _has_index(insp, "calls", "ix_calls_manager_started"):
        op.create_index("ix_calls_manager_started", "calls", ["manager_id", "started_at"])


def downgrade():
    for name in ("ix_calls_manager_started", "ix_calls_status",
                 "ix_calls_manager_id", "ix_calls_started_at"):
        try:
            op.drop_index(name, table_name="calls")
        except Exception:  # noqa: BLE001
            pass
