"""Доска продлений (Этап A): покрытие по бэндам + деньги.

Бэнды считаются от даты окончания подписки (renewal_end), не от этапов.
Деньги (план/подтверждено/риск/отвал/churn) — по выбранному периоду.
"""
from datetime import date, datetime, timedelta
from calendar import monthrange

from extensions import db
from models import Renewal, OpsActivityEvent
from ops_store import ops_int


def _cfg():
    return {
        "early": ops_int("renewal_band_early_months", 6),
        "work": ops_int("renewal_band_work_months", 4),
        "hot": ops_int("renewal_band_hot_months", 1),
        "sla": ops_int("renewal_sla_days", 4),
        "plan": ops_int("renewal_touch_plan", 20),
    }


def band_of(renewal_end, today, cfg):
    if renewal_end is None:
        return "nodate"
    months = (renewal_end - today).days / 30.4
    if months > cfg["early"]:
        return "early"
    if months >= cfg["work"]:
        return "work"
    if months >= cfg["hot"]:
        return "hot"
    return "crit"


BAND_META = [
    ("work", "Рабочее окно (4–6 мес)", "#00BFDC"),
    ("hot", "Горит (1–3 мес)", "#F5A524"),
    ("crit", "Критично (≤1 мес / просрочено)", "#E5484D"),
    ("early", "Рано (>6 мес)", "#6b93e6"),
    ("nodate", "Без даты окончания", "#8A8A96"),
]


def _period_window(period, today):
    start = today.replace(day=1)
    if period == "month":
        end = today.replace(day=monthrange(today.year, today.month)[1])
    elif period == "quarter":
        m = start
        for _ in range(3):
            m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = m - timedelta(days=1)
    else:  # year
        end = date(start.year + 1, start.month, 1) - timedelta(days=1)
    return start, end


def _last_touch_days(renewals):
    """{amo_lead_id: дней с последнего касания} по событиям (lead_id или contact_id)."""
    lead_ids = {r.amo_lead_id for r in renewals}
    contact_ids = {r.amo_contact_id for r in renewals if r.amo_contact_id}
    since = datetime.utcnow() - timedelta(days=180)
    by_lead, by_contact = {}, {}
    if lead_ids:
        rows = (
            db.session.query(OpsActivityEvent.lead_id, db.func.max(OpsActivityEvent.occurred_at))
            .filter(OpsActivityEvent.lead_id.in_(lead_ids), OpsActivityEvent.occurred_at >= since)
            .group_by(OpsActivityEvent.lead_id).all()
        )
        by_lead = {lid: ts for lid, ts in rows if lid is not None}
    if contact_ids:
        rows = (
            db.session.query(OpsActivityEvent.contact_id, db.func.max(OpsActivityEvent.occurred_at))
            .filter(OpsActivityEvent.contact_id.in_(contact_ids), OpsActivityEvent.occurred_at >= since)
            .group_by(OpsActivityEvent.contact_id).all()
        )
        by_contact = {cid: ts for cid, ts in rows if cid is not None}
    now = datetime.utcnow()
    out = {}
    for r in renewals:
        ts = by_lead.get(r.amo_lead_id)
        tc = by_contact.get(r.amo_contact_id) if r.amo_contact_id else None
        latest = max([t for t in (ts, tc) if t is not None], default=None)
        out[r.amo_lead_id] = (now - latest).days if latest else None
    return out


def board(period="month", manager_ids=None, today=None):
    today = today or date.today()
    cfg = _cfg()
    q = Renewal.query
    if manager_ids is not None:
        q = q.filter(Renewal.manager_id.in_(list(manager_ids)))
    allr = q.all()
    for r in allr:
        r._band = band_of(r.renewal_end, today, cfg)
    open_r = [r for r in allr if r.is_open]
    touch = _last_touch_days(open_r)

    def untouched(r):
        d = touch.get(r.amo_lead_id)
        return d is None or d >= cfg["sla"]

    # --- бэнды (по всем открытым) ---
    bands = []
    for key, label, color in BAND_META:
        s = [r for r in open_r if r._band == key]
        if not s and key in ("nodate",):
            # показываем «без даты» только если такие есть
            if not any(r._band == "nodate" for r in open_r):
                continue
        bands.append({
            "key": key, "label": label, "color": color,
            "count": len(s), "sum": sum(int(r.price or 0) for r in s),
            "untouched": sum(1 for r in s if untouched(r)),
            "yellow": sum(1 for r in s if r.zone == "yellow"),
            "red": sum(1 for r in s if r.zone == "red"),
            "focus": key in ("work", "hot"),
        })

    # --- KPI (по всем открытым) ---
    kpi = {
        "open": len(open_r),
        "in_work": sum(1 for r in open_r if r._band == "work"),
        "in_hot": sum(1 for r in open_r if r._band in ("hot", "crit")),
        "untouched": sum(1 for r in open_r if untouched(r)),
        "nodate": sum(1 for r in open_r if r._band == "nodate"),
        "sla": cfg["sla"], "plan": cfg["plan"],
    }

    # --- деньги (по периоду, окно по renewal_end) ---
    w_from, w_to = _period_window(period, today)
    scope = [r for r in allr if r.renewal_end and w_from <= r.renewal_end <= w_to]
    plan_sum = sum(int(r.price or 0) for r in scope)
    paid = [r for r in scope if r.zone == "paid"]
    churn = [r for r in scope if r.zone == "churn"]
    paid_sum = sum(int(r.price or 0) for r in paid)
    churn_sum = sum(int(r.price or 0) for r in churn)
    risk_sum = sum(int(r.price or 0) for r in scope
                   if r.is_open and r._band in ("hot", "crit") and r.zone in ("yellow", "red"))
    money = {
        "plan_sum": plan_sum, "scope_cnt": len(scope),
        "paid_sum": paid_sum, "paid_cnt": len(paid),
        "churn_sum": churn_sum, "churn_cnt": len(churn),
        "risk_sum": risk_sum,
        "churn_pct": round(churn_sum / plan_sum * 100) if plan_sum else 0,
        "collected_pct": round(paid_sum / plan_sum * 100) if plan_sum else 0,
        "remaining": max(0, plan_sum - paid_sum),
        "from": w_from, "to": w_to,
    }
    return {"bands": bands, "kpi": kpi, "money": money, "period": period, "cfg": cfg}
