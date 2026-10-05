"""Окупаемость менеджера и факторная диагностика (Этап 4, §8).

Первая выручка хранится на Deal (is_first_revenue) — см. ingest/amo_deals.
Затраты вводятся вручную (staff_costs), доля РОПа распределяется по рабочим дням
(rop_cost_allocations). Факторная диагностика раскладывает изменение выручки на
вклад факторов воронки (логарифмически, §8.3).
"""
from datetime import date, datetime, timedelta
from calendar import monthrange
from math import log
from statistics import median

from extensions import db
from models import (
    User, Deal, StaffCost, RopCostAllocation, ManagerDayStat, ManagerAbsence,
    WorkCalendar,
)
from ops_store import ops_float, ops_int, get_ops_setting


# --- базовые помощники ----------------------------------------------------

def month_first(d: date) -> date:
    return d.replace(day=1)


def _month_bounds(mf: date):
    last = mf.replace(day=monthrange(mf.year, mf.month)[1])
    nxt = (last + timedelta(days=1))
    return mf, last, nxt


def _is_working_day(d: date) -> bool:
    row = db.session.get(WorkCalendar, d)
    return bool(row.is_working_day) if row is not None else d.weekday() < 5


def own_cost(sc: StaffCost):
    """Свои затраты сотрудника (§8.1). None, если строки затрат нет."""
    if sc is None:
        return None
    salary = float(sc.salary_fixed or 0)
    bonus = float(sc.bonus_paid or 0)
    tax = float(sc.payroll_tax_rate if sc.payroll_tax_rate is not None else 0.302)
    overhead = float(sc.overhead or 0)
    lead = float(sc.lead_cost or 0)
    return (salary + bonus) * (1 + tax) + overhead + lead


def first_revenue(manager_id: int, mf: date):
    """(сумма, количество) первой выручки менеджера за месяц."""
    _, _, nxt = _month_bounds(mf)
    rows = Deal.query.filter(
        Deal.outcome == "won", Deal.is_first_revenue.is_(True),
        Deal.manager_id == manager_id,
        Deal.won_at >= datetime(mf.year, mf.month, mf.day),
        Deal.won_at < datetime(nxt.year, nxt.month, nxt.day),
    ).all()
    return sum(int(d.price or 0) for d in rows), len(rows)


def active_days(manager: User, mf: date) -> int:
    """Рабочие дни месяца, в которые менеджер активен (найм..деактивация − отсутствия)."""
    first, last, _ = _month_bounds(mf)
    hire = manager.hire_date
    deact = manager.deactivated_at.date() if manager.deactivated_at else None
    absent = set()
    for a in ManagerAbsence.query.filter_by(manager_id=manager.id).all():
        cur = max(a.date_from, first)
        while cur <= min(a.date_to, last):
            absent.add(cur)
            cur += timedelta(days=1)
    n, cur = 0, first
    while cur <= last:
        if _is_working_day(cur) and cur not in absent and \
                (hire is None or cur >= hire) and (deact is None or cur <= deact):
            n += 1
        cur += timedelta(days=1)
    return n


def _active_managers():
    return (
        User.query.filter(User.is_active.is_(True), User.role == "manager")
        .order_by(User.full_name, User.email).all()
    )


def recompute_allocations(mf: date) -> dict:
    """Пересчитать долю РОПа на менеджеров за месяц (§8.4). Идемпотентно."""
    mf = month_first(mf)
    RopCostAllocation.query.filter_by(month=mf).delete()

    rops = StaffCost.query.filter_by(month=mf, cost_role="rop").all()
    managers = _active_managers()
    weights = {m.id: active_days(m, mf) for m in managers}
    total = sum(weights.values())
    if not rops or total == 0:
        db.session.commit()
        return {"month": str(mf), "allocated": 0}

    allocated_rows = 0
    for rop in rops:
        pool = own_cost(rop) or 0.0
        acc = 0.0
        per = {}
        for m in managers:
            a = round(pool * weights[m.id] / total, 2)
            per[m.id] = a
            acc += a
        # остаток округления — менеджеру с наибольшим весом
        rem = round(pool - acc, 2)
        if rem and managers:
            top = max(managers, key=lambda x: weights[x.id])
            per[top.id] = round(per[top.id] + rem, 2)
        for m in managers:
            if weights[m.id] <= 0:
                continue
            db.session.add(RopCostAllocation(
                month=mf, rop_user_id=rop.user_id, manager_id=m.id,
                weight=weights[m.id], allocated_cost=per[m.id],
            ))
            allocated_rows += 1
    db.session.commit()
    return {"month": str(mf), "allocated": allocated_rows}


def rop_share(manager_id: int, mf: date) -> float:
    rows = RopCostAllocation.query.filter_by(month=month_first(mf), manager_id=manager_id).all()
    return sum(float(r.allocated_cost or 0) for r in rows)


def _rampup_factor(manager: User, mf: date):
    """Коэффициент адаптации для месяца (None, если адаптация не действует)."""
    if not manager.hire_date:
        return None
    months_since = (mf.year - manager.hire_date.year) * 12 + (mf.month - manager.hire_date.month)
    rampup_months = ops_int("rampup_months", 3)
    if months_since < 0 or months_since >= rampup_months:
        return None
    raw = get_ops_setting("rampup_factors") or "0.3,0.6,0.9"
    try:
        factors = [float(x) for x in raw.split(",")]
    except ValueError:
        factors = [0.3, 0.6, 0.9]
    return factors[months_since] if months_since < len(factors) else factors[-1]


