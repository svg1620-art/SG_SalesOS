# SG_SalesOS — Контекст и передача чата (Handoff)

> Назначение файла: полностью восстановить контекст работы над проектом
> **SG_SalesOS**, чтобы продолжить в новом чате/аккаунте без потери нити.
> Прочитай этот файл целиком перед продолжением. Основной «постоянный» контекст
> проекта — в `CLAUDE.md` (читается каждой сессией). Здесь — сжатая карта + что
> именно делалось в последней серии сессий и как всё сейчас устроено.

Дата последнего обновления: 2026-11-15.

---

## 0. Как продолжить этот чат в новом аккаунте

1. Подключи репозиторий **`svg1620-art/SG_SalesOS`** (GitHub) к сессии.
2. Рабочая ветка разработки: **`claude/salesos-stage-1-scaffold-yr5606`**.
   Деплой-ветка (production на Railway): **`main`**.
3. Прочитай `CLAUDE.md` (корень репо) — это каноничный контекст проекта.
4. Прочитай этот файл (`docs/CHAT_HANDOFF.md`) — свежая история и состояние.
5. Прочие важные доки: `docs/WORKER_SETUP.md` (разнесение планировщика на
   Railway), `docs/AI_PROVIDERS.md` (слой выбора LLM/STT), `docs/HANDOFF.md`
   (более ранний общий обзор), `docs/SG_SalesOS_OpsMetrics_spec.md` (ТЗ модуля
   «Операционный пульт», читать перед любой задачей по пульту).

### Git-воркфлоу (строго)
- Коммитить на ветке `claude/salesos-stage-1-scaffold-yr5606`, затем:
  `git checkout main && git merge --ff-only <branch> && git push origin main`,
  потом вернуться на ветку.
- **НЕ создавать PR** без явной просьбы.
- Пуш с ретраями (сервис credentials Railway иногда отдаёт 503 — ретрай).
- Атрибуция в каждом коммите (последние строки сообщения):
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01CBoutjh8mWoGR4nBEM9yYD
  ```
  (В новом аккаунте строку `Claude-Session` возьми из system-reminder текущей
  сессии — она своя у каждой сессии. Модель-идентификатор в коммиты/артефакты
  НЕ писать, кроме строки Co-Authored-By.)
- UI и комментарии — **на русском**.

---

## 1. Что это за продукт (кратко)

**SG_SalesOS** — операционная система управления качеством продаж для
ServiceGuru. Два больших контура:

- **ОКК (отдел контроля качества):** звонок → транскрибация (стерео-диаризация)
  → оценка по чек-листу (балл + зона 🟢/🟡/🔴) → рекомендации + упущенные
  моменты. РОП видит всё, менеджер — только себя. Ежедневная AI-сводка.
- **Операционный пульт** (`docs/SG_SalesOS_OpsMetrics_spec.md`): метрики
  активности/воронки/результата, «Мой день», геймификация (миссии/XP),
  окупаемость, алерты, AI-диагностика, Telegram-уведомления.
- **Модуль «Продления»** (воронка «Аккаунтинг»): контроль продлений подписок.

### Стек
Python 3.11+, Flask + Jinja2 + HTMX (без React). PostgreSQL + SQLAlchemy,
миграции Flask-Migrate (Alembic). APScheduler (фон). Chart.js. Flask-Login
(роли `admin`=РОП, `manager`). Деплой — **Railway, регион EU** (обязательно для
доступа к Anthropic из РФ-инфры), Volume на `/data` (аудио).

### ИИ-слой (ВАЖНО)
Единая обёртка **`claude_client.claude_complete(prompt, *, system, max_tokens,
require_complete)`** роутит на провайдера по настройке **`LLM_PROVIDER`**
(`anthropic` | `deepseek`). **На этом проекте анализ идёт через DeepSeek.**
Модель Claude не хардкодить. Парсинг JSON от модели — только через
`utils.extract_json()` (балансировка скобок, снятие ```json).

