"""AI-генерация чек-листа из описания процесса продаж (Claude).

Возвращает черновик критериев, который админ правит и сохраняет вручную —
модель ничего не пишет в БД сама.
"""
from claude_client import claude_complete
from utils import extract_json

_SYSTEM = (
    "Ты — методолог отдела контроля качества продаж. Ты составляешь чек-листы "
    "для оценки телефонных звонков менеджеров. Отвечай строго на русском и строго "
    "в формате JSON без пояснений вне JSON."
)

_PROMPT_TEMPLATE = """\
Составь чек-лист оценки звонка отдела продаж по описанию процесса ниже.

Сфера: {domain}
Описание процесса/требований:
\"\"\"
{description}
\"\"\"

Требования к чек-листу:
- 6–10 критериев, домен-специфичные, но по сути оценивающие качество продажи.
- Для каждого критерия: краткий заголовок (title), описание того, что считается
  «хорошо» (description), вес (weight, целое) и признак критичности
  (is_critical: true для ядра продажи — выявление потребности, презентация
  ценности, работа с возражениями, договорённость о следующем шаге и т.п.).
- Веса — целые числа, В СУММЕ РОВНО 100.
- Выделяй как критичные (is_critical=true) 3–4 самых важных критерия.

Верни СТРОГО JSON такого вида (без текста вокруг):
{{
  "name": "короткое название чек-листа",
  "description": "1–2 предложения о назначении",
  "criteria": [
    {{"title": "...", "description": "...", "weight": 20, "is_critical": true}}
  ]
}}
"""


def generate_checklist_draft(description: str, domain: str = "") -> dict:
    """Сгенерировать черновик чек-листа. Возвращает нормализованный dict.

    Бросает ValueError при пустом описании или неразборчивом ответе модели.
    """
    description = (description or "").strip()
    if not description:
        raise ValueError("Пустое описание процесса.")

    prompt = _PROMPT_TEMPLATE.format(
        domain=(domain or "не указана").strip(), description=description
    )
    raw = claude_complete(prompt, system=_SYSTEM, max_tokens=2000, temperature=0.4)
    data = extract_json(raw)

    if not isinstance(data, dict) or not isinstance(data.get("criteria"), list):
        raise ValueError("Модель вернула некорректную структуру чек-листа.")

    criteria = []
    for item in data["criteria"]:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        try:
            weight = int(item.get("weight") or 0)
        except (TypeError, ValueError):
            weight = 0
        criteria.append(
            {
                "title": title[:255],
                "description": (item.get("description") or "").strip(),
                "weight": max(0, weight),
                "is_critical": bool(item.get("is_critical")),
            }
        )

    if not criteria:
        raise ValueError("Модель не вернула ни одного критерия.")

    return {
        "name": (data.get("name") or "").strip()[:255] or "Сгенерированный чек-лист",
        "description": (data.get("description") or "").strip(),
        "criteria": criteria,
    }
