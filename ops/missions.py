"""Миссии и геймификация (Этап 5, §9).

Генерация дневных целей (обратная воронка), тиры Bronze/Silver/Gold, уровни,
серии и заморозки, рекорды, единый журнал XP (идемпотентно), анти-накрутка и
quality gate, командный квест недели.

Компромиссы (помечены): new_leads_contacted не считается (нет дат создания
лидов) — в метриках уровня 1 заменён на messages_out; откат XP за 24ч-реверс
этапа не реализован (funnel_events хранят только первое достижение).
"""
from datetime import datetime, date, timedelta
from math import ceil
from statistics import median

from flask import current_app

from extensions import db
from models import (
    User, DailyMission, ManagerProgress, XpLedger, ManagerDayStat,
    OpsActivityEvent, FunnelEvent,
)
from ops_store import ops_int, ops_float, ops_json, get_ops_setting
from ops.metrics import _utc_bounds, _is_working_day, recent_working_days
from ops.payback import first_revenue, month_first, _month_bounds

RANK = {"none": 0, "bronze": 1, "silver": 2, "gold": 3}
RANK_NAMES = ["none", "bronze", "silver", "gold"]

LEVEL_METRICS = {
    1: ["calls_connected", "touches", "messages_out"],
    2: ["calls_connected", "qualified", "meetings_set"],
    3: ["qualified", "meetings_held", "invoices"],
}
LEVEL_NAMES = {1: "Ритм", 2: "Воронка", 3: "Результат"}
# метрики, на которые влияет обратная воронка (остальные — личный стандарт)
FUNNEL_METRICS = {"calls_connected", "qualified", "meetings_set", "meetings_held", "invoices"}
RECORD_METRICS = ["calls_connected", "touches", "qualified", "meetings_held", "invoices"]
METRIC_LABELS = {
    "calls_connected": "Дозвоны", "touches": "Касания", "messages_out": "Сообщения",
    "qualified": "Квалификации", "meetings_set": "Встречи назн.",
    "meetings_held": "Встречи пров.", "invoices": "Счета",
}


# --- XP leger helpers (идемпотентно по source+ref) ------------------------

def grant_xp(manager_id, source, ref, amount, occurred=None) -> bool:
    if not amount or amount <= 0 or manager_id is None:
        return False
    if XpLedger.query.filter_by(source=source, ref=ref).first():
        return False
    db.session.add(XpLedger(
        manager_id=manager_id, source=source, ref=ref, amount=int(amount),
        occurred_at=occurred or datetime.utcnow(),
    ))
    return True


def set_xp(manager_id, source, ref, amount, occurred=None) -> None:
    """Для изменяемых начислений (день-тир пересчитывается в течение дня)."""
    row = XpLedger.query.filter_by(source=source, ref=ref).first()
    amount = int(amount or 0)
    if row is None:
        if amount > 0 and manager_id is not None:
            db.session.add(XpLedger(manager_id=manager_id, source=source, ref=ref,
                                    amount=amount, occurred_at=occurred or datetime.utcnow()))
    else:
        row.amount = amount


def xp_total(manager_id: int) -> int:
    return int(db.session.query(db.func.coalesce(db.func.sum(XpLedger.amount), 0))
               .filter(XpLedger.manager_id == manager_id).scalar() or 0)


def xp_since(manager_id: int, since: datetime) -> int:
    return int(db.session.query(db.func.coalesce(db.func.sum(XpLedger.amount), 0))
               .filter(XpLedger.manager_id == manager_id,
                       XpLedger.occurred_at >= since).scalar() or 0)


# --- базовые величины -----------------------------------------------------

def _hist_rows(manager_id, before: date, days: int):
    return (
        ManagerDayStat.query.filter(
            ManagerDayStat.manager_id == manager_id,
            ManagerDayStat.date < before, ManagerDayStat.is_working_day.is_(True),
        ).order_by(ManagerDayStat.date.desc()).limit(days).all()
    )


def _own_median(manager_id, metric, before: date, days=20):
    rows = _hist_rows(manager_id, before, days)
    vals = [float(getattr(r, metric) or 0) for r in rows]
    return median(vals) if vals else 0.0


