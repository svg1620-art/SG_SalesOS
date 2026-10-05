"""Модуль «Операционный пульт»: маппинг, валидация, синхронизация (Этап 1),
планы/отсутствия (Этап 2), пульт РОПа и конверсии (Этап 3)."""
import threading
from datetime import datetime, timedelta

from flask import (
    Blueprint, render_template, redirect, url_for, request, flash, current_app,
)

from auth.decorators import admin_required
from extensions import db
from models import OpsActivityEvent, FunnelEvent, FunnelStageMap, OpsSyncLog, User
from settings_store import amo_base_domain, amo_access_token, amo_configured
from utils import now_local, local_to_utc_naive, app_tz
from ops_store import connect_min_sec, backfill_days

ops_bp = Blueprint("ops", __name__, url_prefix="/ops")

# шаги воронки для выпадающего списка маппинга
STEP_CHOICES = ["none", "qualified", "meeting_set", "meeting_held", "invoice", "won", "lost"]
STEP_LABELS = {
    "none": "—",
    "qualified": "Квалификация",
    "meeting_set": "Встреча назначена",
    "meeting_held": "Встреча проведена",
    "invoice": "Счёт",
    "won": "Успех (won)",
    "lost": "Проигрыш (lost)",
}
STEP_ORDER = {
    "none": 0, "qualified": 1, "meeting_set": 2, "meeting_held": 3,
    "invoice": 4, "won": 5, "lost": 6,
}
WON_STATUS_ID, LOST_STATUS_ID = 142, 143


def _run_bg(target):
    """Запуск долгой задачи в фоне (свой app_context)."""
    app = current_app._get_current_object()

    def _job():
        with app.app_context():
            try:
                target(app)
            except Exception:  # noqa: BLE001
                app.logger.exception("[ops] фоновая задача упала")

    threading.Thread(target=_job, daemon=True).start()


def _pipelines():
    """Воронки amoCRM со статусами (best-effort)."""
    if not amo_configured():
        return [], "amoCRM не настроен"
    try:
        from ingest.amo_client import AmoClient
        return AmoClient(amo_base_domain(), amo_access_token()).get_pipelines(), None
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


@ops_bp.route("/")
@admin_required
def index():
    return redirect(url_for("ops.validation"))


@ops_bp.route("/settings/funnel", methods=["GET"])
@admin_required
def funnel():
    pipelines, err = _pipelines()
    current = {
        (int(m.pipeline_id), int(m.status_id)): m.step
        for m in FunnelStageMap.query.all()
    }
    return render_template(
        "ops/funnel.html",
        pipelines=pipelines, pipelines_error=err, current=current,
        step_choices=STEP_CHOICES, step_labels=STEP_LABELS,
        won_status=WON_STATUS_ID, lost_status=LOST_STATUS_ID,
    )


@ops_bp.route("/settings/funnel", methods=["POST"])
@admin_required
def funnel_save():
    pipelines, err = _pipelines()
    if err:
        flash(f"Не удалось получить воронки amoCRM: {err}", "error")
        return redirect(url_for("ops.funnel"))

    existing = {
        (int(m.pipeline_id), int(m.status_id)): m
        for m in FunnelStageMap.query.all()
    }
    saved = 0
    for p in pipelines:
        pid = p.get("id")
        for st in (p.get("statuses") or []):
            sid = st.get("id")
            if pid is None or sid is None:
                continue
            # 142/143 размечаются автоматически
            if sid == WON_STATUS_ID:
                step = "won"
            elif sid == LOST_STATUS_ID:
                step = "lost"
            else:
                raw = request.form.get(f"step_{pid}_{sid}") or "none"
                step = raw if raw in STEP_CHOICES else "none"
            row = existing.get((int(pid), int(sid)))
            if row is None:
                db.session.add(FunnelStageMap(
                    pipeline_id=pid, status_id=sid, step=step,
                    step_order=STEP_ORDER.get(step, 0),
                ))
            else:
                row.step = step
                row.step_order = STEP_ORDER.get(step, 0)
            saved += 1
    db.session.commit()
    flash(f"Маппинг сохранён ({saved} этапов).", "success")
    return redirect(url_for("ops.funnel"))


