"""Дашборд РОПа (админ): KPI, donut зон, лидерборд, лента звонков с фильтрами.

Менеджер редиректится в свой кабинет (Этап 7 — пока заглушка).
"""
from datetime import datetime, timedelta

from flask import Blueprint, render_template, redirect, url_for, request
from flask_login import login_required, current_user

from collections import defaultdict

from extensions import db
from models import Call, User, Recommendation, MissedMoment

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


_PRIORITY_WEIGHT = {"high": 3, "med": 2, "low": 1}


@dashboard_bp.route("/me")
@login_required
def manager_home():
    """Кабинет менеджера: свои звонки, тренд, рекомендации, что улучшить."""
    calls = Call.query.filter_by(manager_id=current_user.id, status="done").all()
    calls.sort(key=lambda c: c.started_at or c.created_at)

    scored = [c for c in calls if c.overall_score is not None]
    avg_score = round(sum(c.overall_score for c in scored) / len(scored), 1) if scored else None
    zones = _zone_counts(scored)

    # тренд балла: точки по датам
    trend = {
        "labels": [
            (c.started_at or c.created_at).strftime("%d.%m") for c in scored
        ],
        "scores": [c.overall_score for c in scored],
    }

    call_ids = [c.id for c in calls]

    # агрегированные рекомендации по навыкам
    skills = defaultdict(lambda: {"skill": "", "count": 0, "weight": 0, "priority": "low", "example": ""})
    if call_ids:
        recs = Recommendation.query.filter(Recommendation.call_id.in_(call_ids)).all()
        for r in recs:
            key = (r.skill or "Общее").strip() or "Общее"
            item = skills[key]
            item["skill"] = key
            item["count"] += 1
            item["weight"] += _PRIORITY_WEIGHT.get(r.priority, 1)
            if _PRIORITY_WEIGHT.get(r.priority, 1) >= _PRIORITY_WEIGHT.get(item["priority"], 1):
                item["priority"] = r.priority or item["priority"]
            if not item["example"] and r.text:
                item["example"] = r.text
    skills_list = sorted(skills.values(), key=lambda s: s["weight"], reverse=True)

    # «что улучшить на этой неделе» — топ-3 навыка по весу
    focus = skills_list[:3]

    # недавние упущенные моменты
    missed = []
    if call_ids:
        missed = (
            MissedMoment.query.filter(MissedMoment.call_id.in_(call_ids))
            .order_by(MissedMoment.id.desc())
            .limit(15)
            .all()
        )

    recent_calls = list(reversed(calls))[:20]

    return render_template(
        "dashboard/manager.html",
        avg_score=avg_score,
        zones=zones,
        calls_count=len(calls),
        trend=trend,
        skills=skills_list,
        focus=focus,
        missed=missed,
        recent_calls=recent_calls,
    )
