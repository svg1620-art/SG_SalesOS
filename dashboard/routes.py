"""Дашборд РОПа (админ): KPI, donut зон, лидерборд, лента звонков с фильтрами.

Менеджер редиректится в свой кабинет (Этап 7 — пока заглушка).
"""
from calendar import monthrange
from datetime import datetime, timedelta

from flask import Blueprint, render_template, redirect, url_for, request, flash, abort
from flask_login import login_required, current_user

from collections import defaultdict

from extensions import db
from models import Call, User, Recommendation, MissedMoment, DailyDigest, Department
from auth.decorators import admin_required
from utils import app_tz, now_local, to_local, local_to_utc_naive

dashboard_bp = Blueprint("dashboard", __name__)

_TREND_EPS = 3

# палитра цветов для столбиков менеджеров
_MANAGER_COLORS = [
    "#1467F5", "#00BFDC", "#22C55E", "#EAB308", "#EF4444", "#A855F7",
    "#EC4899", "#14B8A6", "#F97316", "#84CC16", "#6366F1", "#F43F5E",
]
_RU_MONTHS = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def _build_month_bars(month_year, month_num, dept_manager_ids):
    """Сгруппированная диаграмма: звонки по дням месяца, датасет на менеджера.

    Дни считаются в часовом поясе приложения (TZ), а started_at хранится в UTC —
    поэтому границы месяца переводим в UTC, а каждый звонок раскладываем по
    ЛОКАЛЬНОМУ дню.
    """
    tz = app_tz()
    days_in = monthrange(month_year, month_num)[1]
    start_local = datetime(month_year, month_num, 1, tzinfo=tz)
    end_local = (
        datetime(month_year + 1, 1, 1, tzinfo=tz)
        if month_num == 12
        else datetime(month_year, month_num + 1, 1, tzinfo=tz)
    )
    start = local_to_utc_naive(start_local)
    end = local_to_utc_naive(end_local)

    query = Call.query.filter(
        Call.started_at >= start, Call.started_at < end, Call.excluded.isnot(True)
    )
    calls = query.all()
    if dept_manager_ids is not None:
        calls = [c for c in calls if c.manager_id in dept_manager_ids]

    # counts[manager_id][day-1] — по локальному дню
    counts = defaultdict(lambda: [0] * days_in)
    for c in calls:
        local_dt = to_local(c.started_at or c.created_at)
        if local_dt.year == month_year and local_dt.month == month_num:
            counts[c.manager_id][local_dt.day - 1] += 1

    # имена менеджеров
    datasets = []
    idx = 0
    # стабильный порядок: по имени
    def _name(mid):
        if mid is None:
            return "Не назначен"
        u = db.session.get(User, mid)
        return (u.full_name or u.email) if u else f"#{mid}"

    for mid in sorted(counts.keys(), key=lambda m: _name(m).lower()):
        datasets.append({
            "label": _name(mid),
            "data": counts[mid],
            "color": _MANAGER_COLORS[idx % len(_MANAGER_COLORS)],
        })
        idx += 1

    return {
        "labels": [str(d) for d in range(1, days_in + 1)],
        "datasets": datasets,
        "month_label": f"{_RU_MONTHS[month_num]} {month_year}",
        "total": sum(sum(v) for v in counts.values()),
    }


