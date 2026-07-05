"""Опрос звонков из amoCRM и запуск пайплайна.

Тянем примечания call_in/call_out, дедупим по amo_note_id, привязываем менеджера
(по User.amo_user_id ↔ responsible_user_id) и клиента (по телефону), скачиваем
запись в Volume и отправляем в тот же пайплайн обработки. Падение одного звонка
не роняет прогон.
"""
import os
import uuid
from datetime import datetime

from flask import current_app

from extensions import db
from models import Call, Client, User, Checklist
from utils import normalize_phone
from settings_store import (
    amo_base_domain, amo_access_token, amo_entity, amo_configured,
    get_setting, set_setting,
)
from ingest.amo_client import AmoClient, AmoError
from processing.worker import enqueue_call


def test_connection(app=None) -> tuple[bool, str]:
    app = app or current_app
    if not amo_configured(app):
        return False, "Не задан домен или токен amoCRM."
    client = AmoClient(amo_base_domain(app), amo_access_token(app))
    try:
        acc = client.get_account()
        name = (acc or {}).get("name") or amo_base_domain(app)
        return True, f"Подключение успешно: аккаунт «{name}»."
    except AmoError as exc:
        return False, f"Ошибка подключения: {exc}"


def _audio_dir() -> str:
    path = current_app.config.get("AUDIO_DIR") or "/data"
    os.makedirs(path, exist_ok=True)
    return path


def _save_recording(content: bytes) -> str:
    name = f"amo_{uuid.uuid4().hex}.mp3"
    path = os.path.join(_audio_dir(), name)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _get_or_create_client(phone_norm: str, started_at: datetime) -> Client:
    client = Client.query.filter_by(phone_normalized=phone_norm).first()
    if client is None:
        client = Client(
            phone_normalized=phone_norm,
            first_seen_at=started_at, last_seen_at=started_at,
        )
        db.session.add(client)
        db.session.flush()
    else:
        client.last_seen_at = started_at
    return client


def poll_amo(app=None) -> dict:
    """Опросить amoCRM и завести новые звонки. Возвращает сводку прогона."""
    app = app or current_app
    if not amo_configured(app):
        return {"ok": False, "error": "amoCRM не настроен", "new": 0, "errors": 0}

    client = AmoClient(amo_base_domain(app), amo_access_token(app))
    entity = amo_entity(app)
    since = get_setting("amo_last_sync")
    since_ts = int(since) if since and str(since).isdigit() else None

    active = Checklist.query.filter_by(is_active=True).first()
    new_calls, errors, max_updated = 0, 0, since_ts or 0

    try:
        notes = list(client.iter_call_notes(entity, since_ts))
    except AmoError as exc:
        app.logger.warning("[amo] опрос не удался: %s", exc)
        return {"ok": False, "error": str(exc), "new": 0, "errors": 0}

    for note in notes:
        try:
            updated = int(note.get("updated_at") or 0)
            max_updated = max(max_updated, updated)
            note_id = note.get("id")
            if note_id and Call.query.filter_by(amo_note_id=note_id).first():
                continue

            params = note.get("params") or {}
            phone_norm = normalize_phone(params.get("phone"))
            if not phone_norm:
                continue  # без телефона не привязать клиента

            link = params.get("link")
            duration = int(params.get("duration") or 0)
            direction = "out" if note.get("note_type") == "call_out" else "in"
            started_at = datetime.utcfromtimestamp(int(note.get("created_at") or updated or 0)) \
                if (note.get("created_at") or updated) else datetime.utcnow()

            responsible = note.get("responsible_user_id")
            manager = (
                User.query.filter_by(amo_user_id=responsible).first()
                if responsible else None
            )
            client_obj = _get_or_create_client(phone_norm, started_at)

            call = Call(
                amo_note_id=note_id,
                manager_id=manager.id if manager else None,
                client_id=client_obj.id,
                checklist_id=active.id if active else None,
                direction=direction,
                started_at=started_at,
                duration_sec=duration,
                source_link=link,
                status="new",
            )

            audio = client.download_recording(link) if link else None
            if audio:
                call.audio_path = _save_recording(audio)
            else:
                call.status = "failed"
                call.error = "Не удалось скачать запись (нет ссылки/доступа)."

            db.session.add(call)
            db.session.commit()
            if audio:
                enqueue_call(call.id)
            new_calls += 1
        except Exception as exc:  # noqa: BLE001 — изоляция по звонку
            db.session.rollback()
            errors += 1
            app.logger.warning("[amo] звонок из примечания %s пропущен: %s",
                               note.get("id"), exc)

    if max_updated:
        set_setting("amo_last_sync", max_updated)

    app.logger.info("[amo] опрос завершён: новых %s, ошибок %s", new_calls, errors)
    return {"ok": True, "new": new_calls, "errors": errors, "last_sync": max_updated}
