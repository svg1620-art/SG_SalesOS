"""Оперативный пульт РОПа (Этап 3): строки «Сейчас», воронки/конверсии, прогноз.

Читает материализованные агрегаты (manager_day_stats / manager_hour_stats) и
funnel_events (для когортных конверсий) — без обращения к сырым событиям (§14).

Компромиссы Этапа 3 (до появления миссий на Этапе 5):
- норма дозвонов для светофора = users.daily_call_plan (или медиана команды),
  вместо Silver-нормы миссии;
- выручка/прогноз — по оплатам (won); «первая выручка» уточнится на Этапе 4.
"""
from datetime import datetime, date, timedelta
from calendar import monthrange
from statistics import median

from extensions import db
from models import (
    User, ManagerDayStat, ManagerHourStat, ManagerPlan, FunnelEvent, WorkCalendar,
)
from utils import now_local, to_local
from ops_store import ops_int, ops_float

# цепочка воронки для конверсий
FLOW_STAGES = [
    ("calls_connected", "Дозвоны"),
    ("qualified", "Квалификации"),
    ("meetings_set", "Встречи назн."),
    ("meetings_held", "Встречи пров."),
    ("invoices", "Счета"),
    ("payments", "Оплаты"),
]
COHORT_PAIRS = [
    ("qualified", "meeting_set", "Квал→Встреча назн."),
    ("meeting_set", "meeting_held", "Встреча назн→пров."),
    ("meeting_held", "invoice", "Встреча пров→Счёт"),
    ("invoice", "won", "Счёт→Оплата"),
]


def _active_managers(dept_manager_ids=None):
    q = User.query.filter(User.is_active.is_(True), User.role == "manager")
    managers = q.order_by(User.full_name, User.email).all()
    if dept_manager_ids is not None:
        managers = [m for m in managers if m.id in dept_manager_ids]
    return managers


def _is_working_day(d: date) -> bool:
    row = db.session.get(WorkCalendar, d)
    return bool(row.is_working_day) if row is not None else d.weekday() < 5


def _parse_hhmm(raw: str, default_h: int = 10):
    try:
        h, m = str(raw).split(":")
        return int(h), int(m)
    except Exception:  # noqa: BLE001
        return default_h, 0


def _working_days_left(today: date) -> int:
    last = today.replace(day=monthrange(today.year, today.month)[1])
    n, cur = 0, today
    while cur <= last:
        if _is_working_day(cur):
            n += 1
        cur += timedelta(days=1)
    return n


def forecast_for(manager_id: int, today: date) -> dict:
    """Упрощённый прогноз (§7.4): оплаты MTD + run-rate × остаток × k_dedup."""
    first = today.replace(day=1)
    plan_row = db.session.get(ManagerPlan, {"manager_id": manager_id, "month": first})
    revenue_plan = float(plan_row.revenue_plan) if plan_row and plan_row.revenue_plan else 0.0

    mtd_rows = ManagerDayStat.query.filter(
        ManagerDayStat.manager_id == manager_id,
        ManagerDayStat.date >= first, ManagerDayStat.date <= today,
    ).all()
    payments_mtd = sum(float(r.payments_sum or 0) for r in mtd_rows)

    base_days = ops_int("forecast_baseline_days", 20)
    hist = (
        ManagerDayStat.query.filter(
            ManagerDayStat.manager_id == manager_id,
            ManagerDayStat.date < today,
            ManagerDayStat.is_working_day.is_(True),
        ).order_by(ManagerDayStat.date.desc()).limit(base_days).all()
    )
    avg_daily = (
        sum(float(r.payments_sum or 0) for r in hist) / len(hist) if hist else 0.0
    )
    k = ops_float("k_dedup", 0.5)
    wd_left = _working_days_left(today)
    forecast = payments_mtd + avg_daily * wd_left * k
    return {
        "revenue_plan": revenue_plan,
        "payments_mtd": payments_mtd,
        "progress_pct": round(payments_mtd / revenue_plan * 100) if revenue_plan else None,
        "forecast": forecast,
        "forecast_pct": round(forecast / revenue_plan * 100) if revenue_plan else None,
        "working_days_left": wd_left,
    }


