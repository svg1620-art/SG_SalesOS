"""Лидерборд отдела продаж: рейтинг менеджеров по выручке за месяц + XP.

Доступ: админ и менеджеры отдела продаж. Выручка — сумма успешных сделок
(Deal) за выбранный месяц. XP: +50 за каждые 50 000 руб выручки за месяц.
Первое место выделяется кубком 🏆.
"""
from datetime import datetime

from flask import Blueprint, render_template, request, abort
from flask_login import login_required, current_user

from models import Deal, User, Department
from ingest.amo_deals import xp_for_revenue, XP_STEP_RUB, XP_PER_STEP

leaderboard_bp = Blueprint("leaderboard", __name__)

SALES_DEPARTMENT_NAME = "Отдел продаж"

_RU_MONTHS = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def _sales_department():
    return Department.query.filter_by(name=SALES_DEPARTMENT_NAME).first()


def can_view_leaderboard(user) -> bool:
    """Кто видит лидерборд: админ и любой менеджер отдела продаж."""
    if not user.is_authenticated:
        return False
    if user.is_admin:
        return True
    dept = user.department
    return bool(dept and dept.name == SALES_DEPARTMENT_NAME)


def _fmt_money(rub: int) -> str:
    return f"{int(rub or 0):,}".replace(",", " ")


@leaderboard_bp.route("/leaderboard")
@login_required
def index():
    if not can_view_leaderboard(current_user):
        abort(403)

    now = datetime.utcnow()
    month_raw = (request.args.get("month") or "").strip()
    try:
        m_year, m_num = map(int, month_raw.split("-"))
        assert 1 <= m_num <= 12
    except Exception:  # noqa: BLE001
        m_year, m_num = now.year, now.month

    start = datetime(m_year, m_num, 1)
    end = datetime(m_year + 1, 1, 1) if m_num == 12 else datetime(m_year, m_num + 1, 1)

    # менеджеры отдела продаж
    dept = _sales_department()
    if dept is not None:
        managers = (
            User.query.filter_by(department_id=dept.id, is_active=True)
            .order_by(User.full_name, User.email)
            .all()
        )
    else:
        managers = []

    # выручка за месяц по менеджеру
    deals = Deal.query.filter(Deal.won_at >= start, Deal.won_at < end).all()
    revenue_by_mgr, deals_by_mgr = {}, {}
    for d in deals:
        if d.manager_id is None:
            continue
        revenue_by_mgr[d.manager_id] = revenue_by_mgr.get(d.manager_id, 0) + (d.price or 0)
        deals_by_mgr[d.manager_id] = deals_by_mgr.get(d.manager_id, 0) + 1

    rows = []
    for m in managers:
        revenue = revenue_by_mgr.get(m.id, 0)
        rows.append({
            "manager": m,
            "name": m.full_name or m.email,
            "revenue": revenue,
            "revenue_fmt": _fmt_money(revenue),
            "deals": deals_by_mgr.get(m.id, 0),
            "xp": xp_for_revenue(revenue),
        })

    # рейтинг по выручке (по убыванию), затем по имени
    rows.sort(key=lambda r: (-r["revenue"], r["name"].lower()))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
        r["is_leader"] = i == 1 and r["revenue"] > 0

    total_revenue = sum(r["revenue"] for r in rows)

    return render_template(
        "leaderboard/index.html",
        rows=rows,
        month_label=f"{_RU_MONTHS[m_num]} {m_year}",
        month_value=f"{m_year:04d}-{m_num:02d}",
        total_revenue_fmt=_fmt_money(total_revenue),
        xp_step=XP_STEP_RUB,
        xp_per_step=XP_PER_STEP,
        has_department=dept is not None,
    )
