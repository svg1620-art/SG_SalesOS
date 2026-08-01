# SG_SalesOS — Документация проекта

> Операционная система управления качеством продаж для **ServiceGuru**:
> ОКК (отдел контроля качества) + коучинг менеджеров + ежедневный AI‑агент‑сводка для РОПа.
>
> Этот документ — самодостаточное описание проекта для новых разработчиков и AI‑ассистентов.
> Первоисточники в репозитории: [`CLAUDE.md`](../CLAUDE.md) (рабочие правила), [`SG_SalesOS_spec.md`](../SG_SalesOS_spec.md) (полное ТЗ), [`README.md`](../README.md).

---

## 1. Что это и как работает (в двух абзацах)

Звонок отдела продаж попадает в систему (автоматически из amoCRM или ручной загрузкой) → **авто‑транскрибация с диаризацией** (разделение «Менеджер / Клиент») → **оценка по чек‑листу** (балл 0–100 + зона 🟢/🟡/🔴) → **рекомендации по навыкам и упущенные моменты**. РОП видит всё на одном дашборде, менеджер — только себя. Раз в день формируется AI‑сводка «на что обратить внимание» и уходит Telegram‑пульс.

Дополнительно система тянет из amoCRM **закрытые сделки** (выигранные/проигранные): по выигранным строится **лидерборд по выручке с геймификацией (XP)**, проигранные — размеченная история для **скоринга потенциала лида** по первичному звонку.

Поток обработки:

```
Источник звонка (amoCRM notes / ручная загрузка)
        │
        ▼
[downloading] скачивание записи в Volume (/data)
        │
        ▼
[transcribing] транскрибация + диаризация (OpenAI ИЛИ Deepgram)
        │
        ▼
[analyzing] анализ по активному чек-листу (Claude ИЛИ DeepSeek)
        │            → баллы по критериям, зона, саммери,
        │              рекомендации, упущенные моменты
        ▼
[done] агрегация в «Диалог» по номеру телефона
```

---

## 2. Технологический стек

| Слой | Технология |
|---|---|
| Backend | Python 3.11+, Flask (app‑factory) + Jinja2 + **HTMX** (без React) |
| БД | PostgreSQL + SQLAlchemy 2.0 (драйвер **psycopg v3**) |
| Миграции | Flask‑Migrate (Alembic) |
| Фон / расписание | APScheduler |
| Графики | Chart.js (donut зон, тренды, радар критериев) |
| Транскрибация | **OpenAI** `gpt-4o-transcribe` (стерео через нарезку каналов) **или Deepgram** `nova-2` (multichannel/diarize) — переключается в настройках |
| AI‑анализ | **Anthropic Claude** (по умолчанию) **или DeepSeek** (OpenAI‑совместимый API) — переключается в настройках |
| Auth | Flask‑Login, роли `admin` / `manager` |
| Уведомления | Telegram Bot API (дневной пульс) |
| Деплой | Railway, регион **EU**, Volume на `/data`, gunicorn |

**Жёсткие пины зависимостей** (см. `requirements.txt` и `CLAUDE.md`):
`openai>=1.57.4`, `httpx[socks]==0.27.2` (связка `openai==1.51.2`+`httpx==0.28.0` крашится), `pydub==0.25.1`. Anthropic SDK — свежий.

---

## 3. Структура проекта

```
app.py                 # фабрика Flask: блюпринты, планировщик, error-handlers, CLI
config.py              # чтение переменных окружения (Config)
extensions.py          # db, migrate, login_manager, scheduler (общие объекты)
models.py              # ВСЕ SQLAlchemy-модели
settings_store.py      # настройки: БД (таблица settings) с fallback на env
claude_client.py       # обёртка LLM: Claude ИЛИ DeepSeek (claude_complete)
utils.py               # extract_json, нормализация телефона, работа с TZ, ссылки amoCRM
activity.py            # трекинг активности (last_seen, просмотры звонков)

auth/                  # логин/логаут, декораторы доступа, сид админа
dashboard/             # дашборд РОПа (DashBoard 📈) + кабинет менеджера
checklists/            # CRUD чек-листов, критерии, AI-генерация, сид, выбор активного
calls/                 # список/карточка звонка, ручная загрузка, экспорт, on-demand AI
dialogs/               # агрегаты звонков по клиенту (телефон), экспорт
users/                 # управление пользователями (админ)
departments/           # отделы (Отдел продаж / развития клиентов), сид
leaderboard/           # рейтинг по выручке за месяц + XP
settings_admin/        # страница «Настройки» (Telegram, amoCRM, транскрибация, LLM, сделки)

ingest/                # источники данных
  amo_client.py        #   низкоуровневый клиент amoCRM API v4 (httpx, Bearer)
  amo_source.py        #   опрос звонков (notes call_in/call_out), скачивание записей
  amo_deals.py         #   опрос/импорт сделок (won/lost), лидерборд, геймификация
  amo_notes.py         #   разбор примечаний-звонков
  manual_upload.py     #   ручная загрузка аудио (дедуп по SHA256)

processing/            # пайплайн обработки
  worker.py            #   пул воркеров (ThreadPool), enqueue_call, статусы
  transcribe.py        #   диспетчер провайдера + реализация OpenAI (стерео-нарезка)
  transcribe_deepgram.py #  реализация Deepgram (multichannel/diarize)
  analyze.py           #   анализ по чек-листу через LLM (строгий JSON)
  scoring.py           #   веса → overall → зона; применение анализа к моделям
  aggregate.py         #   агрегация диалогов по телефону
  metrics.py           #   баланс «говорил/слушал» из транскрибации
  lead_score.py        #   скоринг потенциала лида + связка звонок↔сделка (исход)
  next_step.py         #   рекомендация следующего шага (НейроGuru)

digest/                # AI-сводки: daily.py (по компании), manager.py (по менеджеру)
notify/telegram.py     # дневной Telegram-пульс
templates/             # Jinja2-шаблоны (брендинг ServiceGuru, HTMX-фрагменты)
migrations/            # Alembic (14+ ревизий)
railway.toml           # конфиг сборки/запуска Railway (NIXPACKS, gunicorn, healthcheck)
```

---

## 4. Модель данных

Все модели — в `models.py`. Ключевые сущности:

### Пользователи и организация
- **`Department`** — отдел (`name`). Связь: `users`.
- **`User`** (роли `admin` / `manager`): `email`, `password_hash`, `full_name`, `role`, `department_id`, `amo_user_id` (сопоставление с ответственным в amoCRM), `daily_call_plan` (норма звонков/день), `last_seen_at` (вовлечённость), `is_active`. Свойство `is_admin`.

### Чек-листы
- **`Checklist`**: `name`, `description`, `domain`, `department_id` (nullable = общий), пороги зон `zone_green_min` (80), `zone_yellow_min` (60), `is_active`. Активный — по одному на отдел (+ один общий).
- **`Criterion`**: `checklist_id`, `title`, `description` (что «хорошо»), `weight` (веса в сумме = 100), `order_index`, `is_critical`.

### Клиенты и диалоги
- **`Client`**: `phone_normalized` (уникальный, формат `+7XXXXXXXXXX`), `name`, `amo_contact_id`.
- **`Dialog`** — агрегат звонков по клиенту: `client_id`, `manager_id`, `calls_count`, `avg_score`, `last_zone`, `trend` (up/down/flat), `updated_at`.

### Звонки и оценки
- **`Call`** — центральная сущность:
  - привязки: `dialog_id`, `manager_id`, `client_id`, `checklist_id`;
  - amoCRM: `amo_note_id` (unique, дедуп), `amo_entity_type` (contacts|leads), `amo_entity_id`, `direction` (in|out), `source_link`;
  - файлы/время: `audio_path` (в Volume), `started_at`, `duration_sec`;
  - **статус**: `status` = `new→downloading→transcribing→analyzing→done` / `failed`, `error`;
  - результаты: `transcript_json` (реплики `{speaker,start,end,text}`), `summary`, `overall_score`, `zone`, `diarization` (stereo|heuristic);
  - on-demand AI: `next_steps_json`/`next_steps_at`, `lead_score`/`lead_score_json`/`lead_score_at`;
  - служебное: `excluded` (исключить из рейтинга), `manager_channel` (какой канал стерео = менеджер), `content_hash` (SHA256 для дедупа ручной загрузки).
- **`CallCriterionScore`**: `call_id`, `criterion_id`, `score`, `max_score`, `evidence` (цитата), `comment`, `is_missed`.
- **`Recommendation`**: `call_id`, `skill`, `text`, `priority` (high|med|low).
- **`MissedMoment`**: `call_id`, `quote` (точная цитата для инлайн‑подсветки), `label`, `explanation`.