def _status(row, team_median_cr, team_median_calls, now_l) -> str:
    """Светофор дня (§7.2), адаптированный под Этап 3."""
    idle_min = ops_int("idle_gap_min", 60)
    late_h, late_m = _parse_hhmm(ops_store_late_start(), 10)
    wh_from = ops_int("work_hours_from", 9)
    wh_to = ops_int("work_hours_to", 20)
    aft_hour = ops_int("board_afternoon_hour", 15)
    red_pct = ops_int("board_red_calls_pct", 40) / 100.0
    yel_pct = ops_int("board_yellow_calls_pct", 70) / 100.0
    cr_min = ops_int("board_connect_rate_min_pct", 60) / 100.0

    norm = row["norm_calls"]
    connected = row["calls_connected"]
    in_work_now = wh_from <= now_l.hour < wh_to
    after_late = (now_l.hour, now_l.minute) >= (late_h, late_m)
    after_noon = now_l.hour >= aft_hour

    # 🔴
    if after_late and row["first_action_at"] is None:
        return "red"
    if in_work_now and row["minutes_since_last"] is not None and \
            row["minutes_since_last"] > 2 * idle_min:
        return "red"
    if after_noon and norm and connected < red_pct * norm:
        return "red"
    # 🟡
    if after_noon and norm and connected < yel_pct * norm:
        return "yellow"
    if row["connect_rate"] is not None and team_median_cr and \
            row["connect_rate"] < cr_min * team_median_cr:
        return "yellow"
    return "green"


def ops_store_late_start():
    from ops_store import get_ops_setting
    return get_ops_setting("late_start_time") or "10:00"


def board_rows(today: date = None, dept_manager_ids=None) -> list:
    today = today or now_local().date()
    now_l = now_local()
    now_utc = datetime.utcnow()
    managers = _active_managers(dept_manager_ids)

    stats = {
        s.manager_id: s for s in ManagerDayStat.query.filter(
            ManagerDayStat.date == today,
            ManagerDayStat.manager_id.in_([m.id for m in managers] or [-1]),
        ).all()
    }

    # медианы команды за сегодня
    crs, calls = [], []
    for m in managers:
        s = stats.get(m.id)
        if s and s.calls_out:
            crs.append(s.calls_connected / s.calls_out)
        if s:
            calls.append(s.calls_connected)
    team_median_cr = median(crs) if crs else None
    team_median_calls = median(calls) if calls else None

    rows = []
    for m in managers:
        s = stats.get(m.id)
        calls_out = int(s.calls_out) if s else 0
        connected = int(s.calls_connected) if s else 0
        cr = (connected / calls_out) if calls_out else None
        last_action = s.last_action_at if s else None
        minutes_since = (
            (now_utc - last_action).total_seconds() / 60.0 if last_action else None
        )
        first_local = to_local(s.first_action_at) if s and s.first_action_at else None
        norm = m.daily_call_plan or (round(team_median_calls) if team_median_calls else None)
        fc = forecast_for(m.id, today)
        row = {
            "manager": m,
            "name": m.full_name or m.email,
            "calls_out": calls_out,
            "calls_connected": connected,
            "connect_rate": cr,
            "connect_rate_pct": round(cr * 100) if cr is not None else None,
            "norm_calls": norm,
            "calls_pct_of_norm": round(connected / norm * 100) if norm else None,
            "touches": int(s.touches) if s else 0,
            "messages_out": int(s.messages_out) if s else 0,
            "qualified": int(s.qualified) if s else 0,
            "meetings_set": int(s.meetings_set) if s else 0,
            "meetings_held": int(s.meetings_held) if s else 0,
            "first_action_at": s.first_action_at if s else None,
            "first_action_label": first_local.strftime("%H:%M") if first_local else None,
            "minutes_since_last": minutes_since,
            "last_action_label": _ago_label(minutes_since),
            "avg_call_score": s.avg_call_score if s else None,
            "month": fc,
        }
        row["status"] = _status(row, team_median_cr, team_median_calls, now_l)
        rows.append(row)
    return rows


def _ago_label(minutes):
    if minutes is None:
        return "—"
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} мин назад"
    if minutes < 60 * 24:
        return f"{minutes // 60} ч назад"
    return f"{minutes // (60 * 24)} дн назад"


