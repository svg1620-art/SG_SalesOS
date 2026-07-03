"""Дашборд РОПа (админ): KPI, donut зон, лидерборд, лента звонков с фильтрами.

Менеджер редиректится в свой кабинет (Этап 7 — пока заглушка).
"""
from datetime import datetime, timedelta

from flask import Blueprint, render_template, redirect, url_for, request
from flask_login import login_required, current_user

from extensions import db
from models import Call, User

dashboard_bp = Blueprint("dashboard", __name__)

_TREND_EPS = 3


def _parse_date(raw, default):
    raw = (raw or "").strip()
    if not raw:
        return default
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return default


def _manager_trend(calls_sorted):
    """Тренд менеджера: средний балл второй половины vs первой."""
    scored = [c.overall_score for c in calls_sorted if c.overall_score is not None]
    if len(scored) < 2:
        return "flat"
    mid = len(scored) // 2
    first = scored[:mid] or scored[:1]
    second = scored[mid:]
    avg1 = sum(first) / len(first)
    avg2 = sum(second) / len(second)
    if avg2 > avg1 + _TREND_EPS:
        return "up"
    if avg2 < avg1 - _TREND_EPS:
        return "down"
    return "flat"


def _zone_counts(calls):
    counts = {"green": 0, "yellow": 0, "red": 0}
    for c in calls:
        if c.zone in counts:
            counts[c.zone] += 1
    return counts


@dashboard_bp.route("/")
@login_required
def index():
    if not current_user.is_admin:
        return redirect(url_for("dashboard.manager_home"))

    # --- фильтры ---
    now = datetime.utcnow()
    date_from = _parse_date(request.args.get("from"), now - timedelta(days=30))
    date_to_raw = _parse_date(request.args.get("to"), now)
    # включительно по концу дня
    date_to = date_to_raw.replace(hour=23, minute=59, second=59)
    manager_id = request.args.get("manager_id")
    manager_id = int(manager_id) if manager_id and manager_id.isdigit() else None
    zone = request.args.get("zone") or ""

    managers = User.query.order_by(User.full_name, User.email).all()

    # --- звонки за период (done) ---
    period_q = Call.query.filter(
        Call.status == "done",
        Call.started_at >= date_from,
        Call.started_at <= date_to,
    )
    period_calls = period_q.all()

    # набор с учётом фильтра менеджера (для KPI/donut)
    scoped = [c for c in period_calls if manager_id is None or c.manager_id == manager_id]

    # KPI
    zone_counts = _zone_counts(scoped)
    total_scored = sum(zone_counts.values())
    dialogs_count = len({c.client_id for c in scoped if c.client_id})
    kpi = {
        "dialogs": dialogs_count,
        "calls": len(scoped),
        "zones": zone_counts,
        "zone_pct": {
            z: (round(zone_counts[z] / total_scored * 100) if total_scored else 0)
            for z in zone_counts
        },
    }

    # --- лидерборд (по всем менеджерам за период, без фильтра менеджера) ---
    by_manager = {}
    for c in period_calls:
        by_manager.setdefault(c.manager_id, []).append(c)
    leaderboard = []
    for mid, mcalls in by_manager.items():
        manager = db.session.get(User, mid) if mid else None
        mcalls.sort(key=lambda c: c.started_at or c.created_at)
        scored = [c.overall_score for c in mcalls if c.overall_score is not None]
        leaderboard.append(
            {
                "manager": manager,
                "calls": len(mcalls),
                "avg_score": round(sum(scored) / len(scored), 1) if scored else None,
                "zones": _zone_counts(mcalls),
                "trend": _manager_trend(mcalls),
            }
        )
    leaderboard.sort(key=lambda r: (r["avg_score"] is not None, r["avg_score"] or 0), reverse=True)

    # --- лента (scoped + фильтр зоны) ---
    feed = [c for c in scoped if not zone or c.zone == zone]
    feed.sort(key=lambda c: c.started_at or c.created_at, reverse=True)
    feed = feed[:100]

    return render_template(
        "dashboard/index.html",
        kpi=kpi,
        leaderboard=leaderboard,
        feed=feed,
        managers=managers,
        filters={
            "from": date_from.strftime("%Y-%m-%d"),
            "to": date_to_raw.strftime("%Y-%m-%d"),
            "manager_id": manager_id,
            "zone": zone,
        },
    )


@dashboard_bp.route("/me")
@login_required
def manager_home():
    return render_template("dashboard/manager.html")
