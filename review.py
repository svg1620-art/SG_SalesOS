"""Самоконтроль менеджера: обязательный разбор своих звонков.

Разбор обязателен для ВСЕХ оценённых звонков (status=done, есть балл, не
исключён). Комментарий менеджера обязателен. Срок — до конца следующего
рабочего дня после звонка; просрочка подсвечивается.
"""
from datetime import timedelta

from extensions import db
from models import Call, WorkCalendar
from utils import to_local, now_local


def is_scored(call) -> bool:
    return (
        call.status == "done"
        and call.overall_score is not None
        and not call.excluded
    )


def _is_working_day(d) -> bool:
    row = db.session.get(WorkCalendar, d)
    return bool(row.is_working_day) if row is not None else d.weekday() < 5


def due_date(call):
    """Крайний день разбора: конец следующего рабочего дня после звонка."""
    base = call.started_at or call.created_at
    if base is None:
        return None
    d = to_local(base).date()
    nxt = d + timedelta(days=1)
    guard = 0
    while not _is_working_day(nxt) and guard < 10:
        nxt += timedelta(days=1)
        guard += 1
    return nxt


def state(call) -> str:
    """reviewed | pending | overdue | na."""
    if call.reviewed_at is not None:
        return "reviewed"
    if not is_scored(call):
        return "na"
    due = due_date(call)
    if due is not None and now_local().date() > due:
        return "overdue"
    return "pending"


def states_for(calls) -> dict:
    return {c.id: state(c) for c in calls}


def pending_for_manager(manager_id, limit=None):
    """Неразобранные оценённые звонки менеджера (сначала просроченные/красные)."""
    q = Call.query.filter(
        Call.manager_id == manager_id, Call.status == "done",
        Call.overall_score.isnot(None), Call.reviewed_at.is_(None),
        db.or_(Call.excluded.is_(False), Call.excluded.is_(None)),
    )
    rows = q.all()
    zone_rank = {"red": 0, "yellow": 1, "green": 2}
    rows.sort(key=lambda c: (
        0 if state(c) == "overdue" else 1,
        zone_rank.get(c.zone, 3),
        -(int((c.started_at or c.created_at).timestamp()) if (c.started_at or c.created_at) else 0),
    ))
    return rows[:limit] if limit else rows


def manager_stats(manager_id, date_from, date_to) -> dict:
    """Статистика разбора менеджера за период (для РОПа)."""
    calls = Call.query.filter(
        Call.manager_id == manager_id, Call.status == "done",
        Call.overall_score.isnot(None),
        db.or_(Call.excluded.is_(False), Call.excluded.is_(None)),
        Call.started_at >= date_from, Call.started_at <= date_to,
    ).all()
    total = len(calls)
    reviewed = sum(1 for c in calls if c.reviewed_at is not None)
    overdue = sum(1 for c in calls if state(c) == "overdue")
    return {
        "total": total, "reviewed": reviewed, "overdue": overdue,
        "pending": total - reviewed,
        "pct": round(reviewed / total * 100) if total else None,
    }
