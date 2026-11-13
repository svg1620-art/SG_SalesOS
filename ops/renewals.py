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
            "yellow": sum(1 for r in s if _health(r._band, untouched(r)) == "yellow"),
            "red": sum(1 for r in s if _health(r._band, untouched(r)) == "red"),
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
                   if r.is_open and r._band in ("hot", "crit") and untouched(r))
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


# ================= Этап B: рабочий список, план, касания =================

BAND_SHORT = {"work": "Рабочее окно", "hot": "Горит", "crit": "Критично",
              "early": "Рано", "nodate": "Без даты"}
BAND_COLOR = {k: c for k, _l, c in [(m[0], m[1], m[2]) for m in BAND_META]}
_PRIORITY = {"crit": 0, "hot": 1, "work": 2, "early": 3, "nodate": 4}


def _health(band, untouched):
    """Статус-светофор открытой сделки по данным (срок + свежесть касания).

    🔴 горит/критично и давно не тронут; 🟡 брошен или критичный срок;
    🟢 в работе и тронут недавно. Этапы воронки не используются.
    """
    if untouched and band in ("crit", "hot"):
        return "red"
    if untouched or band == "crit":
        return "yellow"
    return "green"


def status_of(zone, band, untouched):
    """Итоговый статус: терминальный исход (paid/churn) или health открытой."""
    if zone in ("paid", "churn"):
        return zone
    return _health(band, untouched)


def renewal_managers():
    """Менеджеры, у кого есть открытые сделки продлений (по убыванию числа)."""
    rows = (
        db.session.query(Renewal.manager_id, db.func.count(Renewal.amo_lead_id))
        .filter(Renewal.is_open.is_(True), Renewal.manager_id.isnot(None))
        .group_by(Renewal.manager_id).all()
    )
    from models import User
    ids_by_cnt = sorted(rows, key=lambda r: -r[1])
    out = []
    for mid, cnt in ids_by_cnt:
        u = db.session.get(User, mid)
        if u is not None:
            out.append({"id": u.id, "name": u.full_name or u.email, "count": cnt})
    return out


def _last_notes(renewals):
    """{amo_lead_id: текст последнего примечания} по contact_id/lead_id."""
    lead_ids = {r.amo_lead_id for r in renewals}
    contact_ids = {r.amo_contact_id for r in renewals if r.amo_contact_id}
    ev = OpsActivityEvent.query.filter(
        OpsActivityEvent.type == "note",
        db.or_(OpsActivityEvent.lead_id.in_(lead_ids),
               OpsActivityEvent.contact_id.in_(contact_ids)),
    ).order_by(OpsActivityEvent.occurred_at.desc()).limit(4000).all()
    by_lead, by_contact = {}, {}
    for e in ev:
        txt = ""
        try:
            txt = (e.raw or {}).get("params", {}).get("text") or (e.raw or {}).get("text") or ""
        except Exception:  # noqa: BLE001
            txt = ""
        if not txt:
            continue
        if e.lead_id is not None and e.lead_id not in by_lead:
            by_lead[e.lead_id] = txt
        if e.contact_id is not None and e.contact_id not in by_contact:
            by_contact[e.contact_id] = txt
    out = {}
    for r in renewals:
        out[r.amo_lead_id] = by_lead.get(r.amo_lead_id) or (
            by_contact.get(r.amo_contact_id) if r.amo_contact_id else None)
    return out


def _display_name(r):
    return r.company or r.name or f"Сделка {r.amo_lead_id}"