def _manager_cards(period_calls, date_from, date_to, dept_manager_ids, period_days=1):
    """Карточки менеджеров: объём звонков за период, отклонение к такому же
    прошлому периоду (стрелка/%), разбивка по зонам, выполнение плана звонков."""
    span = date_to - date_from
    prev_from = date_from - span
    prev_calls = Call.query.filter(
        Call.status == "done",
        Call.excluded.isnot(True),
        Call.started_at >= prev_from,
        Call.started_at < date_from,
    ).all()
    if dept_manager_ids is not None:
        prev_calls = [c for c in prev_calls if c.manager_id in dept_manager_ids]

    cur_by = defaultdict(list)
    for c in period_calls:
        cur_by[c.manager_id].append(c)
    prev_by = defaultdict(int)
    for c in prev_calls:
        prev_by[c.manager_id] += 1

    cards = []
    for mid, mcalls in cur_by.items():
        manager = db.session.get(User, mid) if mid else None
        cur = len(mcalls)
        prev = prev_by.get(mid, 0)
        if prev > 0:
            deviation = round((cur - prev) / prev * 100)
            direction = "up" if cur > prev else "down" if cur < prev else "flat"
        else:
            deviation = 100 if cur > 0 else 0
            direction = "up" if cur > 0 else "flat"
        from processing.metrics import aggregate_talk_listen
        # выполнение плана звонков: цель = план/день × число дней периода
        plan = manager.daily_call_plan if manager else None
        plan_info = None
        if plan and plan > 0:
            target = plan * max(1, period_days)
            plan_info = {
                "per_day": plan,
                "target": target,
                "met": cur >= target,
                "remaining": max(0, target - cur),
            }
        cards.append({
            "manager": manager,
            "name": (manager.full_name or manager.email) if manager else "Не назначен",
            "calls": cur,
            "prev": prev,
            "deviation": deviation,
            "direction": direction,
            "zones": _zone_counts(mcalls),
            "balance": aggregate_talk_listen(mcalls),
            "plan": plan_info,
        })
    cards.sort(key=lambda c: c["calls"], reverse=True)
    return cards


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

    # --- фильтры (в часовом поясе приложения, TZ) ---
    tz = app_tz()
    now_l = now_local()  # aware, локальное время
    # по умолчанию — текущий день (можно расширить период фильтрами)
    df_naive = _parse_date(request.args.get("from"), now_l.replace(tzinfo=None))
    dt_naive = _parse_date(request.args.get("to"), now_l.replace(tzinfo=None))
    # локальные границы суток → naive-UTC для сравнения со started_at (хранится в UTC)
    date_from_local = df_naive.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
    date_to_local = dt_naive.replace(hour=23, minute=59, second=59, microsecond=0, tzinfo=tz)
    date_from = local_to_utc_naive(date_from_local)
    date_to = local_to_utc_naive(date_to_local)
    manager_id = request.args.get("manager_id")
    manager_id = int(manager_id) if manager_id and manager_id.isdigit() else None
    zone = request.args.get("zone") or ""

    # --- отдел (вкладки) ---
    departments = Department.query.order_by(Department.name).all()
    department_id = request.args.get("department_id")
    department_id = int(department_id) if department_id and department_id.isdigit() else None
    dept_manager_ids = None
    if department_id is not None:
        dept_manager_ids = {
            u.id for u in User.query.filter_by(department_id=department_id).all()
        }

    # менеджеры для фильтра: все или только выбранного отдела
    mgr_query = User.query
    if department_id is not None:
        mgr_query = mgr_query.filter(User.department_id == department_id)
    managers = mgr_query.order_by(User.full_name, User.email).all()

    # --- звонки за период (done, не исключённые из рейтинга) ---
    period_q = Call.query.filter(
        Call.status == "done",
        Call.excluded.isnot(True),
        Call.started_at >= date_from,
        Call.started_at <= date_to,
    )
    period_calls = period_q.all()
    # ограничение отделом (по менеджеру звонка)
    if dept_manager_ids is not None:
        period_calls = [c for c in period_calls if c.manager_id in dept_manager_ids]

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

    # --- карточки менеджеров: объём звонков + отклонение + зоны + план ---
    period_days = (dt_naive.date() - df_naive.date()).days + 1
    manager_cards = _manager_cards(
        period_calls, date_from, date_to, dept_manager_ids, period_days
    )

    # --- дневная сводка (последняя) ---
    digest = DailyDigest.query.order_by(DailyDigest.date.desc()).first()

    # --- диаграмма звонков по дням месяца (по менеджерам) ---
    month_raw = (request.args.get("month") or "").strip()
    try:
        m_year, m_num = map(int, month_raw.split("-"))
        assert 1 <= m_num <= 12
    except Exception:  # noqa: BLE001
        m_year, m_num = now_l.year, now_l.month
    bars = _build_month_bars(m_year, m_num, dept_manager_ids)

    return render_template(
        "dashboard/index.html",
        kpi=kpi,
        leaderboard=leaderboard,
        manager_cards=manager_cards,
        managers=managers,
        digest=digest,
        departments=departments,
        bars=bars,
        filters={
            "from": df_naive.strftime("%Y-%m-%d"),
            "to": dt_naive.strftime("%Y-%m-%d"),
            "manager_id": manager_id,
            "zone": zone,
            "department_id": department_id,
            "month": f"{m_year:04d}-{m_num:02d}",
        },
    )


@dashboard_bp.route("/manager/<int:manager_id>/digest", methods=["POST", "GET"])
@login_required
def manager_digest(manager_id):
    """Персональная AI-сводка по менеджеру (HTMX-фрагмент).

    Доступ: админ — по любому менеджеру; менеджер — только по себе.
    Период берётся из from/to (по умолчанию последние 30 дней).
    """
    from flask import current_app
    from digest.manager import generate_manager_digest

    if not current_user.is_admin and current_user.id != manager_id:
        abort(403)

    tz = app_tz()
    now_l = now_local()
    df_naive = _parse_date(
        request.args.get("from"), (now_l - timedelta(days=30)).replace(tzinfo=None)
    )
    dt_naive = _parse_date(request.args.get("to"), now_l.replace(tzinfo=None))
    date_from = local_to_utc_naive(
        df_naive.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
    )
    date_to = local_to_utc_naive(
        dt_naive.replace(hour=23, minute=59, second=59, microsecond=0, tzinfo=tz)
    )

    result = generate_manager_digest(
        current_app._get_current_object(), manager_id, date_from, date_to
    )
    return render_template("dashboard/_manager_digest.html", d=result)


@dashboard_bp.route("/digest/refresh", methods=["POST"])
@admin_required
def digest_refresh():
    """Сформировать/обновить дневную сводку прямо сейчас."""
    from flask import current_app
    from digest.daily import generate_daily_digest

    try:
        generate_daily_digest(current_app._get_current_object())
        flash("Сводка обновлена.", "success")
    except Exception as exc:  # noqa: BLE001
        flash(f"Не удалось сформировать сводку: {exc}", "error")
    return redirect(url_for("dashboard.index"))


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
