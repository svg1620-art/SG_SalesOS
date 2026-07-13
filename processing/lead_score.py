"""Скоринг потенциала лида по первичной коммуникации (НейроGuru).

По транскрипту первого звонка оцениваем вероятность сделки по фиксированному
рубрикатору (интент, срочность, ЛПР, бюджет, соответствие продукту, следующий
шаг) + «красные флаги» слитого лида. На выходе — балл 0-100, уровень
(hot/warm/cold), драйверы (что за/против) и рекомендация менеджеру.

Пока рубрикатор фиксированный (мало выигранных для обучения). Позже — плейбук,
обученный на истории won/lost (Фаза 1).
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
    return "\n".join(lines)[:limit_chars]


def deal_outcome_for_call(call):
    """Фактический исход сделки клиента ('won'|'lost'|None) — для валидации.

    Схема: звонок → контакт (client.amo_contact_id) → сделка (Deal.amo_contact_id).
    """
    from models import Deal
    contact_id = None
    if call.client and call.client.amo_contact_id:
        contact_id = call.client.amo_contact_id
    elif call.amo_entity_type == "contacts" and call.amo_entity_id:
        contact_id = call.amo_entity_id
    if not contact_id:
        return None
    deal = (
        Deal.query.filter(
            Deal.amo_contact_id == contact_id,
            Deal.outcome.in_(["won", "lost"]),
        )
        .order_by(Deal.won_at.desc())
        .first()
    )
    return deal.outcome if deal else None


def _level_for(potential: int) -> str:
    if potential >= 70:
        return "hot"
    if potential >= 40:
        return "warm"
    return "cold"


def score_lead(app, call) -> dict:
    """Оценить потенциал лида по звонку. Возвращает {ok, potential, level,
    drivers:[{signal,status,note}], summary, action}."""
    app = app or current_app
    transcript = _transcript_text(call)
    if not transcript:
        return {"ok": False, "error": "Нет транскрибации для оценки."}

    prompt = (
        "Ты — НейроGuru, оцениваешь ПОТЕНЦИАЛ сделки по первичному разговору "
        "менеджера с клиентом. Оцени вероятность, что сделка закроется успешно, "
        "по шкале 0-100. Опирайся на сигналы:\n"
        "1. Интерес/вовлечённость клиента (задаёт вопросы, реагирует, сам инициативен)\n"
        "2. Срочность/потребность (есть задача и сроки, а не «просто узнать»)\n"
        "3. ЛПР (говорим с тем, кто принимает решение)\n"
        "4. Бюджет/платёжеспособность (обсуждалось, адекватно продукту)\n"
        "5. Соответствие продукту (наш продукт реально решает задачу)\n"
        "6. Договорённость о следующем шаге (согласован конкретный шаг)\n\n"
        "Учитывай КРАСНЫЕ ФЛАГИ слитого лида: клиент отстранён/уклончив, «просто "
        "смотрю», нет бюджета/сроков, не ЛПР, обещания «сам перезвоню», монолог "
        "менеджера без реакции клиента.\n\n"
        f"Транскрибация первого контакта:\n{transcript}\n\n"
        "Верни СТРОГО JSON: {\"potential\": 0-100, "
        "\"drivers\": [{\"signal\": \"название сигнала\", \"status\": \"plus|minus\", "
        "\"note\": \"1 фраза\"}], \"summary\": \"1-2 фразы вывод\", "
        "\"action\": \"что менеджеру сделать, чтобы поднять шанс\"}"
    )
    try:
        raw = claude_complete(
            prompt,
            system="Ты трезвый оценщик лидов в продажах. Отвечай на русском, строго JSON.",
            max_tokens=1200,
        )
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("[lead-score] Claude недоступен: %s", exc)
        return {"ok": False, "error": f"AI недоступен: {exc}"}

    data = extract_json(raw)
    try:
        potential = int(round(float(data.get("potential"))))
    except (TypeError, ValueError):
        return {"ok": False, "error": "Не удалось получить балл, попробуйте ещё раз."}
    potential = max(0, min(100, potential))

    drivers = []
    for d in (data.get("drivers") or []):
        if isinstance(d, dict) and (d.get("signal") or "").strip():
            status = "plus" if str(d.get("status")).lower().startswith("plus") else "minus"
            drivers.append({
                "signal": d["signal"].strip(),
                "status": status,
                "note": (d.get("note") or "").strip(),
            })
    return {
        "ok": True,
        "potential": potential,
        "level": _level_for(potential),
        "drivers": drivers[:8],
        "summary": (data.get("summary") or "").strip(),
        "action": (data.get("action") or "").strip(),
    }
