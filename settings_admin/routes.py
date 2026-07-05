"""Настройки платформы (только админ): Telegram-бот, часы расписания."""
from flask import Blueprint, render_template, redirect, url_for, request, flash, current_app

from auth.decorators import admin_required
from settings_store import (
    get_setting, set_setting, telegram_chat_ids, telegram_hour, digest_hour,
    telegram_token,
)

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")


def _clamp_hour(raw, default):
    try:
        return max(0, min(23, int(raw)))
    except (TypeError, ValueError):
        return default


@settings_bp.route("/", methods=["GET"])
@admin_required
def index():
    from extensions import scheduler

    return render_template(
        "settings/index.html",
        token_set=bool(telegram_token()),
        chat_ids=", ".join(telegram_chat_ids()),
        telegram_hour=telegram_hour(),
        digest_hour=digest_hour(),
        scheduler_running=scheduler.running,
        scheduler_enabled=bool(current_app.config.get("SCHEDULER_ENABLED")),
    )


@settings_bp.route("/", methods=["POST"])
@admin_required
def save():
    # токен: пустое поле — не менять; спец-значение для очистки
    token = (request.form.get("telegram_bot_token") or "").strip()
    if token == "__clear__":
        set_setting("telegram_bot_token", "")
    elif token:
        set_setting("telegram_bot_token", token)

    set_setting("telegram_chat_ids", (request.form.get("telegram_chat_ids") or "").strip())
    set_setting("telegram_hour", _clamp_hour(request.form.get("telegram_hour"), 19))
    set_setting("digest_hour", _clamp_hour(request.form.get("digest_hour"), 20))

    # перепланировать джобы под новые часы
    from app import reschedule_jobs

    reschedule_jobs(current_app._get_current_object())

    flash("Настройки сохранены.", "success")
    return redirect(url_for("settings.index"))


@settings_bp.route("/test-pulse", methods=["POST"])
@admin_required
def test_pulse():
    from notify.telegram import send_daily_pulse

    try:
        sent = send_daily_pulse(current_app._get_current_object(), force=True)
    except Exception as exc:  # noqa: BLE001
        flash(f"Ошибка отправки: {exc}", "error")
        return redirect(url_for("settings.index"))

    if sent:
        flash("Тестовый пульс отправлен в Telegram.", "success")
    else:
        flash("Не отправлено: проверьте токен и chat_id (и что боту нажали Start).", "error")
    return redirect(url_for("settings.index"))
