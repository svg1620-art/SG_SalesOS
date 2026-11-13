"""Движок системных алертов пульта (§10.3).

Шесть типов алертов считаются по материализованным дневным метрикам и
окупаемости. Пересчёт идемпотентный: один активный алерт на (тип, менеджер);
повторное срабатывание обновляет его, исчезновение причины — закрывает.

Типы:
- plan_unrealistic — требуемая выручка/день > max_silver_ratio × медианы;
- show_rate_drop  — ShowRate 14д ниже своей медианы 90д на ≥ X%;
- activity_drop   — дозвоны 5 раб.дней ниже своей медианы на ≥ X%;
- quality_drop    — средний балл 7д упал на ≥ X пунктов;
- payback_risk    — payback 2 мес подряд < порога (вне адаптации);
- crm_hygiene     — просроченных задач > лимита.
"""
from datetime import date, datetime, timedelta
from statistics import median

from extensions import db
from models import ManagerDayStat, ManagerPlan, OpsAlert, User
from ops_store import ops_int, ops_float


# ---------- утилиты ----------

def _managers():
    return User.query.filter_by(role="manager", is_active=True).all()


def _working_days_back(up_to: date, count: int) -> list:
    """Последние `count` рабочих дней по календарю (включая up_to, если рабочий)."""
    from ops.metrics import _is_working_day
    out, cur = [], up_to
    guard = 0
    while len(out) < count and guard < count * 4 + 10:
        if _is_working_day(cur):
            out.append(cur)
        cur -= timedelta(days=1)
        guard += 1
    return out


def _day_rows(manager_id, days):
    if not days:
        return []
    return ManagerDayStat.query.filter(
        ManagerDayStat.manager_id == manager_id,
        ManagerDayStat.date.in_(days),
    ).all()


def _wd_left(today: date) -> int:
    from calendar import monthrange
    from ops.metrics import _is_working_day
    last = today.replace(day=monthrange(today.year, today.month)[1])
    n, cur = 0, today
    while cur <= last:
        if _is_working_day(cur):
            n += 1
        cur += timedelta(days=1)
    return n


# ---------- отдельные детекторы (возвращают dict|None) ----------

def _show_rate_drop(manager_id, today, cfg):
    d14 = _working_days_back(today, 14)
    d90 = _working_days_back(today, 90)
    r14 = _day_rows(manager_id, d14)
    r90 = _day_rows(manager_id, d90)
    set14 = sum(r.meetings_set or 0 for r in r14)
    held14 = sum(r.meetings_held or 0 for r in r14)
    set90 = sum(r.meetings_set or 0 for r in r90)
    held90 = sum(r.meetings_held or 0 for r in r90)
    if set14 < 5 or set90 < 10:
        return None
    sr14 = held14 / set14
    sr90 = held90 / set90
    if sr90 <= 0:
        return None
    drop = (sr90 - sr14) / sr90 * 100
    if drop >= cfg["show_drop"]:
        return {
            "severity": "warn",
            "title": "Падение доходимости встреч",
            "detail": f"ShowRate за 14 дней {sr14*100:.0f}% против медианы 90 дней "
                      f"{sr90*100:.0f}% (−{drop:.0f}%).",
            "data": {"sr14": round(sr14, 3), "sr90": round(sr90, 3),
                     "drop_pct": round(drop)},
        }
    return None


def _activity_drop(manager_id, today, cfg):
    d5 = _working_days_back(today, 5)
    base_days = _working_days_back(today, 90)
    r5 = _day_rows(manager_id, d5)
    rbase = _day_rows(manager_id, base_days)
    if len(rbase) < 10 or not r5:
        return None
    daily_base = [r.calls_connected or 0 for r in rbase]
    med = median(daily_base)
    if med <= 0:
        return None
    avg5 = sum(r.calls_connected or 0 for r in r5) / len(r5)
    drop = (med - avg5) / med * 100
    if drop >= cfg["activity_drop"]:
        return {
            "severity": "warn",
            "title": "Просадка активности (дозвоны)",
            "detail": f"Дозвоны ~{avg5:.0f}/день за 5 раб.дней против медианы "
                      f"{med:.0f}/день (−{drop:.0f}%).",
            "data": {"avg5": round(avg5, 1), "median": round(med, 1),
                     "drop_pct": round(drop)},
        }
    return None