def _conversions(manager_id, before: date):
    """CR обратной воронки + средний чек по своим 90 дням (fallback — команда)."""
    def _cr_set(mid):
        rows = _hist_rows(mid, before, 90)
        agg = {k: 0 for k in ("calls_connected", "qualified", "meetings_set",
                              "meetings_held", "invoices")}
        for r in rows:
            for k in agg:
                agg[k] += int(getattr(r, k) or 0)
        return agg

    own = _cr_set(manager_id)

    def cr(num, den, fallback):
        return (own[num] / own[den]) if own[den] else fallback

    # средний чек по первой выручке за 90 дней
    from models import Deal
    since = datetime.combine(before - timedelta(days=90), datetime.min.time())
    deals = Deal.query.filter(Deal.manager_id == manager_id, Deal.outcome == "won",
                              Deal.is_first_revenue.is_(True), Deal.won_at >= since).all()
    rev_sum = sum(int(d.price or 0) for d in deals)
    rev_cnt = len(deals)
    avg_check = (rev_sum / rev_cnt) if rev_cnt else 0.0

    return {
        "cr_c_q": cr("qualified", "calls_connected", 0.3),
        "cr_q_ms": cr("meetings_set", "qualified", 0.5),
        "cr_ms_mh": cr("meetings_held", "meetings_set", 0.6),
        "cr_mh_inv": cr("invoices", "meetings_held", 0.5),
        "cr_inv_pay": (rev_cnt / own["invoices"]) if own["invoices"] else 0.5,
        "avg_check": avg_check,
    }


# --- генерация миссии -----------------------------------------------------

def _progress(manager_id) -> ManagerProgress:
    p = db.session.get(ManagerProgress, manager_id)
    if p is None:
        p = ManagerProgress(manager_id=manager_id, level=1, records={})
        db.session.add(p)
        db.session.commit()
    return p


def generate_mission(manager: User, day: date) -> DailyMission:
    """Создать/пересоздать цели миссии на день (idempotent — перезапишет targets)."""
    prog = _progress(manager.id)
    level = prog.level or 1
    metrics = LEVEL_METRICS.get(level, LEVEL_METRICS[1])
    floors = ops_json("floor_silver_json", {}) or {}
    gold_ratio = ops_float("gold_ratio", 1.25)
    bronze_ratio = ops_float("bronze_ratio", 0.8)
    max_silver_ratio = ops_float("max_silver_ratio", 2.0)

    # потребность по обратной воронке
    mf = month_first(day)
    plan_row = _plan(manager.id, mf)
    revenue_plan = float(plan_row.revenue_plan) if plan_row and plan_row.revenue_plan else 0.0
    rev_mtd, _ = first_revenue(manager.id, mf)
    need_revenue = max(revenue_plan - rev_mtd, 0.0)
    days_left = max(1, _working_days_left(day))
    conv = _conversions(manager.id, day)

    required = {}
    if need_revenue > 0 and conv["avg_check"] > 0:
        pay_pd = (need_revenue / conv["avg_check"]) / days_left
        inv_pd = pay_pd / conv["cr_inv_pay"] if conv["cr_inv_pay"] else 0
        mh_pd = inv_pd / conv["cr_mh_inv"] if conv["cr_mh_inv"] else 0
        ms_pd = mh_pd / conv["cr_ms_mh"] if conv["cr_ms_mh"] else 0
        q_pd = ms_pd / conv["cr_q_ms"] if conv["cr_q_ms"] else 0
        c_pd = q_pd / conv["cr_c_q"] if conv["cr_c_q"] else 0
        required = {"calls_connected": c_pd, "qualified": q_pd, "meetings_set": ms_pd,
                    "meetings_held": mh_pd, "invoices": inv_pd}

    targets = {}
    for metric in metrics:
        own_med = _own_median(manager.id, metric, day, 20)
        base = required.get(metric) if (metric in FUNNEL_METRICS and metric in required) else own_med
        silver = max(base or 0, float(floors.get(metric, 0)))
        if own_med and silver > max_silver_ratio * own_med:
            silver = max_silver_ratio * own_med
        silver = int(ceil(silver))
        bronze = int(ceil(min(own_med if own_med else silver * bronze_ratio, bronze_ratio * silver)))
        bronze = max(0, min(bronze, silver))
        gold = int(ceil(gold_ratio * silver)) if silver else 0
        targets[metric] = {"bronze": bronze, "silver": silver, "gold": gold}

    m = db.session.get(DailyMission, {"manager_id": manager.id, "date": day})
    if m is None:
        m = DailyMission(manager_id=manager.id, date=day)
        db.session.add(m)
    if not m.finalized:
        m.level = level
        m.targets = targets
    db.session.commit()
    return m


