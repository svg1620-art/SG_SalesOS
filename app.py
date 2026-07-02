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
    def seed_admin():
        """Создать/обновить админа из ADMIN_EMAIL / ADMIN_PASSWORD."""
        from models import User

        email = (app.config.get("ADMIN_EMAIL") or "").strip().lower()
        password = app.config.get("ADMIN_PASSWORD")
        name = app.config.get("ADMIN_NAME")

        if not email or not password:
            raise click.ClickException(
                "Не заданы ADMIN_EMAIL и/или ADMIN_PASSWORD в окружении."
            )

        user = User.query.filter_by(email=email).first()
        if user is None:
            user = User(email=email, full_name=name, role="admin", is_active=True)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            click.echo(f"Админ создан: {email}")
        else:
            user.role = "admin"
            user.is_active = True
            user.full_name = name or user.full_name
            user.set_password(password)
            db.session.commit()
            click.echo(f"Админ обновлён: {email}")


# Экземпляр для gunicorn: `gunicorn app:app`
app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
