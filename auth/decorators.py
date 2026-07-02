"""Декораторы защиты роутов по ролям."""
from functools import wraps

from flask import abort
from flask_login import current_user


def admin_required(view):
    """Пускает только авторизованного пользователя с ролью admin."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def roles_required(*roles):
    """Пускает авторизованного пользователя с одной из указанных ролей."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator
