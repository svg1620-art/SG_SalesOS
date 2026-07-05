"""Агрегация звонков в диалоги по клиенту (нормализованному телефону).

Диалог — агрегат всех звонков одного клиента: количество, средний балл,
последняя зона, тренд. Пересчитывается при доведении звонка до done и при
переоценке. Клиент уже создаётся/матчится по нормализованному телефону на
загрузке (ingest), поэтому здесь группируем по client_id.
"""
from extensions import db
from models import Dialog, Call

_TREND_EPS = 3  # порог изменения балла для up/down


def _trend(scores: list[int]) -> str:
    if len(scores) < 2:
        return "flat"
    last, prev = scores[-1], scores[-2]
    if last > prev + _TREND_EPS:
        return "up"
    if last < prev - _TREND_EPS:
        return "down"
    return "flat"


def recompute_dialog_for_call(call: Call):
    """Найти/создать диалог клиента и пересчитать агрегаты. Без commit."""
    if not call.client_id:
        return None

    dialog = Dialog.query.filter_by(client_id=call.client_id).first()
    if dialog is None:
        dialog = Dialog(client_id=call.client_id)
        db.session.add(dialog)
        db.session.flush()  # нужен dialog.id для привязки звонков

    calls = Call.query.filter_by(client_id=call.client_id).all()
    calls.sort(key=lambda c: c.started_at or c.created_at)

    done_scored = [
        c for c in calls
        if c.status == "done" and c.overall_score is not None and not c.excluded
    ]
    dialog.calls_count = len(calls)
    if done_scored:
        dialog.avg_score = round(
            sum(c.overall_score for c in done_scored) / len(done_scored), 1
        )
        dialog.last_zone = done_scored[-1].zone
        dialog.trend = _trend([c.overall_score for c in done_scored])
    else:
        dialog.avg_score = None
        dialog.last_zone = None
        dialog.trend = None

    # ответственный — последний менеджер по звонку
    dialog.manager_id = call.manager_id or dialog.manager_id

    for c in calls:
        c.dialog_id = dialog.id

    return dialog


def rebuild_all_dialogs() -> int:
    """Пересобрать диалоги по всем звонкам (backfill). Возвращает число диалогов."""
    client_ids = {c.client_id for c in Call.query.all() if c.client_id}
    for client_id in client_ids:
        any_call = Call.query.filter_by(client_id=client_id).first()
        if any_call:
            recompute_dialog_for_call(any_call)
    db.session.commit()
    return len(client_ids)
