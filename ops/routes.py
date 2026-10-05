"""Модуль «Операционный пульт» — Этап 1: маппинг воронки, валидация, синхронизация."""
import threading
from datetime import timedelta

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
