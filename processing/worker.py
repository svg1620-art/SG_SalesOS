"""Фоновый прогон пайплайна обработки звонка по статусам.

new → downloading → transcribing → analyzing → done (или failed с сохранением ошибки).

Обработка идёт в ОГРАНИЧЕННОМ пуле потоков (не «поток на звонок»!), чтобы массовый
импорт из amoCRM не плодил сотни потоков и не исчерпывал пул соединений с БД.
UI следит за статусом polling'ом. Падение одного звонка не роняет процесс.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import current_app

from extensions import db
from models import Call, Checklist

# единый ограниченный пул воркеров на процесс (ленивая инициализация)
_executor = None
_executor_lock = threading.Lock()


def _get_executor(app):
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                workers = max(1, int(app.config.get("WORKER_CONCURRENCY") or 2))
                _executor = ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="sg-worker"
                )
    return _executor


def process_call(call_id: int) -> None:
    """Синхронно прогнать один звонок через пайплайн (внутри app context)."""
    from processing.transcribe import transcribe_call
    from processing.analyze import analyze_call
    from processing.scoring import apply_analysis

    call = db.session.get(Call, call_id)
    if call is None:
        return

    try:
        # 0) скачивание записи из amoCRM/телефонии, если файла ещё нет
        if not call.audio_path and call.source_link:
            call.status = "downloading"
            call.error = None
            db.session.commit()
            from ingest.amo_source import download_recording_to_volume

            path = download_recording_to_volume(current_app, call.source_link)
            if not path:
                raise RuntimeError("Не удалось скачать запись по ссылке из amoCRM.")
            call.audio_path = path
            db.session.commit()

        # 1) транскрибация (файл уже в Volume)
        call.status = "transcribing"
        call.error = None
        db.session.commit()

        transcript, diarization = transcribe_call(call)
        call.transcript_json = transcript
        call.diarization = diarization
        db.session.commit()

        # 2) анализ по активному чек-листу
        call.status = "analyzing"
        db.session.commit()

        checklist = None
        if call.checklist_id:
            checklist = db.session.get(Checklist, call.checklist_id)
        if checklist is None:
            checklist = Checklist.query.filter_by(is_active=True).first()
        if checklist is None:
            raise RuntimeError("Нет активного чек-листа для оценки звонка.")
        call.checklist_id = checklist.id

        data = analyze_call(call, checklist)
        apply_analysis(call, checklist, data)

        call.status = "done"
        call.processed_at = datetime.utcnow()

        # 3) агрегация диалога по клиенту
        from processing.aggregate import recompute_dialog_for_call

        recompute_dialog_for_call(call)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 — изоляция сбоя по звонку
        db.session.rollback()
        call = db.session.get(Call, call_id)
        if call is not None:
            call.status = "failed"
            call.error = str(exc)[:2000]
            db.session.commit()
        current_app.logger.exception("[worker] звонок %s упал: %s", call_id, exc)


def _run_in_context(app, call_id: int) -> None:
    with app.app_context():
        process_call(call_id)


def enqueue_call(call_id: int) -> None:
    """Поставить звонок в очередь ограниченного пула воркеров.

    Сколько бы звонков ни поставили, одновременно обрабатывается не больше
    WORKER_CONCURRENCY (по умолчанию 2) — остальные ждут в очереди пула.
    """
    app = current_app._get_current_object()
    _get_executor(app).submit(_run_in_context, app, call_id)