def payback_for(manager: User, mf: date) -> dict:
    """Окупаемость менеджера за месяц (§8.1)."""
    mf = month_first(mf)
    sc = db.session.get(StaffCost, {"user_id": manager.id, "month": mf})
    own = own_cost(sc)
    rop = rop_share(manager.id, mf)
    rev_sum, rev_cnt = first_revenue(manager.id, mf)
    gmr = ops_float("gross_margin_rate", 1.0)
    margin = rev_sum * gmr

    if own is None:
        return {"status": "no_data", "rev_sum": rev_sum, "rev_cnt": rev_cnt,
                "margin": margin, "own": None, "rop": rop, "full": None,
                "ratio": None, "direct": None, "adaptation": False}

    full = own + rop
    ratio = (margin / full) if full else None
    direct = (margin / own) if own else None

    factor = _rampup_factor(manager, mf)
    green_thr = 1.0 * (factor if factor else 1.0)
    yellow_thr = 0.8 * (factor if factor else 1.0)
    if ratio is None:
        status = "no_data"
    elif ratio >= green_thr:
        status = "green"
    elif ratio >= yellow_thr:
        status = "yellow"
    else:
        status = "red"
    return {
        "status": status, "rev_sum": rev_sum, "rev_cnt": rev_cnt, "margin": margin,
        "own": own, "rop": rop, "full": full, "ratio": ratio, "direct": direct,
        "adaptation": factor is not None, "rampup_factor": factor,
    }


def cumulative_net(manager: User, up_to: date) -> list:
    """Помесячная серия (маржа, стоимость, накопительный net) с даты найма."""
    start = month_first(manager.hire_date) if manager.hire_date else None
    # fallback: самый ранний месяц затрат или выигранной сделки
    if start is None:
        earliest = (
            db.session.query(db.func.min(StaffCost.month))
            .filter(StaffCost.user_id == manager.id).scalar()
        )
        start = earliest or month_first(up_to)
    series, cum, cur = [], 0.0, start
    end = month_first(up_to)
    while cur <= end:
        pb = payback_for(manager, cur)
        margin = pb["margin"]
        full = pb["full"] or 0.0
        net = margin - full if pb["full"] is not None else 0.0
        cum += net
        series.append({
            "month": cur, "margin": margin, "full": full,
            "net": net, "cumulative": cum, "has_cost": pb["full"] is not None,
            "ratio": pb["ratio"], "status": pb["status"],
        })
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return series


# --- факторная диагностика (§8.3) ----------------------------------------

_FACTOR_LABELS = [
    "Дозвоны", "CR дозвон→квал", "CR квал→встреча", "ShowRate",
    "CR встреча→счёт", "CR счёт→оплата", "Средний чек",
]


def _activity_sums(manager_id: int, d_from: date, d_to: date) -> dict:
    rows = ManagerDayStat.query.filter(
        ManagerDayStat.manager_id == manager_id,
        ManagerDayStat.date >= d_from, ManagerDayStat.date <= d_to,
    ).all()
    agg = {k: 0 for k in ("calls_connected", "qualified", "meetings_set",
                          "meetings_held", "invoices")}
    for r in rows:
        for k in agg:
            agg[k] += int(getattr(r, k) or 0)
    return agg


def _factors(manager_id: int, mf: date, shift_days: int = 0):
    """Семь факторов выручки за месяц (активность со сдвигом, выручка — за месяц)."""
    first, last, _ = _month_bounds(mf)
    a_from = first - timedelta(days=shift_days)
    a_to = last - timedelta(days=shift_days)
    a = _activity_sums(manager_id, a_from, a_to)
    rev_sum, rev_cnt = first_revenue(manager_id, mf)

    connected = a["calls_connected"]
    f = [
        connected,
        (a["qualified"] / connected) if connected else 0.0,
        (a["meetings_set"] / a["qualified"]) if a["qualified"] else 0.0,
        (a["meetings_held"] / a["meetings_set"]) if a["meetings_set"] else 0.0,
        (a["invoices"] / a["meetings_held"]) if a["meetings_held"] else 0.0,
        (rev_cnt / a["invoices"]) if a["invoices"] else 0.0,
        (rev_sum / rev_cnt) if rev_cnt else 0.0,
    ]
    revenue = rev_sum  # произведение факторов телескопически = первая выручка
    return f, revenue


def _prev_month(mf: date) -> date:
    return (mf - timedelta(days=1)).replace(day=1)


def factor_diagnosis(manager_id: int, mf: date, base_mode: str = "prev",
                     shift_days: int = 0) -> dict:
    """Разложение ΔВыручки на вклад факторов (§8.3)."""
    mf = month_first(mf)
    fM, revM = _factors(manager_id, mf, shift_days)

    if base_mode == "own3m":
        months = [_prev_month(mf)]
        for _ in range(2):
            months.append(_prev_month(months[-1]))
        mats = [_factors(manager_id, m, shift_days) for m in months]
        fB = [median([mt[0][i] for mt in mats]) for i in range(len(_FACTOR_LABELS))]
        revB = median([mt[1] for mt in mats])
        base_label = "своя медиана 3 мес"
    else:
        fB, revB = _factors(manager_id, _prev_month(mf), shift_days)
        base_label = "прошлый месяц"

    rows = [
        {"label": _FACTOR_LABELS[i], "m": fM[i], "b": fB[i]}
        for i in range(len(_FACTOR_LABELS))
    ]
    delta = revM - revB
    decomposable = (
        revM > 0 and revB > 0 and revM != revB
        and all(x > 0 for x in fM) and all(x > 0 for x in fB)
    )
    if decomposable:
        denom = log(revM / revB)
        for i, row in enumerate(rows):
            row["contribution"] = log(fM[i] / fB[i]) / denom * delta
    return {
        "rows": rows, "rev_m": revM, "rev_b": revB, "delta": delta,
        "decomposable": decomposable, "base_label": base_label,
        "shift_days": shift_days,
    }