def _plan(manager_id, mf):
    from models import ManagerPlan
    return db.session.get(ManagerPlan, {"manager_id": manager_id, "month": mf})


def _working_days_left(day: date) -> int:
    from calendar import monthrange
    last = day.replace(day=monthrange(day.year, day.month)[1])
    n, cur = 0, day
    while cur <= last:
        if _is_working_day(cur):
            n += 1
        cur += timedelta(days=1)
    return n


# --- значения метрик дня с анти-накруткой --------------------------------

def mission_metric_values(manager_id: int, day: date) -> dict:
    utc_from, utc_to = _utc_bounds(day)
    events = OpsActivityEvent.query.filter(
        OpsActivityEvent.manager_id == manager_id,
        OpsActivityEvent.occurred_at >= utc_from, OpsActivityEvent.occurred_at < utc_to,
    ).all()
    cap_calls = ops_int("max_calls_per_contact_day", 3)
    cap_msgs = ops_int("max_messages_per_contact_day", 10)

    calls_by_contact = {}
    msgs_by_contact = {}
    calls_no_contact = 0
    touch_contacts = set()
    msgs_total_nc = 0
    for e in events:
        if e.type in ("call_out", "call_in") and e.is_connected:
            if e.contact_id is not None:
                calls_by_contact[e.contact_id] = calls_by_contact.get(e.contact_id, 0) + 1
                touch_contacts.add(e.contact_id)
            else:
                calls_no_contact += 1
        elif e.type == "msg_out":
            if e.contact_id is not None:
                msgs_by_contact[e.contact_id] = msgs_by_contact.get(e.contact_id, 0) + 1
                touch_contacts.add(e.contact_id)
            else:
                msgs_total_nc += 1

    calls_connected = sum(min(c, cap_calls) for c in calls_by_contact.values()) + calls_no_contact
    messages_out = sum(min(c, cap_msgs) for c in msgs_by_contact.values()) + msgs_total_nc
    touches = len(touch_contacts)

    fe = FunnelEvent.query.filter(
        FunnelEvent.manager_id == manager_id,
        FunnelEvent.occurred_at >= utc_from, FunnelEvent.occurred_at < utc_to,
    ).all()
    qualified = sum(1 for f in fe if f.step == "qualified")
    meetings_set = sum(1 for f in fe if f.step == "meeting_set")
    meetings_held = sum(1 for f in fe if f.step == "meeting_held")
    invoices = sum(1 for f in fe if f.step == "invoice")

    stat = db.session.get(ManagerDayStat, {"manager_id": manager_id, "date": day})
    return {
        "calls_connected": calls_connected, "touches": touches, "messages_out": messages_out,
        "qualified": qualified, "meetings_set": meetings_set, "meetings_held": meetings_held,
        "invoices": invoices,
        "_avg_call_score": (stat.avg_call_score if stat else None),
        "_funnel_rows": fe,
    }


def _tier_for(value, t) -> str:
    silver = t.get("silver", 0)
    if silver <= 0:
        return "gold"  # не задано — не ограничивает день
    if value >= t.get("gold", 0) and t.get("gold", 0) > 0:
        return "gold"
    if value >= silver:
        return "silver"
    if value >= t.get("bronze", 0):
        return "bronze"
    return "none"


# --- оценка миссии --------------------------------------------------------

