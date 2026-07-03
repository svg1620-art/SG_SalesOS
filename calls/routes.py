"""Звонки: ручная загрузка, список, карточка, polling-статус, отдача аудио."""
import os
from datetime import datetime

from flask import (
    Blueprint,
    render_template,
    redirect,
    url_for,
    request,
    flash,
    abort,
    send_file,
)
from flask_login import login_required, current_user

from extensions import db
from models import Call, Checklist, User
from auth.decorators import admin_required
from ingest.manual_upload import save_manual_call, DuplicateCallError
from processing.worker import enqueue_call

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
    query = Call.query.order_by(Call.created_at.desc())
    if not current_user.is_admin:
        query = query.filter(Call.manager_id == current_user.id)
    calls = query.limit(200).all()
    return render_template("calls/index.html", calls=calls)


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


@calls_bp.route("/<int:call_id>")
@login_required
def detail(call_id):
    call = _get_call_or_404(call_id)
    return render_template(
        "calls/detail.html", call=call, in_progress=IN_PROGRESS,
        checklists=_all_checklists_for(call),
    )


@calls_bp.route("/<int:call_id>/panel")
@login_required
def panel(call_id):
    """HTMX-фрагмент: статус во время обработки, результат по готовности."""
    call = _get_call_or_404(call_id)
    return render_template(
        "calls/_panel.html", call=call, in_progress=IN_PROGRESS,
        checklists=_all_checklists_for(call),
    )


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


@calls_bp.route("/<int:call_id>/audio")
@login_required
def audio(call_id):
    call = _get_call_or_404(call_id)
    if not call.audio_path or not os.path.exists(call.audio_path):
        abort(404)
    return send_file(call.audio_path, mimetype="audio/mpeg", conditional=True)
