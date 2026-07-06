"""Выгрузка результата анализа звонка в ленту amoCRM (примечание).

Пишем в сущность (сделка/контакт), к которой привязан звонок, текстовое
примечание: балл, зона, саммери, рекомендации, упущенные моменты и, по желанию,
транскрибацию с ролями. amoCRM ограничивает длину примечания — режем с пометкой.
"""
from flask import current_app

from settings_store import amo_base_domain, amo_access_token, amo_configured
from ingest.amo_client import AmoClient, AmoError

_SPEAKER_RU = {"manager": "Менеджер", "client": "Клиент", "unknown": "Говорящий"}
_ZONE_RU = {"green": "🟢 Зелёная", "yellow": "🟡 Жёлтая", "red": "🔴 Красная"}
_MAX_LEN = 9000  # безопасный лимит примечания amoCRM


def _transcript_lines(call) -> str:
    lines = []
    for seg in call.transcript_json or []:
        who = _SPEAKER_RU.get(seg.get("speaker"), "Говорящий")
        text = (seg.get("text") or "").strip()
        if text:
            lines.append(f"{who}: {text}")
    return "\n".join(lines)


def build_call_note(call, include_transcript: bool = True) -> str:
    """Собрать текст примечания для ленты amoCRM."""
    parts = ["📞 Оценка звонка — SG SalesOS"]

    if call.overall_score is not None:
        zone = _ZONE_RU.get(call.zone, call.zone or "—")
        parts.append(f"Балл: {call.overall_score}/100 · Зона: {zone}")
    if call.manager:
        parts.append(f"Менеджер: {call.manager.full_name or call.manager.email}")

    if call.summary:
        parts.append(f"\n📝 Саммери:\n{call.summary.strip()}")

    if call.recommendations:
        recs = []
        for r in call.recommendations:
            skill = (r.skill or "").strip()
            text = (r.text or "").strip()
            recs.append(f"• {skill + ': ' if skill else ''}{text}")
        if recs:
            parts.append("\n🎯 Рекомендации:\n" + "\n".join(recs))

    if call.missed_moments:
        missed = []
        for m in call.missed_moments:
            label = (m.label or "").strip()
            quote = (m.quote or "").strip()
            line = f"• {label}" if label else "•"
            if quote:
                line += f" — «{quote}»"
            missed.append(line)
        if missed:
            parts.append("\n⚠️ Упущенные моменты:\n" + "\n".join(missed))

    if include_transcript:
        transcript = _transcript_lines(call)
        if transcript:
            parts.append("\n🗒 Транскрибация:\n" + transcript)

    text = "\n".join(parts)
    if len(text) > _MAX_LEN:
        text = text[:_MAX_LEN].rstrip() + "\n…(текст обрезан, полная версия — на платформе SG SalesOS)"
    return text


def push_call_note(app=None, call=None, include_transcript: bool = True) -> dict:
    """Отправить примечание по звонку в ленту amoCRM.

    Требует: настроенный amoCRM и заполненные amo_entity_type/amo_entity_id
    (привязка звонка к сделке/контакту — есть у звонков из amoCRM).
    """
    app = app or current_app
    if not amo_configured(app):
        return {"ok": False, "error": "amoCRM не настроен."}
    if not (call.amo_entity_type and call.amo_entity_id):
        return {"ok": False, "error": "Звонок не привязан к сущности amoCRM (нет сделки/контакта)."}
    if call.status != "done":
        return {"ok": False, "error": "Звонок ещё не обработан."}

    text = build_call_note(call, include_transcript=include_transcript)
    client = AmoClient(amo_base_domain(app), amo_access_token(app))
    try:
        client.add_note(call.amo_entity_type, call.amo_entity_id, text)
    except AmoError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}