def _quality_drop(manager_id, today, cfg):
    d7 = _working_days_back(today, 7)
    dprev = _working_days_back(today - timedelta(days=10), 30)
    r7 = [r for r in _day_rows(manager_id, d7) if r.avg_call_score is not None]
    rprev = [r for r in _day_rows(manager_id, dprev) if r.avg_call_score is not None]
    if len(r7) < 2 or len(rprev) < 3:
        return None
    avg7 = sum(r.avg_call_score for r in r7) / len(r7)
    base = sum(r.avg_call_score for r in rprev) / len(rprev)
    drop = base - avg7
    if drop >= cfg["quality_drop"]:
        return {
            "severity": "crit" if drop >= cfg["quality_drop"] * 1.5 else "warn",
            "title": "Падение качества звонков",
            "detail": f"Средний балл за 7 дней {avg7:.0f} против базы {base:.0f} "
                      f"(−{drop:.0f} пунктов).",
            "data": {"avg7": round(avg7), "base": round(base), "drop": round(drop)},
        }
    return None


def _payback_risk(manager, today, cfg):
    from ops.payback import payback_for, month_first, _prev_month
    mf = month_first(today)
    prev = _prev_month(mf)
    pb_now = payback_for(manager, mf)
    pb_prev = payback_for(manager, prev)
    thr = cfg["payback_min"]
    for pb in (pb_now, pb_prev):
        if pb.get("ratio") is None or pb.get("adaptation"):
            return None
        if pb["ratio"] >= thr:
            return None
    return {
        "severity": "crit",
        "title": "Риск окупаемости",
        "detail": f"Payback второй месяц подряд ниже {thr:.1f}: "
                  f"{prev:%m.%Y} {pb_prev['ratio']:.2f} → {mf:%m.%Y} {pb_now['ratio']:.2f}.",
        "data": {"ratio_now": round(pb_now["ratio"], 2),
                 "ratio_prev": round(pb_prev["ratio"], 2), "thr": thr},
    }


def _crm_hygiene(manager_id, today, cfg):
    row = db.session.get(ManagerDayStat, {"manager_id": manager_id, "date": today})
    if row is None:
        # последний доступный день
        row = (ManagerDayStat.query.filter_by(manager_id=manager_id)
               .order_by(ManagerDayStat.date.desc()).first())
    if row is None:
        return None
    overdue = int(row.tasks_overdue or 0)
    if overdue > cfg["overdue_limit"]:
        return {
            "severity": "warn",
            "title": "Просроченные задачи в CRM",
            "detail": f"Просроченных задач: {overdue} (лимит {cfg['overdue_limit']}).",
            "data": {"overdue": overdue, "limit": cfg["overdue_limit"]},
        }
    return None


def _plan_unrealistic(manager, today, cfg):
    mf = today.replace(day=1)
    plan = db.session.get(ManagerPlan, {"manager_id": manager.id, "month": mf})
    if plan is None or not plan.revenue_plan:
        return None
    rev_plan = float(plan.revenue_plan)
    rows = ManagerDayStat.query.filter(
        ManagerDayStat.manager_id == manager.id,
        ManagerDayStat.date >= mf, ManagerDayStat.date <= today,
    ).all()
    paid_mtd = sum(float(r.payments_sum or 0) for r in rows)
    remaining = max(0.0, rev_plan - paid_mtd)
    wd_left = _wd_left(today)
    if remaining <= 0 or wd_left <= 0:
        return None
    required_per_day = remaining / wd_left
    # медиана дневной выручки по истории (рабочие дни с выручкой)
    base_days = _working_days_back(today, 90)
    rbase = _day_rows(manager.id, base_days)
    daily = [float(r.payments_sum or 0) for r in rbase]
    daily = [x for x in daily if x > 0]
    if len(daily) < 5:
        return None
    med = median(daily)
    if med <= 0:
        return None
    ratio = required_per_day / med
    if ratio > cfg["max_silver_ratio"]:
        return {
            "severity": "warn",
            "title": "Нереалистичный план месяца",
            "detail": f"Чтобы добрать план, нужно ~{required_per_day:,.0f} ₽/день — "
                      f"это ×{ratio:.1f} от медианы {med:,.0f} ₽/день."
                      .replace(",", " "),
            "data": {"required_per_day": round(required_per_day),
                     "median_per_day": round(med), "ratio": round(ratio, 1),
                     "remaining": round(remaining), "wd_left": wd_left},
        }
    return None


# ---------- upsert/resolve ----------