---

## 2. Текущая инфраструктура на Railway (ОСНОВНОЕ, настроено в этом чате)

Планировщик разнесён на **два сервиса из одного репозитория** (чтобы тяжёлый
LLM/аналитический фон не тормозил веб-запросы):

| Сервис | `SCHEDULER_ROLE` | Что делает | Volume `/data` |
|---|---|---|---|
| **web** (`sgsalesos-production`) | `web` | миграции + gunicorn; крутит только `amo_poll` (скачивание записей звонков в Volume) + отдаёт плеер | **да** |
| **worker** (назван «Worker») | `worker` | все остальные 14 джоб (метрики, диагностика, продления, Telegram) | **нет** |

- Старт обоих сервисов — через **`start.sh`** (railway.toml: `startCommand =
  "bash start.sh"`). Скрипт ветвится по `SCHEDULER_ROLE`: `worker` → `python -u
  worker.py`; иначе → `flask db upgrade` + gunicorn.
- **`worker.py`** — entrypoint воркера: принудительно ставит
  `SCHEDULER_ENABLED=1` и `SCHEDULER_ROLE=worker` (до импорта app), поднимает
  мини-HTTP на `$PORT` (200 на `/healthz` — чтобы проходил общий healthcheck),
  настраивает логирование (INFO в stdout; httpx/APScheduler приглушены, т.к.
  httpx на INFO светит токен бота в URL), запускает планировщик, блокируется.
- **Ограничение:** Railway Volume монтируется ТОЛЬКО к одному сервису. Поэтому
  `amo_poll` (пишет аудио в `/data`, плеер читает оттуда) обязан жить на web.
  Полный stateless (amo_poll тоже на worker, веб на N воркеров) возможен только
  после переноса аудио в объектное хранилище (S3/R2) — отдельная задача.
- Веб остаётся `--workers 1` (держит `amo_poll`, иначе планировщик задвоится).
- Переменные воркера = как у web (копировать через Raw Editor), но
  `SCHEDULER_ROLE=worker`. Интеграции (Telegram-токен, amo, DeepSeek) worker
  читает из той же БД (`DATABASE_URL` общий). Пошагово — `docs/WORKER_SETUP.md`.
- **Нюанс:** смена часов пульса/сводки в «Настройках» перепланирует джобы только
  в том процессе, где нажали «Сохранить» (web). Воркерные `telegram_pulse`/
  `daily_digest` подхватят новые часы после **Redeploy** сервиса worker.

