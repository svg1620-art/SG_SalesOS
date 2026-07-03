"""Ручная загрузка аудио: сохранение в Volume + создание Call с дедупом.

Дедуп ручной загрузки — SHA256(нормализованное имя файла + длительность + дата).
Файлы кладём только в Volume (AUDIO_DIR=/data), т.к. локальный диск эфемерен.
"""
import hashlib
import os
import uuid
from datetime import datetime

from flask import current_app
from pydub import AudioSegment
from pydub.utils import mediainfo

from extensions import db
from models import Call, Client, User
from utils import normalize_phone

ALLOWED_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".oga", ".flac", ".aac", ".mp4"}


def _audio_dir() -> str:
    path = current_app.config.get("AUDIO_DIR") or "/data"
    os.makedirs(path, exist_ok=True)
    return path


def _duration_seconds(path: str) -> int:
    """Длительность аудио в секундах (0 при неудаче — не блокируем загрузку)."""
    try:
        info = mediainfo(path)
        return int(float(info.get("duration") or 0))
    except Exception:
        try:
            return int(AudioSegment.from_file(path).duration_seconds)
        except Exception:
            return 0


def _content_hash(orig_name: str, duration: int, started_at: datetime) -> str:
    base = f"{(orig_name or '').strip().lower()}|{duration}|{started_at:%Y-%m-%d}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


class DuplicateCallError(Exception):
    """Такой звонок уже загружен (совпал content_hash)."""


def save_manual_call(
    *,
    file_storage,
    manager_id: int | None,
    phone: str,
    client_name: str = "",
    direction: str = "in",
    started_at: datetime | None = None,
    manager_channel: int | None = None,
) -> Call:
    """Сохранить загруженный файл и создать Call(status='new').

    Возвращает созданный Call. Бросает ValueError при некорректных данных,
    DuplicateCallError при дубле.
    """
    if file_storage is None or not file_storage.filename:
        raise ValueError("Не выбран аудиофайл.")

    orig_name = file_storage.filename
    ext = os.path.splitext(orig_name)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"Неподдерживаемый формат файла: {ext or 'без расширения'}.")

    phone_norm = normalize_phone(phone)
    if not phone_norm:
        raise ValueError("Некорректный номер телефона клиента.")

    if direction not in {"in", "out"}:
        direction = "in"
    started_at = started_at or datetime.utcnow()

    # сохраняем в Volume под уникальным именем
    stored_name = f"{uuid.uuid4().hex}{ext}"
    stored_path = os.path.join(_audio_dir(), stored_name)
    file_storage.save(stored_path)

    duration = _duration_seconds(stored_path)
    content_hash = _content_hash(orig_name, duration, started_at)

    existing = Call.query.filter_by(content_hash=content_hash).first()
    if existing is not None:
        os.remove(stored_path) if os.path.exists(stored_path) else None
        raise DuplicateCallError(
            f"Этот звонок уже загружен (id={existing.id})."
        )

    # клиент по нормализованному номеру
    client = Client.query.filter_by(phone_normalized=phone_norm).first()
    if client is None:
        client = Client(
            phone_normalized=phone_norm,
            name=(client_name or "").strip() or None,
            first_seen_at=started_at,
            last_seen_at=started_at,
        )
        db.session.add(client)
        db.session.flush()
    else:
        if client_name and not client.name:
            client.name = client_name.strip()
        client.last_seen_at = started_at

    manager = db.session.get(User, manager_id) if manager_id else None

    call = Call(
        manager_id=manager.id if manager else None,
        client_id=client.id,
        direction=direction,
        started_at=started_at,
        duration_sec=duration,
        audio_path=stored_path,
        source_link=None,
        status="new",
        diarization=None,
        manager_channel=manager_channel if manager_channel in (0, 1) else None,
        content_hash=content_hash,
    )
    db.session.add(call)
    db.session.commit()
    return call