@ops_bp.route("/sync/incremental", methods=["POST"])
@admin_required
def sync_incremental():
    from ingest.ops_sync import sync_incremental as _inc
    _run_bg(_inc)
    flash("Инкрементальная синхронизация запущена в фоне. Обновите страницу через минуту.", "success")
    return redirect(url_for("ops.validation"))


@ops_bp.route("/sync/backfill", methods=["POST"])
@admin_required
def sync_backfill():
    from ingest.ops_sync import backfill as _bf
    _run_bg(_bf)
    flash(
        f"Бэкфилл за {backfill_days()} дней запущен в фоне. Это может занять "
        "несколько минут — следите за статусом ниже.",
        "success",
    )
    return redirect(url_for("ops.validation"))


def _yesterday_bounds():
    """Границы «вчера» в Europe/Moscow → UTC-naive (как хранится occurred_at)."""
    tz = app_tz()
    today_local = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = today_local - timedelta(days=1)
    end_local = today_local
    return local_to_utc_naive(start_local), local_to_utc_naive(end_local)


def _month_arg():
    """Выбранный месяц (1-е число) из ?month=YYYY-MM, иначе текущий."""
    from datetime import date
    raw = (request.args.get("month") or request.form.get("month") or "").strip()
    try:
        y, mo = map(int, raw.split("-"))
        return date(y, mo, 1)
    except Exception:  # noqa: BLE001
        t = now_local().date()
        return t.replace(day=1)


def _sales_managers():
    return (
        User.query.filter(User.is_active.is_(True), User.role == "manager")
        .order_by(User.full_name, User.email).all()
    )


@ops_bp.route("/plans", methods=["GET"])
@admin_required
def plans():
    from models import ManagerPlan
    month = _month_arg()
    plans_map = {
        p.manager_id: p
        for p in ManagerPlan.query.filter_by(month=month).all()
    }
    rows = [{"m": m, "plan": plans_map.get(m.id)} for m in _sales_managers()]
    return render_template(
        "ops/plans.html", rows=rows, month=month,
        month_value=month.strftime("%Y-%m"),
    )


@ops_bp.route("/plans", methods=["POST"])
@admin_required
def plans_save():
    from datetime import date
    from models import ManagerPlan
    month = _month_arg()
    action = request.form.get("action")

    existing = {p.manager_id: p for p in ManagerPlan.query.filter_by(month=month).all()}

    if action == "copy":
        # скопировать с прошлого месяца (revenue/qualified/meetings) там, где пусто
        prev = (month.replace(day=1) - timedelta(days=1)).replace(day=1)
        prev_map = {p.manager_id: p for p in ManagerPlan.query.filter_by(month=prev).all()}
        copied = 0
        for m in _sales_managers():
            src = prev_map.get(m.id)
            if src is None:
                continue
            row = existing.get(m.id) or ManagerPlan(manager_id=m.id, month=month)
            row.revenue_plan = src.revenue_plan
            row.qualified_plan = src.qualified_plan
            row.meetings_plan = src.meetings_plan
            db.session.add(row)
            copied += 1
        db.session.commit()
        flash(f"Скопировано планов с {prev.strftime('%m.%Y')}: {copied}.", "success")
        return redirect(url_for("ops.plans", month=month.strftime("%Y-%m")))

    def _num(raw):
        raw = (raw or "").strip().replace(" ", "")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def _intn(raw):
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    for m in _sales_managers():
        rev = _num(request.form.get(f"revenue_{m.id}"))
        qual = _intn(request.form.get(f"qualified_{m.id}"))
        meet = _intn(request.form.get(f"meetings_{m.id}"))
        row = existing.get(m.id)
        if rev is None and qual is None and meet is None:
            if row is not None:
                db.session.delete(row)
            continue
        if row is None:
            row = ManagerPlan(manager_id=m.id, month=month)
            db.session.add(row)
        row.revenue_plan = rev
        row.qualified_plan = qual
        row.meetings_plan = meet
    db.session.commit()
    flash("Планы сохранены.", "success")
    return redirect(url_for("ops.plans", month=month.strftime("%Y-%m")))


