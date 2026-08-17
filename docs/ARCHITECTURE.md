# Архитектура

## Обзор компонентов

```
                 ┌─────────────────────────── Flask (app-factory) ───────────────────────────┐
                 │  Блюпринты: auth, dashboard, checklists, calls, dialogs, users,            │
                 │             departments, settings_admin, leaderboard                       │
                 │  Расширения: db (SQLAlchemy), migrate (Alembic), login_manager, scheduler  │
                 └───────────────┬───────────────────────────────────┬────────────────────────┘
                                 │                                   │
                         Web-запросы (HTMX)                   APScheduler (SCHEDULER_ENABLED)
                                 │                                   │  telegram_pulse / daily_digest / amo_poll
                                 ▼                                   ▼
                 ┌───────────────────────────┐          ┌──────────────────────────────┐
                 │ processing/ (пайплайн)    │◀── enqueue│ ingest/ (источники)          │
                 │ worker → transcribe →     │          │ amo_source (звонки),          │
                 │ analyze → scoring →       │          │ amo_deals (сделки),           │
                 │ aggregate                 │          │ manual_upload (ручная загрузка)│
                 └──────┬─────────┬──────────┘          └──────────────┬───────────────┘
                        │         │                                    │
                        ▼         ▼                                    ▼
                 транскрибация  анализ (LLM)                     amoCRM API v4
                 OpenAI/Deepgram Claude/DeepSeek                 (ingest/amo_client)
                        │         │
                        ▼         ▼
                 ┌─────────────────────────┐
                 │ PostgreSQL (SQLAlchemy) │  + Volume /data (аудио), Telegram (пульс)
                 └─────────────────────────┘
```

## Сборка приложения (`app.py`)
`create_app()` последовательно: инициализирует расширения (`db`, `migrate`, `login_manager`), регистрирует блюпринты, хук `before_request` (обновление `last_seen`), error‑handlers (403/404/500 + `/healthz`), CLI‑команды, авто‑сид админа (`SEED_ADMIN_ON_START`), сид отделов, запуск планировщика (`SCHEDULER_ENABLED`). Экземпляр `app = create_app()` поднимается gunicorn’ом (`app:app`).

## Поток обработки звонка (`processing/worker.py`)
Звонки исполняются в **пуле потоков** `ThreadPoolExecutor` (размер = `WORKER_CONCURRENCY`, деф. 2). `enqueue_call(call_id)` кладёт задачу; `process_call` выполняет её в своём `app_context`:

```
downloading → transcribing → analyzing → done        (ошибка → failed, текст в Call.error)
    │              │              │            │
скачать в       transcribe_call  resolve_checklist   recompute_dialog_for_call
Volume          (провайдер)      + analyze_call      (агрегат по телефону)
                                 + apply_analysis
```
**Изоляция сбоев:** падение одного звонка не роняет прогон; статус `failed`, ошибка в `Call.error`.

## Модель потоков и БД
- gunicorn `--workers 1 --threads 4` (см. `railway.toml`) — **один процесс** (иначе APScheduler задвоит джобы).
- Пайплайн и фоновые импорты сделок (`amo_deals.import_lost`, `resync_deals`) запускаются в **демон‑потоках** со своим `app_context`, чтобы не блокировать веб.
- Каждый поток использует свою сессию SQLAlchemy; долгие импорты коммитят батчами.

## Слои и зоны ответственности
| Слой | Каталог | Ответственность |
|---|---|---|
| Представление | `templates/`, блюпринты | HTTP‑роуты, рендер, HTMX‑фрагменты |
| Доменные модели | `models.py` | схема, связи |
| Обработка | `processing/` | транскрибация, анализ, скоринг, агрегация, метрики |
| Источники | `ingest/` | amoCRM (звонки/сделки), ручная загрузка |
| AI‑обёртки | `claude_client.py`, `processing/transcribe*.py` | вызовы LLM/STT, переключение провайдеров |
| Конфиг/настройки | `config.py`, `settings_store.py` | env + БД‑настройки |
| Уведомления/сводки | `notify/`, `digest/` | Telegram, AI‑сводки |

## Ключевые архитектурные решения
- **Провайдеры AI абстрагированы** за единым интерфейсом (`transcribe_call`, `claude_complete`) — переключаются настройкой, без правки бизнес‑логики.
- **Настройки в БД с fallback на env** (`settings_store.effective`) — правки из интерфейса без редеплоя.
- **Ingestion абстрагирован**: интерфейс источника → amoCRM/ручная загрузка; пайплайн обработки один.
- **Идемпотентность**: повторный анализ звонка удаляет прежние оценки и создаёт заново; импорт сделок — upsert по `amo_lead_id`.