### Ключевые env-переменные (config.py)
`DATABASE_URL`, `SECRET_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`CLAUDE_MODEL`, `CLAUDE_MODEL_DIGEST`, `OPENAI_TRANSCRIBE_MODEL`, `AUDIO_DIR`
(=/data), `POLL_INTERVAL_MIN`, `DIGEST_HOUR`, `TELEGRAM_HOUR`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`, `TELEGRAM_BOT_USERNAME`, `TZ`,
amoCRM-переменные (`AMO_BASE_DOMAIN`, `AMO_ACCESS_TOKEN`, `AMO_ENTITY`, …),
`SCHEDULER_ENABLED`, `SCHEDULER_ROLE`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`.
Многие интеграционные настройки также хранятся в БД (страница «Настройки») с
fallback на env — см. `settings_store._KEYS`.
**Правило Railway:** переменные добавлять по одной / через Raw Editor, без
кавычек; регион EU; следить за кириллицей в `DATABASE_URL`.

---

## 3. Что сделано в этой серии сессий (хронологически, с файлами)

Ниже — работа последних сессий поверх уже готовых Этапов 1–5 пульта и
Этапов A–D продлений. Коммиты новее — вверху списка §4.

### 3.1 Завершение модуля «Операционный пульт», Этап 6

**Этап 6a — системные алерты + еженедельная AI-диагностика** (коммит `d775f4f`):
- Миграция **`c9d0e1f2a3b4`** (down_revision `b8c9d0e1f2a3`): таблицы
  `ops_alerts`, `manager_diagnosis` + поля `users.telegram_chat_id`,
  `users.telegram_code`, `users.notify_enabled` (задел под 6b).
- **`ops/alerts.py`** — 6 типов алертов (§10.3 ТЗ): `plan_unrealistic`,
  `show_rate_drop`, `activity_drop`, `quality_drop`, `payback_risk`,
  `crm_hygiene`. Считаются по материализованным дневным метрикам + окупаемости.
  Один активный алерт на `(atype, manager_id)`, идемпотентный upsert/резолв
  (`recompute_alerts`). Пороги в `ops_settings` (`alert_*`, `overdue_tasks_limit`
  и т.д.). Чтение для UI: `active_alerts(manager_ids)`.
- **`ops/diagnosis.py`** — недельный AI-разбор каждого менеджера. `build_input`
  собирает JSON (метрики L1–L3 за 4 недели, конверсии vs команда, факторная
  диагностика §8.3, тренд качества, топ-3 упущения, уровни/серии). Вызов через
  `claude_complete(require_complete=True)` (= DeepSeek), строгий JSON
  `{main_cause, evidence[], action_for_manager, question_for_rop, risk_level}`
  через `extract_json`. Failure isolation по менеджеру. `diagnose_all`,
  `latest_diagnosis`, `diagnosis_map`. `week_start_for` = понедельник недели.
- UI: `/ops/board` — блок системных алертов сверху + под каждым менеджером
  раскрываемый «🧠 Разбор недели» (РОП видит всё), кнопки «Пересчитать алерты»
  и «Разбор недели (ИИ)» (routes `ops.alerts_recompute`, `ops.diagnosis_run`).
  `/me/today` — блок **«Фокус недели»** (менеджеру только `action_for_manager`,
  §12). Шаблоны: `templates/ops/board.html`, `templates/dashboard/me_today.html`.
- Cron: алерты `8:50/12:50/16:50`, диагностика `пн 08:00`.

**Этап 6b — Telegram** (коммит `ce790c9`):
- **`notify/bot.py`**: привязка личного чата через `/start <код>` (код из
  профиля `/me/notifications`, `User.telegram_code` одноразовый → `chat_id`),
  `/stop`. `poll_updates` — `getUpdates` со смещением (offset в `ops_settings`
  ключ `tg_update_offset`), без inbound-вебхука, cron каждые 2 мин.
  `send_to_chat`, `notify_manager` (уважает `notify_enabled` + тихие часы =
  рабочие часы ops). `gen_code`, `bind_link`.
- **`notify/checkpoints.py`**: РОПу в общий чат `TELEGRAM_CHAT_IDS` — 11:00
  (кто не начал день + низкий connect rate), 15:00 (светофор команды + красные
  с причиной). Менеджеру лично — 09:30 миссия дня, 16:00 финишная прямая (до
  тира ≤ `tg_finish_within_pct`=25%). Идемпотентно за день через `ops_settings`.
- Итог 19:00 встроен ops-блоком в существующий пульс
  (`notify/telegram.py::_ops_pulse_block` в `build_pulse`: тиры миссий +
  встречи/счета/оплаты за день).
- Настройка `telegram_bot_username` на `/settings` (для ссылки
  `t.me/бот?start=код`). Профиль `/me/notifications` (route
  `dashboard.me_notifications`): статус привязки, код+ссылка, тумблер
  уведомлений, отвязка. Шаблон `templates/dashboard/notifications.html`.
- Токен/username бота — через `settings_store` (БД→env). **Это тот же бот, что
  и для дневного пульса** (одна настройка `telegram_bot_token`).
- Cron: `tg_poll` (2 мин), `tg_mission` 09:30, `tg_cp_morning` 11:00,
  `tg_cp_afternoon` 15:00, `tg_finish` 16:00.

### 3.2 Модуль «Продления» — гибридный светофор коммуникации (коммит `2f9210c`)
- Статус 🟢/🟡/🔴 на сделке = **статус КОММУНИКАЦИИ (переговоров), НЕ срок**.
  Срок до продления + свежесть касания остаётся операционным приоритетом
  рабочего списка (band + SLA), но зона/риск считаются по коммуникации.
- Поля `Renewal.comm_*` (миграция **`b8c9d0e1f2a3`**): `comm_status`
  (green/yellow/red/None), `comm_source` (ai|manual), `comm_note`,
  `comm_updated_at`, `comm_by`, `comm_call_id`. Дефолт `Renewal.zone` → `open`.
- Источник статуса гибридный: **НейроGuru** (`processing/renewal_classify.py::
  classify_renewal` — по транскрибации последнего разобранного ОКК-звонка, cron
  `ops.renewals.ai_refresh_all` 05:30) ИЛИ **ручной override РОПа** на карточке
  клиента (POST `/ops/renewals/<id>/status`). Ручной override не перетирается
  нейронкой (force только по кнопке «Оценить по звонку»).
- `comm_status_of(r)`: терминальные paid/churn приоритетны, иначе `comm_status`
  или `'unknown'`. Доска (risk_sum, bands 🟡/🔴), рабочий список, касания красят
  зону по `comm_status_of`. zp-словари в шаблонах содержат ключ `'unknown'`.

### 3.3 Продления — фильтр месяца + ответственный менеджер (коммит `449330f`)
- `worklist(month=YYYY-MM)` — фильтр рабочего списка по месяцу продления;
  `worklist_months()` — доступные месяцы; селект «Месяц продления» вверху списка
  (route `ops.renewals` параметр `wl_month`).
- `manager_of(r)` — ответственный менеджер: имя по связке `User.amo_user_id` ↔
  `Renewal.manager_id`, иначе `amo#<id> (не привязан)`. Показывается в строке
  списка и в карточке клиента (тайл «Ответственный» + строка «Касались клиента»
  = уникальные менеджеры из событий истории по `manager_id`).
- **Важно:** имя менеджера видно только если у `User` заполнено `amo_user_id`.

### 3.4 Самоконтроль менеджера (разбор звонков) (коммит `ebbfe50`)
- `Call.manager_comment` + `Call.reviewed_at` (миграция `f6a7b8c9d0e1`). Разбор
  обязателен для ВСЕХ оценённых звонков, комментарий обязателен, срок — до конца
  следующего рабочего дня. Логика — `review.py` (state reviewed/pending/overdue/
  na). UI: блок «Мой разбор» в карточке звонка, колонка/фильтр в списке, блок
  «На разбор» на `/me`, «Разбор звонков» на дашборде РОПа. Без XP.

### 3.5 Производительность (коммит `54624dd`)
Основные причины тормозов и фиксы:
- **Кэш настроек в процессе (TTL ~30с):** `ops_store.get_ops_setting` и
  `settings_store.get_setting` читали БД на КАЖДЫЙ ключ (сотни round-trip за
  запрос). Теперь кэшируется вся таблица; запись сбрасывает кэш
  (`invalidate_ops_cache` / `invalidate_settings_cache`).
- **Кэш производственного календаря** (`ops.metrics._work_calendar_map`, TTL
  120с): `_is_working_day` больше не делает `db.get` на каждый день в циклах;
  `board.py` и `payback.py` делегируют сюда.
- **`/me/today`** не делает полный `recompute_day` на каждый заход — только если
  агрегат устарел (>90с).
- **Пул БД** (config, только Postgres): pool_size=10/max_overflow=20/
  pool_recycle=280/pool_timeout=30 (+pool_pre_ping). Для sqlite — только pre_ping.
- **Разнесение планировщика** (см. §2) — главный выигрыш.

### 3.6 Навигация (коммит `8ca5a21`)
- Верхнее меню админа: убраны Чек-листы/Пользователи/Отделы, добавлена
  «📅 Продления» (прямой заход на `/ops/renewals`).
- В «Настройках» новый блок «Управление» со ссылками на Чек-листы, Пользователи,
  Отделы. Шаблоны: `templates/base.html`, `templates/settings/index.html`.

---

## 4. Коммиты этой серии (новые → старые)
```
8ca5a21 Навигация: справочники в Настройки, Продления — в верхнее меню
449330f Продления: фильтр по месяцу в рабочем списке + ответственный менеджер
ac154a8 worker.py: приглушить httpx (токен в URL) и шум APScheduler в логах
ed32033 worker.py: видимые логи старта (stdout, unbuffered)
1f7e36c worker.py: принудительно включает планировщик (SCHEDULER_ENABLED=1)
8fe5e73 Старт по роли: start.sh ветвит web/worker, worker проходит healthcheck
cff1e6f Разнесение планировщика: worker.py + SCHEDULER_ROLE (web/worker/all)
54624dd Производительность: кэш настроек/календаря, пул БД, троттлинг /me/today
ce790c9 Ops Этап 6b: Telegram — привязка, чекпоинты РОПу, личные уведомления
d775f4f Ops Этап 6a: системные алерты + еженедельная AI-диагностика (DeepSeek)
2f9210c Продления: гибридный светофор коммуникации (ИИ + ручной)
6b70bfb Продления: выбор месяца + журнал циклов (ретроспектива)
15e51e0 Продления: защитить парсер даты от переполнения (Errno 75)
bd55647 Продления: диагностика доп-полей сделки + устойчивый парсер даты
f7aaf2f Продления, Этап D: разбор звонков из ОКК в карточке клиента
1b0c065 Продления, Этап C: карточка клиента с историей взаимодействий
ebbfe50 Разбор звонков: обязательный самоконтроль менеджера
1cd0610 Продления: статус по данным (дата+касания), а не по этапам
d7a530a Продления: распознавать «оплатил/отказался» по названию этапа
e6a7b55 Продления, Этап B: рабочий список, план касаний, тепловая карта, касания
```

---

## 5. Модель данных — новое/изменённое + цепочка миграций

**Голова миграций: `c9d0e1f2a3b4`.** Цепочка (последние):
```
… → e5f6a7b8c9d0 (renewals) → f6a7b8c9d0e1 (call_review)
   → a7b8c9d0e1f2 (renewal_cycles) → b8c9d0e1f2a3 (renewal_comm)
   → c9d0e1f2a3b4 (ops_alerts + manager_diagnosis + users.telegram_*)
```

Новые/изменённые сущности (файл `models.py`):
- **`User`**: `+ telegram_chat_id, telegram_code, notify_enabled`, а также
  (ранее) `amo_user_id, daily_call_plan, hire_date, deactivated_at,
  last_seen_at`.
- **`OpsAlert`** (`ops_alerts`): atype, manager_id, severity, title, detail,
  data_json, is_active, created/updated/resolved_at. Индекс
  `ix_ops_alerts_active(is_active, atype, manager_id)`.
- **`ManagerDiagnosis`** (`manager_diagnosis`): manager_id, week_start,
  main_cause, evidence_json, action_for_manager, question_for_rop, risk_level,
  provider, error. Unique `(manager_id, week_start)`.
- **`Renewal`** (`renewals`): amo_lead_id PK, manager_id, amo_responsible_id,
  name, company, amo_contact_id, price, pipeline_id, status_id, status_name,
  `zone`(open|paid|churn), is_open, renewal_end, **comm_**(status/source/note/
  updated_at/by/call_id), amo_updated_at, synced_at.
- **`RenewalCycle`** (`renewal_cycles`): журнал исходов циклов (renewed|churned)
  по `cycle_end` (месяц), unique `(amo_lead_id, cycle_end)` — для ретроспективы
  «кого продляли в <месяц>», когда дата окончания съезжает вперёд.