@ops_bp.route("/absences", methods=["GET"])
@admin_required
def absences():
    from models import ManagerAbsence
    items = (
        ManagerAbsence.query.order_by(ManagerAbsence.date_from.desc()).limit(200).all()
    )
    mgr_names = {m.id: (m.full_name or m.email) for m in _sales_managers()}
    return render_template(
        "ops/absences.html", items=items, managers=_sales_managers(),
        mgr_names=mgr_names,
    )


@ops_bp.route("/absences", methods=["POST"])
@admin_required
def absences_add():
    from datetime import datetime as _dt
    from models import ManagerAbsence
    mid = request.form.get("manager_id")
    kind = request.form.get("kind") or "vacation"
    try:
        df = _dt.strptime(request.form.get("date_from"), "%Y-%m-%d").date()
        dt = _dt.strptime(request.form.get("date_to"), "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        flash("Укажите корректные даты.", "error")
        return redirect(url_for("ops.absences"))
    if not (mid and mid.isdigit()) or dt < df:
        flash("Проверьте менеджера и порядок дат.", "error")
        return redirect(url_for("ops.absences"))
    db.session.add(ManagerAbsence(
        manager_id=int(mid), date_from=df, date_to=dt,
        kind=kind if kind in ("vacation", "sick", "other") else "other",
    ))
    db.session.commit()
    flash("Отсутствие добавлено.", "success")
    return redirect(url_for("ops.absences"))


@ops_bp.route("/absences/<int:absence_id>/delete", methods=["POST"])
@admin_required
def absences_delete(absence_id):
    from models import ManagerAbsence
    row = db.session.get(ManagerAbsence, absence_id)
    if row is not None:
        db.session.delete(row)
        db.session.commit()
        flash("Отсутствие удалено.", "success")
    return redirect(url_for("ops.absences"))


def _dept_filter():
    """(departments, selected_id, manager_ids|None) для фильтра по отделу."""
    from models import Department
    departments = Department.query.order_by(Department.name).all()
    raw = request.args.get("department_id")
    dep_id = int(raw) if raw and raw.isdigit() else None
    mgr_ids = None
    if dep_id is not None:
        mgr_ids = {u.id for u in User.query.filter_by(department_id=dep_id).all()}
    return departments, dep_id, mgr_ids


_STATUS_RANK = {"red": 0, "yellow": 1, "green": 2}


@ops_bp.route("/board")
@admin_required
def board():
    from ops.board import board_rows
    departments, dep_id, mgr_ids = _dept_filter()
    rows = board_rows(dept_manager_ids=mgr_ids)

    sort = request.args.get("sort", "status")
    keymap = {
        "status": lambda r: (_STATUS_RANK.get(r["status"], 9), -(r["calls_connected"])),
        "name": lambda r: r["name"].lower(),
        "calls": lambda r: -r["calls_connected"],
        "connect": lambda r: -(r["connect_rate"] or 0),
        "quality": lambda r: -(r["avg_call_score"] or 0),
        "month": lambda r: -((r["month"] or {}).get("progress_pct") or 0),
    }
    rows.sort(key=keymap.get(sort, keymap["status"]))

    summary = {"green": 0, "yellow": 0, "red": 0}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1

    return render_template(
        "ops/board.html", rows=rows, summary=summary, sort=sort,
        departments=departments, department_id=dep_id,
        now_label=now_local().strftime("%H:%M"),
    )


@ops_bp.route("/board/refresh", methods=["POST"])
@admin_required
def board_refresh():
    from ops.metrics import recompute_today
    _run_bg(recompute_today)
    flash("Пересчёт метрик за сегодня запущен. Обновите страницу через несколько секунд.", "success")
    return redirect(url_for("ops.board", department_id=request.form.get("department_id") or None))


@ops_bp.route("/board/conversions")
@admin_required
def board_conversions():
    from datetime import timedelta as _td
    from ops.board import (
        funnel_counts, flow_conversions, cohort_conversions, team_heatmap,
        FLOW_STAGES, COHORT_PAIRS,
    )
    departments, dep_id, mgr_ids = _dept_filter()
    mode = request.args.get("mode", "cohort")
    mode = "flow" if mode == "flow" else "cohort"

    today = now_local().date()
    try:
        to_d = datetime.strptime(request.args.get("to", ""), "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        to_d = today
    try:
        from_d = datetime.strptime(request.args.get("from", ""), "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        from_d = today - _td(days=30)

    managers = (
        User.query.filter(User.is_active.is_(True), User.role == "manager")
        .order_by(User.full_name, User.email).all()
    )
    if mgr_ids is not None:
        managers = [m for m in managers if m.id in mgr_ids]
    mids = [m.id for m in managers]

    counts = funnel_counts(from_d, to_d, mids)
    team_funnel = counts["team"]

    # конверсии: team + по менеджерам
    if mode == "flow":
        columns = [f"{l1}→{l2}" for (k1, l1), (k2, l2) in zip(FLOW_STAGES, FLOW_STAGES[1:])]
        team_conv = flow_conversions(team_funnel)
        per_rows = []
        for m in managers:
            c = counts["per_manager"].get(m.id, {k: 0 for k, _ in FLOW_STAGES})
            per_rows.append({"name": m.full_name or m.email,
                             "conv": flow_conversions(c)})
    else:
        columns = [label for _, _, label in COHORT_PAIRS]
        team_conv = cohort_conversions(from_d, to_d, None)
        per_rows = []
        for m in managers:
            per_rows.append({"name": m.full_name or m.email,
                             "conv": cohort_conversions(from_d, to_d, m.id)})

    # медианы колонок (для подсветки отклонений)
    medians = []
    for i in range(len(columns)):
        vals = [pr["conv"][i]["value"] for pr in per_rows if pr["conv"][i]["value"] is not None]
        from statistics import median as _med
        medians.append(_med(vals) if vals else None)

    heat = team_heatmap()
    weekday_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    return render_template(
        "ops/board_conversions.html",
        mode=mode, columns=columns, team_conv=team_conv, per_rows=per_rows,
        medians=medians, team_funnel=team_funnel, flow_stages=FLOW_STAGES,
        from_d=from_d, to_d=to_d, departments=departments, department_id=dep_id,
        heat=heat, weekday_names=weekday_names,
    )


def _cost_users(mf):
    """Пользователи для страницы затрат: менеджеры + админы (возможные РОПы),
    активные ИЛИ деактивированные в выбранном месяце или позже."""
    from calendar import monthrange
    users = User.query.filter(User.role.in_(["manager", "admin"])).order_by(
        User.role, User.full_name, User.email
    ).all()
    out = []
    for u in users:
        if u.is_active:
            out.append(u)
        elif u.deactivated_at and u.deactivated_at.date() >= mf:
            out.append(u)
    return out


@ops_bp.route("/costs", methods=["GET"])
@admin_required
def costs():
    from models import StaffCost
    from ops.payback import own_cost, recompute_bonuses, bonus_info
    from ops_store import ops_int
    mf = _month_arg()
    recompute_bonuses(mf)  # освежить авто-бонус перед показом
    bonus_auto = ops_int("bonus_auto", 1) == 1
    users = _cost_users(mf)
    sc_map = {s.user_id: s for s in StaffCost.query.filter_by(month=mf).all()}
    rows = []
    for u in users:
        sc = sc_map.get(u.id)
        info = bonus_info(u.id, mf) if u.role == "manager" else None
        rows.append({
            "user": u, "sc": sc,
            "default_role": "rop" if u.role == "admin" else "manager",
            "full_cost": own_cost(sc),
            "bonus_info": info,
            "is_manager": u.role == "manager",
        })
    return render_template("ops/costs.html", rows=rows, month=mf,
                           month_value=mf.strftime("%Y-%m"), bonus_auto=bonus_auto)


@ops_bp.route("/costs", methods=["POST"])
@admin_required
def costs_save():
    from datetime import datetime as _dt
    from models import StaffCost
    from ops.payback import recompute_allocations
    from flask_login import current_user
    mf = _month_arg()
    action = request.form.get("action")

    def _num(raw, default=0.0):
        raw = (raw or "").strip().replace(" ", "").replace(",", ".")
        if raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    if action == "copy":
        prev = (mf.replace(day=1) - timedelta(days=1)).replace(day=1)
        prev_map = {s.user_id: s for s in StaffCost.query.filter_by(month=prev).all()}
        existing = {s.user_id: s for s in StaffCost.query.filter_by(month=mf).all()}
        copied = 0
        for u in _cost_users(mf):
            src = prev_map.get(u.id)
            if src is None:
                continue
            row = existing.get(u.id) or StaffCost(user_id=u.id, month=mf)
            row.cost_role = src.cost_role
            row.salary_fixed = src.salary_fixed
            row.payroll_tax_rate = src.payroll_tax_rate
            row.overhead = src.overhead
            row.lead_cost = src.lead_cost
            row.bonus_paid = 0  # бонус обнуляется
            row.updated_by = current_user.id
            db.session.add(row)
            copied += 1
        db.session.commit()
        from ops.payback import recompute_bonuses
        recompute_bonuses(mf)
        recompute_allocations(mf)
        flash(f"Скопировано с {prev.strftime('%m.%Y')}: {copied}. Бонусы и доли РОПа пересчитаны.", "success")
        return redirect(url_for("ops.costs", month=mf.strftime("%Y-%m")))

    existing = {s.user_id: s for s in StaffCost.query.filter_by(month=mf).all()}
    for u in _cost_users(mf):
        prefix = f"u{u.id}_"
        salary = _num(request.form.get(prefix + "salary"))
        bonus = _num(request.form.get(prefix + "bonus"))
        tax = _num(request.form.get(prefix + "tax"), 0.302)
        overhead = _num(request.form.get(prefix + "overhead"))
        lead = request.form.get(prefix + "lead")
        role = request.form.get(prefix + "role") or ("rop" if u.role == "admin" else "manager")
        comment = (request.form.get(prefix + "comment") or "").strip()[:500]
        filled = any(request.form.get(prefix + k) for k in ("salary", "bonus", "overhead", "lead"))
        row = existing.get(u.id)
        if not filled and row is None:
            continue
        if row is None:
            row = StaffCost(user_id=u.id, month=mf)
            db.session.add(row)
        row.cost_role = "rop" if role == "rop" else "manager"
        row.salary_fixed = salary
        row.bonus_paid = bonus
        row.payroll_tax_rate = tax
        row.overhead = overhead
        row.lead_cost = _num(lead, None) if (lead or "").strip() else None
        row.comment = comment or None
        row.updated_by = current_user.id
    db.session.commit()
    from ops.payback import recompute_bonuses
    recompute_bonuses(mf)  # авто-бонус от первой выручки (перетирает ручной у менеджеров с планом)
    recompute_allocations(mf)
    flash("Затраты сохранены. Бонусы и доли РОПа пересчитаны.", "success")
    return redirect(url_for("ops.costs", month=mf.strftime("%Y-%m")))


@ops_bp.route("/payback/recompute-first-revenue", methods=["POST"])
@admin_required
def payback_recompute_fr():
    from ingest.amo_deals import recompute_first_revenue
    _run_bg(recompute_first_revenue)
    flash("Пересчёт первой выручки запущен в фоне.", "success")
    return redirect(url_for("ops.payback"))


@ops_bp.route("/payback")
@admin_required
def payback():
    from ops.payback import (
        payback_for, cumulative_net, factor_diagnosis, month_first,
        recompute_bonuses, recompute_allocations,
    )
    from models import Deal
    mf = _month_arg()
    recompute_bonuses(mf)       # авто-бонус от первой выручки
    recompute_allocations(mf)   # доли РОПа с учётом свежих бонусов
    managers = _sales_managers()

    # сводка: менеджер × последние 6 месяцев (payback_ratio)
    months = []
    cur = mf
    for _ in range(6):
        months.append(cur)
        cur = (cur.replace(day=1) - timedelta(days=1)).replace(day=1)
    months = list(reversed(months))

    summary = []
    for m in managers:
        cells = [payback_for(m, mo) for mo in months]
        summary.append({"manager": m, "cells": cells})

    # --- атрибуция первой выручки за выбранный месяц (диагностика «почему одной суммой») ---
    from ops.payback import first_revenue, _month_bounds
    _f, _l, _nxt = _month_bounds(mf)
    attr_rows = []
    attr_total = 0
    for m in managers:
        s, _c = first_revenue(m.id, mf)
        if s:
            attr_rows.append({"name": m.full_name or m.email, "sum": s})
        attr_total += s
    attr_rows.sort(key=lambda r: -r["sum"])
    from datetime import datetime as _dt
    unattr = db.session.query(db.func.coalesce(db.func.sum(Deal.price), 0)).filter(
        Deal.outcome == "won", Deal.is_first_revenue.is_(True), Deal.manager_id.is_(None),
        Deal.won_at >= _dt(mf.year, mf.month, mf.day),
        Deal.won_at < _dt(_nxt.year, _nxt.month, _nxt.day),
    ).scalar() or 0
    attribution = {
        "rows": attr_rows, "attributed": attr_total,
        "unattributed": int(unattr), "total": attr_total + int(unattr),
    }

    # выбранный менеджер — детальный разбор
    sel_id = request.args.get("manager_id")
    selected = None
    if sel_id and sel_id.isdigit():
        selected = db.session.get(User, int(sel_id))
    if selected is None and managers:
        selected = managers[0]

    detail = None
    if selected is not None:
        base_mode = request.args.get("base", "prev")
        try:
            shift_days = int(request.args.get("shift", "0"))
        except ValueError:
            shift_days = 0
        detail = {
            "manager": selected,
            "pb": payback_for(selected, mf),
            "series": cumulative_net(selected, mf),
            "diag": factor_diagnosis(selected.id, mf, base_mode, shift_days),
            "base_mode": base_mode, "shift_days": shift_days,
        }

    return render_template(
        "ops/payback.html", managers=managers, months=months, summary=summary,
        detail=detail, month=mf, month_value=mf.strftime("%Y-%m"),
        attribution=attribution,
    )


@ops_bp.route("/validation")
@admin_required
def validation():
    # 1) глубина истории по типам
    depth = (
        db.session.query(
            OpsActivityEvent.type, db.func.min(OpsActivityEvent.occurred_at),
            db.func.count(OpsActivityEvent.id),
        ).group_by(OpsActivityEvent.type).all()
    )
    depth_rows = [{"type": t, "earliest": e, "count": c} for t, e, c in depth]
    funnel_min = db.session.query(db.func.min(FunnelEvent.occurred_at)).scalar()
    funnel_count = db.session.query(db.func.count(FunnelEvent.id)).scalar()

    y_from, y_to = _yesterday_bounds()

    # 2) звонки за вчера: с длительностью/статусом vs без
    calls_q = OpsActivityEvent.query.filter(
        OpsActivityEvent.type.in_(["call_in", "call_out"]),
        OpsActivityEvent.occurred_at >= y_from,
        OpsActivityEvent.occurred_at < y_to,
    )
    calls_total = calls_q.count()
    calls_with = calls_q.filter(
        OpsActivityEvent.duration_sec.isnot(None),
        OpsActivityEvent.call_status.isnot(None),
    ).count()
    calls_quality = {
        "total": calls_total, "with_meta": calls_with,
        "without_meta": calls_total - calls_with,
    }

    # 3) сообщения по менеджерам за 14 дней
    msg_from = local_to_utc_naive(
        now_local().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=14)
    )
    msg_counts = dict(
        db.session.query(OpsActivityEvent.manager_id, db.func.count(OpsActivityEvent.id))
        .filter(OpsActivityEvent.type == "msg_out", OpsActivityEvent.occurred_at >= msg_from)
        .group_by(OpsActivityEvent.manager_id).all()
    )
    active_managers = (
        User.query.filter(User.is_active.is_(True), User.role == "manager")
        .order_by(User.full_name, User.email).all()
    )
    msg_rows = [
        {"name": m.full_name or m.email, "count": msg_counts.get(m.id, 0),
         "warn": msg_counts.get(m.id, 0) == 0 and m.amo_user_id is not None,
         "no_amo": m.amo_user_id is None}
        for m in active_managers
    ]

    # 4) звонки за вчера по менеджерам (наша БД) — для сверки с amoCRM (кнопка)
    db_calls = dict(
        db.session.query(OpsActivityEvent.manager_id, db.func.count(OpsActivityEvent.id))
        .filter(OpsActivityEvent.type.in_(["call_in", "call_out"]),
                OpsActivityEvent.occurred_at >= y_from, OpsActivityEvent.occurred_at < y_to)
        .group_by(OpsActivityEvent.manager_id).all()
    )
    sverka_rows = [
        {"name": m.full_name or m.email, "amo_user_id": m.amo_user_id,
         "db_calls": db_calls.get(m.id, 0)}
        for m in active_managers if m.amo_user_id is not None
    ][:3]

    # 5) неразмеченные этапы воронок
    pipelines, pl_err = _pipelines()
    mapped = {
        (int(m.pipeline_id), int(m.status_id)): m.step
        for m in FunnelStageMap.query.all()
    }
    unmapped = []
    for p in pipelines:
        pid = p.get("id")
        for st in (p.get("statuses") or []):
            sid = st.get("id")
            if sid in (WON_STATUS_ID, LOST_STATUS_ID):
                continue
            step = mapped.get((int(pid), int(sid))) if pid and sid else None
            if not step or step == "none":
                unmapped.append({"pipeline": p.get("name"), "status": st.get("name"),
                                 "pipeline_id": pid, "status_id": sid})

    recent_syncs = OpsSyncLog.query.order_by(OpsSyncLog.started_at.desc()).limit(10).all()

    return render_template(
        "ops/validation.html",
        depth_rows=depth_rows, funnel_min=funnel_min, funnel_count=funnel_count,
        calls_quality=calls_quality, msg_rows=msg_rows, sverka_rows=sverka_rows,
        unmapped=unmapped, pipelines_error=pl_err, recent_syncs=recent_syncs,
        connect_min_sec=connect_min_sec(),
    )


@ops_bp.route("/validation/crosscheck", methods=["POST"])
@admin_required
def validation_crosscheck():
    """Живая сверка звонков за вчера: наша БД vs amoCRM /events (по created_by)."""
    if not amo_configured():
        flash("amoCRM не настроен.", "error")
        return redirect(url_for("ops.validation"))
    y_from, y_to = _yesterday_bounds()
    mgr_map = {u.amo_user_id: (u.full_name or u.email)
               for u in User.query.filter(User.amo_user_id.isnot(None)).all()}
    try:
        from ingest.amo_client import AmoClient
        client = AmoClient(amo_base_domain(), amo_access_token())
        amo_counts = {}
        for ev in client.iter_events(
            ["outgoing_call", "incoming_call"],
            since_ts=int(y_from.timestamp()), until_ts=int(y_to.timestamp()),
            max_pages=50,
        ):
            cb = ev.get("created_by") or 0
            amo_counts[cb] = amo_counts.get(cb, 0) + 1
    except Exception as exc:  # noqa: BLE001
        flash(f"Сверка не удалась: {exc}", "error")
        return redirect(url_for("ops.validation"))

    lines = []
    for amo_uid, cnt in sorted(amo_counts.items(), key=lambda kv: -kv[1])[:10]:
        name = mgr_map.get(amo_uid, f"amo_user {amo_uid}")
        lines.append(f"{name}: amoCRM {cnt}")
    flash("Сверка amoCRM (звонки за вчера по created_by): " + "; ".join(lines or ["нет событий"]),
          "success")
    return redirect(url_for("ops.validation"))
