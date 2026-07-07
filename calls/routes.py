"""Звонки: ручная загрузка, список, карточка, polling-статус, отдача аудио, экспорт."""
import csv
import io
import os
from datetime import datetime, timedelta

from flask import (
    Blueprint,
    render_template,
    redirect,
    url_for,
    request,
    flash,
    abort,
    send_file,
    Response,
)
from flask_login import login_required, current_user

from extensions import db
from models import Call, Checklist, User
from auth.decorators import admin_required
from ingest.manual_upload import save_manual_call, DuplicateCallError
from processing.worker import enqueue_call

_SPEAKER_RU = {"manager": "Менеджер", "client": "Клиент", "unknown": "Говорящий"}

calls_bp = Blueprint("calls", __name__, url_prefix="/calls")

# статусы, при которых обработка ещё идёт
IN_PROGRESS = {"new", "downloading", "transcribing", "analyzing"}


def _get_call_or_404(call_id: int) -> Call:
    call = db.session.get(Call, call_id)
    if call is None:
        abort(404)
    # менеджер видит только свои звонки
    if not current_user.is_admin and call.manager_id != current_user.id:
        abort(403)
    return call


@calls_bp.route("/")
@login_required
def index():
    now = datetime.utcnow()
    date_from = _parse_date(request.args.get("from"), now - timedelta(days=30))
    date_to_raw = _parse_date(request.args.get("to"), now)
    date_to = date_to_raw.replace(hour=23, minute=59, second=59)
    zone = request.args.get("zone") or ""
    manager_id = request.args.get("manager_id")
    manager_id = int(manager_id) if manager_id and manager_id.isdigit() else None

    query = Call.query.filter(Call.started_at >= date_from, Call.started_at <= date_to)
    if not current_user.is_admin:
        query = query.filter(Call.manager_id == current_user.id)
        manager_id = None  # менеджеру не даём выбор чужих
    elif manager_id:
        query = query.filter(Call.manager_id == manager_id)
    if zone in {"green", "yellow", "red"}:
        query = query.filter(Call.zone == zone)

    calls = query.all()
    calls.sort(key=lambda c: c.started_at or c.created_at, reverse=True)
    calls = calls[:500]

    managers = (
        User.query.filter_by(is_active=True).order_by(User.full_name, User.email).all()
        if current_user.is_admin else []
    )
    filters = {
        "from": date_from.strftime("%Y-%m-%d"),
        "to": date_to_raw.strftime("%Y-%m-%d"),
        "manager_id": manager_id,
        "zone": zone,
    }
    return render_template(
        "calls/index.html", calls=calls, managers=managers, filters=filters,
        query_string=request.query_string.decode(),
    )


def _checklists_for_select():
    """Все чек-листы для выбора (активный первым) + активный отдельно."""
    checklists = Checklist.query.order_by(
        Checklist.is_active.desc(), Checklist.name
    ).all()
    active = next((c for c in checklists if c.is_active), None)
    return checklists, active


def _resolve_checklist(checklist_id_raw, default):
    """Выбрать чек-лист по id из формы, иначе дефолт (активный)."""
    if checklist_id_raw and checklist_id_raw.isdigit():
        chosen = db.session.get(Checklist, int(checklist_id_raw))
        if chosen is not None:
            return chosen
    return default


