"""Диалоги: агрегаты звонков по клиенту. Список, карточка, экспорт."""
import csv
import io

from flask import Blueprint, render_template, abort, Response
from flask_login import login_required, current_user

from extensions import db
from models import Dialog, Call
from auth.decorators import admin_required

dialogs_bp = Blueprint("dialogs", __name__, url_prefix="/dialogs")


@dialogs_bp.route("/")
@login_required
def index():
    query = Dialog.query
    if not current_user.is_admin:
        # только диалоги, где у менеджера есть звонки
        query = query.join(Call, Call.dialog_id == Dialog.id).filter(
            Call.manager_id == current_user.id
        ).distinct()
    dialogs = query.all()
    # сортировка: по времени последнего обновления
    dialogs.sort(key=lambda d: d.updated_at or d.id, reverse=True)
    return render_template("dialogs/index.html", dialogs=dialogs)


@dialogs_bp.route("/<int:dialog_id>")
@login_required
def detail(dialog_id):
    dialog = db.session.get(Dialog, dialog_id) or abort(404)

    calls = Call.query.filter_by(dialog_id=dialog.id).all()
    calls.sort(key=lambda c: c.started_at or c.created_at, reverse=True)

    if not current_user.is_admin:
        # менеджер видит диалог, только если в нём есть его звонки
        if not any(c.manager_id == current_user.id for c in calls):
            abort(403)

    return render_template("dialogs/detail.html", dialog=dialog, calls=calls)


@dialogs_bp.route("/export.csv")
@admin_required
def export_csv():
    """Выгрузка всех диалогов в CSV (для анализа)."""
    dialogs = Dialog.query.all()
    dialogs.sort(key=lambda d: d.updated_at or d.id, reverse=True)

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([
        "id", "телефон клиента", "имя клиента", "менеджер",
        "звонков", "средний_балл", "последняя_зона", "тренд", "обновлён",
    ])
    for d in dialogs:
        writer.writerow([
            d.id,
            d.client.phone_normalized if d.client else "",
            (d.client.name or "") if d.client else "",
            (d.manager.full_name or d.manager.email) if d.manager else "",
            d.calls_count,
            d.avg_score if d.avg_score is not None else "",
            d.last_zone or "",
            d.trend or "",
            d.updated_at.strftime("%Y-%m-%d %H:%M") if d.updated_at else "",
        ])
    data = "﻿" + buf.getvalue()
    return Response(
        data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=dialogs_export.csv"},
    )