def worklist(manager_ids=None, band_filter="all", limit=20, today=None):
    today = today or date.today()
    cfg = _cfg()
    q = Renewal.query.filter(Renewal.is_open.is_(True))
    if manager_ids is not None:
        q = q.filter(Renewal.manager_id.in_(list(manager_ids)))
    rows = q.all()
    for r in rows:
        r._band = band_of(r.renewal_end, today, cfg)
    touch = _last_touch_days(rows)
    notes = _last_notes(rows)
    rows.sort(key=lambda r: (_PRIORITY.get(r._band, 9),
                             -(touch.get(r.amo_lead_id) or 999)))
    out = []
    for r in rows:
        if band_filter == "untouched":
            d = touch.get(r.amo_lead_id)
            if not (d is None or d >= cfg["sla"]):
                continue
        elif band_filter != "all" and r._band != band_filter:
            continue
        d = touch.get(r.amo_lead_id)
        dl = (r.renewal_end - today).days if r.renewal_end else None
        out.append({
            "lead_id": r.amo_lead_id, "name": _display_name(r), "contact": r.name,
            "end": r.renewal_end, "days_left": dl, "band": r._band,
            "band_label": BAND_SHORT.get(r._band, ""), "band_color": BAND_COLOR.get(r._band),
            "last_touch": d, "untouched": (d is None or d >= cfg["sla"]),
            "zone": status_of(r.zone, r._band, d is None or d >= cfg["sla"]),
            "agreement": notes.get(r.amo_lead_id) or "—",
            "amo_lead_id": r.amo_lead_id,
        })
        if len(out) >= limit:
            break
    return out


_HIST_META = {
    "call_out": ("📞", "Звонок (исх.)"), "call_in": ("📞", "Звонок (вх.)"),
    "msg_out": ("💬", "Сообщение"), "msg_in": ("💬", "Входящее сообщение"),
    "note": ("📝", "Примечание"), "task_done": ("✅", "Задача выполнена"),
}


