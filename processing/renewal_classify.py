"""Классификатор исхода переговоров о продлении (НейроGuru).

По транскрибации звонка определяет статус коммуникации по продлению:
🟢 договорились продлевать, 🟡 думает/сомневается, 🔴 возражение/уходит/не
выходит на связь. Используется для гибридного светофора продлений (ИИ + ручной).
"""
from flask import current_app

from claude_client import claude_complete
from utils import extract_json

_SPEAKER_RU = {"manager": "Менеджер", "client": "Клиент", "unknown": "Говорящий"}
_VALID = {"green", "yellow", "red"}


def _transcript_text(call, limit_chars=6000) -> str:
    lines = []
    for seg in call.transcript_json or []:
        who = _SPEAKER_RU.get(seg.get("speaker"), "Говорящий")
        text = (seg.get("text") or "").strip()
        if text:
            lines.append(f"{who}: {text}")
    return "\n".join(lines)[:limit_chars]


def classify_renewal(app, call) -> dict:
    """{ok, status(green|yellow|red), reason, error} по разговору о продлении."""
    app = app or current_app
    transcript = _transcript_text(call)
    if not transcript:
        return {"ok": False, "error": "Нет транскрибации."}
    prompt = (
        "Ты оцениваешь РИСК НЕ ПРОДЛЕНИЯ клиента по разговору менеджера продлений "
        "с клиентом. Определи статус переговоров о продлении подписки:\n"
        "- \"green\" — клиент согласен/готов продлевать, договорились, позитив;\n"
        "- \"yellow\" — клиент думает, сомневается, просит время/КП/скидку, нужен "
        "следующий шаг, исход неясен;\n"
        "- \"red\" — клиент возражает, недоволен, рассматривает уход/конкурентов, "
        "не выходит на связь, склоняется к отказу.\n\n"
        "Если по разговору непонятно (вводный/не о продлении) — ставь \"yellow\".\n\n"
        f"Транскрибация:\n{transcript}\n\n"
        "Верни СТРОГО JSON: {\"status\": \"green|yellow|red\", \"reason\": \"одна "
        "короткая фраза почему\"}."
    )
    try:
        raw = claude_complete(
            prompt,
            system="Ты лаконичный аналитик продаж. Отвечай на русском, строго JSON.",
            max_tokens=300,
        )
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("[renewal-classify] Claude недоступен: %s", exc)
        return {"ok": False, "error": f"AI недоступен: {exc}"}
    try:
        data = extract_json(raw)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Разбор ответа ИИ не удался: {exc}"}
    status = str(data.get("status") or "").strip().lower()
    if status not in _VALID:
        return {"ok": False, "error": "ИИ вернул недопустимый статус."}
    return {"ok": True, "status": status, "reason": (data.get("reason") or "").strip()[:500]}