- Пульт (ранее): `ops_activity_events`, `funnel_events`, `funnel_stage_map`,
  `ops_settings`, `ops_sync_log`, `work_calendar`, `manager_day_stats`,
  `manager_hour_stats`, `manager_plans`, `manager_absences`, `staff_costs`,
  `rop_cost_allocations`, `daily_missions`, `manager_progress`, `xp_ledger`.

---

## 6. Крон-джобы (app.py `_add_schedule_jobs`) и их роли

Регистрация фильтруется по `SCHEDULER_ROLE` (web/worker/all):
- **web:** `amo_poll` (IntervalTrigger `POLL_INTERVAL_MIN`) — опрос amoCRM +
  скачивание записей в Volume + запуск пайплайна обработки.
- **worker:** `telegram_pulse` (TELEGRAM_HOUR:00), `daily_digest`
  (DIGEST_HOUR:00), `ops_sync` (каждые `sync_interval_min`),
  `ops_recompute_yesterday` (00:30), `ops_generate_missions` (06:00),
  `renewals_sync` (ежечасно), `renewals_classify` (05:30, AI переговоров),
  `ops_alerts` (8:50/12:50/16:50), `ops_diagnosis` (пн 08:00), `tg_poll`
  (2 мин), `tg_mission` (09:30), `tg_cp_morning` (11:00), `tg_cp_afternoon`
  (15:00), `tg_finish` (16:00).
- `all` (дефолт, один сервис) = web+worker вместе.

---

## 7. Карта ключевых модулей/функций

- `claude_client.claude_complete(...)` — единый вызов LLM (роутит DeepSeek/Claude
  по `LLM_PROVIDER`). `utils.extract_json`.
- `utils`: `now_local/to_local/app_tz/local_to_utc_naive/normalize_phone`,
  `amo_entity_url`.
- `ops_store`: `get_ops_setting/set_ops_setting/ops_int/ops_float/ops_json`,
  `OPS_DEFAULTS` (все пороги/настройки пульта), кэш + `invalidate_ops_cache`.
- `settings_store`: `effective/get_setting/set_setting`, `llm_provider`,
  `telegram_token/telegram_chat_ids/telegram_bot_username`, `amo_*`, кэш +
  `invalidate_settings_cache`.
- `ops/metrics.py`: `recompute_day/recompute_today/recompute_yesterday`,
  `_is_working_day` (+кэш календаря `_work_calendar_map`), `avg_day_baseline`,
  `recent_working_days`, `_utc_bounds`.
- `ops/board.py`: `board_rows`, `forecast_for`, воронки/конверсии, `team_heatmap`.
- `ops/payback.py`: `payback_for`, `factor_diagnosis`, `cumulative_net`.
- `ops/missions.py`: генерация/оценка миссий, XP, уровни, серии, командный квест.
- `ops/alerts.py`, `ops/diagnosis.py` — см. §3.1.
- `ops/renewals.py`: `board`, `worklist(month=)`, `worklist_months`,
  `client_card`, `comm_status_of`, `manager_of`, `set_comm_manual`,
  `ai_refresh_one/all`, `latest_analyzed_call`, `touch_plan`, `today_touches`,
  `renewal_managers`.
- `ingest/renewals_sync.py`: `sync_renewals` (снимок воронки «Аккаунтинг», зона
  по этапу/ключевым словам, дата из custom-поля, `_record_cycle`),
  `sample_custom_fields` (диагностика полей).
- `ingest/ops_sync.py`, `ingest/amo_source.py`, `ingest/amo_client.py`,
  `processing/worker.py` (пайплайн звонка), `processing/analyze.py`,
  `processing/transcribe*.py`, `processing/renewal_classify.py`.
- `notify/telegram.py` (пульс+ops-блок), `notify/bot.py`, `notify/checkpoints.py`.
- `review.py` (самоконтроль звонков).

