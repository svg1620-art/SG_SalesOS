# Развёртывание и разработка

## Railway (прод)

Конфиг — `railway.toml` (builder **NIXPACKS**).

- **Команда старта:**
  ```
  flask --app app db upgrade && gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
  ```
  Миграции применяются перед стартом. **`--workers 1` обязателен** — иначе APScheduler задвоит задачи.
- **Healthcheck:** `/healthz` (таймаут 120с), restart on failure (до 3 попыток).
- **Регион — EU.** Иначе нет доступа к Anthropic API с РФ‑инфры. Не деплоить в US.
- **Volume на `/data`** (`AUDIO_DIR`). Файловая система эфемерна между деплоями — аудио и всё, что должно пережить рестарт, только в Volume. Никаких файловых сессий/кэшей на локальном диске.
- **Переменные:** добавлять по одной, без кавычек (см. [CONFIGURATION.md](CONFIGURATION.md)).
- **PostgreSQL** — отдельный сервис Railway; `DATABASE_URL` подставляется автоматически (следить за кириллицей/BOM).
- **Деплой** идёт из ветки **`main`**. Рабочая ветка мержится в `main`, Railway пересобирает автоматически.
- **Диск:** место в контейнере ограничено; при «no space left» чистить артефакты/кэши/старые записи в Volume.
- **Транзиторные сбои сборки NIXPACKS** (шаг «Build image») лечатся повторным деплоем (пустой коммит в `main`) — это не ошибка кода, если `requirements.txt`/конфиг не менялись.

## Локальная разработка

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SECRET_KEY=dev
export ANTHROPIC_API_KEY=... CLAUDE_MODEL=<модель>
export DATABASE_URL=sqlite:///salesos_dev.db     # или локальный Postgres
export ADMIN_EMAIL=you@example.com ADMIN_PASSWORD=secret

flask --app app db upgrade
flask --app app seed-admin
flask --app app run          # http://127.0.0.1:5000
```
Для полного прогона звонка нужны ключи транскрибации (OpenAI/Deepgram) и анализа (Claude/DeepSeek). Для транскрибации требуется **ffmpeg** в системе (для `pydub`).

## Миграции (Flask‑Migrate / Alembic)
```bash
flask --app app db migrate -m "описание"   # сгенерировать
flask --app app db upgrade                 # применить
flask --app app db downgrade               # откатить на шаг
```
Для SQLite `batch_alter_table` требует **именованных** constraint’ов. На проде миграции применяются автоматически при старте.

## CLI‑команды (`flask --app app <cmd>`)
| Команда | Действие |
|---|---|
| `seed-admin` | создать/обновить админа из `ADMIN_EMAIL`/`ADMIN_PASSWORD` |
| `seed-checklist` | создать дефолтный чек‑лист (Приложение A) и активировать |
| `seed-departments` | создать отделы по умолчанию |
| `rebuild-dialogs` | пересобрать агрегаты диалогов по всем звонкам |
| `run-digest` | сформировать дневную AI‑сводку за сегодня |
| `send-pulse` | отправить Telegram‑пульс (принудительно) |
| `amo-test` | проверить подключение к amoCRM |
| `amo-poll` | опросить amoCRM и завести новые звонки |
| `amo-poll-deals` | опросить успешные сделки (выручка/XP) |
| `amo-resync-deals` | пересобрать сделки заново |

## Зависимости — жёсткие пины (`requirements.txt`)
`openai>=1.57.4`, `httpx[socks]==0.27.2` (связка `openai==1.51.2`+`httpx==0.28.0` крашится), `pydub==0.25.1`. Anthropic SDK — свежий. Не менять без причины.

## Гит‑процесс
- Разработка — на рабочей ветке; финальный мерж — в `main` (Railway деплоит из `main`).
- `git push -u origin <branch>`; при сетевых ошибках — ретраи с backoff.
- Не пушить в чужие ветки без явного разрешения.
