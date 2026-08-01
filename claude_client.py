"""Обёртка над LLM для текстового анализа.

Провайдер выбирается настройкой LLM_PROVIDER: 'anthropic' (Claude, по умолчанию)
или 'deepseek' (OpenAI-совместимый API). Модель НЕ хардкодим — берём из env/настроек.
Используется анализом звонка, скорингом лида, «следующим шагом», генерацией
чек-листа и дневной сводкой.
"""
import anthropic
from flask import current_app


def get_claude_client() -> anthropic.Anthropic:
    key = current_app.config.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY не задан в окружении.")
    return anthropic.Anthropic(api_key=key)


def _deepseek_complete(
    prompt: str, *, system: str | None, max_tokens: int,
    temperature: float | None, require_complete: bool,
) -> str:
    """Вызов DeepSeek через OpenAI-совместимый API (openai SDK + base_url).

    Модель Claude тут не применима — используем модель DeepSeek из настроек.
    """
    from openai import OpenAI
    from settings_store import deepseek_api_key, deepseek_model, deepseek_base_url

    key = deepseek_api_key()
    if not key:
        raise RuntimeError("Ключ DeepSeek не задан (Настройки → Анализ (LLM)).")

    client = OpenAI(api_key=key, base_url=deepseek_base_url())
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = {
        "model": deepseek_model(),
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature

    resp = client.chat.completions.create(**kwargs)
    choice = resp.choices[0]
    if require_complete and getattr(choice, "finish_reason", None) == "length":
        raise RuntimeError(
            "Ответ модели обрезан по лимиту токенов — звонок слишком длинный "
            "для одного ответа. Повторите обработку или используйте более "
            "короткий чек-лист."
        )
    return (choice.message.content or "").strip()


def claude_complete(
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int = 4096,
    model: str | None = None,
    temperature: float | None = None,
    require_complete: bool = False,
) -> str:
    """Один вызов Claude, возвращает склеенный текст ответа.

    model=None → берём CLAUDE_MODEL из конфига (для сводки можно передать
    CLAUDE_MODEL_DIGEST явно).

    temperature по умолчанию НЕ передаётся: новые модели (claude-sonnet-5 и др.)
    не принимают этот параметр — используем дефолт модели. Передавать явно только
    если модель точно его поддерживает.

    require_complete=True → если ответ обрезан по лимиту токенов
    (stop_reason == 'max_tokens'), бросаем понятную ошибку вместо возврата
    неполного (обрезанного) JSON. Для строгого JSON-анализа.
    """
    from settings_store import llm_provider
    if llm_provider() == "deepseek":
        return _deepseek_complete(
            prompt, system=system, max_tokens=max_tokens,
            temperature=temperature, require_complete=require_complete,
        )

    resolved_model = model or current_app.config.get("CLAUDE_MODEL")
    if not resolved_model:
        raise RuntimeError("CLAUDE_MODEL не задан в окружении.")

    client = get_claude_client()
    kwargs = {
        "model": resolved_model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    if temperature is not None:
        kwargs["temperature"] = temperature

    message = client.messages.create(**kwargs)
    if require_complete and getattr(message, "stop_reason", None) == "max_tokens":
        raise RuntimeError(
            "Ответ модели обрезан по лимиту токенов — звонок слишком длинный "
            "для одного ответа. Повторите обработку (лимит увеличен) или "
            "используйте более короткий чек-лист."
        )
    return "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )
