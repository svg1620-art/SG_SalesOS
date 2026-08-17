# Модель данных

Все модели — в `models.py`. Ниже — таблицы, поля и связи. Первоисточник — сам `models.py`.

## Диаграмма связей (упрощённо)

```
Department 1───* User 1───* Call *───1 Checklist 1───* Criterion
                 │            │  \
                 │            │   *── CallCriterionScore *──1 Criterion
                 │            │   *── Recommendation
                 │            │   *── MissedMoment
                 │            │
Client 1───* Dialog 1───* Call
                 │
User 1───* Dialog

Deal *───1 User        (сделки amoCRM; связь со звонком — по amo_contact_id / amo_lead_id, без FK)
DailyDigest            (одна на дату)
ActivityEvent *───1 User (+ опц. Call)
Setting                (key-value)
AmoToken               (OAuth, запасной путь)
```

## Пользователи и организация
### `Department`
`id`, `name` (unique), `created_at`. Связь `users`.

### `User` (`UserMixin`)
`email` (unique), `password_hash`, `full_name`, `role` (`admin`|`manager`), `department_id` (FK, nullable), `amo_user_id` (сопоставление с `responsible_user_id` в amoCRM), `daily_call_plan` (норма звонков/день), `last_seen_at`, `is_active`, `created_at`. Методы `set_password`/`check_password`, свойство `is_admin`.

## Чек‑листы
### `Checklist`
`name`, `description`, `domain`, `department_id` (nullable = общий), `zone_green_min` (80), `zone_yellow_min` (60), `is_active`. Связь `criteria` (каскад, сортировка по `order_index`). **Активный — по одному на отдел (+ один общий).**

### `Criterion`
`checklist_id` (FK, каскад), `title`, `description` (что «хорошо»), `weight` (веса в сумме = 100), `order_index`, `is_critical`.

## Клиенты и диалоги
### `Client`
`phone_normalized` (unique, `+7XXXXXXXXXX`), `name`, `amo_contact_id`, `first_seen_at`, `last_seen_at`.

### `Dialog`
`client_id` (FK), `manager_id` (FK), `calls_count`, `avg_score`, `last_zone` (green|yellow|red), `trend` (up|down|flat), `updated_at`.

## Звонки и оценки
### `Call`
- Привязки: `dialog_id`, `manager_id`, `client_id`, `checklist_id`.
- amoCRM: `amo_note_id` (**unique — дедуп**), `amo_entity_type` (contacts|leads), `amo_entity_id`, `direction` (in|out), `source_link`.
- Файлы/время: `audio_path` (в Volume), `started_at`, `duration_sec`.
- Статус: `status` (`new|downloading|transcribing|analyzing|done|failed`), `error`.
- Результаты: `transcript_json` (`[{speaker,start,end,text}]`), `summary`, `overall_score`, `zone`, `diarization` (stereo|heuristic).
- On‑demand AI: `next_steps_json`/`next_steps_at`, `lead_score`/`lead_score_json`/`lead_score_at`.
- Служебное: `excluded` (исключить из рейтинга/метрик), `manager_channel` (0/1 — какой канал стерео = менеджер), `content_hash` (**unique** — SHA256 для дедупа ручной загрузки), `created_at`, `processed_at`.
- Связи (каскад): `criterion_scores`, `recommendations`, `missed_moments`.

### `CallCriterionScore`
`call_id` (каскад), `criterion_id`, `score`, `max_score`, `evidence` (цитата), `comment`, `is_missed`.

### `Recommendation`
`call_id` (каскад), `skill`, `text`, `priority` (high|med|low).

### `MissedMoment`
`call_id` (каскад), `quote` (точная цитата для инлайн‑подсветки), `label`, `explanation`, `transcript_span_start/end`.

## Сделки, сводки, служебное
### `Deal`
`amo_lead_id` (**unique**), `manager_id` (FK), `amo_contact_id` (для связки со звонками), `price` (руб), `name`, `pipeline_id`, `status_id`, **`outcome`** (`won`|`lost`), **`won_at`** (дата закрытия), `created_at`.
> Связь со звонком — **не** через FK, а логически: по `amo_contact_id` (контакт) или `amo_lead_id` (лид). См. `processing/lead_score.py`.

### `DailyDigest`
`date` (**unique**), `content_json`, `created_at`.

### `ActivityEvent`
`user_id` (FK), `kind` (`login|view_call|next_step|listen`), `call_id` (nullable), `created_at`.

### `Setting`
`key` (unique), `value`, `updated_at`. Key‑value настройки из интерфейса; приоритет над env (`settings_store.py`).

### `AmoToken`
`access_token`, `refresh_token`, `expires_at`, `base_domain` — OAuth‑токены amoCRM (запасной путь; основной — долгосрочный токен из настроек).

## Ключи дедупликации
- Звонок из amoCRM: `amo_note_id` (unique).
- Ручная загрузка: `content_hash` = SHA256(имя + длительность + дата).
- Сделка: `amo_lead_id` (unique).

## Статусы звонка
`new → downloading → transcribing → analyzing → done`, либо `failed` (+ `error`).