def _upsert(atype, manager_id, payload):
    """Создать или обновить активный алерт (тип, менеджер)."""
    a = OpsAlert.query.filter_by(atype=atype, manager_id=manager_id,
                                 is_active=True).first()
    if a is None:
        a = OpsAlert(atype=atype, manager_id=manager_id, is_active=True,
                     created_at=datetime.utcnow())
        db.session.add(a)
    a.severity = payload["severity"]
    a.title = payload["title"]
    a.detail = payload.get("detail")
    a.data_json = payload.get("data")
    a.resolved_at = None
    a.is_active = True
    a.updated_at = datetime.utcnow()


def _resolve(atype, manager_id):
    """Закрыть активный алерт, если причина исчезла."""
    a = OpsAlert.query.filter_by(atype=atype, manager_id=manager_id,
                                 is_active=True).first()
    if a is not None:
        a.is_active = False
        a.resolved_at = datetime.utcnow()


_DETECTORS_MGR = {
    "show_rate_drop": _show_rate_drop,
    "activity_drop": _activity_drop,
    "quality_drop": _quality_drop,
    "crm_hygiene": _crm_hygiene,
}
_DETECTORS_USER = {
    "payback_risk": _payback_risk,
    "plan_unrealistic": _plan_unrealistic,
}


def _cfg():
    return {
        "show_drop": ops_float("alert_show_rate_drop_pct", 25.0),
        "activity_drop": ops_float("alert_activity_drop_pct", 30.0),
        "quality_drop": ops_float("alert_quality_drop_pts", 10.0),
        "payback_min": ops_float("alert_payback_min", 0.8),
        "overdue_limit": ops_int("overdue_tasks_limit", 5),
        "max_silver_ratio": ops_float("max_silver_ratio", 2.0),
        "min_wd": ops_int("alert_min_working_days", 3),
    }


def recompute_alerts(app=None, today: date = None) -> dict:
    """Пересчитать все алерты по всем менеджерам (идемпотентно)."""
    from flask import current_app
    app = app or current_app
    today = today or date.today()
    cfg = _cfg()
    fired = 0
    for m in _managers():
        # не считаем алерты без минимальной истории (адаптация)
        hist = _day_rows(m.id, _working_days_back(today, cfg["min_wd"]))
        has_hist = len(hist) >= cfg["min_wd"]
        for atype, fn in _DETECTORS_MGR.items():
            try:
                res = fn(m.id, today, cfg) if has_hist else None
            except Exception as exc:  # noqa: BLE001
                app.logger.warning("[alerts] %s m=%s: %s", atype, m.id, exc)
                res = None
            if res:
                _upsert(atype, m.id, res); fired += 1
            else:
                _resolve(atype, m.id)
        for atype, fn in _DETECTORS_USER.items():
            try:
                res = fn(m, today, cfg)
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                app.logger.warning("[alerts] %s m=%s: %s", atype, m.id, exc)
                res = None
            if res:
                _upsert(atype, m.id, res); fired += 1
            else:
                _resolve(atype, m.id)
    db.session.commit()
    return {"fired": fired}


# ---------- чтение для UI ----------

_SEV_RANK = {"crit": 0, "warn": 1, "info": 2}
_TYPE_EMOJI = {
    "plan_unrealistic": "🎯", "show_rate_drop": "📉", "activity_drop": "📞",
    "quality_drop": "⭐", "payback_risk": "💸", "crm_hygiene": "🧹",
}


def active_alerts(manager_ids=None):
    """Активные алерты для пульта (с именами менеджеров), отсортированные."""
    q = OpsAlert.query.filter_by(is_active=True)
    if manager_ids is not None:
        q = q.filter(OpsAlert.manager_id.in_(list(manager_ids)))
    rows = q.all()
    name_by_id = {u.id: (u.full_name or u.email) for u in User.query.all()}
    out = []
    for a in rows:
        out.append({
            "id": a.id, "atype": a.atype, "emoji": _TYPE_EMOJI.get(a.atype, "⚠️"),
            "severity": a.severity, "title": a.title, "detail": a.detail,
            "manager_id": a.manager_id,
            "manager": name_by_id.get(a.manager_id, "—") if a.manager_id else "Команда",
            "data": a.data_json or {}, "updated_at": a.updated_at,
        })
    out.sort(key=lambda r: (_SEV_RANK.get(r["severity"], 9), r["manager"]))
    return out


def alerts_for_manager(manager_id):
    return active_alerts(manager_ids=[manager_id])
