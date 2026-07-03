"""Фоновый прогон пайплайна обработки звонка по статусам.

new → transcribing → analyzing → done  (или failed с сохранением ошибки).

Обработка идёт в отдельном потоке, чтобы не блокировать HTTP-ответ; UI следит за
статусом polling'ом. Падение одного звонка не роняет процесс — ошибка пишется в
Call.error, статус failed (изоляция сбоев по звонку).
"""
import threading
from datetime import datetime

from flask import current_app

from extensions import db
from models import Call, Checklist


def process_call(call_id: int) -> None:
    """Синхронно прогнать один звонок через пайплайн (внутри app context)."""
    from processing.transcribe import transcribe_call
    from processing.analyze import analyze_call
    from processing.scoring import apply_analysis

    call = db.session.get(Call, call_id)
    if call is None:
        return

    try:
        # 1) транскрибация (файл уже в Volume — этап downloading для ручной пуст)
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
    """Запустить обработку звонка в фоновом потоке."""
    app = current_app._get_current_object()
    thread = threading.Thread(
        target=_run_in_context, args=(app, call_id), daemon=True
    )
    thread.start()
