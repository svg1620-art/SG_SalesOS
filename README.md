# SG_SalesOS

Операционная система управления качеством продаж ServiceGuru: ОКК + коучинг
менеджеров + ежедневный агент-сводка для РОПа. Полное ТЗ — в
[`SG_SalesOS_spec.md`](SG_SalesOS_spec.md), рабочие правила — в
[`CLAUDE.md`](CLAUDE.md).

> **Этап 1 (текущий) — Каркас.** Flask-фабрика, config, все модели данных,
> начальная миграция, аутентификация (роли admin/manager), брендовый layout
> ServiceGuru, страница логина, заглушки дашборда/кабинета, CLI-сид админа.
> Транскрибация, анализ, чек-листы, дашборд-данные и amoCRM — следующие этапы.

## Стек

- **Backend:** Python 3.11+, Flask + Jinja2 + HTMX (без React)
- **DB:** PostgreSQL + SQLAlchemy, миграции — Flask-Migrate (Alembic)
- **Auth:** Flask-Login, роли `admin` / `manager`
- **Фон:** APScheduler (задачи появятся на Этапах 8–9)
- **Деплой:** Railway, регион **EU**, Volume на `/data`

## Структура (Этап 1)

```
app.py            # фабрика Flask, регистрация блюпринтов, CLI seed-admin
config.py         # чтение переменных окружения
extensions.py     # db, migrate, login_manager, scheduler
models.py         # все SQLAlchemy-модели (раздел 4 ТЗ)
auth/             # логин/логаут, декораторы ролей
dashboard/        # заглушка дашборда РОПа и кабинета менеджера
templates/        # Jinja2 (base, login, дашборд, ошибки)
static/css/sg.css # бренд ServiceGuru
migrations/       # Alembic, начальная миграция
railway.toml      # сборка + startCommand (миграции при старте, затем gunicorn)
```

Модели данных: `User`, `Checklist`, `Criterion`, `Client`, `Dialog`, `Call`,
`CallCriterionScore`, `Recommendation`, `MissedMoment`, `DailyDigest`,
`AmoToken`.

## Локальный запуск

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # заполнить SECRET_KEY, ADMIN_EMAIL, ADMIN_PASSWORD
                            # DATABASE_URL можно не задавать — по умолчанию SQLite

flask --app app db upgrade  # применить миграции
flask --app app seed-admin  # создать админа из ADMIN_EMAIL/ADMIN_PASSWORD
flask --app app run         # http://127.0.0.1:5000
```

Логин под админом → пустой дашборд РОПа. Логин под менеджером → кабинет
менеджера (обе страницы — заглушки Этапа 1).

## Деплой на Railway

> ⚠️ **Регион проекта — EU** (обязательно для доступа к Anthropic API с
> РФ-инфры). Не деплоить в US.

1. **Создать проект** на Railway в регионе **EU**, подключить этот репозиторий.
2. **Добавить PostgreSQL** (плагин Railway) — он создаст `DATABASE_URL`.
3. **Создать Volume** и примонтировать в `/data` (для аудио, нужно с Этапа 3).
4. **Выставить переменные окружения** — по одной через «New Variable»,
   **без кавычек** (копипаст блоком может склеить значения; в `DATABASE_URL`
   следить за кириллицей — известный баг). См. список ниже.
5. **Деплой.** `railway.toml` при **старте контейнера** (не в билде) прогоняет
   `flask --app app db upgrade`, затем поднимает gunicorn. Healthcheck —
   `/healthz`.
6. **Создать админа** одним из способов:
   - **Авто-сид (проще на Railway):** задать `SEED_ADMIN_ON_START=true` —
     админ создаётся из `ADMIN_EMAIL`/`ADMIN_PASSWORD` при старте приложения.
     После первого успешного старта переменную убрать.
   - **Вручную через консоль:** зависит от того, где лежит venv (у Railpack —
     `/opt/venv`), поэтому вызывать бинарь по полному пути:
     ```bash
     /opt/venv/bin/flask --app app seed-admin
     ```

## Переменные окружения для Этапа 1

Минимально необходимые сейчас:

| Переменная | Назначение |
|---|---|
| `DATABASE_URL` | PostgreSQL (создаётся плагином Railway Postgres) |
| `SECRET_KEY` | подпись Flask-сессий (длинная случайная строка) |
| `ADMIN_EMAIL` | email админа для `seed-admin` |
| `ADMIN_PASSWORD` | пароль админа для `seed-admin` |
| `ADMIN_NAME` | (опц.) имя админа, по умолчанию «Администратор» |
| `SEED_ADMIN_ON_START` | (опц.) `true` → создать админа при старте, затем убрать |
| `TZ` | (опц.) таймзона, напр. `Europe/Moscow` |
| `AUDIO_DIR` | (опц., но задать сразу) путь Volume, `/data` |

Задаются заранее, но используются с последующих этапов (можно не заполнять для
проверки Этапа 1): `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CLAUDE_MODEL`,
`CLAUDE_MODEL_DIGEST`, `OPENAI_TRANSCRIBE_MODEL`, `SCHEDULER_ENABLED`,
`POLL_INTERVAL_MIN`, `DIGEST_HOUR`, `AMO_BASE_DOMAIN`, `AMO_CLIENT_ID`,
`AMO_CLIENT_SECRET`, `AMO_REDIRECT_URI`, `AMO_AUTH_CODE`.

Полный справочник — раздел 8 ТЗ и [`.env.example`](.env.example).

## Проверка Этапа 1 (Definition of Done)

- [x] Приложение поднимается, миграции применяются (`db upgrade`).
- [x] `seed-admin` создаёт админа из env.
- [x] Логин по email+паролю, неверные данные отклоняются.
- [x] Неавторизованный редиректится на `/login`.
- [x] Админ видит дашборд; менеджер редиректится в свой кабинет.
- [x] Брендовый layout ServiceGuru (тёмная тема, Manrope/JetBrains Mono).
