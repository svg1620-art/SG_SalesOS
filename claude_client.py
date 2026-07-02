"""Тонкая обёртка над Anthropic SDK.

Модель НЕ хардкодим — берём из env (`CLAUDE_MODEL`). Используется AI-генерацией
чек-листа (Этап 2), анализом звонка (Этап 3) и дневной сводкой (Этап 9).
"""
import anthropic
from flask import current_app


def get_claude_client() -> anthropic.Anthropic:
    key = current_app.config.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY не задан в окружении.")
    return anthropic.Anthropic(api_key=key)


def claude_complete(
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int = 4096,
    model: str | None = None,
    temperature: float = 0.2,
) -> str:
    """Один вызов Claude, возвращает склеенный текст ответа.

    model=None → берём CLAUDE_MODEL из конфига (для сводки можно передать
    CLAUDE_MODEL_DIGEST явно).
    """
    resolved_model = model or current_app.config.get("CLAUDE_MODEL")
    if not resolved_model:
        raise RuntimeError("CLAUDE_MODEL не задан в окружении.")

    client = get_claude_client()
    kwargs = {
        "model": resolved_model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    message = client.messages.create(**kwargs)
    return "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )
