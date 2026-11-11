"""Вовлечённость менеджера: тепловая карта активности по часам.

Считаем «касания» — любые действия менеджера из ops_activity_events (звонки,
сообщения; CRM-записи и задачи — каркас готов, появятся после расширения сбора).
Данные те же, что у «Операционного пульта»; ничего нового не интегрируем.

Используется на дашборде РОПа (команда + разбор по менеджеру) и на «Мой день»
(личный блок менеджера).
"""
from extensions import db
from models import OpsActivityEvent, User
from utils import now_local, to_local
from ops_store import ops_int
from ops.metrics import recent_working_days, _utc_bounds

# тип касания → (события amoCRM, цвет, подпись)
TOUCH_TYPES = [
    ("calls", ("call_out", "call_in"), "#1467F5", "Звонки"),
    ("msgs", ("msg_out", "msg_in"), "#00BFDC", "Сообщения"),
    ("crm", ("note",), "#8B7CF6", "CRM-записи"),
    ("tasks", ("task_done",), "#F5C542", "Задачи"),
]
ALL_EVENT_TYPES = [t for _, types, _, _ in TOUCH_TYPES for t in types]
_TYPE_TO_KEY = {t: key for key, types, _, _ in TOUCH_TYPES for t in types}


def _hours():
    f = ops_int("work_hours_from", 9)
    t = ops_int("work_hours_to", 20)
    return list(range(f, t))


def _metric_types(metric):
    if metric == "all":
        return set(ALL_EVENT_TYPES)
    for key, types, _, _ in TOUCH_TYPES:
        if key == metric:
            return set(types)
    return set(ALL_EVENT_TYPES)


def _cell(v, mx):
    """Клетка тепловой карты: значение + цвет фона/текста (один синий тон)."""
    if v <= 0:
        return {"v": 0, "bg": "rgba(255,255,255,.035)", "fg": "var(--sg-faint,#5c5c66)"}
    inten = (v / mx) if mx else 0
    a = 0.10 + 0.85 * (inten ** 0.75)
    fg = "#fff" if inten > 0.45 else "var(--sg-muted,#8A8A96)"
    return {"v": v, "bg": f"rgba(20,103,245,{a:.3f})", "fg": fg}


def _fetch(manager_ids, utc_from, utc_to):
    q = OpsActivityEvent.query.filter(
        OpsActivityEvent.type.in_(ALL_EVENT_TYPES),
        OpsActivityEvent.occurred_at >= utc_from,
        OpsActivityEvent.occurred_at < utc_to,
    )
    if manager_ids is not None:
        q = q.filter(OpsActivityEvent.manager_id.in_(list(manager_ids)))
    else:
        q = q.filter(OpsActivityEvent.manager_id.isnot(None))
    return q.with_entities(
        OpsActivityEvent.manager_id, OpsActivityEvent.type, OpsActivityEvent.occurred_at
    ).all()


def manager_engagement(manager_id, metric="all", up_to=None, days=10):
    """Данные вовлечённости одного менеджера: KPI, тепловая карта, состав."""
    up_to = up_to or now_local().date()
    day_list = recent_working_days(up_to, days)  # от свежих к старым
    if not day_list:
        return None
    oldest = day_list[-1]
    utc_from = _utc_bounds(oldest)[0]
    utc_to = _utc_bounds(up_to)[1]
    rows = _fetch([manager_id], utc_from, utc_to)

    hours = _hours()
    hidx = {h: i for i, h in enumerate(hours)}
    didx = {d: i for i, d in enumerate(day_list)}
    mtypes = _metric_types(metric)

    grid_v = [[0] * len(hours) for _ in day_list]
    comp = {h: {key: 0 for key, _, _, _ in TOUCH_TYPES} for h in hours}
    present = set()  # какие типы реально встречались (для подсказки про CRM/задачи)

    for _mid, ev_type, occurred in rows:
        loc = to_local(occurred)
        h, d = loc.hour, loc.date()
        if h not in hidx or d not in didx:
            continue
        present.add(_TYPE_TO_KEY.get(ev_type))
        if ev_type in mtypes:
            grid_v[didx[d]][hidx[h]] += 1
        if d == up_to:
            comp[h][_TYPE_TO_KEY.get(ev_type)] += 1

    grid_max = max((max(r) for r in grid_v), default=0)
    grid = [[_cell(v, grid_max) for v in row] for row in grid_v]

    # KPI по сегодня (все касания)
    today_by_hour = {h: sum(comp[h].values()) for h in hours}
    active = [h for h in hours if today_by_hour[h] > 0]
    total_today = sum(today_by_hour.values())
    first_h = active[0] if active else None
    last_h = active[-1] if active else None
    quiet_gap = 0
    if first_h is not None:
        run = 0
        for h in hours:
            if h < first_h or h > last_h:
                continue
            if today_by_hour[h] == 0:
                run += 1
                quiet_gap = max(quiet_gap, run)
            else:
                run = 0
    kpi = {
        "active_hours": len(active), "work_hours": len(hours),
        "first_h": first_h, "last_h": last_h,
        "total_today": total_today, "quiet_gap": quiet_gap,
    }

    comp_max = max((today_by_hour[h] for h in hours), default=0) or 1
    composition = []
    for h in hours:
        segs = []
        for key, _types, color, _label in TOUCH_TYPES:
            val = comp[h][key]
            segs.append({"color": color, "val": val,
                         "pct": round(val / comp_max * 100, 1) if val else 0})
        composition.append({"hour": h, "total": today_by_hour[h], "segs": segs})

    types = [{"key": k, "label": lb, "color": c} for k, _t, c, lb in TOUCH_TYPES]
    return {
        "hours": hours,
        "days": [{"label": d.strftime("%d.%m"), "is_today": d == up_to} for d in day_list],
        "grid": grid, "grid_max": grid_max,
        "metric": metric, "types": types,
        "kpi": kpi, "composition": composition, "comp_max": comp_max,
        "crm_tasks_present": bool(present & {"crm", "tasks"}),
    }


def team_engagement(manager_ids=None, up_to=None):
    """Полоса «команда сегодня»: менеджеры × часы, суммарные касания."""
    up_to = up_to or now_local().date()
    hours = _hours()
    hidx = {h: i for i, h in enumerate(hours)}
    utc_from, utc_to = _utc_bounds(up_to)
    rows = _fetch(manager_ids, utc_from, utc_to)

    per = {}
    for mid, _type, occurred in rows:
        loc = to_local(occurred)
        if loc.hour not in hidx:
            continue
        per.setdefault(mid, [0] * len(hours))[hidx[loc.hour]] += 1

    managers = (
        User.query.filter(User.is_active.is_(True), User.role == "manager")
        .order_by(User.full_name, User.email).all()
    )
    if manager_ids is not None:
        managers = [m for m in managers if m.id in manager_ids]
    mx = max((max(v) for v in per.values()), default=0)
    out = []
    for m in managers:
        vals = per.get(m.id, [0] * len(hours))
        out.append({
            "manager_id": m.id, "name": m.full_name or m.email,
            "total": sum(vals), "cells": [_cell(v, mx) for v in vals],
        })
    out.sort(key=lambda r: (-r["total"], r["name"].lower()))
    return {"hours": hours, "rows": out, "max": mx}
