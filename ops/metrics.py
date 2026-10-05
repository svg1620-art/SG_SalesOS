"""Движок метрик Ops (Этап 2): материализация дневных/часовых агрегатов.

Все определения метрик (§2 ТЗ) — в этом модуле, пороги берутся из ops_settings
(не хардкод). Читает ops_activity_events / funnel_events / Deal / Call, пишет
manager_day_stats / manager_hour_stats. Идемпотентно (upsert по PK). «Мой день»
и пульт читают из агрегатов, а не из сырых событий (§14).
"""
from datetime import datetime, date, timedelta

from flask import current_app

from extensions import db
from models import (
    OpsActivityEvent, FunnelEvent, Deal, Call, User,
    ManagerDayStat, ManagerHourStat, WorkCalendar,
)
from utils import app_tz, to_local, local_to_utc_naive
from ops_store import ops_int

_CALL_TYPES = ("call_out", "call_in")


def _utc_bounds(day: date):
    """UTC-naive границы локальных суток [00:00, 24:00) для даты day."""
    tz = app_tz()
    start_local = datetime(day.year, day.month, day.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return local_to_utc_naive(start_local), local_to_utc_naive(end_local)


def _is_working_day(day: date) -> bool:
    row = db.session.get(WorkCalendar, day)
    if row is not None:
        return bool(row.is_working_day)
    return day.weekday() < 5


def _idle_gaps(times_local, wh_from: int, wh_to: int, idle_min: int) -> int:
    """Число простоев ≥ idle_min между последовательными действиями в рабочие часы."""
    within = sorted(t for t in times_local if wh_from <= t.hour < wh_to)
    gaps = 0
    for prev, nxt in zip(within, within[1:]):
        if (nxt - prev).total_seconds() / 60.0 >= idle_min:
            gaps += 1
    return gaps


def recompute_day(manager_id: int, day: date) -> None:
    """Пересчитать дневные агрегаты менеджера за day (upsert). Идемпотентно."""
    utc_from, utc_to = _utc_bounds(day)
    connect_min = ops_int("connect_min_sec", 30)
    idle_min = ops_int("idle_gap_min", 60)
    wh_from = ops_int("work_hours_from", 9)
    wh_to = ops_int("work_hours_to", 20)

    events = OpsActivityEvent.query.filter(
        OpsActivityEvent.manager_id == manager_id,
        OpsActivityEvent.occurred_at >= utc_from,
        OpsActivityEvent.occurred_at < utc_to,
    ).all()

    calls_out = sum(1 for e in events if e.type == "call_out")
    connected = [e for e in events if e.type in _CALL_TYPES and e.is_connected]
    calls_connected = len(connected)
    talk_time_sec = sum(int(e.duration_sec or 0) for e in connected)
    messages_out = sum(1 for e in events if e.type == "msg_out")

    # касания: уникальные контакты с результативным действием (дозвон / сообщение)
    touch_contacts = {
        e.contact_id for e in events
        if e.contact_id is not None and (
            (e.type in _CALL_TYPES and e.is_connected) or e.type == "msg_out"
        )
    }
    touches = len(touch_contacts)

    times = [e.occurred_at for e in events if e.occurred_at]
    first_action = min(times) if times else None
    last_action = max(times) if times else None
    idle_gaps = _idle_gaps([to_local(t) for t in times], wh_from, wh_to, idle_min)

    # воронка (L2/L3) за день
    funnel = FunnelEvent.query.filter(
        FunnelEvent.manager_id == manager_id,
        FunnelEvent.occurred_at >= utc_from,
        FunnelEvent.occurred_at < utc_to,
    ).all()
    qualified = sum(1 for f in funnel if f.step == "qualified")
    meetings_set = sum(1 for f in funnel if f.step == "meeting_set")
    meetings_held = sum(1 for f in funnel if f.step == "meeting_held")
    invoices = sum(1 for f in funnel if f.step == "invoice")
    invoices_sum = sum(float(f.amount or 0) for f in funnel if f.step == "invoice")

    # оплаты — из существующих сделок (won) по дате закрытия
    deals = Deal.query.filter(
        Deal.manager_id == manager_id, Deal.outcome == "won",
        Deal.won_at >= utc_from, Deal.won_at < utc_to,
    ).all()
    payments = len(deals)
    payments_sum = sum(int(d.price or 0) for d in deals)

    # качество — из существующих оценок звонков за день
    scored = [
        c.overall_score for c in Call.query.filter(
            Call.manager_id == manager_id, Call.overall_score.isnot(None),
            Call.started_at >= utc_from, Call.started_at < utc_to,
        ).all()
    ]
    avg_call_score = round(sum(scored) / len(scored), 1) if scored else None

    row = db.session.get(ManagerDayStat, {"manager_id": manager_id, "date": day})
    if row is None:
        row = ManagerDayStat(manager_id=manager_id, date=day)
        db.session.add(row)
    row.calls_out = calls_out
    row.calls_connected = calls_connected
    row.talk_time_sec = talk_time_sec
    row.messages_out = messages_out
    row.touches = touches
    row.new_leads_contacted = 0        # требует дат создания лидов — следующий этап
    row.speed_to_lead_median_min = None
    row.first_action_at = first_action
    row.last_action_at = last_action
    row.idle_gaps_count = idle_gaps
    row.tasks_overdue = 0              # задачи amoCRM не синхронизируются на Этапе 1
    row.qualified = qualified
    row.meetings_set = meetings_set
    row.meetings_held = meetings_held
    row.invoices = invoices
    row.invoices_sum = invoices_sum
    row.payments = payments
    row.payments_sum = payments_sum
    row.avg_call_score = avg_call_score
    row.is_working_day = _is_working_day(day)
    db.session.commit()

    recompute_hours(manager_id, day, events)


def recompute_hours(manager_id: int, day: date, events=None) -> None:
    """Пересчитать почасовые агрегаты (для тепловой карты). Идемпотентно."""
    if events is None:
        utc_from, utc_to = _utc_bounds(day)
        events = OpsActivityEvent.query.filter(
            OpsActivityEvent.manager_id == manager_id,
            OpsActivityEvent.occurred_at >= utc_from,
            OpsActivityEvent.occurred_at < utc_to,
        ).all()

    buckets = {}  # hour -> dict
    for e in events:
        hour = to_local(e.occurred_at).hour
        b = buckets.setdefault(hour, {"calls_out": 0, "calls_connected": 0,
                                      "messages_out": 0, "talk_time_sec": 0})
        if e.type == "call_out":
            b["calls_out"] += 1
        if e.type in _CALL_TYPES and e.is_connected:
            b["calls_connected"] += 1
            b["talk_time_sec"] += int(e.duration_sec or 0)
        if e.type == "msg_out":
            b["messages_out"] += 1

    ManagerHourStat.query.filter_by(manager_id=manager_id, date=day).delete()
    for hour, b in buckets.items():
        db.session.add(ManagerHourStat(
            manager_id=manager_id, date=day, hour=hour,
            calls_out=b["calls_out"], calls_connected=b["calls_connected"],
            messages_out=b["messages_out"], talk_time_sec=b["talk_time_sec"],
        ))
    db.session.commit()


def _active_manager_ids(day: date) -> set:
    ids = {
        u.id for u in User.query.filter(
            User.is_active.is_(True), User.role == "manager"
        ).all()
    }
    utc_from, utc_to = _utc_bounds(day)
    for (mid,) in db.session.query(OpsActivityEvent.manager_id).filter(
        OpsActivityEvent.manager_id.isnot(None),
        OpsActivityEvent.occurred_at >= utc_from,
        OpsActivityEvent.occurred_at < utc_to,
    ).distinct().all():
        ids.add(mid)
    return ids


def recompute_for_date(app=None, day: date = None) -> dict:
    """Пересчитать агрегаты всех активных менеджеров за дату. Failure isolation."""
    app = app or current_app
    day = day or to_local(datetime.utcnow()).date()
    ok, errors = 0, 0
    for mid in _active_manager_ids(day):
        try:
            recompute_day(mid, day)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            errors += 1
            app.logger.warning("[ops_metrics] менеджер %s за %s: %s", mid, day, exc)
    return {"date": str(day), "managers": ok, "errors": errors}


def recompute_today(app=None) -> dict:
    app = app or current_app
    return recompute_for_date(app, to_local(datetime.utcnow()).date())


def recompute_yesterday(app=None) -> dict:
    app = app or current_app
    return recompute_for_date(app, to_local(datetime.utcnow()).date() - timedelta(days=1))


# --- baseline «мой средний день» -----------------------------------------

_BASELINE_FIELDS = [
    "calls_out", "calls_connected", "talk_time_sec", "messages_out", "touches",
    "qualified", "meetings_set", "meetings_held", "invoices", "payments",
]


def avg_day_baseline(manager_id: int, up_to: date, days: int = 20) -> dict:
    """Средние значения метрик по последним рабочим дням ДО up_to (мой стандарт)."""
    rows = (
        ManagerDayStat.query.filter(
            ManagerDayStat.manager_id == manager_id,
            ManagerDayStat.date < up_to,
            ManagerDayStat.is_working_day.is_(True),
        ).order_by(ManagerDayStat.date.desc()).limit(days).all()
    )
    if not rows:
        return {f: None for f in _BASELINE_FIELDS}
    out = {}
    for f in _BASELINE_FIELDS:
        vals = [float(getattr(r, f) or 0) for r in rows]
        out[f] = round(sum(vals) / len(vals), 1) if vals else None
    return out


def recent_working_days(up_to: date, count: int = 10) -> list:
    """Последние count рабочих дней включая up_to (для тепловой карты)."""
    out, cur = [], up_to
    guard = 0
    while len(out) < count and guard < count * 4 + 10:
        if _is_working_day(cur):
            out.append(cur)
        cur -= timedelta(days=1)
        guard += 1
    return out  # от свежих к старым
