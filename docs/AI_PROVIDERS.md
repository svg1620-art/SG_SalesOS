# AI‑провайдеры: транскрибация и анализ

Оба слоя переключаются в «Настройках» (значение хранится в БД с fallback на env). Ключи задаются в интерфейсе (спец‑значение `__clear__` очищает поле, пустое поле — «не менять»).

---

## 1. Транскрибация (STT)

**Единая точка:** `processing/transcribe.transcribe_call(call) → (list[{speaker,start,end,text}], diarization)`.
`diarization` = `stereo` (стороны по каналам) или `heuristic` (моно, роли определит LLM).

Провайдер — настройка `TRANSCRIBE_PROVIDER` (`openai` по умолчанию | `deepgram`), функция `settings_store.transcribe_provider()`.

### OpenAI (`processing/transcribe.py`)
- Модель `OPENAI_TRANSCRIBE_MODEL` (деф. `gpt-4o-transcribe`), язык `ru`.
- **Стерео**: запись Мегафона — честное стерео (стороны на разных каналах). Каждый канал режется на реплики по паузам (`pydub.detect_nonsilent`, `min_silence=700ms`) и транскрибируется по кускам (модель не отдаёт посегментные тайминги), затем склейка по времени → диалог по очереди.
- **Моно**: один прогон, роли размечает LLM (`heuristic`).
- Ограничение: до `_MAX_CHUNKS_PER_CHANNEL=120` кусков на канал (иначе канал целиком) — защита от лавины запросов.

### Deepgram (`processing/transcribe_deepgram.py`)
- Модель `DEEPGRAM_MODEL` (деф. `nova-2`; поддерживает русский, как и `nova-3`), язык `ru`, прямой REST‑вызов через `httpx` (без SDK).
- **Стерео**: `multichannel=true` — каждый канал отдельно, точные тайминги, **один запрос**. Реплики берутся из `utterances`, `channel` → роль по `manager_channel`.
- **Моно**: `diarize=true` — разметка спикеров; роли уточняет LLM (`heuristic`).
- Ключ — `settings_store.deepgram_api_key()`.

### Как устроен выбор канала «менеджер»
Флаг `Call.manager_channel` (0/1). Если не задан — дефолт 0. Логика единая для обоих провайдеров.

---

## 2. Текстовый анализ (LLM)

**Единая обёртка:** `claude_client.claude_complete(prompt, *, system, max_tokens, require_complete, ...)`.
Провайдер — настройка `LLM_PROVIDER` (`anthropic` по умолчанию | `deepseek`), функция `settings_store.llm_provider()`.

### Claude (Anthropic) — по умолчанию
Модель из `CLAUDE_MODEL` (не хардкодить). Anthropic SDK. Требует `ANTHROPIC_API_KEY` и доступ к API (регион **EU** на Railway).

### DeepSeek
OpenAI‑совместимый API: вызывается через `openai` SDK с `base_url=DEEPSEEK_BASE_URL` (деф. `https://api.deepseek.com`). Модель `DEEPSEEK_MODEL` (деф. `deepseek-chat`; есть `deepseek-reasoner`). Ключ `DEEPSEEK_API_KEY`. Модель Claude в этом пути не применяется.

### Общие механики
- **`require_complete=True`** — ловит обрезку ответа по лимиту токенов (`stop_reason=='max_tokens'` у Claude, `finish_reason=='length'` у DeepSeek) и бросает понятную ошибку вместо возврата неполного JSON. Используется в анализе звонка.
- **Парсинг JSON** — только через `utils.extract_json()` (снятие ```json‑ограждения, балансировка скобок). Не `json.loads`.

### Функции, работающие через `claude_complete`
| Функция | Модуль | Заметки |
|---|---|---|
| Анализ звонка по чек‑листу | `processing/analyze.py` | `max_tokens=8000`, `require_complete=True` |
| Скоринг потенциала лида (НейроGuru) | `processing/lead_score.py` | рубрикатор 0–100, hot/warm/cold |
| Рекомендация следующего шага | `processing/next_step.py` | 3–5 шагов, по запросу |
| AI‑генерация чек‑листа | `checklists/ai.py` | из текстового описания процесса |
| Дневная сводка (компания) | `digest/daily.py` | cron |
| Сводка по менеджеру | `digest/manager.py` | по запросу |

> Один тумблер `LLM_PROVIDER` переключает **все** функции сразу. Если понадобится пофункциональный выбор — развести по отдельным настройкам (см. RUNBOOK / открытые вопросы).

---

## Рекомендации по выбору
- **Claude** сильнее в русскоязычном коучинге и нюансах оценки — оптимален для анализа звонка и скоринга.
- **Deepgram** — точнее и дешевле для стерео Мегафона (один запрос вместо сотен), рекомендуемый STT для боевого потока.
- **DeepSeek** — дешевле для объёмных/частых текстовых задач; проверяйте качество на реальных звонках перед полным переходом.
