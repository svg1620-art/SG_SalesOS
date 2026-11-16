"""Оценка реального рабочего времени менеджера по касаниям (общий модуль).

Единая логика для пульта РОПа (/ops/board) и продлений (/ops/renewals):
звонок = разговор (duration_sec для дозвона) + накладные на набор/завершение;
сообщение / задача / примечание — норматив. Активная загрузка = активное время
/ норма рабочего дня (часы смены минус обед). Пороги в ops_settings.
"""
from datetime import date

from extensions import db
from models import OpsActivityEvent
from ops_store import ops_int


def work_cfg():
    return {
        "call_overhead": ops_int("touch_call_overhead_sec", 30),
        "msg": ops_int("touch_msg_sec", 30),
        "task": ops_int("touch_task_sec", 30),
        "note": ops_int("touch_note_sec", 30),
    }


def event_work_sec(e, cfg) -> int:
    """Оценка затраченного времени на событие, сек."""
    t = e.type
    if t in ("call_out", "call_in"):
        base = cfg["call_overhead"]
        if e.is_connected:
            base += int(e.duration_sec or 0)
        return base
    if t in ("msg_out", "msg_in"):
        return cfg["msg"]
    if t == "task_done":
        return cfg["task"]
    if t == "note":
        return cfg["note"]
    return 0


def net_working_minutes() -> int:
    """Норма рабочего дня в минутах: (часы смены) − обед. Не меньше 1."""
    wh_from = ops_int("work_hours_from", 10)
    wh_to = ops_int("work_hours_to", 19)
    lunch = max(0, ops_int("lunch_break_min", 60))
    gross = max(0, (wh_to - wh_from)) * 60
    return max(1, gross - lunch)


def fmt_dur(sec) -> str:
    sec = int(sec or 0)
    if sec < 60:
        return f"{sec} с"
    m = sec // 60
    if m < 60:
        return f"{m} мин"
    return f"{m // 60} ч {m % 60:02d} мин"


def active_sec_by_manager(manager_ids, day=None) -> dict:
    """{manager_id: активные секунды за день} одним запросом — для пульта."""
    day = day or date.today()
    ids = list(manager_ids or [])
    if not ids:
        return {}
    from ops.metrics import _utc_bounds
    uf, ut = _utc_bounds(day)
    ev = OpsActivityEvent.query.filter(
        OpsActivityEvent.manager_id.in_(ids),
        OpsActivityEvent.occurred_at >= uf, OpsActivityEvent.occurred_at < ut,
    ).all()
    cfg = work_cfg()
    out = {}
    for e in ev:
        if e.manager_id is None:
            continue
        out[e.manager_id] = out.get(e.manager_id, 0) + event_work_sec(e, cfg)
    return out


def load_pct(active_sec) -> int:
    """Активная загрузка от нормы рабочего дня, %."""
    net_sec = net_working_minutes() * 60
    return min(100, round(active_sec / net_sec * 100)) if net_sec else 0


def manager_load(manager_id, day=None) -> dict:
    """Детальная оценка по одному менеджеру за день (для карточки/детали)."""
    day = day or date.today()
    from ops.metrics import _utc_bounds
    from utils import to_local
    uf, ut = _utc_bounds(day)
    ev = OpsActivityEvent.query.filter(
        OpsActivityEvent.manager_id == manager_id,
        OpsActivityEvent.occurred_at >= uf, OpsActivityEvent.occurred_at < ut,
    ).all()
    cfg = work_cfg()
    total = talk = 0
    by = {"calls": 0, "msgs": 0, "tasks": 0, "notes": 0}
    times = []
    for e in ev:
        total += event_work_sec(e, cfg)
        times.append(e.occurred_at)
        if e.type in ("call_out", "call_in"):
            by["calls"] += 1
            if e.is_connected:
                talk += int(e.duration_sec or 0)
        elif e.type in ("msg_out", "msg_in"):
            by["msgs"] += 1
        elif e.type == "task_done":
            by["tasks"] += 1
        elif e.type == "note":
            by["notes"] += 1
    net_min = net_working_minutes()
    span = 0
    first = last = None
    if times:
        first, last = min(times), max(times)
        span = int((last - first).total_seconds())
    return {
        "count": len(ev), "total_sec": total, "total_label": fmt_dur(total),
        "talk_label": fmt_dur(talk), "by": by,
        "net_min": net_min, "net_label": fmt_dur(net_min * 60),
        "load_pct": load_pct(total),
        "span_sec": span, "span_label": fmt_dur(span) if span else "—",
        "idle_sec": max(0, span - total), "idle_label": fmt_dur(max(0, span - total)) if span else "—",
        "first": to_local(first).strftime("%H:%M") if first else None,
        "last": to_local(last).strftime("%H:%M") if last else None,
    }
