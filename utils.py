"""Общие утилиты.

`extract_json` — единый экстрактор JSON из ответов Claude. Модель может обернуть
JSON в ```json ... ```, добавить пояснения до/после, поэтому не полагаемся на
наивный json.loads, а вырезаем сбалансированный по скобкам фрагмент.
"""
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def app_tz(app=None):
    """Часовой пояс приложения (TZ из конфига, по умолчанию UTC)."""
    from flask import current_app
    app = app or current_app
    try:
        return ZoneInfo(app.config.get("TZ") or "UTC")
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def now_local(app=None) -> datetime:
    """Текущее время в часовом поясе приложения (aware)."""
    return datetime.now(app_tz(app))


def to_local(dt_utc_naive: datetime, app=None) -> datetime:
    """naive-UTC (как хранится started_at) → aware-локальное время."""
    if dt_utc_naive is None:
        return None
    return dt_utc_naive.replace(tzinfo=timezone.utc).astimezone(app_tz(app))


def local_to_utc_naive(dt_local_aware: datetime) -> datetime:
    """aware-локальное время → naive-UTC (для сравнения со started_at)."""
    return dt_local_aware.astimezone(timezone.utc).replace(tzinfo=None)


def _find_balanced(text: str, open_ch: str, close_ch: str):
    """Вернуть первый сбалансированный фрагмент от open_ch до парного close_ch.

    Учитывает строки и экранирование, чтобы скобки внутри строк не ломали баланс.
    """
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_json(text: str):
    """Извлечь и распарсить JSON-объект/массив из произвольного текста Claude.

    Порядок попыток:
    1) снять markdown-ограждение ```json ... ``` (или просто ```);
    2) прямой json.loads очищенного текста;
    3) вырезать сбалансированный { ... } либо [ ... ] и распарсить его.

    Бросает ValueError, если JSON не найден/не распарсился.
    """
    if text is None:
        raise ValueError("Пустой ответ модели: нечего парсить.")

    cleaned = text.strip()

    # 1) снять ограждение ```json ... ``` / ``` ... ```
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()

    # 2) прямой разбор
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 3) сбалансированный фрагмент — берём тот, что начинается раньше
    obj = _find_balanced(cleaned, "{", "}")
    arr = _find_balanced(cleaned, "[", "]")
    candidates = [c for c in (obj, arr) if c]
    candidates.sort(key=lambda c: cleaned.find(c))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise ValueError("Не удалось извлечь валидный JSON из ответа модели.")


def amo_entity_url(domain, entity_type, entity_id):
    """Прямая ссылка на карточку сущности amoCRM (контакт/сделка) или None."""
    if not (domain and entity_type and entity_id):
        return None
    domain = str(domain).replace("https://", "").replace("http://", "").strip("/")
    # amoCRM: /contacts/detail/{id}, /leads/detail/{id}
    path = "leads" if entity_type == "leads" else "contacts"
    return f"https://{domain}/{path}/detail/{entity_id}"


_PHONE_RE = re.compile(r"\D")


def normalize_phone(raw: str):
    """Нормализовать телефон к виду +7XXXXXXXXXX.

    Убирает всё нецифровое, приводит ведущую 8 → +7, 11 цифр с 7 → +7XXXXXXXXXX.
    Возвращает None, если номер не приводится к российскому формату из 11 цифр.
    Единая точка матчинга Client и агрегации Dialog (используется с Этапа 5).
    """
    if not raw:
        return None
    digits = _PHONE_RE.sub("", raw)
    if not digits:
        return None
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) == 10:  # без кода страны
        digits = "7" + digits
    if len(digits) == 11 and digits[0] == "7":
        return "+" + digits
    return None
