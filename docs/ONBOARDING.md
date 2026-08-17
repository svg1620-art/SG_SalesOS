# Онбординг за 15 минут

## Что это
SG_SalesOS превращает звонок отдела продаж в оценку качества: **транскрибация с ролями → оценка по чек‑листу (балл + зона 🟢/🟡/🔴) → рекомендации и упущенные моменты**. РОП видит всё, менеджер — себя. Плюс дневная AI‑сводка, Telegram‑пульс, лидерборд по выручке из amoCRM.

## Ментальная модель
- **Звонок** (`Call`) — центральная сущность, проходит пайплайн со статусами `new→downloading→transcribing→analyzing→done`/`failed`.
- **Чек‑лист** (`Checklist`+`Criterion`) — по нему оценивается звонок; активный чек‑лист выбирается по отделу менеджера.
- **Диалог** (`Dialog`) — агрегат звонков одного клиента (по телефону).
- **Сделка** (`Deal`) — из amoCRM, выигранная/проигранная; выигранные → лидерборд.
- **Настройки** (`Setting`) — правятся из интерфейса, имеют приоритет над env.

## Поднять локально
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SECRET_KEY=dev
export ANTHROPIC_API_KEY=sk-ant-...     # для AI-анализа
export CLAUDE_MODEL=<модель>
export DATABASE_URL=sqlite:///salesos_dev.db
export ADMIN_EMAIL=you@example.com ADMIN_PASSWORD=secret

flask --app app db upgrade      # схема
flask --app app seed-admin      # админ
flask --app app seed-checklist  # дефолтный чек-лист
flask --app app run             # http://127.0.0.1:5000
```
Войдите под `ADMIN_EMAIL`/`ADMIN_PASSWORD`. Загрузите тестовое аудио через «Звонки → Загрузить» (нужны ключи транскрибации/анализа для полного прогона).

## Куда смотреть в коде
| Хочу понять… | Файл |
|---|---|
| Как собирается приложение | `app.py` (create_app, блюпринты, планировщик, CLI) |
| Конфиг/переменные | `config.py`, `settings_store.py` |
| Схема данных | `models.py` |
| Пайплайн звонка | `processing/worker.py` → `transcribe.py` / `analyze.py` / `scoring.py` |
| Транскрибация | `processing/transcribe.py`, `processing/transcribe_deepgram.py` |
| Вызов LLM | `claude_client.py` |
| amoCRM | `ingest/amo_client.py`, `ingest/amo_source.py`, `ingest/amo_deals.py` |
| Экраны | `templates/`, соответствующие блюпринты (`dashboard/`, `calls/`, …) |

## 5 правил, которые сэкономят время
1. **Никаких хардкод‑секретов/моделей** — только через `config.py`/настройки.
2. **JSON от LLM** парсить только через `utils.extract_json()` (не `json.loads`).
3. **Телефон** нормализовать через `utils.normalize_phone()` (`+7XXXXXXXXXX`).
4. **В списках** — eager‑load связей (`joinedload`), не тянуть большие таблицы целиком (иначе N+1 и зависания — см. RUNBOOK).
5. **Railway** — 1 воркер, регион EU, Volume `/data`, переменные по одной без кавычек.

Дальше: [ARCHITECTURE.md](ARCHITECTURE.md) и [PROJECT_DOCUMENTATION.md](PROJECT_DOCUMENTATION.md).
