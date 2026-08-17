# Конфигурация: переменные окружения и настройки

## Приоритет значений
`settings_store.effective(key)` → **значение из БД (таблица `settings`, правится в «Настройках») → env (config.py) → дефолт**. То есть правки из интерфейса перекрывают переменные окружения без редеплоя. Секреты можно задавать и там, и там; в интерфейсе спец‑значение `__clear__` очищает поле, пустое поле — «не менять».

## Переменные окружения (`config.py`)

### Обязательные
| Переменная | Назначение |
|---|---|
| `DATABASE_URL` | Postgres. Авто‑нормализация: `postgres://`→`postgresql+psycopg://` (psycopg v3), чистка пробелов/BOM/кириллицы по краям. |
| `SECRET_KEY` | ключ сессий Flask |
| `ANTHROPIC_API_KEY` | ключ Claude (нужен, если `LLM_PROVIDER=anthropic`) |
| `CLAUDE_MODEL` | модель Claude (не хардкодить) |

### Транскрибация / LLM
| Переменная | Дефолт | Назначение |
|---|---|---|
| `TRANSCRIBE_PROVIDER` | `openai` | `openai` \| `deepgram` |
| `OPENAI_API_KEY` | — | ключ OpenAI (STT) |
| `OPENAI_TRANSCRIBE_MODEL` | `gpt-4o-transcribe` | модель STT OpenAI |
| `DEEPGRAM_API_KEY` | — | ключ Deepgram |
| `DEEPGRAM_MODEL` | `nova-2` | модель Deepgram |
| `LLM_PROVIDER` | `anthropic` | `anthropic` \| `deepseek` |
| `DEEPSEEK_API_KEY` | — | ключ DeepSeek |
| `DEEPSEEK_MODEL` | `deepseek-chat` | модель DeepSeek |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | endpoint DeepSeek |
| `CLAUDE_MODEL_DIGEST` | — | (опц.) отдельная модель для сводок |

### Инфраструктура и фон
| Переменная | Дефолт | Назначение |
|---|---|---|
| `AUDIO_DIR` | `/data` | Volume для аудио (обязателен на Railway) |
| `TZ` | `Europe/Moscow` | таймзона расчётов |
| `SCHEDULER_ENABLED` | `false` | включить APScheduler |
| `POLL_INTERVAL_MIN` | `15` | период опроса amoCRM (мин) |
| `DIGEST_HOUR` | `20` | час дневной сводки |
| `TELEGRAM_HOUR` | `19` | час Telegram‑пульса |
| `WORKER_CONCURRENCY` | `2` | параллелизм пайплайна |

### Telegram
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS` (chat_id через запятую).

### amoCRM
`AMO_BASE_DOMAIN`, `AMO_ACCESS_TOKEN` (долгосрочный токен), `AMO_ENTITY` (`contacts`|`leads`), `RECORDING_PROXY` (РФ‑прокси для скачивания записей Мегафона). Запасной OAuth‑путь: `AMO_CLIENT_ID`, `AMO_CLIENT_SECRET`, `AMO_REDIRECT_URI`, `AMO_AUTH_CODE`.

### Сид админа
`ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ADMIN_NAME` (деф. «Администратор»), `SEED_ADMIN_ON_START` (создать/обновить админа при старте — удобно на Railway; после первого старта убрать).

## Что где менять
| Хочу изменить… | Где |
|---|---|
| Провайдер/ключ транскрибации | «Настройки → Транскрибация» (или `TRANSCRIBE_PROVIDER`/`DEEPGRAM_API_KEY`) |
| Провайдер/ключ анализа (LLM) | «Настройки → Анализ (LLM)» (или `LLM_PROVIDER`/`DEEPSEEK_API_KEY`) |
| Telegram, часы пульса/сводки | «Настройки → Telegram» |
| Домен/токен amoCRM, окно опроса, прокси | «Настройки → amoCRM» |
| Воронка лидерборда | «Настройки → Сделки и Leaderboard» |
| Пороги зон, критерии, веса | «Чек‑листы» |
| Модели Claude/OpenAI | только env (`CLAUDE_MODEL`, `OPENAI_TRANSCRIBE_MODEL`) |

## Правила Railway (важно)
- Переменные добавлять **по одной** («New Variable»), **без кавычек** (копипаст блоком может склеить значения).
- `DATABASE_URL` — следить за невидимыми символами/кириллицей.
- Регион проекта — **EU** (доступ к Anthropic API). Volume на `/data`. См. [DEPLOYMENT.md](DEPLOYMENT.md).