### Сделки, сводки, служебное
- **`Deal`** — закрытая сделка из amoCRM: `amo_lead_id` (unique), `manager_id`, `amo_contact_id`, `price`, `name`, `pipeline_id`, `status_id`, **`outcome`** (`won`|`lost`), **`won_at`** (дата закрытия). Выигранные → лидерборд; проигранные → история для скоринга.
- **`DailyDigest`**: `date` (unique), `content_json` — дневная сводка РОПа.
- **`ActivityEvent`**: `user_id`, `kind` (login|view_call|next_step|listen), `call_id`, `created_at`.
- **`Setting`**: key‑value настройки из интерфейса (приоритет над env).
- **`AmoToken`**: OAuth‑токены amoCRM (запасной путь; основной — долгосрочный токен из настроек).

---

## 5. Роли и доступ

- **`admin`** (РОП/владелец): видит всё — дашборд по всем менеджерам, все звонки/диалоги, настройки, пользователей, отделы, лидерборд.
- **`manager`**: видит только свои звонки/диалоги и свой кабинет; лидерборд — если состоит в «Отделе продаж».
- Декораторы: `@login_required`, `@admin_required` (`auth/decorators.py`). Загрузчик пользователя — `load_user` в `models.py`.
- На каждом запросе авторизованного пользователя обновляется `last_seen_at` (троттлинг ~5 мин) — `activity.touch_last_seen`.

---

## 6. Пайплайн обработки звонка

Реализация — `processing/worker.py` (`process_call`). Звонки ставятся в **пул потоков** (`ThreadPoolExecutor`, размер = `WORKER_CONCURRENCY`, по умолчанию 2) через `enqueue_call`. Изоляция сбоев: падение одного звонка не роняет прогон — статус `failed`, текст в `Call.error`.

Шаги и статусы:
1. **`downloading`** — если нет `audio_path`, но есть `source_link`: скачивание записи из amoCRM/телефонии в Volume (`ingest/amo_source.download_recording_to_volume`). Для Мегафона может требоваться РФ‑прокси (`RECORDING_PROXY`).
2. **`transcribing`** — `processing/transcribe.transcribe_call(call)` → `(transcript, diarization)`.
3. **`analyzing`** — выбор чек‑листа (`checklists/selection.resolve_checklist_for_call`) → `processing/analyze.analyze_call` → `processing/scoring.apply_analysis` (баллы, зона, рекомендации, упущения).
4. **`done`** — агрегация диалога по телефону (`processing/aggregate.recompute_dialog_for_call`).

Статус на UI обновляется через **HTMX polling**.

**Выбор чек‑листа** (`resolve_checklist_for_call`), приоритет: явный `call.checklist_id` → активный чек‑лист отдела менеджера → общий активный → любой активный. При «Переоценить» с вариантом «по чек‑листу отдела (авто)» поле `checklist_id` сбрасывается в `None`, и воркер переразрешает чек‑лист.

---

## 7. AI‑провайдеры (оба переключаются в «Настройках»)

### 7.1 Транскрибация — OpenAI или Deepgram
Единая точка: `processing/transcribe.transcribe_call(call) → (list[{speaker,start,end,text}], diarization)`. Провайдер выбирается настройкой `TRANSCRIBE_PROVIDER` (`openai` по умолчанию | `deepgram`).

- **OpenAI** (`transcribe.py`): стерео Мегафона режется на 2 моно; каждый канал дробится по паузам (`pydub.detect_nonsilent`) и транскрибируется по кускам (`gpt-4o-transcribe` не отдаёт посегментные тайминги), затем склейка по времени → диалог. Моно → один прогон, роли размечает Claude (`heuristic`).
- **Deepgram** (`transcribe_deepgram.py`): один REST‑запрос (`nova-2`). Стерео → `multichannel=true` (каждый канал = сторона, точные тайминги); моно → `diarize=true`. Прямой вызов через `httpx` (без SDK), ключ/модель из настроек.

`manager_channel` (флаг на звонке) определяет, какой канал = менеджер.

### 7.2 Текстовый анализ (LLM) — Claude или DeepSeek
Единая обёртка: `claude_client.claude_complete(...)`. Провайдер — `LLM_PROVIDER` (`anthropic` по умолчанию | `deepseek`). DeepSeek вызывается через `openai` SDK с `base_url=https://api.deepseek.com`. Параметр `require_complete=True` ловит обрезку ответа по лимиту токенов (`stop_reason`/`finish_reason`) и даёт понятную ошибку.

