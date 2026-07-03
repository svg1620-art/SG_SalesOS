"""Конфигурация приложения. Всё читается из переменных окружения (см. README).

Никаких хардкод-секретов и моделей — только env. Значения по умолчанию заданы
лишь для нечувствительных параметров (пороги, часы, интервалы, таймзона).
"""
import os


def _bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _normalize_db_url(url: str) -> str:
    """Приводим DATABASE_URL к драйверу psycopg v3 и чистим невидимые символы.

    - Railway/старые провайдеры отдают `postgres://` — SQLAlchemy 2.0 такой
      префикс не понимает.
    - Голый `postgresql://` по умолчанию тянет драйвер psycopg2, которого у нас
      нет: стоит psycopg v3 (`psycopg[binary]`). Явно указываем `+psycopg`.
    - Может незаметно протащиться кириллица/пробелы/zero-width при копипасте
      (известный баг) — чистим края.
    """
    if not url:
        return url
    # убираем случайные пробелы и BOM/zero-width по краям
    url = url.strip().strip("﻿​")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    # только голый postgresql:// без явного драйвера → psycopg v3
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Config:
    # --- Ядро Flask ---
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")

    # --- База данных ---
    SQLALCHEMY_DATABASE_URI = _normalize_db_url(
        os.environ.get("DATABASE_URL", "sqlite:///salesos_dev.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # --- ИИ (модели не хардкодим, только из env) ---
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
    CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL")
    CLAUDE_MODEL_DIGEST = os.environ.get("CLAUDE_MODEL_DIGEST")
    OPENAI_TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")

    # --- Хранилище аудио (Railway Volume) ---
    AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data")

    # --- Фон/расписание ---
    POLL_INTERVAL_MIN = int(os.environ.get("POLL_INTERVAL_MIN", "15"))
    DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "20"))
    TELEGRAM_HOUR = int(os.environ.get("TELEGRAM_HOUR", "19"))
    TZ = os.environ.get("TZ", "Europe/Moscow")
    SCHEDULER_ENABLED = _bool(os.environ.get("SCHEDULER_ENABLED"), default=False)

    # --- Telegram (дневной пульс) ---
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    # получатели: chat_id через запятую (РОП, владелец)
    TELEGRAM_CHAT_IDS = os.environ.get("TELEGRAM_CHAT_IDS", "")

    # --- amoCRM (используется с Этапа 8, читаем заранее) ---
    AMO_BASE_DOMAIN = os.environ.get("AMO_BASE_DOMAIN")
    AMO_CLIENT_ID = os.environ.get("AMO_CLIENT_ID")
    AMO_CLIENT_SECRET = os.environ.get("AMO_CLIENT_SECRET")
    AMO_REDIRECT_URI = os.environ.get("AMO_REDIRECT_URI")
    AMO_AUTH_CODE = os.environ.get("AMO_AUTH_CODE")

    # --- Сид админа ---
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
    ADMIN_NAME = os.environ.get("ADMIN_NAME", "Администратор")
    # Если true — админ создаётся/обновляется при старте приложения
    # (удобно на Railway без доступа к консоли; после первого старта убрать).
    SEED_ADMIN_ON_START = _bool(os.environ.get("SEED_ADMIN_ON_START"), default=False)