@calls_bp.route("/upload", methods=["GET", "POST"])
@admin_required
def upload():
    managers = User.query.filter_by(is_active=True).order_by(User.full_name).all()
    checklists, active_checklist = _checklists_for_select()

    def _render(status=200):
        return render_template(
            "calls/upload.html", managers=managers, checklists=checklists,
            active_checklist=active_checklist, form=request.form,
        ), status

    if request.method == "POST":
        checklist = _resolve_checklist(
            request.form.get("checklist_id"), active_checklist
        )
        if checklist is None:
            flash("Выберите чек-лист для оценки (или создайте и активируйте).", "error")
            return _render(400)
        if not checklist.criteria:
            flash("У выбранного чек-листа нет критериев — оценивать не по чему.", "error")
            return _render(400)

        started_raw = (request.form.get("started_at") or "").strip()
        try:
            started_at = (
                datetime.strptime(started_raw, "%Y-%m-%dT%H:%M")
                if started_raw
                else datetime.utcnow()
            )
        except ValueError:
            started_at = datetime.utcnow()

        mgr_channel_raw = request.form.get("manager_channel")
        manager_channel = int(mgr_channel_raw) if mgr_channel_raw in {"0", "1"} else None
        manager_id_raw = request.form.get("manager_id")
        manager_id = int(manager_id_raw) if manager_id_raw and manager_id_raw.isdigit() else None

        try:
            call = save_manual_call(
                file_storage=request.files.get("audio"),
                manager_id=manager_id,
                phone=request.form.get("phone") or "",
                client_name=request.form.get("client_name") or "",
                direction=request.form.get("direction") or "in",
                started_at=started_at,
                manager_channel=manager_channel,
            )
        except DuplicateCallError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("calls.index"))
        except ValueError as exc:
            flash(str(exc), "error")
            return _render(400)

        # фиксируем выбранный чек-лист и запускаем пайплайн
        call.checklist_id = checklist.id
        db.session.commit()
        enqueue_call(call.id)

        flash("Звонок загружен, обработка запущена.", "success")
        return redirect(url_for("calls.detail", call_id=call.id))

    return render_template(
        "calls/upload.html", managers=managers, checklists=checklists,
        active_checklist=active_checklist, form={},
    )


def _all_checklists_for(call):
    """Чек-листы для переоценки (только админу); менеджеру не нужен выбор."""
    if not current_user.is_admin:
        return []
    return Checklist.query.order_by(Checklist.is_active.desc(), Checklist.name).all()


def _radar_data(call):
    """Данные для радара: критерии + оценки, нормированные к 10."""
    labels, scores = [], []
    for cs in call.criterion_scores:
        labels.append(cs.criterion.title if cs.criterion else "—")
        max_score = cs.max_score or 10
        scores.append(round((cs.score or 0) / max_score * 10, 1))
    return {"labels": labels, "scores": scores}


def _split_text_by_quotes(text, quotes):
    """Разбить строку на части, помечая совпадения с цитатами упущений.

    quotes: список (quote_lower, moment). Возвращает список
    {text, missed(None|MissedMoment)}. Регистронезависимый поиск.
    """
    if not quotes:
        return [{"text": text, "missed": None}]
    parts = []
    low = text.lower()
    i = 0
    while i < len(text):
        best_pos, best = None, None
        for q, moment in quotes:
            pos = low.find(q, i)
            if pos != -1 and (best_pos is None or pos < best_pos):
                best_pos, best = pos, (q, moment)
        if best is None:
            parts.append({"text": text[i:], "missed": None})
            break
        q, moment = best
        if best_pos > i:
            parts.append({"text": text[i:best_pos], "missed": None})
        parts.append({"text": text[best_pos:best_pos + len(q)], "missed": moment})
        i = best_pos + len(q)
    return parts


def _annotate_transcript(call):
    """Транскрипт с разметкой упущенных моментов для инлайн-подсветки."""
    quotes = []
    for moment in call.missed_moments:
        q = (moment.quote or "").strip().lower()
        if q:
            quotes.append((q, moment))
    annotated = []
    for seg in call.transcript_json or []:
        annotated.append(
            {
                "speaker": seg.get("speaker"),
                "parts": _split_text_by_quotes(seg.get("text") or "", quotes),
            }
        )
    return annotated


def _panel_context(call):
    """Общий контекст для карточки/панели."""
    from settings_store import amo_base_domain
    from utils import amo_entity_url

    return {
        "call": call,
        "in_progress": IN_PROGRESS,
        "checklists": _all_checklists_for(call),
        "radar": _radar_data(call),
        "annotated": _annotate_transcript(call),
        "amo_url": amo_entity_url(
            amo_base_domain(), call.amo_entity_type, call.amo_entity_id
        ),
    }


@calls_bp.route("/<int:call_id>")
@login_required
def detail(call_id):
    call = _get_call_or_404(call_id)
    return render_template("calls/detail.html", **_panel_context(call))


@calls_bp.route("/<int:call_id>/panel")
@login_required
def panel(call_id):
    """HTMX-фрагмент: статус во время обработки, результат по готовности."""
    call = _get_call_or_404(call_id)
    return render_template("calls/_panel.html", **_panel_context(call))


