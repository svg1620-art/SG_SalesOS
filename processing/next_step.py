"""Рекомендация следующего шага от НейроGuru.

По запросу анализирует диалог с клиентом (транскрибация + саммери + упущения)
и предлагает менеджеру несколько конкретных следующих шагов: что сделать
дальше с этим клиентом. Генерится по кнопке, в БД не хранится.
"""
from flask import current_app

from claude_client import claude_complete
from utils import extract_json

_SPEAKER_RU = {"manager": "Менеджер", "client": "Клиент", "unknown": "Говорящий"}


def _transcript_text(call, limit_chars=6000) -> str:
    lines = []
    for seg in call.transcript_json or []:
        who = _SPEAKER_RU.get(seg.get("speaker"), "Говорящий")
        text = (seg.get("text") or "").strip()
        if text:
            lines.append(f"{who}: {text}")
    joined = "\n".join(lines)
    return joined[:limit_chars]


def generate_next_steps(app, call) -> dict:
    """Вернуть {ok, steps:[{action, why}], error}. Шаги — что делать дальше."""
    app = app or current_app
    transcript = _transcript_text(call)
    if not transcript:
        return {"ok": False, "error": "Нет транскрибации для анализа."}

    missed = [
        f"- {(m.label or '').strip()}" for m in call.missed_moments if m.label
    ]
    summary = (call.summary or "").strip()

    prompt = (
        "Ты — НейроGuru, наставник по продажам. Проанализируй диалог менеджера с "
        "клиентом ниже и предложи менеджеру КОНКРЕТНЫЕ следующие шаги: что сделать "
        "дальше с этим клиентом, чтобы продвинуть сделку. Шаги — практичные и "
        "выполнимые (что написать/спросить/предложить, когда связаться и т.п.).\n\n"
    )
    if summary:
        prompt += f"Саммери звонка:\n{summary}\n\n"
    if missed:
        prompt += "Упущенные моменты:\n" + "\n".join(missed) + "\n\n"
    prompt += (
        f"Транскрибация:\n{transcript}\n\n"
        "Верни СТРОГО JSON вида: {\"steps\": [{\"action\": \"что сделать (коротко, "
        "повелительно)\", \"why\": \"зачем это, 1 фраза\"}]}. "
        "Дай 3-5 шагов в порядке приоритета."
    )

    try:
        raw = claude_complete(
            prompt,
            system="Ты лаконичный наставник по продажам. Отвечай на русском, строго JSON.",
            max_tokens=1200,
        )
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("[next-step] Claude недоступен: %s", exc)
        return {"ok": False, "error": f"AI недоступен: {exc}"}

    data = extract_json(raw)
    steps = []
    for item in (data.get("steps") or []):
        if isinstance(item, dict) and (item.get("action") or "").strip():
            steps.append({
                "action": item["action"].strip(),
                "why": (item.get("why") or "").strip(),
            })
        elif isinstance(item, str) and item.strip():
            steps.append({"action": item.strip(), "why": ""})
    if not steps:
        return {"ok": False, "error": "Не удалось сформировать шаги, попробуйте ещё раз."}
    return {"ok": True, "steps": steps[:5]}
