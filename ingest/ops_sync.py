"""Синхронизация событий amoCRM для модуля Ops (Этап 1).

Тянем сырые действия менеджеров в ops_activity_events и строим funnel_events из
смен этапов по маппингу. Инкрементально (курсор + перекрытие) и бэкфиллом за N
дней. Идемпотентно (дедуп по amo_event_id / UNIQUE(lead_id,step)). Failure
isolation: падение одного источника/пакета не роняет синхронизацию — ошибка в
ops_sync_log.

Источники:
- звонки: примечания call_in/call_out (длительность/статус) — iter_call_notes;
- сообщения: события outgoing/incoming_chat_message — iter_events;
- смены этапов: события lead_status_changed → funnel_events.
"""
from datetime import datetime, timedelta

from flask import current_app

from extensions import db
from models import OpsActivityEvent, FunnelEvent, FunnelStageMap, OpsSyncLog, User
from settings_store import amo_base_domain, amo_access_token, amo_configured
from ingest.amo_client import AmoClient, AmoError
from ops_store import connect_min_sec, sync_overlap_min, backfill_days

# канонический порядок шагов воронки (lost — вне линейной цепочки)
STEP_ORDER = {
    "qualified": 1, "meeting_set": 2, "meeting_held": 3,
    "invoice": 4, "won": 5, "lost": 6,
}
LINEAR_STEPS = ["qualified", "meeting_set", "meeting_held", "invoice", "won"]

_CHAT_TYPES = {"outgoing_chat_message": "msg_out", "incoming_chat_message": "msg_in"}
_STATUS_EVENT = "lead_status_changed"
_BACKFILL_CHUNK_DAYS = 7


def _client() -> AmoClient:
    return AmoClient(amo_base_domain(), amo_access_token())


def _manager_map() -> dict:
    """amo_user_id -> users.id (только с проставленным amo_user_id)."""
    return {
        u.amo_user_id: u.id
        for u in User.query.filter(User.amo_user_id.isnot(None)).all()
    }


def _stage_map() -> dict:
    """(pipeline_id, status_id) -> step (кроме none)."""
    out = {}
    for m in FunnelStageMap.query.all():
        if m.step and m.step != "none":
            out[(int(m.pipeline_id), int(m.status_id))] = m.step
    return out


def _load_existing_event_ids(from_dt: datetime) -> set:
    rows = db.session.query(OpsActivityEvent.amo_event_id).filter(
        OpsActivityEvent.occurred_at >= from_dt
    ).all()
    return {r[0] for r in rows}


def _load_existing_funnel() -> set:
    rows = db.session.query(FunnelEvent.lead_id, FunnelEvent.step).all()
    return {(r[0], r[1]) for r in rows}


def _sync_calls(client, from_dt, to_dt, mgr_map, existing_ids, counters) -> None:
    """Звонки из примечаний call_in/call_out (контакты + сделки)."""
    threshold = connect_min_sec()
    from_ts = int(from_dt.timestamp())
    for entity in ("contacts", "leads"):
        try:
            for note in client.iter_call_notes(entity, since_ts=from_ts, max_pages=100):
                occurred_ts = int(note.get("created_at") or 0)
                if not occurred_ts:
                    continue
                occurred = datetime.utcfromtimestamp(occurred_ts)
                if occurred < from_dt or occurred > to_dt:
                    continue
                note_id = note.get("id")
                if note_id is None:
                    continue
                amo_event_id = f"note:{note_id}"
                if amo_event_id in existing_ids:
                    continue
                note_type = note.get("note_type")
                ev_type = "call_in" if note_type == "call_in" else "call_out"
                params = note.get("params") or {}
                try:
                    duration = int(params.get("duration") or 0)
                except (TypeError, ValueError):
                    duration = 0
                try:
                    call_status = int(params.get("call_status")) if params.get("call_status") is not None else None
                except (TypeError, ValueError):
                    call_status = None
                created_by = note.get("created_by") or 0
                ent_id = note.get("entity_id")
                contact_id = ent_id if entity == "contacts" else None
                lead_id = ent_id if entity == "leads" else None
                db.session.add(OpsActivityEvent(
                    amo_event_id=amo_event_id,
                    manager_id=mgr_map.get(created_by),
                    amo_user_id=created_by or None,
                    type=ev_type,
                    contact_id=contact_id,
                    lead_id=lead_id,
                    occurred_at=occurred,
                    duration_sec=duration or None,
                    call_status=call_status,
                    is_connected=duration >= threshold,
                    raw=note,
                ))
                existing_ids.add(amo_event_id)
                counters["calls"] = counters.get("calls", 0) + 1
                if counters["calls"] % 500 == 0:
                    db.session.commit()
            db.session.commit()
        except AmoError as exc:
            db.session.rollback()
            counters.setdefault("errors", []).append(f"calls/{entity}: {exc}")


