"""Учёт активности менеджеров (вовлечённость в платформу).

Слои 1–2: тихо логируем ключевые действия (вход, просмотр своего звонка) и
обновляем last_seen. По этим данным считаем «разобрано N/Y» и «последний вход».
"""
from datetime import datetime, timedelta

from extensions import db
from models import ActivityEvent

_TOUCH_INTERVAL = timedelta(minutes=5)


def log_event(user_id, kind, call_id=None) -> None:
    """Записать событие активности (best-effort, не роняет запрос)."""
    try:
        db.session.add(ActivityEvent(user_id=user_id, kind=kind, call_id=call_id))
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()


def touch_last_seen(user) -> None:
    """Обновить last_seen (не чаще раза в 5 минут)."""
    try:
        now = datetime.utcnow()
        if user.last_seen_at is None or (now - user.last_seen_at) > _TOUCH_INTERVAL:
            user.last_seen_at = now
            db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()


def viewed_call_ids(user_ids) -> dict:
    """{user_id: set(call_id)} — какие звонки менеджер открывал (view_call)."""
    out = {}
    ids = [u for u in (user_ids or []) if u is not None]
    if not ids:
        return out
    rows = (
        ActivityEvent.query
        .filter(
            ActivityEvent.kind == "view_call",
            ActivityEvent.user_id.in_(ids),
            ActivityEvent.call_id.isnot(None),
        )
        .with_entities(ActivityEvent.user_id, ActivityEvent.call_id)
        .all()
    )
    for uid, cid in rows:
        out.setdefault(uid, set()).add(cid)
    return out
