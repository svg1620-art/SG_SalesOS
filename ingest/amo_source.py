"""Опрос звонков из amoCRM и запуск пайплайна.

Тянем примечания call_in/call_out, дедупим по amo_note_id, привязываем менеджера
(по User.amo_user_id ↔ responsible_user_id) и клиента (по телефону), скачиваем
запись в Volume и отправляем в тот же пайплайн обработки. Падение одного звонка
не роняет прогон.
"""
import os
import uuid
from datetime import datetime, timedelta

from flask import current_app

from extensions import db
from models import Call, Client, User, Checklist
from utils import normalize_phone
from settings_store import (
    amo_base_domain, amo_access_token, amo_entity, amo_configured, amo_since_days,
    amo_min_duration, recording_proxy, get_setting, set_setting,
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


def sync_users(app=None) -> dict:
    """Подтянуть пользователей amoCRM и авто-проставить amo_user_id по email.

    Возвращает {ok, amo_users:[{id,name,email,matched_to}], matched, unmatched_sg}.
    """
    app = app or current_app
    if not amo_configured(app):
        return {"ok": False, "error": "amoCRM не настроен", "amo_users": []}

    client = AmoClient(amo_base_domain(app), amo_access_token(app))
    try:
        amo_users = client.get_users()
    except AmoError as exc:
        return {"ok": False, "error": str(exc), "amo_users": []}

    matched = 0
    result_users = []
    for au in amo_users:
        matched_to = None
        if au["email"]:
            sg_user = User.query.filter(db.func.lower(User.email) == au["email"]).first()
            if sg_user is not None:
                sg_user.amo_user_id = au["id"]
                matched_to = sg_user.full_name or sg_user.email
                matched += 1
        result_users.append({**au, "matched_to": matched_to})
    db.session.commit()

    unmatched_sg = [
        (u.full_name or u.email)
        for u in User.query.filter(User.amo_user_id.is_(None)).all()
    ]
    return {
        "ok": True,
        "amo_users": result_users,
        "matched": matched,
        "total": len(amo_users),
        "unmatched_sg": unmatched_sg,
    }


def debug_recent_notes(app=None, days=None, limit=15) -> dict:
    """Диагностика: сырые примечания-звонки за N дней (без дедупа/фильтров).

    Показывает реальные поля amoCRM, чтобы понять, почему звонки не подтягиваются.
    """
    app = app or current_app
    if not amo_configured(app):
        return {"ok": False, "error": "amoCRM не настроен"}

    client = AmoClient(amo_base_domain(app), amo_access_token(app))
    entity = amo_entity(app)
    days = days or amo_since_days(app)
    since_ts = int((datetime.utcnow() - timedelta(days=days)).timestamp())

    out = []
    try:
        for note in client.iter_call_notes(entity, since_ts):
            params = note.get("params") or {}
            created = note.get("created_at")
            updated = note.get("updated_at")
            out.append({
                "id": note.get("id"),
                "type": note.get("note_type"),
                "call_date": datetime.utcfromtimestamp(int(created)).strftime("%d.%m.%Y") if created else "—",
                "updated_date": datetime.utcfromtimestamp(int(updated)).strftime("%d.%m.%Y") if updated else "—",
                "responsible": note.get("responsible_user_id"),
                "phone": params.get("phone"),
                "link": bool(params.get("link")),
                "duration": params.get("duration"),
            })
            if len(out) >= limit:
                break
    except AmoError as exc:
        return {"ok": False, "error": str(exc)}

    # для подсказки — сколько на другой сущности
    other = "leads" if entity == "contacts" else "contacts"
    other_count = None
    try:
        other_count = sum(1 for _ in zip(range(limit), client.iter_call_notes(other, since_ts)))
    except Exception:  # noqa: BLE001
        other_count = None

    return {
        "ok": True, "entity": entity, "other": other, "other_count": other_count,
        "days": days, "count": len(out), "notes": out,
    }


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


def download_recording_to_volume(app, url: str):
    """Скачать запись по ссылке в Volume. Возвращает (path|None, diag:str).

    Пробуем без авторизации (внешняя ссылка Мегафона) и с Bearer amoCRM.
    Вызывается фоновым worker'ом перед транскрибацией.
    """
    if not url:
        return None, "нет ссылки"
    token = amo_access_token(app)
    client = AmoClient(amo_base_domain(app) or "x", token or "")
    content, diag = client.download_recording(url, proxy=recording_proxy(app))
    if not content:
        return None, diag
    return _save_recording(content), diag


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
    # первый опрос без курсора — берём только последние N дней (не всю историю)
    if since_ts is None:
        days = amo_since_days(app)
        since_ts = int((datetime.utcnow() - timedelta(days=days)).timestamp())
        app.logger.info("[amo] первый опрос: беру звонки за последние %s дн.", days)

    active = Checklist.query.filter_by(is_active=True).first()
    new_calls, errors, max_updated = 0, 0, since_ts or 0

    # окно по ДАТЕ ЗВОНКА (created_at) — чтобы не тянуть старые звонки,
    # у которых недавно обновился updated_at
    now_ts = int(datetime.utcnow().timestamp())
    window_start = now_ts - amo_since_days(app) * 86400
    min_dur = amo_min_duration(app)

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

            created_ts = int(note.get("created_at") or updated or 0)
            if created_ts and created_ts < window_start:
                continue  # звонок старше окна (по дате звонка) — пропускаем

            params = note.get("params") or {}
            link = params.get("link")
            if not link:
                continue  # нет записи (старые/служебные) — пропускаем, курсор двигается

            duration = int(params.get("duration") or 0)
            if duration < min_dur:
                continue  # слишком короткий (недозвон/сброс) — не анализируем

            phone_norm = normalize_phone(params.get("phone"))
            if not phone_norm:
                continue  # без телефона не привязать клиента

            direction = "out" if note.get("note_type") == "call_out" else "in"
            started_at = datetime.utcfromtimestamp(created_ts) if created_ts else datetime.utcnow()

            responsible = note.get("responsible_user_id")
            manager = (
                User.query.filter_by(amo_user_id=responsible).first()
                if responsible else None
            )
            client_obj = _get_or_create_client(phone_norm, started_at)

            call = Call(
                amo_note_id=note_id,
                amo_entity_type=entity,
                amo_entity_id=note.get("entity_id"),
                manager_id=manager.id if manager else None,
                client_id=client_obj.id,
                checklist_id=active.id if active else None,
                direction=direction,
                started_at=started_at,
                duration_sec=duration,
                source_link=link,
                status="new",
            )

            # сохраняем id контакта amoCRM на клиента (для ссылки в CRM)
            if entity == "contacts" and note.get("entity_id") and not client_obj.amo_contact_id:
                client_obj.amo_contact_id = note.get("entity_id")

            # запись НЕ качаем здесь (иначе опрос упирается в таймаут запроса) —
            # скачает фоновый worker перед транскрибацией
            db.session.add(call)
            db.session.commit()
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