def _status_from_event(ev) -> tuple:
    """(pipeline_id, status_id) из value_after события смены этапа, иначе (None,None)."""
    for item in (ev.get("value_after") or []):
        if isinstance(item, dict):
            ls = item.get("lead_status") or {}
            if ls:
                return ls.get("pipeline_id"), ls.get("id")
    return None, None


def _record_funnel(lead_id, pipeline_id, step, occurred, manager_id,
                   existing_funnel, counters) -> None:
    """Зафиксировать достижение шага (+ пропущенные линейные шаги для перескока)."""
    if step == "lost":
        steps = ["lost"]
    else:
        reached = STEP_ORDER.get(step, 0)
        steps = [s for s in LINEAR_STEPS if STEP_ORDER[s] <= reached]
    for s in steps:
        key = (lead_id, s)
        if key in existing_funnel:
            continue
        db.session.add(FunnelEvent(
            lead_id=lead_id, step=s, manager_id=manager_id,
            occurred_at=occurred, pipeline_id=pipeline_id,
        ))
        existing_funnel.add(key)
        counters["funnel"] = counters.get("funnel", 0) + 1


def _sync_events(client, from_dt, to_dt, mgr_map, stage_map,
                 existing_ids, existing_funnel, counters) -> None:
    """Сообщения и смены этапов из /events."""
    from_ts = int(from_dt.timestamp())
    to_ts = int(to_dt.timestamp())
    types = list(_CHAT_TYPES.keys()) + [_STATUS_EVENT]
    try:
        for ev in client.iter_events(types, since_ts=from_ts, until_ts=to_ts, max_pages=200):
            occurred_ts = int(ev.get("created_at") or 0)
            if not occurred_ts:
                continue
            occurred = datetime.utcfromtimestamp(occurred_ts)
            ev_id = ev.get("id")
            ev_type = ev.get("type")
            created_by = ev.get("created_by") or 0
            entity_id = ev.get("entity_id")
            entity_type = ev.get("entity_type")

            if ev_type in _CHAT_TYPES:
                amo_event_id = f"event:{ev_id}"
                if amo_event_id in existing_ids:
                    continue
                contact_id = entity_id if entity_type == "contact" else None
                lead_id = entity_id if entity_type == "lead" else None
                db.session.add(OpsActivityEvent(
                    amo_event_id=amo_event_id,
                    manager_id=mgr_map.get(created_by),
                    amo_user_id=created_by or None,
                    type=_CHAT_TYPES[ev_type],
                    contact_id=contact_id,
                    lead_id=lead_id,
                    occurred_at=occurred,
                    is_connected=False,
                    raw=ev,
                ))
                existing_ids.add(amo_event_id)
                counters["messages"] = counters.get("messages", 0) + 1

            elif ev_type == _STATUS_EVENT:
                pipeline_id, status_id = _status_from_event(ev)
                lead_id = entity_id if entity_type == "lead" else entity_id
                if pipeline_id is None or status_id is None or lead_id is None:
                    continue
                step = stage_map.get((int(pipeline_id), int(status_id)))
                counters["status_changes"] = counters.get("status_changes", 0) + 1
                if not step:
                    continue
                _record_funnel(
                    lead_id, pipeline_id, step, occurred,
                    mgr_map.get(created_by), existing_funnel, counters,
                )

            if (counters.get("messages", 0) + counters.get("funnel", 0)) % 500 == 0:
                db.session.commit()
        db.session.commit()
    except AmoError as exc:
        db.session.rollback()
        counters.setdefault("errors", []).append(f"events: {exc}")


def _run(app, kind: str, from_dt: datetime, to_dt: datetime,
         calls_whole_window=False) -> dict:
    """Общий прогон синхронизации за окно [from_dt, to_dt]. Пишет ops_sync_log."""
    log = OpsSyncLog(
        kind=kind, status="running", started_at=datetime.utcnow(),
        cursor_from=from_dt, cursor_to=to_dt, counts={},
    )
    db.session.add(log)
    db.session.commit()

    counters = {}
    try:
        mgr_map = _manager_map()
        stage_map = _stage_map()
        existing_ids = _load_existing_event_ids(from_dt)
        existing_funnel = _load_existing_funnel()

        _sync_calls(client := _client(), from_dt, to_dt, mgr_map, existing_ids, counters)

        if kind == "backfill":
            # события — пакетами по дням (прогресс/изоляция)
            chunk_from = from_dt
            while chunk_from < to_dt:
                chunk_to = min(chunk_from + timedelta(days=_BACKFILL_CHUNK_DAYS), to_dt)
                _sync_events(client, chunk_from, chunk_to, mgr_map, stage_map,
                             existing_ids, existing_funnel, counters)
                log.counts = dict(counters)
                db.session.commit()
                chunk_from = chunk_to
        else:
            _sync_events(client, from_dt, to_dt, mgr_map, stage_map,
                         existing_ids, existing_funnel, counters)

        log.status = "error" if counters.get("errors") else "ok"
        log.message = "; ".join(counters.get("errors", []))[:2000] or None
        log.counts = dict(counters)
        log.finished_at = datetime.utcnow()
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        log = db.session.get(OpsSyncLog, log.id)
        if log is not None:
            log.status = "error"
            log.message = str(exc)[:2000]
            log.counts = dict(counters)
            log.finished_at = datetime.utcnow()
            db.session.commit()
        app.logger.warning("[ops_sync] %s упал: %s", kind, exc)
    return dict(counters, log_id=log.id if log else None)


def _last_cursor():
    log = (
        OpsSyncLog.query.filter(OpsSyncLog.cursor_to.isnot(None))
        .order_by(OpsSyncLog.cursor_to.desc()).first()
    )
    return log.cursor_to if log else None


def sync_incremental(app=None) -> dict:
    """Инкрементальная синхронизация: от последнего курсора (минус перекрытие)."""
    app = app or current_app
    if not amo_configured(app):
        return {"ok": False, "error": "amoCRM не настроен"}
    now = datetime.utcnow()
    cursor = _last_cursor() or (now - timedelta(minutes=app_sync_interval(app)))
    from_dt = cursor - timedelta(minutes=sync_overlap_min())
    return _run(app, "incremental", from_dt, now)


def backfill(app=None, days: int | None = None) -> dict:
    """Бэкфилл за N дней (по умолчанию из настроек, 90)."""
    app = app or current_app
    if not amo_configured(app):
        return {"ok": False, "error": "amoCRM не настроен"}
    days = days or backfill_days()
    now = datetime.utcnow()
    from_dt = now - timedelta(days=days)
    return _run(app, "backfill", from_dt, now)


def app_sync_interval(app) -> int:
    from ops_store import sync_interval_min
    return sync_interval_min()
