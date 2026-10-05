"""Настройки модуля Ops (таблица ops_settings): key/value с дефолтами из §13 ТЗ.

Отдельно от общих настроек (settings_store), чтобы не смешивать. Значение из БД
имеет приоритет; если ключа нет — берётся дефолт отсюда.
"""
from flask import current_app

from extensions import db
from models import OpsSetting

# Дефолты модуля (ТЗ §13). Только значения, нужные Этапу 1, плюс базовый набор
# порогов — чтобы следующие этапы не заводили их заново.
OPS_DEFAULTS = {
    "connect_min_sec": "30",
    "idle_gap_min": "60",
    "late_start_time": "10:00",
    "work_hours_from": "9",
    "work_hours_to": "20",
    "ops_timezone": "Europe/Moscow",
    "sync_interval_min": "5",
    "sync_overlap_min": "10",
    "backfill_days": "90",
    "cohort_window_days": "45",
    "min_sample": "30",
    "max_silver_ratio": "2.0",
    "quality_gate_score": "60",
    "max_calls_per_contact_day": "3",
    "rampup_months": "3",
    "gross_margin_rate": "1.0",
    "payroll_tax_rate": "0.302",
    "overhead_default": "0",
}


def get_ops_setting(key: str):
    """Значение из БД или дефолт (строка) или None."""
    try:
        row = OpsSetting.query.filter_by(key=key).first()
    except Exception:  # noqa: BLE001 — таблицы ещё нет (во время миграций)
        return OPS_DEFAULTS.get(key)
    if row is not None and row.value not in (None, ""):
        return row.value
    return OPS_DEFAULTS.get(key)


def set_ops_setting(key: str, value) -> None:
    row = OpsSetting.query.filter_by(key=key).first()
    value = "" if value is None else str(value)
    if row is None:
        db.session.add(OpsSetting(key=key, value=value))
    else:
        row.value = value
    db.session.commit()


def ops_int(key: str, default: int = 0) -> int:
    try:
        return int(float(get_ops_setting(key)))
    except (TypeError, ValueError):
        return default


def ops_float(key: str, default: float = 0.0) -> float:
    try:
        return float(get_ops_setting(key))
    except (TypeError, ValueError):
        return default


def connect_min_sec() -> int:
    return ops_int("connect_min_sec", 30)


def sync_interval_min() -> int:
    return max(1, ops_int("sync_interval_min", 5))


def sync_overlap_min() -> int:
    return max(0, ops_int("sync_overlap_min", 10))


def backfill_days() -> int:
    return max(1, ops_int("backfill_days", 90))


def seed_ops_settings(app=None) -> int:
    """Идемпотентно создать недостающие ключи ops_settings с дефолтами."""
    app = app or current_app
    created = 0
    existing = {s.key for s in OpsSetting.query.all()}
    for key, value in OPS_DEFAULTS.items():
        if key not in existing:
            db.session.add(OpsSetting(key=key, value=value))
            created += 1
    if created:
        db.session.commit()
    return created
