"""Аналитика звонков: попытки → дозвоны (сняли трубку) → разговоры.

Разделяем три уровня (решение по жалобе «набираю, а не берут трубку — не
засчитывается»):
- **Попытка** — любой исходящий звонок (менеджер набрал). Полностью под
  контролем менеджера.
- **Дозвон** — клиент снял трубку: `duration_sec > 0`. Зависит от базы и времени.
- **Разговор** — состоявшийся разговор `duration_sec >= connect_min_sec` (качество).

Считается напрямую из OpsActivityEvent за произвольный период — без изменения
агрегатов/геймификации (существующий функционал не трогаем).
"""
from datetime import date, timedelta

from extensions import db
from models import OpsActivityEvent, User
from utils import to_local, now_local, local_to_utc_naive
from ops_store import connect_min_sec


def _utc_range(from_d: date, to_d: date):
    """[from_d 00:00, to_d+1 00:00) по локальному TZ → UTC-naive границы."""
    from datetime import datetime, time
    start_local = datetime.combine(from_d, time.min)
    end_local = datetime.combine(to_d + timedelta(days=1), time.min)
    return local_to_utc_naive(start_local), local_to_utc_naive(end_local)


def _blank():
    return {"attempts": 0, "pickups": 0, "talks": 0, "talk_time": 0,
            "in_answered": 0}


def _rates(agg: dict) -> dict:
    a, p, t = agg["attempts"], agg["pickups"], agg["talks"]
    agg["connect_rate"] = round(p / a * 100) if a else None      # % дозвона
    agg["talk_rate"] = round(t / p * 100) if p else None          # % разговора из дозвонов
    agg["avg_talk"] = round(agg["talk_time"] / t) if t else 0     # ср. длительность, сек
    return agg


def calls_overview(from_d: date, to_d: date, manager_ids=None) -> dict:
    """Сводка по звонкам за период: по менеджерам + по дням + итого.

    manager_ids=None — все; иначе ограничить набором id (фильтр по отделу).
    """
    threshold = connect_min_sec()
    utc_from, utc_to = _utc_range(from_d, to_d)

    q = OpsActivityEvent.query.filter(
        OpsActivityEvent.type.in_(["call_out", "call_in"]),
        OpsActivityEvent.occurred_at >= utc_from,
        OpsActivityEvent.occurred_at < utc_to,
    ).with_entities(
        OpsActivityEvent.manager_id, OpsActivityEvent.type,
        OpsActivityEvent.duration_sec, OpsActivityEvent.occurred_at,
    )

    per_mgr = {}
    per_day = {}
    total = _blank()
    for manager_id, ev_type, duration, occurred in q:
        dur = int(duration or 0)
        if ev_type == "call_in":
            if dur > 0:
                total["in_answered"] += 1
                if manager_id is not None:
                    per_mgr.setdefault(manager_id, _blank())["in_answered"] += 1
            continue
        # call_out
        d_local = to_local(occurred).date() if occurred else None
        day_agg = per_day.setdefault(d_local, _blank()) if d_local else None
        m_agg = per_mgr.setdefault(manager_id, _blank()) if manager_id is not None else None
        for bucket in (total, day_agg, m_agg):
            if bucket is None:
                continue
            bucket["attempts"] += 1
            if dur > 0:
                bucket["pickups"] += 1
            if dur >= threshold:
                bucket["talks"] += 1
                bucket["talk_time"] += dur

    # строки по менеджерам
    managers = (
        User.query.filter(User.is_active.is_(True), User.role == "manager")
        .order_by(User.full_name, User.email).all()
    )
    if manager_ids is not None:
        managers = [m for m in managers if m.id in manager_ids]
    mgr_rows = []
    for m in managers:
        agg = per_mgr.get(m.id, _blank())
        row = {"name": m.full_name or m.email, "no_amo": m.amo_user_id is None}
        row.update(_rates(dict(agg)))
        mgr_rows.append(row)
    # менеджер сверху по попыткам
    mgr_rows.sort(key=lambda r: (-r["attempts"], r["name"].lower()))

    # непривязанные к менеджеру исходящие (нет amo_user_id → manager_id=None)
    unattributed = None
    if None in per_mgr:
        unattributed = _rates(dict(per_mgr[None]))

    # ряд по дням (для графика), по возрастанию даты
    days = []
    cur = from_d
    while cur <= to_d:
        a = per_day.get(cur, _blank())
        days.append({"date": cur, "attempts": a["attempts"], "pickups": a["pickups"],
                     "talks": a["talks"]})
        cur += timedelta(days=1)

    return {
        "from": from_d, "to": to_d, "threshold": threshold,
        "total": _rates(dict(total)),
        "mgr_rows": mgr_rows, "unattributed": unattributed,
        "days": days,
    }
