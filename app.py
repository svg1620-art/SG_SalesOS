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

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)


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
    """Запускаем планировщик только если явно включён (Этапы 8-9).

    На Этапе 1 задач нет, но проверяем, чтобы не плодить процессы при
    нескольких воркерах gunicorn.
    """
    if not app.config.get("SCHEDULER_ENABLED"):
        return
    if scheduler.running:
        return
    # избегаем двойного старта в reloader'е dev-сервера
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        scheduler.start()


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


# Экземпляр для gunicorn: `gunicorn app:app`
app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