@calls_bp.route("/<int:call_id>/reprocess", methods=["POST"])
@admin_required
def reprocess(call_id):
    call = db.session.get(Call, call_id) or abort(404)
    if call.status in IN_PROGRESS:
        flash("Звонок уже обрабатывается.", "warning")
        return redirect(url_for("calls.detail", call_id=call.id))

    # можно переоценить по другому чек-листу
    checklist_id_raw = request.form.get("checklist_id")
    if checklist_id_raw and checklist_id_raw.isdigit():
        chosen = db.session.get(Checklist, int(checklist_id_raw))
        if chosen is None:
            flash("Выбранный чек-лист не найден.", "error")
            return redirect(url_for("calls.detail", call_id=call.id))
        if not chosen.criteria:
            flash("У выбранного чек-листа нет критериев.", "error")
            return redirect(url_for("calls.detail", call_id=call.id))
        call.checklist_id = chosen.id

    call.status = "new"
    call.error = None
    db.session.commit()
    enqueue_call(call.id)
    flash("Повторная обработка запущена.", "success")
    return redirect(url_for("calls.detail", call_id=call.id))


@calls_bp.route("/<int:call_id>/push-amo", methods=["POST"])
@admin_required
def push_amo(call_id):
    """Выгрузить оценку/транскрибацию звонка в ленту amoCRM (примечание)."""
    from flask import current_app
    from ingest.amo_notes import push_call_note

    call = db.session.get(Call, call_id) or abort(404)
    include_transcript = request.form.get("include_transcript") != "0"
    result = push_call_note(
        current_app._get_current_object(), call, include_transcript=include_transcript
    )
    if result.get("ok"):
        flash("Выгружено в ленту amoCRM.", "success")
    else:
        flash(f"Не удалось выгрузить в amoCRM: {result.get('error')}", "error")
    return redirect(url_for("calls.detail", call_id=call.id))


def _remove_audio(call):
    if call.audio_path and os.path.exists(call.audio_path):
        try:
            os.remove(call.audio_path)
        except OSError:
            pass


@calls_bp.route("/<int:call_id>/delete", methods=["POST"])
@admin_required
def delete(call_id):
    call = db.session.get(Call, call_id) or abort(404)
    _remove_audio(call)
    db.session.delete(call)
    db.session.commit()
    flash("Звонок удалён.", "success")
    return redirect(url_for("calls.index"))


@calls_bp.route("/delete-failed", methods=["POST"])
@admin_required
def delete_failed():
    calls = Call.query.filter_by(status="failed").all()
    for c in calls:
        _remove_audio(c)
        db.session.delete(c)
    db.session.commit()
    flash(f"Удалено звонков со статусом «ошибка»: {len(calls)}.", "success")
    return redirect(url_for("calls.index"))


@calls_bp.route("/delete-amo", methods=["POST"])
@admin_required
def delete_amo():
    """Удалить все звонки, импортированные из amoCRM (для чистого переопроса)."""
    calls = Call.query.filter(Call.amo_note_id.isnot(None)).all()
    for c in calls:
        _remove_audio(c)
        db.session.delete(c)
    db.session.commit()
    flash(f"Удалено импортированных из amoCRM звонков: {len(calls)}.", "success")
    return redirect(url_for("calls.index"))


@calls_bp.route("/reprocess-stuck", methods=["POST"])
@admin_required
def reprocess_stuck():
    """Переочередить звонки, застрявшие в статусе «new» (фон не дошёл)."""
    calls = Call.query.filter_by(status="new").all()
    for c in calls:
        enqueue_call(c.id)
    flash(f"Отправлено в обработку зависших звонков: {len(calls)}.", "success")
    return redirect(url_for("calls.index"))


@calls_bp.route("/<int:call_id>/toggle-exclude", methods=["POST"])
@admin_required
def toggle_exclude(call_id):
    call = db.session.get(Call, call_id) or abort(404)
    call.excluded = not bool(call.excluded)
    db.session.commit()
    # пересчёт диалога без учёта исключённого
    from processing.aggregate import recompute_dialog_for_call

    recompute_dialog_for_call(call)
    db.session.commit()
    flash(
        "Звонок исключён из рейтинга." if call.excluded
        else "Звонок возвращён в рейтинг.",
        "success",
    )
    return redirect(url_for("calls.detail", call_id=call.id))


