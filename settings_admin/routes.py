"""Настройки платформы (только админ): Telegram-бот, часы расписания."""
from flask import Blueprint, render_template, redirect, url_for, request, flash, current_app

from auth.decorators import admin_required
from settings_store import (
    get_setting, set_setting, telegram_chat_ids, telegram_hour, digest_hour,
    telegram_token, amo_base_domain, amo_access_token, amo_entity, amo_configured,
    amo_since_days, amo_min_duration, recording_proxy, leaderboard_pipeline_id,
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

    # список воронок amoCRM (для выбора воронки лидерборда) — best-effort
    pipelines = []
    if amo_configured():
        try:
            from ingest.amo_client import AmoClient
            pipelines = AmoClient(amo_base_domain(), amo_access_token()).get_pipelines()
        except Exception as exc:  # noqa: BLE001
            current_app.logger.info("[settings] воронки amoCRM не получены: %s", exc)

    return render_template(
        "settings/index.html",
        pipelines=pipelines,
        leaderboard_pipeline_id=leaderboard_pipeline_id(),
        token_set=bool(telegram_token()),
        chat_ids=", ".join(telegram_chat_ids()),
        telegram_hour=telegram_hour(),
        digest_hour=digest_hour(),
        scheduler_running=scheduler.running,
        scheduler_enabled=bool(current_app.config.get("SCHEDULER_ENABLED")),
        amo_domain=amo_base_domain() or "",
        amo_token_set=bool(amo_access_token()),
        amo_entity=amo_entity(),
        amo_configured=amo_configured(),
        amo_last_sync=get_setting("amo_last_sync"),
        amo_since_days=amo_since_days(),
        amo_min_duration=amo_min_duration(),
        recording_proxy=recording_proxy() or "",
        poll_min=current_app.config.get("POLL_INTERVAL_MIN"),
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


@settings_bp.route("/amo", methods=["POST"])
@admin_required
def save_amo():
    domain = (request.form.get("amo_base_domain") or "").strip()
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    set_setting("amo_base_domain", domain)

    token = (request.form.get("amo_access_token") or "").strip()
    if token == "__clear__":
        set_setting("amo_access_token", "")
    elif token:
        set_setting("amo_access_token", token)

    entity = request.form.get("amo_entity") or "contacts"
    set_setting("amo_entity", entity if entity in ("contacts", "leads") else "contacts")

    try:
        since_days = max(1, int(request.form.get("amo_since_days") or 3))
    except ValueError:
        since_days = 3
    set_setting("amo_since_days", since_days)

    try:
        min_dur = max(0, int(request.form.get("amo_min_duration") or 10))
    except ValueError:
        min_dur = 10
    set_setting("amo_min_duration", min_dur)

    set_setting("recording_proxy", (request.form.get("recording_proxy") or "").strip())

    flash("Настройки amoCRM сохранены.", "success")
    return redirect(url_for("settings.index"))


@settings_bp.route("/amo/reset-cursor", methods=["POST"])
@admin_required
def amo_reset_cursor():
    # очищаем курсор → следующий опрос применит окно «за N дней»
    set_setting("amo_last_sync", "")
    flash("Курсор очищен: следующий опрос возьмёт звонки за последние N дней.", "success")
    return redirect(url_for("settings.index"))


@settings_bp.route("/amo/debug", methods=["POST"])
@admin_required
def amo_debug():
    from ingest.amo_source import debug_recent_notes

    result = debug_recent_notes(current_app._get_current_object())
    if not result.get("ok"):
        flash(f"Диагностика: {result.get('error')}", "error")
        return redirect(url_for("settings.index"))
    return render_template("settings/amo_debug.html", result=result)


@settings_bp.route("/amo/test", methods=["POST"])
@admin_required
def amo_test():
    from ingest.amo_source import test_connection

    ok, message = test_connection(current_app._get_current_object())
    flash(message, "success" if ok else "error")
    return redirect(url_for("settings.index"))


@settings_bp.route("/amo/sync-users", methods=["POST"])
@admin_required
def amo_sync_users():
    from ingest.amo_source import sync_users

    result = sync_users(current_app._get_current_object())
    if not result.get("ok"):
        flash(f"Не удалось получить пользователей: {result.get('error')}", "error")
        return redirect(url_for("settings.index"))
    return render_template("settings/amo_users.html", result=result)


@settings_bp.route("/amo/poll", methods=["POST"])
@admin_required
def amo_poll():
    from ingest.amo_source import poll_amo

    try:
        result = poll_amo(current_app._get_current_object())
    except Exception as exc:  # noqa: BLE001
        flash(f"Ошибка опроса: {exc}", "error")
        return redirect(url_for("settings.index"))

    if result.get("ok"):
        flash(
            f"Опрос выполнен: новых звонков {result['new']}, ошибок {result['errors']}.",
            "success",
        )
    else:
        flash(f"Опрос не выполнен: {result.get('error')}", "error")
    return redirect(url_for("settings.index"))


@settings_bp.route("/amo/poll-deals", methods=["POST"])
@admin_required
def amo_poll_deals():
    from ingest.amo_deals import poll_deals

    try:
        result = poll_deals(current_app._get_current_object())
    except Exception as exc:  # noqa: BLE001
        flash(f"Ошибка опроса сделок: {exc}", "error")
        return redirect(url_for("settings.index"))

    if result.get("ok"):
        flash(
            f"Опрос сделок: новых {result['new']}, удалено {result.get('removed', 0)}, "
            f"поздравлений {result.get('congrats', 0)}"
            f"{' (первичная загрузка без поздравлений)' if result.get('backfill') else ''}.",
            "success",
        )
    else:
        flash(f"Опрос сделок не выполнен: {result.get('error')}", "error")
    return redirect(url_for("settings.index"))


@settings_bp.route("/amo/leaderboard-pipeline", methods=["POST"])
@admin_required
def amo_leaderboard_pipeline():
    """Сохранить воронку, по которой считается лидерборд (0 — все воронки)."""
    raw = (request.form.get("leaderboard_pipeline_id") or "").strip()
    set_setting("leaderboard_pipeline_id", raw if raw.isdigit() else "")
    flash("Воронка лидерборда сохранена. Нажмите «Пересобрать сделки», чтобы "
          "очистить сделки других воронок.", "success")
    return redirect(url_for("settings.index"))


@settings_bp.route("/amo/resync-deals", methods=["POST"])
@admin_required
def amo_resync_deals():
    """Очистить и загрузить сделки заново по дате закрытия, без поздравлений."""
    from ingest.amo_deals import resync_deals

    try:
        result = resync_deals(current_app._get_current_object())
    except Exception as exc:  # noqa: BLE001
        flash(f"Ошибка пересинхронизации: {exc}", "error")
        return redirect(url_for("settings.index"))

    if result.get("ok"):
        flash(
            f"Сделки пересобраны: удалено {result.get('deleted', 0)}, "
            f"загружено {result['new']} (по дате закрытия, без поздравлений).",
            "success",
        )
    else:
        flash(f"Пересинхронизация не выполнена: {result.get('error')}", "error")
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