### Основные роуты
- Админ: `/` (дашборд), `/calls`, `/dialogs`, `/leaderboard`,
  `/ops/board`, `/ops/renewals` (+ `/ops/renewals/<lead_id>`,
  `/renewals/<id>/status`, `/renewals/<id>/classify`), `/ops/validation`,
  `/ops/payback`, `/ops/costs`, `/ops/calls`, `/settings/` (+ управление:
  `checklists/users/departments`).
- Менеджер: `/me/today`, `/me`, `/me/notifications`, `/calls`.

---

## 8. Открытые вопросы и следующие шаги

- **Связка `User.amo_user_id`** — чтобы в продлениях/пульте показывались имена
  менеджеров (а не `amo#<id> (не привязан)`), у каждого пользователя SalesOS
  должен быть проставлен `amo_user_id`. Возможная задача: экран/подсказка для
  заполнения связки + диагностика «какие ответственные amo не привязаны».
- **Telegram боевой запуск:** создать бота у @BotFather, вписать на `/settings`
  токен, `TELEGRAM_CHAT_IDS` (РОП) и `telegram_bot_username`. Менеджеры
  привязываются через `/me/notifications`. (Используется тот же бот, что и пульс.)
- **Безопасность:** токен бота пару минут светился в логах worker (httpx INFO)
  до фикса `ac154a8` — при желании перевыпустить токен.
- **Полный stateless web** (масштабирование на N воркеров) — после переноса
  аудио из Volume в объектное хранилище (S3/R2).
- Модуль пульта: Этапы 1–6 готовы. Продления A–D + доработки готовы.
  Явного «Этапа 7» в ТЗ пульта нет — дальнейшее по запросу заказчика.
- Старые открытые вопросы Этапа 8 (amoCRM): формат `LINK` в примечании, по какой
  сущности тянуть звонки, какой канал = менеджер (флаг на аккаунт).

---

## 9. Полезные команды

```bash
# окружение
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# миграции (локально на временной БД)
export DATABASE_URL="sqlite:////tmp/test.db" SECRET_KEY=t ANTHROPIC_API_KEY=x \
  OPENAI_API_KEY=x CLAUDE_MODEL=x OPENAI_TRANSCRIBE_MODEL=x
.venv/bin/flask db upgrade            # применить
.venv/bin/flask db downgrade <rev>    # откатить
.venv/bin/flask db current            # текущая голова

# тесты пишем в scratchpad, запуск с PYTHONPATH=. (Flask-Login кэширует
# current_user в g per app_context → для мультиаутентификации использовать
# отдельный app.app_context() на каждый запрос, либо test_client session)

# worker локально (проверка планировщика)
SCHEDULER_ENABLED=1 SCHEDULER_ROLE=worker PORT=8099 .venv/bin/python -u worker.py
```

### Пиннинг зависимостей (не ломать)
`openai>=1.57.4`, `httpx==0.27.2` (связка `openai==1.51.2`+`httpx==0.28.0`
крашится). Anthropic SDK — свежий.

---

## 10. Диаризация и оценка (справка)
- Записи Мегафон ВАТС — честное стерео (стороны на разных каналах). Режем на два
  моно, транскрибируем отдельно, склеиваем по таймингам. Флаг `manager_channel`
  на аккаунт. Фолбэк моно — разметка спикеров LLM (`diarization='heuristic'`).
- Оценка: критерий 0–10, итог = Σ(score/10 × weight), веса в сумме 100. Зоны:
  🟢 ≥80, 🟡 60–79, 🔴 <60. Критичный критерий с 0/10 капает зону в 🟡.
- STT-провайдер: `TRANSCRIBE_PROVIDER` (openai|deepgram). LLM: `LLM_PROVIDER`
  (anthropic|deepseek, здесь **deepseek**). Детали — `docs/AI_PROVIDERS.md`.