@calls_bp.route("/<int:call_id>/audio")
@login_required
def audio(call_id):
    call = _get_call_or_404(call_id)
    if not call.audio_path or not os.path.exists(call.audio_path):
        abort(404)
    return send_file(call.audio_path, mimetype="audio/mpeg", conditional=True)


# --- Экспорт (только админ) ---------------------------------------------

def _parse_date(raw, default):
    raw = (raw or "").strip()
    if not raw:
        return default
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return default


def _filtered_calls_from_args():
    """Звонки по фильтрам из query (from/to/manager_id/zone/status)."""
    now = datetime.utcnow()
    date_from = _parse_date(request.args.get("from"), now - timedelta(days=90))
    date_to = _parse_date(request.args.get("to"), now).replace(
        hour=23, minute=59, second=59
    )
    query = Call.query.filter(
        Call.started_at >= date_from, Call.started_at <= date_to
    )
    manager_id = request.args.get("manager_id")
    if manager_id and manager_id.isdigit():
        query = query.filter(Call.manager_id == int(manager_id))
    department_id = request.args.get("department_id")
    if department_id and department_id.isdigit():
        dept_ids = [u.id for u in User.query.filter_by(department_id=int(department_id)).all()]
        query = query.filter(Call.manager_id.in_(dept_ids or [-1]))
    zone = request.args.get("zone")
    if zone in {"green", "yellow", "red"}:
        query = query.filter(Call.zone == zone)
    status = request.args.get("status")
    if status:
        query = query.filter(Call.status == status)
    calls = query.all()
    calls.sort(key=lambda c: c.started_at or c.created_at)
    return calls


def _transcript_text(call):
    lines = []
    for seg in call.transcript_json or []:
        who = _SPEAKER_RU.get(seg.get("speaker"), "Говорящий")
        text = (seg.get("text") or "").strip()
        if text:
            lines.append(f"{who}: {text}")
    return "\n".join(lines)


@calls_bp.route("/export.csv")
@admin_required
def export_csv():
    calls = _filtered_calls_from_args()
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([
        "id", "дата", "менеджер", "отдел", "телефон клиента", "имя клиента",
        "направление", "длительность_сек", "статус", "чек-лист",
        "балл", "зона", "саммери", "транскрибация",
    ])
    for c in calls:
        writer.writerow([
            c.id,
            c.started_at.strftime("%Y-%m-%d %H:%M") if c.started_at else "",
            (c.manager.full_name or c.manager.email) if c.manager else "",
            (c.manager.department.name if c.manager and c.manager.department else ""),
            c.client.phone_normalized if c.client else "",
            (c.client.name or "") if c.client else "",
            c.direction or "",
            c.duration_sec or 0,
            c.status,
            c.checklist.name if c.checklist else "",
            c.overall_score if c.overall_score is not None else "",
            c.zone or "",
            (c.summary or "").replace("\r", " "),
            _transcript_text(c),
        ])
    data = "﻿" + buf.getvalue()  # BOM для Excel
    return Response(
        data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=calls_export.csv"},
    )


@calls_bp.route("/export.txt")
@admin_required
def export_txt():
    calls = _filtered_calls_from_args()
    blocks = []
    for c in calls:
        header = (
            f"=== Звонок #{c.id} | "
            f"{c.started_at.strftime('%Y-%m-%d %H:%M') if c.started_at else '—'} | "
            f"Менеджер: {(c.manager.full_name or c.manager.email) if c.manager else '—'} | "
            f"Клиент: {c.client.phone_normalized if c.client else '—'} | "
            f"Балл: {c.overall_score if c.overall_score is not None else '—'} "
            f"({c.zone or '—'})"
        )
        body = _transcript_text(c) or "(нет транскрибации)"
        summary = f"\nСаммери: {c.summary}" if c.summary else ""
        blocks.append(f"{header}\n{'-' * 60}\n{body}{summary}\n")
    text = "\n".join(blocks) or "Нет звонков под выбранные фильтры."
    return Response(
        text,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=transcripts_export.txt"},
    )