def _event_text(e):
    raw = e.raw or {}
    if e.type == "note":
        try:
            return raw.get("params", {}).get("text") or raw.get("text") or ""
        except Exception:  # noqa: BLE001
            return ""
    if e.type in ("call_out", "call_in"):
        if e.is_connected:
            mins = int((e.duration_sec or 0) // 60)
            return f"дозвон, {mins} мин" if mins else "дозвон"
        return "недозвон"
    if e.type == "task_done":
        try:
            return (raw.get("result") or {}).get("text") or raw.get("text") or ""
        except Exception:  # noqa: BLE001
            return ""
    return ""


def client_card(lead_id, today=None):
    """Карточка клиента продления: сводка + полная история взаимодействий."""
    from utils import to_local, amo_entity_url
    from settings_store import amo_base_domain
    today = today or date.today()
    cfg = _cfg()
    r = db.session.get(Renewal, lead_id)
    if r is None:
        return None
    band = band_of(r.renewal_end, today, cfg)
    conds = [OpsActivityEvent.lead_id == lead_id]
    if r.amo_contact_id:
        conds.append(OpsActivityEvent.contact_id == r.amo_contact_id)
    ev = (
        OpsActivityEvent.query.filter(db.or_(*conds))
        .order_by(OpsActivityEvent.occurred_at.desc()).limit(300).all()
    )
    now = datetime.utcnow()
    last_touch = min(((now - e.occurred_at).days for e in ev), default=None)

    # разбор звонков из ОКК: связка касание-звонок с Call по amo_note_id
    def _note_id(e):
        aid = e.amo_event_id or ""
        if aid.startswith("note:"):
            try:
                return int(aid.split(":", 1)[1])
            except ValueError:
                return None
        return None

    from models import Call, Recommendation
    note_ids = [n for n in (_note_id(e) for e in ev
                if e.type in ("call_out", "call_in")) if n is not None]
    call_by_note, rec_by_call = {}, {}
    if note_ids:
        calls = Call.query.filter(Call.amo_note_id.in_(note_ids)).all()
        call_by_note = {c.amo_note_id: c for c in calls}
        done_ids = [c.id for c in calls if c.status == "done" and c.overall_score is not None]
        if done_ids:
            for rc in Recommendation.query.filter(Recommendation.call_id.in_(done_ids)).all():
                rec_by_call.setdefault(rc.call_id, []).append(rc)

    history = []
    for e in ev:
        item = {
            "when": to_local(e.occurred_at), "type": e.type,
            "icon": _HIST_META.get(e.type, ("•", e.type))[0],
            "label": _HIST_META.get(e.type, ("•", e.type))[1],
            "text": _event_text(e), "analysis": None,
        }
        if e.type in ("call_out", "call_in"):
            c = call_by_note.get(_note_id(e))
            if c is not None and c.status == "done" and c.overall_score is not None:
                item["analysis"] = {
                    "call_id": c.id, "score": c.overall_score, "zone": c.zone,
                    "summary": c.summary,
                    "recs": [r.text for r in rec_by_call.get(c.id, [])[:3] if r.text],
                    "manager_comment": c.manager_comment,
                }
        history.append(item)
    untouched = last_touch is None or last_touch >= cfg["sla"]
    notes = _last_notes([r])
    return {
        "r": r, "name": _display_name(r), "band": band,
        "band_label": BAND_SHORT.get(band, ""), "band_color": BAND_COLOR.get(band),
        "status": status_of(r.zone, band, untouched),
        "days_left": (r.renewal_end - today).days if r.renewal_end else None,
        "touch_count": len(ev), "last_touch": last_touch,
        "agreement": notes.get(r.amo_lead_id),
        "history": history,
        "amo_url": amo_entity_url(amo_base_domain(), "leads", r.amo_lead_id),
    }


def touch_plan(manager_ids, today=None):
    """Дневной план касаний менеджера(ов) продлений: факт сегодня / план."""
    from ops.metrics import _utc_bounds
    today = today or date.today()
    cfg = _cfg()
    if not manager_ids:
        return {"fact": 0, "plan": cfg["plan"], "pct": 0}
    uf, ut = _utc_bounds(today)
    fact = OpsActivityEvent.query.filter(
        OpsActivityEvent.manager_id.in_(list(manager_ids)),
        OpsActivityEvent.occurred_at >= uf, OpsActivityEvent.occurred_at < ut,
    ).count()
    plan = cfg["plan"] or 1
    return {"fact": fact, "plan": cfg["plan"], "pct": min(100, round(fact / plan * 100))}


def today_touches(manager_ids, today=None, limit=40):
    """Касания сегодня менеджеров продлений, связанные с клиентом-сделкой."""
    from ops.metrics import _utc_bounds
    from utils import to_local
    today = today or date.today()
    if not manager_ids:
        return []
    uf, ut = _utc_bounds(today)
    ev = OpsActivityEvent.query.filter(
        OpsActivityEvent.manager_id.in_(list(manager_ids)),
        OpsActivityEvent.occurred_at >= uf, OpsActivityEvent.occurred_at < ut,
    ).order_by(OpsActivityEvent.occurred_at.desc()).limit(300).all()
    # карты сделок
    opens = Renewal.query.all()
    by_lead = {r.amo_lead_id: r for r in opens}
    by_contact = {r.amo_contact_id: r for r in opens if r.amo_contact_id}
    cfg = _cfg()
    TYPE_LABEL = {"call_out": "Звонок", "call_in": "Входящий", "msg_out": "Сообщение",
                  "msg_in": "Входящее", "note": "Примечание", "task_done": "Задача"}
    matched = []
    for e in ev:
        r = (by_lead.get(e.lead_id) if e.lead_id else None) or \
            (by_contact.get(e.contact_id) if e.contact_id else None)
        if r is not None:
            matched.append((e, r))
    touch = _last_touch_days([r for _e, r in matched])
    out = []
    for e, r in matched:
        dl = (r.renewal_end - today).days if r.renewal_end else None
        b = band_of(r.renewal_end, today, cfg)
        d = touch.get(r.amo_lead_id)
        out.append({
            "time": to_local(e.occurred_at).strftime("%H:%M"),
            "name": _display_name(r), "contact": r.name,
            "type": TYPE_LABEL.get(e.type, e.type),
            "days_left": dl, "band": b, "amo_lead_id": r.amo_lead_id,
            "zone": status_of(r.zone, b, d is None or d >= cfg["sla"]),
        })
        if len(out) >= limit:
            break
    return out
