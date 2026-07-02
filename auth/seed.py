"""Сид администратора из переменных окружения.

Используется CLI-командой `seed-admin` и авто-сидом при старте
(`SEED_ADMIN_ON_START=true`). Идемпотентно: повторный вызов обновляет пароль и
поднимает роль/активность, не плодя записи.
"""
from flask import Flask

from extensions import db


def seed_admin(app: Flask) -> tuple[bool, str]:
    """Создать или обновить админа. Возвращает (created, message).

    Ничего не делает и возвращает (False, ...), если не заданы креды —
    чтобы авто-сид при старте не ронял приложение.
    """
    from models import User

    email = (app.config.get("ADMIN_EMAIL") or "").strip().lower()
    password = app.config.get("ADMIN_PASSWORD")
    name = app.config.get("ADMIN_NAME")

    if not email or not password:
        return False, "Не заданы ADMIN_EMAIL и/или ADMIN_PASSWORD — сид пропущен."

    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(email=email, full_name=name, role="admin", is_active=True)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return True, f"Админ создан: {email}"

    user.role = "admin"
    user.is_active = True
    user.full_name = name or user.full_name
    user.set_password(password)
    db.session.commit()
    return False, f"Админ обновлён: {email}"
