"""Telegram-бот: привязка личного чата менеджера и отправка уведомлений (Этап 6b).

- Привязка: менеджер открывает бота и шлёт `/start <код>` (код из профиля
  /me/notifications). Бот находит пользователя по User.telegram_code, пишет
  chat_id, подтверждает. `/stop` — отвязка/выключение уведомлений.
- Доставка: `notify_manager` уважает notify_enabled и тихие часы (рабочие часы
  ops). Если бот/чат не настроен — тихий no-op.
- Polling: `poll_updates` опрашивает getUpdates со смещением (offset хранится в
  ops_settings), без inbound-вебхука. Запускается по крону каждые ~2 минуты.

Токен и username бота — через settings_store (БД → env), не хардкодить.
"""
import secrets
from datetime import datetime
from html import escape

import httpx

from extensions import db
from models import User
from settings_store import telegram_token


_OFFSET_KEY = "tg_update_offset"


# ---------- низкоуровневая отправка ----------

def _api(app, method, payload, timeout=20):
    token = telegram_token(app)
    if not token:
        return None
    try:
        resp = httpx.post(f"https://api.telegram.org/bot{token}/{method}",
                          json=payload, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        app.logger.warning("[tg-bot] %s: %s %s", method, resp.status_code, resp.text[:200])
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("[tg-bot] %s ошибка: %s", method, exc)
    return None


def send_to_chat(app, chat_id, text) -> bool:
    """Отправить сообщение в один чат (HTML)."""
    if not chat_id:
        return False
    res = _api(app, "sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    return bool(res and res.get("ok"))


# ---------- тихие часы ----------

def _within_quiet_hours(app) -> bool:
    """True, если СЕЙЧАС вне рабочих часов (личные уведомления не шлём)."""
    from utils import now_local
    from ops_store import ops_int
    wh_from = ops_int("work_hours_from", 9)
    wh_to = ops_int("work_hours_to", 20)
    h = now_local(app).hour
    return not (wh_from <= h < wh_to)


def notify_manager(app, manager, text, respect_quiet=True) -> bool:
    """Личное уведомление менеджеру с учётом настроек и тихих часов."""
    if manager is None or not manager.telegram_chat_id:
        return False
    if not manager.notify_enabled:
        return False
    if respect_quiet and _within_quiet_hours(app):
        return False
    return send_to_chat(app, manager.telegram_chat_id, text)


# ---------- одноразовый код привязки ----------

def gen_code(user) -> str:
    """Сгенерировать и сохранить одноразовый код привязки для пользователя."""
    code = secrets.token_hex(4)  # 8 hex-символов
    user.telegram_code = code
    db.session.commit()
    return code


def bind_link(app, code) -> str | None:
    """Ссылка t.me для привязки (если известен username бота)."""
    from settings_store import telegram_bot_username
    uname = telegram_bot_username(app)
    if not uname or not code:
        return None
    return f"https://t.me/{uname}?start={code}"


# ---------- обработка входящих апдейтов ----------

def _handle_start(app, chat_id, arg):
    arg = (arg or "").strip()
    if not arg:
        send_to_chat(app, chat_id,
                     "Привет! Чтобы привязать уведомления, открой в SalesOS "
                     "«Уведомления» и нажми кнопку привязки, либо пришли "
                     "<code>/start КОД</code> из профиля.")
        return
    user = User.query.filter_by(telegram_code=arg).first()
    if user is None:
        send_to_chat(app, chat_id, "Код не найден или уже использован. "
                                   "Обнови код в профиле SalesOS.")
        return
    user.telegram_chat_id = str(chat_id)
    user.telegram_code = None          # код одноразовый
    user.notify_enabled = True
    db.session.commit()
    name = escape(user.full_name or user.email)
    send_to_chat(app, chat_id,
                 f"✅ Готово, {name}! Личные уведомления SalesOS привязаны к "
                 f"этому чату. Отключить — командой <code>/stop</code>.")


def _handle_stop(app, chat_id):
    user = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
    if user is not None:
        user.notify_enabled = False
        db.session.commit()
    send_to_chat(app, chat_id, "🔕 Личные уведомления отключены. Включить снова — "
                               "привяжи чат заново в профиле SalesOS.")


def _dispatch(app, message):
    chat = (message or {}).get("chat") or {}
    chat_id = chat.get("id")
    text = (message or {}).get("text") or ""
    if chat_id is None or not text:
        return
    cmd, _, arg = text.strip().partition(" ")
    cmd = cmd.lower().lstrip("/").split("@")[0]  # /start@bot → start
    if cmd == "start":
        _handle_start(app, chat_id, arg)
    elif cmd == "stop":
        _handle_stop(app, chat_id)


def poll_updates(app=None, limit=50) -> dict:
    """Опросить getUpdates и обработать /start, /stop. Хранит offset в ops_settings."""
    from flask import current_app
    from ops_store import get_ops_setting, set_ops_setting
    app = app or current_app
    if not telegram_token(app):
        return {"ok": False, "skip": "no_token"}
    try:
        offset = int(get_ops_setting(_OFFSET_KEY) or 0)
    except (TypeError, ValueError):
        offset = 0
    payload = {"timeout": 0, "limit": limit, "allowed_updates": ["message"]}
    if offset:
        payload["offset"] = offset
    res = _api(app, "getUpdates", payload, timeout=25)
    if not res or not res.get("ok"):
        return {"ok": False, "skip": "no_updates"}
    updates = res.get("result") or []
    last = offset
    handled = 0
    for u in updates:
        last = max(last, int(u.get("update_id", 0)) + 1)
        msg = u.get("message") or u.get("edited_message")
        if msg:
            try:
                _dispatch(app, msg)
                handled += 1
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                app.logger.warning("[tg-bot] апдейт пропущен: %s", exc)
    if last != offset:
        set_ops_setting(_OFFSET_KEY, str(last))
    return {"ok": True, "handled": handled, "offset": last}
