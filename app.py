"""Фабрика приложения SG_SalesOS + CLI-команды.

Запуск локально:  flask --app app run
Миграции:         flask --app app db upgrade
Сид админа:       flask --app app seed-admin
"""
import os

import click
from flask import Flask, render_template

from config import Config
from extensions import db, migrate, login_manager, scheduler


def create_app(config_object: type = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)

    _init_extensions(app)
    _register_blueprints(app)
    _register_error_handlers(app)
    _register_cli(app)
    _maybe_seed_admin(app)
    _maybe_start_scheduler(app)

    return app


def _init_extensions(app: Flask) -> None:
    db.init_app(app)
    # models должны быть импортированы до migrate.init_app, чтобы Alembic видел схему
    import models  # noqa: F401

    migrate.init_app(app, db)
    login_manager.init_app(app)


def _register_blueprints(app: Flask) -> None:
    from auth import auth_bp
    from dashboard import dashboard_bp
    from checklists import checklists_bp
    from calls import calls_bp
    from users import users_bp
    from dialogs import dialogs_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(checklists_bp)
    app.register_blueprint(calls_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(dialogs_bp)


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("errors/404.html"), 404

    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}, 200


def _maybe_seed_admin(app: Flask) -> None:
    """Авто-сид админа при старте, если задан SEED_ADMIN_ON_START.

    Удобно на Railway без доступа к консоли. Обёрнуто в try/except: при импорте
    приложения командой `flask db upgrade` таблиц ещё нет — тогда просто
    пропускаем, а сид отработает при следующем импорте (старт gunicorn после
    применения миграций).
    """
    if not app.config.get("SEED_ADMIN_ON_START"):
        return
    from auth.seed import seed_admin

    with app.app_context():
        try:
            _created, message = seed_admin(app)
            app.logger.info("[seed-admin] %s", message)
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            app.logger.warning(
                "[seed-admin] пропущен (возможно, миграции ещё не применены): %s", exc
            )


def _maybe_start_scheduler(app: Flask) -> None:
    """Запускаем планировщик, если включён (SCHEDULER_ENABLED).

    Джобы: Telegram-пульс (TELEGRAM_HOUR, по умолч. 19:00) и дневная AI-сводка
    (DIGEST_HOUR, по умолч. 20:00). Рассчитано на 1 gunicorn-воркер (иначе
    задачи задвоятся) — см. railway.toml (--workers 1 --threads 4).
    """
    if not app.config.get("SCHEDULER_ENABLED"):
        return
    if scheduler.running:
        return
    # избегаем двойного старта в reloader'е dev-сервера
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    from apscheduler.triggers.cron import CronTrigger

    tz = app.config.get("TZ") or "UTC"

    def _run_pulse():
        with app.app_context():
            from notify.telegram import send_daily_pulse
            send_daily_pulse(app)

    def _run_digest():
        with app.app_context():
            from digest.daily import generate_daily_digest
            generate_daily_digest(app)

    try:
        scheduler.add_job(
            _run_pulse, CronTrigger(hour=app.config["TELEGRAM_HOUR"], minute=0, timezone=tz),
            id="telegram_pulse", replace_existing=True, max_instances=1, coalesce=True,
        )
        scheduler.add_job(
            _run_digest, CronTrigger(hour=app.config["DIGEST_HOUR"], minute=0, timezone=tz),
            id="daily_digest", replace_existing=True, max_instances=1, coalesce=True,
        )
        scheduler.start()
        app.logger.info(
            "[scheduler] запущен: пульс %s:00, сводка %s:00 (%s)",
            app.config["TELEGRAM_HOUR"], app.config["DIGEST_HOUR"], tz,
        )
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("[scheduler] не удалось запустить: %s", exc)


def _register_cli(app: Flask) -> None:
    @app.cli.command("seed-admin")
    def seed_admin_cmd():
        """Создать/обновить админа из ADMIN_EMAIL / ADMIN_PASSWORD."""
        from auth.seed import seed_admin

        if not (app.config.get("ADMIN_EMAIL") and app.config.get("ADMIN_PASSWORD")):
            raise click.ClickException(
                "Не заданы ADMIN_EMAIL и/или ADMIN_PASSWORD в окружении."
            )
        _created, message = seed_admin(app)
        click.echo(message)

    @app.cli.command("seed-checklist")
    def seed_checklist_cmd():
        """Создать дефолтный чек-лист (Приложение A) и сделать активным."""
        from checklists.seed import seed_default_checklist

        _created, message = seed_default_checklist(app, activate=True)
        click.echo(message)

    @app.cli.command("rebuild-dialogs")
    def rebuild_dialogs_cmd():
        """Пересобрать агрегаты диалогов по всем звонкам (backfill)."""
        from processing.aggregate import rebuild_all_dialogs

        count = rebuild_all_dialogs()
        click.echo(f"Пересобрано диалогов: {count}")

    @app.cli.command("run-digest")
    def run_digest_cmd():
        """Сформировать дневную AI-сводку за сегодня (вручную)."""
        from digest.daily import generate_daily_digest

        digest = generate_daily_digest(app)
        click.echo(f"Сводка за {digest.date}: {(digest.content_json or {}).get('summary', '')[:120]}")

    @app.cli.command("send-pulse")
    def send_pulse_cmd():
        """Отправить Telegram-пульс за сегодня (вручную, принудительно)."""
        from notify.telegram import send_daily_pulse

        sent = send_daily_pulse(app, force=True)
        click.echo("Пульс отправлен." if sent else "Пульс не отправлен (проверьте TELEGRAM_*).")


# Экземпляр для gunicorn: `gunicorn app:app`
app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