**Все функции, работающие через `claude_complete`:**
| Функция | Модуль |
|---|---|
| Анализ звонка по чек‑листу | `processing/analyze.py` (`max_tokens=8000`) |
| Скоринг потенциала лида (НейроGuru) | `processing/lead_score.py` |
| Рекомендация следующего шага | `processing/next_step.py` |
| AI‑генерация чек‑листа из описания | `checklists/ai.py` |
| Дневная сводка по компании | `digest/daily.py` |
| Сводка по конкретному менеджеру | `digest/manager.py` |

**Важно:** JSON от модели парсится через `utils.extract_json()` (балансировка скобок, снятие ```json‑ограждения) — не наивный `json.loads`.

---

## 8. Оценка и зоны (правила)

Реализация — `processing/scoring.py`.
- Каждый критерий модель оценивает **0–10**. Итог `overall = Σ(score/max_score × weight)`; **бэкенд считает сам**, не доверяя числу от модели.
- Пороги зон (из активного чек‑листа, редактируются): 🟢 ≥ `zone_green_min` (80), 🟡 ≥ `zone_yellow_min` (60), иначе 🔴.
- **Правило критичных критериев**: если критичный критерий (`is_critical`) получил 0/10 — зона не может быть 🟢 (капается в 🟡), даже если сумма ≥ порога.
- Зона считается **на звонок**; диалог хранит агрегат (`avg_score`, `last_zone`, `trend`).
- Применение анализа **идемпотентно**: при «Переоценить» прежние оценки/рекомендации/упущения удаляются и создаются заново.

---

## 9. Интеграция amoCRM

Клиент — `ingest/amo_client.py` (API v4, долгосрочный Bearer‑токен). Домен/токен задаются в «Настройках».

### 9.1 Звонки
`ingest/amo_source.poll_amo` опрашивает примечания `call_in`/`call_out` (по контактам или сделкам), дедуп по `amo_note_id`, привязка менеджера (`User.amo_user_id` ↔ `responsible_user_id`) и клиента (по нормализованному телефону), скачивание записи в Volume и запуск пайплайна. Периодичность — `POLL_INTERVAL_MIN`.

### 9.2 Сделки (won/lost) — `ingest/amo_deals.py`
- **Выигрыш/проигрыш определяются по системным id статусов amoCRM: `142` = «успешно реализовано» (won), `143` = «закрыто и не реализовано» (lost).** Их id менять нельзя — можно только переименовать метку (в текущем аккаунте `142` = «Оплата получена»). Поле `type` статуса **не** индикатор выигрыша (`type=1` — это «Неразобранное»).
- Импорт идёт из **ВСЕХ воронок** и хранит `pipeline_id`; ограничение «только Воронка X» применяется **на экране лидерборда**, а не при импорте (иначе смена воронки стирала бы данные).
- Выигранные и проигранные тянутся **раздельными запросами** (`order=desc`), т.к. проигранных могут быть десятки тысяч и они «топят» немногочисленные выигранные при едином запросе.
- Кнопки в «Настройках»:
  - **✅ Импортировать выигранные (быстро)** — синхронно, батч‑коммит, отчёт по воронкам (`import_won`).
  - **❌ Импортировать проигранные (в фоне)** — фоновый батч‑импорт (`import_lost`).
  - **🔎 Где лежат сделки (по воронкам)** — диагностика: сколько выигранных в каждой воронке (`status_histogram`).
  - **💰 Опросить сделки / ↺ Пересобрать сделки** — инкрементальный опрос / полный пересбор (`poll_deals` / `resync_deals`, оба безопасны и идут в фоне).
- **Воронка для лидерборда** выбирается в настройках (`leaderboard_pipeline_id`).

### 9.3 Связка звонок ↔ сделка (исход)
`processing/lead_score.py`: `call_outcome(call, by_contact, by_lead)` — сначала по лиду (`amo_entity_type=='leads'`), потом по контакту. `outcome_lookup(contact_ids, lead_ids)` выбирает **только** нужные сделки (по id показываемых звонков) — критично для производительности «Звонков» на больших аккаунтах.

---

## 10. Лидерборд и геймификация

`leaderboard/routes.py` + `ingest/amo_deals.py`.
- Рейтинг менеджеров по **выручке за месяц** (сумма `price` выигранных сделок, `won_at` в пределах месяца, фильтр по воронке лидерборда).
- **XP**: `+50` за каждые `50 000 ₽` выручки за месяц (`XP_STEP_RUB`, `XP_PER_STEP`).
- **Поздравления в Telegram** за свежие выигрыши: `🔥 Имя ✅ ХХХ руб +ХХ Xp 🚀 Поздравляем!` Защита от спама: не поздравляем на первичном бэкфилле, только за закрытия не старше 2 дней, лимит на прогон, только по воронке лидерборда.
- Показываются менеджеры «Отдела продаж» **и** любой менеджер с выигрышами за месяц (даже без формальной привязки к отделу).

---

## 11. Планировщик (APScheduler)

Запускается при `SCHEDULER_ENABLED=true` (см. `app._maybe_start_scheduler`). Рассчитан на **1 gunicorn‑воркер** (иначе задачи задвоятся). Джобы (`_add_schedule_jobs`):
| Джоба | Триггер | Что делает |
|---|---|---|
| `telegram_pulse` | cron `TELEGRAM_HOUR:00` (по умолч. 19:00) | Telegram‑пульс по менеджерам |
| `daily_digest` | cron `DIGEST_HOUR:00` (по умолч. 20:00) | AI‑сводка за день |
| `amo_poll` | каждые `POLL_INTERVAL_MIN` мин | опрос звонков + сделок amoCRM |

Часы редактируются в «Настройках»; после изменения вызывается `reschedule_jobs`.

---

## 12. Настройки: БД + env

`settings_store.py` реализует приоритет: **значение из БД (таблица `settings`, редактируется в «Настройках») → env (config) → дефолт**. Секреты (токены/ключи) тоже можно задавать в интерфейсе; спец‑значение `__clear__` очищает поле, пустое поле — «не менять».

Разделы страницы «Настройки» (`settings_admin/`):
- Telegram (токен, chat_id, часы пульса/сводки);
- **Транскрибация** (провайдер OpenAI/Deepgram, ключ Deepgram, модель);
- **Анализ (LLM)** (провайдер Claude/DeepSeek, ключ DeepSeek, модель);
- amoCRM (домен, токен, сущность, окно опроса, мин. длительность, прокси записей);
- Сделки и Leaderboard (воронка лидерборда, импорт/диагностика сделок).

---

## 13. Переменные окружения

Читаются в `config.py`. Многие дублируются в «Настройках» (БД имеет приоритет).

**Обязательные:**
| Переменная | Назначение |
|---|---|
| `DATABASE_URL` | Postgres (авто‑нормализация `postgres://`→`postgresql+psycopg://`; чистка кириллицы/BOM) |
| `SECRET_KEY` | ключ сессий Flask |
| `ANTHROPIC_API_KEY` | ключ Claude (нужен всегда, если LLM=anthropic) |
| `CLAUDE_MODEL` | модель Claude (не хардкодить) |

**Транскрибация / LLM (по выбору провайдера):**
| Переменная | По умолчанию | Назначение |
|---|---|---|
| `OPENAI_API_KEY` | — | ключ OpenAI (транскрибация) |
| `OPENAI_TRANSCRIBE_MODEL` | `gpt-4o-transcribe` | модель транскрибации OpenAI |
| `TRANSCRIBE_PROVIDER` | `openai` | `openai` \| `deepgram` |
| `DEEPGRAM_API_KEY` / `DEEPGRAM_MODEL` | — / `nova-2` | Deepgram |
| `LLM_PROVIDER` | `anthropic` | `anthropic` \| `deepseek` |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` / `DEEPSEEK_BASE_URL` | — / `deepseek-chat` / `https://api.deepseek.com` | DeepSeek |
| `CLAUDE_MODEL_DIGEST` | — | (опц.) отдельная модель для сводок |

**Инфраструктура/фон:**
| Переменная | По умолчанию | Назначение |
|---|---|---|
| `AUDIO_DIR` | `/data` | Volume для аудио (обязателен на Railway) |
| `TZ` | `Europe/Moscow` | таймзона расчётов |
| `SCHEDULER_ENABLED` | `false` | включить APScheduler |
| `POLL_INTERVAL_MIN` | `15` | период опроса amoCRM |
| `DIGEST_HOUR` / `TELEGRAM_HOUR` | `20` / `19` | часы сводки/пульса |
| `WORKER_CONCURRENCY` | `2` | параллелизм пайплайна |

**Telegram / amoCRM / сид админа:**
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`; `AMO_BASE_DOMAIN`, `AMO_ACCESS_TOKEN`, `AMO_ENTITY`, `RECORDING_PROXY` (+ запасные OAuth `AMO_CLIENT_ID/SECRET/REDIRECT_URI/AUTH_CODE`); `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ADMIN_NAME`, `SEED_ADMIN_ON_START`.

---

## 14. Деплой (Railway)

Конфиг — `railway.toml` (builder **NIXPACKS**).
- **Старт:** `flask --app app db upgrade && gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120` (миграции применяются перед стартом; **1 воркер** — обязателен из‑за APScheduler).
- **Healthcheck:** `/healthz`.
- **Регион — EU** (иначе нет доступа к Anthropic API с РФ‑инфры; не деплоить в US).
- **Volume на `/data`** (`AUDIO_DIR`) — файловая система эфемерна между деплоями; аудио должно переживать рестарты только в Volume.
- **Переменные добавлять по одной** («New Variable»), **без кавычек**; следить за невидимыми символами в `DATABASE_URL`.
- Деплой идёт из ветки **`main`** (рабочая ветка мержится в `main`).

---

## 15. Локальная разработка

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SECRET_KEY=dev ANTHROPIC_API_KEY=... CLAUDE_MODEL=...
export DATABASE_URL=sqlite:///salesos_dev.db   # локально можно sqlite

flask --app app db upgrade          # миграции
flask --app app seed-admin          # админ из ADMIN_EMAIL/ADMIN_PASSWORD
flask --app app run                 # dev-сервер
```

**CLI‑команды** (`flask --app app <cmd>`): `seed-admin`, `seed-checklist`, `seed-departments`, `rebuild-dialogs`, `run-digest`, `send-pulse`, `amo-test`, `amo-poll`, `amo-poll-deals`, `amo-resync-deals`.

**Новая миграция:** `flask --app app db migrate -m "..."` → `db upgrade`. Для SQLite `batch_alter_table` требует именованных constraint’ов.

---

## 16. Операционные заметки и типовые грабли (уроки эксплуатации)

- **Производительность списков.** «Звонки»/«Диалоги» на больших объёмах вешались из‑за **N+1** (ленивая загрузка `client`/`manager` на каждую строку) и полной выборки таблицы сделок. Лечение: `joinedload(client, manager)`, точечная `outcome_lookup(contact_ids, lead_ids)` (только по id показываемых звонков), лимит выборки. **Правило:** в списках — eager‑load связей и не тянуть большие таблицы целиком.
- **Обрезка ответа LLM.** На длинных звонках анализ обрезался по лимиту токенов → `extract_json` возвращал вложенный массив вместо объекта («не JSON‑объект анализа»). Лечение: `max_tokens=8000` + `require_complete=True` (честная ошибка про обрезку).
- **Сделки amoCRM.** Выигрыш/проигрыш — строго по id `142`/`143`, не по `type`. Импорт из всех воронок, фильтр воронки — на экране. Выигранные и проигранные — раздельными запросами. Диагностика «Где лежат сделки» показывает, в какой воронке реально копятся победы.
- **Диаризация.** Записи Мегафон ВАТС — честное стерео (стороны на разных каналах). OpenAI‑путь режет по паузам и склеивает; Deepgram делает это `multichannel` одним запросом. Для моно роли размечает Claude (`heuristic`).
- **Пины зависимостей.** Не трогать связку `openai`/`httpx` без причины (см. §2).
- **Railway.** Только 1 воркер (APScheduler), регион EU, Volume `/data`, переменные по одной без кавычек. Транзиторные сбои сборки NIXPACKS лечатся повторным деплоем (пустой коммит).

---

## 17. Дальнейшее развитие / открытые вопросы

- **Связка звонок↔сделка**: возможны случаи, когда звонки и сделки висят на разных контактах amoCRM → фильтр по исходу в «Звонках» пуст. Кандидат на матчинг по нормализованному телефону, а не только по `amo_contact_id`.
- **Скоринг лида**: рубрикатор пока фиксированный (мало выигранных для обучения); дальше — плейбук, обученный на истории won/lost.
- **Пофункциональный выбор LLM/провайдера** (сейчас один тумблер на все AI‑функции) — при необходимости развести (например, сводки на DeepSeek, анализ на Claude).
- **amoCRM**: маппинг `User.amo_user_id`, формат `LINK` в примечании, выбор сущности для звонков — добиваются по факту.

---

*Документ отражает состояние кода на момент выгрузки. Первоисточник истины — сам код (`models.py`, `config.py`, `app.py`) и `CLAUDE.md`/`SG_SalesOS_spec.md`.*