# --- Воронки и конверсии --------------------------------------------------

def _period_bounds(from_d: date, to_d: date):
    return from_d, to_d


def funnel_counts(from_d: date, to_d: date, manager_ids) -> dict:
    """Суммы ступеней воронки за период: team + по менеджерам (из day_stats)."""
    rows = ManagerDayStat.query.filter(
        ManagerDayStat.date >= from_d, ManagerDayStat.date <= to_d,
        ManagerDayStat.manager_id.in_(manager_ids or [-1]),
    ).all()
    per = {}
    team = {k: 0 for k, _ in FLOW_STAGES}
    for r in rows:
        d = per.setdefault(r.manager_id, {k: 0 for k, _ in FLOW_STAGES})
        for key, _ in FLOW_STAGES:
            v = int(getattr(r, key) or 0)
            d[key] += v
            team[key] += v
    return {"team": team, "per_manager": per}


def _ratio(a, b):
    return (a / b) if b else None


def flow_conversions(counts_for_one: dict) -> list:
    """Потоковые конверсии для одного набора сумм: count[i+1]/count[i]."""
    out = []
    for (k1, l1), (k2, l2) in zip(FLOW_STAGES, FLOW_STAGES[1:]):
        out.append({"label": f"{l1}→{l2}",
                    "value": _ratio(counts_for_one.get(k2, 0), counts_for_one.get(k1, 0))})
    return out


def cohort_conversions(from_d: date, to_d: date, manager_id=None) -> list:
    """Когортные конверсии по funnel-шагам: вошёл в шаг → дошёл до следующего
    в пределах cohort_window_days."""
    from utils import app_tz, local_to_utc_naive
    tz = app_tz()
    utc_from = local_to_utc_naive(datetime(from_d.year, from_d.month, from_d.day, tzinfo=tz))
    utc_to = local_to_utc_naive(datetime(to_d.year, to_d.month, to_d.day, tzinfo=tz) + timedelta(days=1))
    window = timedelta(days=ops_int("cohort_window_days", 45))

    out = []
    for entered_step, next_step, label in COHORT_PAIRS:
        q = FunnelEvent.query.filter(
            FunnelEvent.step == entered_step,
            FunnelEvent.occurred_at >= utc_from, FunnelEvent.occurred_at < utc_to,
        )
        if manager_id is not None:
            q = q.filter(FunnelEvent.manager_id == manager_id)
        entered = {fe.lead_id: fe.occurred_at for fe in q.all()}
        if not entered:
            out.append({"label": label, "value": None, "n": 0})
            continue
        nexts = {
            fe.lead_id: fe.occurred_at for fe in FunnelEvent.query.filter(
                FunnelEvent.step == next_step,
                FunnelEvent.lead_id.in_(list(entered.keys())),
            ).all()
        }
        reached = sum(
            1 for lid, t0 in entered.items()
            if lid in nexts and nexts[lid] - t0 <= window and nexts[lid] >= t0
        )
        out.append({"label": label, "value": reached / len(entered), "n": len(entered)})
    return out


def team_heatmap(window_days=None) -> dict:
    """Командная тепловая карта «день недели × час» по connect rate."""
    window_days = window_days or ops_int("heatmap_window_days", 30)
    today = now_local().date()
    since = today - timedelta(days=window_days)
    rows = ManagerHourStat.query.filter(ManagerHourStat.date >= since).all()
    wh_from = ops_int("work_hours_from", 9)
    wh_to = ops_int("work_hours_to", 20)

    agg = {}  # (weekday, hour) -> [connected, out]
    for r in rows:
        if not (wh_from <= r.hour < wh_to):
            continue
        wd = r.date.weekday()
        a = agg.setdefault((wd, r.hour), [0, 0])
        a[0] += int(r.calls_connected or 0)
        a[1] += int(r.calls_out or 0)

    cells, best = [], []
    for (wd, hour), (conn, out) in agg.items():
        cr = (conn / out) if out else None
        if cr is not None and out >= 3:
            best.append({"weekday": wd, "hour": hour, "cr": cr, "out": out})
    best.sort(key=lambda x: -x["cr"])
    return {
        "agg": agg, "hours": list(range(wh_from, wh_to)),
        "best": best[:5],
    }
