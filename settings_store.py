"""Настройки приложения: значения из БД (интерфейс) с fallback на env.

Ключи Telegram/расписания редактируются на странице «Настройки». Если в БД
значение не задано — берётся из config (переменные Railway).
"""
from flask import current_app

from extensions import db
from models import Setting

# ключи в БД → соответствующий ключ config (env) для fallback
_KEYS = {
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_ids": "TELEGRAM_CHAT_IDS",
    "telegram_hour": "TELEGRAM_HOUR",
    "digest_hour": "DIGEST_HOUR",
    "amo_base_domain": "AMO_BASE_DOMAIN",
    "amo_access_token": "AMO_ACCESS_TOKEN",
    "amo_entity": "AMO_ENTITY",
    # amo_last_sync — только в БД, без env
}


def get_setting(key: str, default=None):
    """Значение из БД или None, если нет (без обращения к env)."""
    try:
        s = Setting.query.filter_by(key=key).first()
    except Exception:
        return default
    if s is not None and s.value not in (None, ""):
        return s.value
    return default


def set_setting(key: str, value) -> None:
    s = Setting.query.filter_by(key=key).first()
    value = "" if value is None else str(value)
    if s is None:
        db.session.add(Setting(key=key, value=value))
    else:
        s.value = value
    db.session.commit()


def effective(key: str, app=None):
    """Итоговое значение: БД → env(config) → None."""
    app = app or current_app
    val = get_setting(key)
    if val not in (None, ""):
        return val
    env_key = _KEYS.get(key)
    if env_key:
        return app.config.get(env_key)
    return None


def telegram_token(app=None):
    return effective("telegram_bot_token", app)


def telegram_chat_ids(app=None) -> list[str]:
    raw = effective("telegram_chat_ids", app) or ""
    return [c.strip() for c in str(raw).split(",") if c.strip()]


def _int_setting(key: str, app=None, default=0) -> int:
    val = effective(key, app)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def telegram_hour(app=None) -> int:
    return _int_setting("telegram_hour", app, 19)


def digest_hour(app=None) -> int:
    return _int_setting("digest_hour", app, 20)


def amo_base_domain(app=None):
    val = effective("amo_base_domain", app)
    if not val:
        return None
    # нормализуем: без схемы и слэшей
    return val.strip().replace("https://", "").replace("http://", "").strip("/")


def amo_access_token(app=None):
    return effective("amo_access_token", app)


def amo_entity(app=None) -> str:
    val = effective("amo_entity", app)
    return (val or "contacts").strip()


def amo_configured(app=None) -> bool:
    return bool(amo_base_domain(app) and amo_access_token(app))


def amo_since_days(app=None) -> int:
    """За сколько последних дней брать звонки на ПЕРВОМ опросе (без курсора)."""
    val = get_setting("amo_since_days")
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 3