def evaluate_mission(manager: User, day: date, finalize: bool = False) -> DailyMission:
    m = db.session.get(DailyMission, {"manager_id": manager.id, "date": day})
    if m is None or not m.targets:
        m = generate_mission(manager, day)
    targets = m.targets or {}
    vals = mission_metric_values(manager.id, day)
    metrics = LEVEL_METRICS.get(m.level, LEVEL_METRICS[1])

    results = {}
    per_tier = {}
    for metric in metrics:
        v = int(vals.get(metric, 0))
        results[metric] = v
        per_tier[metric] = _tier_for(v, targets.get(metric, {}))

    day_rank = min((RANK[per_tier[x]] for x in metrics), default=0)
    day_tier = RANK_NAMES[day_rank]

    # quality gate: Gold недоступен при низком среднем балле
    qmin = ops_int("quality_gate_min_calls", 3)
    qscore = ops_float("quality_gate_score", 60)
    scored = db.session.get(ManagerDayStat, {"manager_id": manager.id, "date": day})
    scored_n = 0
    if scored and scored.avg_call_score is not None:
        # число оценённых звонков берём из Call напрямую
        from models import Call
        uf, ut = _utc_bounds(day)
        scored_n = Call.query.filter(
            Call.manager_id == manager.id, Call.overall_score.isnot(None),
            Call.started_at >= uf, Call.started_at < ut,
        ).count()
    if day_tier == "gold" and scored and scored.avg_call_score is not None \
            and scored.avg_call_score < qscore and scored_n >= qmin:
        day_tier = "silver"

    # XP за день-тир (изменяемо в течение дня)
    xp_by_tier = {"bronze": ops_int("xp_day_bronze", 10),
                  "silver": ops_int("xp_day_silver", 20),
                  "gold": ops_int("xp_day_gold", 35)}
    day_xp = xp_by_tier.get(day_tier, 0)
    set_xp(manager.id, "day_tier", f"{manager.id}:{day.isoformat()}", day_xp,
           occurred=datetime.utcnow())

    # XP за события воронки (идемпотентно по id события)
    fe_xp = {"qualified": ops_int("xp_qualified", 5),
             "meeting_set": ops_int("xp_meeting_set", 10),
             "meeting_held": ops_int("xp_meeting_held", 20),
             "invoice": ops_int("xp_invoice", 25)}
    for f in vals["_funnel_rows"]:
        amt = fe_xp.get(f.step)
        if amt:
            grant_xp(manager.id, "funnel", f"{f.step}:{f.id}", amt, occurred=f.occurred_at)

    # рекорды
    prog = _progress(manager.id)
    records = dict(prog.records or {})
    for metric in RECORD_METRICS:
        v = int(vals.get(metric, 0))
        rec = records.get(metric) or {"value": 0}
        if v > int(rec.get("value", 0)) and v > 0:
            records[metric] = {"value": v, "date": day.isoformat()}
            if grant_xp(manager.id, "record", f"{metric}:{manager.id}:{day.isoformat()}",
                        ops_int("xp_record", 15)):
                pass
    prog.records = records

    m.results = results
    m.tier_reached = day_tier
    m.xp_awarded = (db.session.query(db.func.coalesce(db.func.sum(XpLedger.amount), 0))
                    .filter(XpLedger.manager_id == manager.id,
                            XpLedger.ref.like(f"%{day.isoformat()}%")).scalar() or 0)
    if finalize:
        m.finalized = True
    db.session.commit()

    if finalize:
        _finalize_streak_level(manager, day)
    return m


def _finalize_streak_level(manager: User, day: date) -> None:
    prog = _progress(manager.id)
    m = db.session.get(DailyMission, {"manager_id": manager.id, "date": day})
    tier = m.tier_reached if m else "none"

    # серия
    if RANK.get(tier, 0) >= RANK["bronze"]:
        prog.streak_days = (prog.streak_days or 0) + 1
        prog.streak_best = max(prog.streak_best or 0, prog.streak_days)
        every = ops_int("streak_freeze_every", 10)
        fmax = ops_int("streak_freeze_max", 2)
        if every and prog.streak_days % every == 0 and (prog.freezes_available or 0) < fmax:
            prog.freezes_available = (prog.freezes_available or 0) + 1
        # бейджи серий
        badges = {5: ops_int("xp_streak_5", 20), 10: ops_int("xp_streak_10", 40),
                  20: ops_int("xp_streak_20", 80), 40: ops_int("xp_streak_40", 150)}
        if prog.streak_days in badges:
            grant_xp(manager.id, "streak", f"{manager.id}:{prog.streak_days}", badges[prog.streak_days])
    else:
        if (prog.freezes_available or 0) > 0:
            prog.freezes_available -= 1  # заморозка спасает серию
        else:
            prog.streak_days = 0

    # уровень по истории последних N рабочих дней
    window = ops_int("level_window", 10)
    promote_need = ops_int("level_promote_need", 8)
    demote_need = ops_int("level_demote_need", 6)
    recent = (
        DailyMission.query.filter(DailyMission.manager_id == manager.id,
                                  DailyMission.finalized.is_(True),
                                  DailyMission.date <= day)
        .order_by(DailyMission.date.desc()).limit(window).all()
    )
    if len(recent) >= window:
        silver_plus = sum(1 for r in recent if RANK.get(r.tier_reached, 0) >= RANK["silver"])
        below_bronze = sum(1 for r in recent if RANK.get(r.tier_reached, 0) < RANK["bronze"])
        if silver_plus >= promote_need and prog.level < 3:
            prog.level += 1
            grant_xp(manager.id, "level", f"up:{manager.id}:{day.isoformat()}",
                     ops_int("xp_level_up", 100))
        elif below_bronze >= demote_need and prog.level > 1:
            prog.level -= 1
    db.session.commit()


# --- командный квест ------------------------------------------------------

def team_quest() -> dict:
    metric = get_ops_setting("team_quest_metric") or "meetings_held"
    target = ops_int("team_quest_target", 0)
    label = get_ops_setting("team_quest_label") or ""
    if target <= 0:
        return {"active": False}
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    rows = ManagerDayStat.query.filter(
        ManagerDayStat.date >= monday, ManagerDayStat.date <= today,
    ).all()
    current = sum(int(getattr(r, metric, 0) or 0) for r in rows)
    return {
        "active": True, "metric": metric, "label": label or METRIC_LABELS.get(metric, metric),
        "target": target, "current": current,
        "pct": min(100, round(current / target * 100)) if target else 0,
        "done": current >= target, "week": monday.isoformat(),
    }


# --- пакетные операции (планировщик) --------------------------------------

def _active_managers():
    return User.query.filter(User.is_active.is_(True), User.role == "manager").all()


def generate_all(app=None, day: date = None) -> dict:
    app = app or current_app
    from utils import now_local
    day = day or now_local().date()
    if not _is_working_day(day):
        return {"skipped": "nonworking", "date": str(day)}
    n = 0
    for m in _active_managers():
        try:
            generate_mission(m, day)
            n += 1
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            app.logger.warning("[missions] генерация %s/%s: %s", m.id, day, exc)
    return {"generated": n, "date": str(day)}


def evaluate_all(app=None, day: date = None, finalize: bool = False) -> dict:
    app = app or current_app
    from utils import now_local
    day = day or now_local().date()
    if not _is_working_day(day):
        return {"skipped": "nonworking", "date": str(day)}
    n = 0
    for m in _active_managers():
        try:
            evaluate_mission(m, day, finalize=finalize)
            n += 1
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            app.logger.warning("[missions] оценка %s/%s: %s", m.id, day, exc)
    # командный квест: начислить XP при достижении (идемпотентно по неделе)
    try:
        q = team_quest()
        if q.get("active") and q.get("done"):
            monday = date.fromisoformat(q["week"])
            for m in _active_managers():
                contributed = ManagerDayStat.query.filter(
                    ManagerDayStat.manager_id == m.id, ManagerDayStat.date >= monday,
                ).with_entities(db.func.coalesce(db.func.sum(
                    getattr(ManagerDayStat, q["metric"]), ), 0)).scalar() or 0
                if contributed and contributed > 0:
                    grant_xp(m.id, "team_quest", f"{m.id}:{q['week']}", ops_int("xp_team_quest", 50))
            db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        app.logger.warning("[missions] квест: %s", exc)
    return {"evaluated": n, "date": str(day), "finalized": finalize}


def backfill_first_revenue_xp(app=None) -> dict:
    """Перенести правило «XP за выручку» в леджер на базе первой выручки (§9.4)."""
    app = app or current_app
    from models import Deal
    step = ops_int("xp_step_rub", 50000)
    per = ops_int("xp_per_step", 50)
    if step <= 0:
        return {"ok": False}
    n = 0
    deals = Deal.query.filter(Deal.outcome == "won", Deal.is_first_revenue.is_(True),
                              Deal.manager_id.isnot(None)).all()
    for d in deals:
        amt = (int(d.price or 0) // step) * per
        if amt > 0 and grant_xp(d.manager_id, "first_revenue", str(d.amo_lead_id), amt,
                                occurred=d.won_at):
            n += 1
    db.session.commit()
    return {"ok": True, "granted": n}
