"""SQLAlchemy-модели SG_SalesOS (раздел 4 ТЗ).

Все сущности продукта заводятся уже на Этапе 1, чтобы начальная миграция
содержала полную схему. Бизнес-логика (пайплайн, скоринг, агрегация) будет
навешиваться на эти модели на следующих этапах.
"""
from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db, login_manager


# --- Пользователи и роли -------------------------------------------------

class Department(db.Model):
    """Отдел (напр. «Отдел продаж», «Отдел развития клиентов»)."""

    __tablename__ = "departments"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    users = db.relationship("User", back_populates="department")

    def __repr__(self) -> str:
        return f"<Department {self.name}>"


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(255))
    role = db.Column(db.String(20), nullable=False, default="manager")  # admin|manager
    department_id = db.Column(
        db.Integer, db.ForeignKey("departments.id"), nullable=True, index=True
    )
    # для сопоставления с ответственным в amoCRM (Этап 8)
    amo_user_id = db.Column(db.BigInteger, nullable=True, index=True)
    # план по звонкам в день (норма); None/0 — план не задан
    daily_call_plan = db.Column(db.Integer, nullable=True)
    # дата найма (для адаптации/окупаемости); Ops-модуль
    hire_date = db.Column(db.Date, nullable=True)
    # момент деактивации «Учётка активна» (для окупаемости/распределения РОПа)
    deactivated_at = db.Column(db.DateTime, nullable=True)
    # последняя активность (для контроля использования платформы)
    last_seen_at = db.Column(db.DateTime, nullable=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    department = db.relationship("Department", back_populates="users")
    # звонки, где этот пользователь — менеджер
    calls = db.relationship("Call", back_populates="manager", lazy="dynamic")
    dialogs = db.relationship("Dialog", back_populates="manager", lazy="dynamic")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role})>"


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))


# --- Чек-листы -----------------------------------------------------------

class Checklist(db.Model):
    __tablename__ = "checklists"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    domain = db.Column(db.String(255))  # свободный текст, напр. "HoReCa"
    # отдел, к которому привязан чек-лист (None — общий/для всех отделов).
    # активный чек-лист — по одному на отдел (+ один общий).
    department_id = db.Column(
        db.Integer, db.ForeignKey("departments.id"), nullable=True, index=True
    )
    # пороги зон (см. раздел 12 ТЗ)
    zone_green_min = db.Column(db.Integer, nullable=False, default=80)
    zone_yellow_min = db.Column(db.Integer, nullable=False, default=60)
    is_active = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    department = db.relationship("Department")
    criteria = db.relationship(
        "Criterion",
        back_populates="checklist",
        cascade="all, delete-orphan",
        order_by="Criterion.order_index",
    )

    def __repr__(self) -> str:
        return f"<Checklist {self.name}{' *' if self.is_active else ''}>"


class Criterion(db.Model):
    __tablename__ = "criteria"

    id = db.Column(db.Integer, primary_key=True)
    checklist_id = db.Column(
        db.Integer, db.ForeignKey("checklists.id", ondelete="CASCADE"), nullable=False
    )
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)  # что считается "хорошо"
    weight = db.Column(db.Integer, nullable=False, default=0)  # вклад в итог
    order_index = db.Column(db.Integer, nullable=False, default=0)
    is_critical = db.Column(db.Boolean, nullable=False, default=False)

    checklist = db.relationship("Checklist", back_populates="criteria")

    def __repr__(self) -> str:
        return f"<Criterion {self.title} w={self.weight}>"


# --- Клиенты и диалоги ---------------------------------------------------

class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    phone_normalized = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255))  # из amoCRM
    amo_contact_id = db.Column(db.BigInteger, nullable=True, index=True)
    first_seen_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    dialogs = db.relationship("Dialog", back_populates="client")

    def __repr__(self) -> str:
        return f"<Client {self.phone_normalized}>"


class Dialog(db.Model):
    """Агрегат звонков по клиенту (нормализованному номеру)."""

    __tablename__ = "dialogs"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    calls_count = db.Column(db.Integer, nullable=False, default=0)
    avg_score = db.Column(db.Float)
    last_zone = db.Column(db.String(10))  # green|yellow|red
    trend = db.Column(db.String(10))  # up|down|flat
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    client = db.relationship("Client", back_populates="dialogs")
    manager = db.relationship("User", back_populates="dialogs")
    calls = db.relationship("Call", back_populates="dialog")

    def __repr__(self) -> str:
        return f"<Dialog client={self.client_id} calls={self.calls_count}>"


# --- Звонки и оценки -----------------------------------------------------

class Call(db.Model):
    __tablename__ = "calls"

    id = db.Column(db.Integer, primary_key=True)
    dialog_id = db.Column(db.Integer, db.ForeignKey("dialogs.id"), nullable=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=True)
    checklist_id = db.Column(db.Integer, db.ForeignKey("checklists.id"), nullable=True)

    amo_note_id = db.Column(db.BigInteger, unique=True, nullable=True)
    # сущность amoCRM, к которой привязано примечание-звонок (для ссылки в CRM)
    amo_entity_type = db.Column(db.String(20))  # contacts|leads
    amo_entity_id = db.Column(db.BigInteger)
    direction = db.Column(db.String(3))  # in|out
    started_at = db.Column(db.DateTime)
    duration_sec = db.Column(db.Integer)
    source_link = db.Column(db.Text)  # ссылка amoCRM/телефонии
    audio_path = db.Column(db.Text)  # путь в Volume

    status = db.Column(db.String(20), nullable=False, default="new")
    # new|downloading|transcribing|analyzing|done|failed
    error = db.Column(db.Text, nullable=True)

    transcript_json = db.Column(db.JSON)  # реплики со спикером и таймингами
    summary = db.Column(db.Text)
    # рекомендация следующего шага от НейроGuru (по запросу): [{action, why}]
    next_steps_json = db.Column(db.JSON)
    next_steps_at = db.Column(db.DateTime)
    # скоринг потенциала лида (по запросу): 0-100 + разбор
    lead_score = db.Column(db.Integer, index=True)
    lead_score_json = db.Column(db.JSON)  # {potential, level, drivers, summary, action}
    lead_score_at = db.Column(db.DateTime)
    overall_score = db.Column(db.Integer)
    zone = db.Column(db.String(10))  # green|yellow|red
    diarization = db.Column(db.String(10))  # stereo|heuristic
    # нецелевой звонок: исключён из рейтинга/метрик, но остаётся в списке
    excluded = db.Column(db.Boolean, default=False)
    # какой канал стерео = менеджер (0=левый, 1=правый); None → дефолт по направлению
    manager_channel = db.Column(db.Integer, nullable=True)
    # для дедупликации ручной загрузки: SHA256(имя+длительность+дата)
    content_hash = db.Column(db.String(64), unique=True, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    processed_at = db.Column(db.DateTime, nullable=True)

    dialog = db.relationship("Dialog", back_populates="calls")
    manager = db.relationship("User", back_populates="calls")
    client = db.relationship("Client")
    checklist = db.relationship("Checklist")
    criterion_scores = db.relationship(
        "CallCriterionScore", back_populates="call", cascade="all, delete-orphan"
    )
    recommendations = db.relationship(
        "Recommendation", back_populates="call", cascade="all, delete-orphan"
    )
    missed_moments = db.relationship(
        "MissedMoment", back_populates="call", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Call {self.id} status={self.status} zone={self.zone}>"


class CallCriterionScore(db.Model):
    """Разбалловка звонка по критериям чек-листа."""

    __tablename__ = "call_criterion_scores"

    id = db.Column(db.Integer, primary_key=True)
    call_id = db.Column(
        db.Integer, db.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    criterion_id = db.Column(db.Integer, db.ForeignKey("criteria.id"), nullable=True)
    score = db.Column(db.Integer)
    max_score = db.Column(db.Integer)
    evidence = db.Column(db.Text)  # цитата из транскрибации
    comment = db.Column(db.Text)
    is_missed = db.Column(db.Boolean, nullable=False, default=False)

    call = db.relationship("Call", back_populates="criterion_scores")
    criterion = db.relationship("Criterion")


class Recommendation(db.Model):
    """Коучинг по навыкам."""

    __tablename__ = "recommendations"

    id = db.Column(db.Integer, primary_key=True)
    call_id = db.Column(
        db.Integer, db.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    skill = db.Column(db.String(255))  # тег навыка
    text = db.Column(db.Text)
    priority = db.Column(db.String(10))  # high|med|low

    call = db.relationship("Call", back_populates="recommendations")


class MissedMoment(db.Model):
    """Упущенные моменты для инлайн-подсветки в транскрибации."""

    __tablename__ = "missed_moments"

    id = db.Column(db.Integer, primary_key=True)
    call_id = db.Column(
        db.Integer, db.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    transcript_span_start = db.Column(db.Integer)
    transcript_span_end = db.Column(db.Integer)
    label = db.Column(db.String(255))
    explanation = db.Column(db.Text)
    # точная цитата из транскрибации для инлайн-подсветки
    quote = db.Column(db.Text)

    call = db.relationship("Call", back_populates="missed_moments")


# --- Сводка и токены -----------------------------------------------------

class DailyDigest(db.Model):
    """Дневная сводка РОПа (одна на дату)."""

    __tablename__ = "daily_digests"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False)
    content_json = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class Deal(db.Model):
    """Закрытая сделка из amoCRM (выигранная/проигранная).

    Выигранные (outcome='won') — для рейтинга по выручке / геймификации.
    Проигранные (outcome='lost') — размеченная история для скоринга лидов.
    """

    __tablename__ = "deals"

    id = db.Column(db.Integer, primary_key=True)
    amo_lead_id = db.Column(db.BigInteger, unique=True, nullable=False, index=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    # основной контакт сделки (для связки со звонками клиента)
    amo_contact_id = db.Column(db.BigInteger, nullable=True, index=True)
    price = db.Column(db.Integer, default=0)  # сумма сделки, руб
    name = db.Column(db.String(500))
    pipeline_id = db.Column(db.BigInteger)
    status_id = db.Column(db.Integer)
    outcome = db.Column(db.String(10), index=True)  # won | lost
    won_at = db.Column(db.DateTime, index=True)  # дата закрытия (успех/провал)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Ops Этап 4: первая выручка клиента (единая база окупаемости/плана)
    amo_company_id = db.Column(db.BigInteger, nullable=True, index=True)
    client_key = db.Column(db.String(64), nullable=True, index=True)  # company:<id>|contact:<id>
    is_first_revenue = db.Column(db.Boolean, nullable=False, default=False, index=True)

    manager = db.relationship("User")


class ActivityEvent(db.Model):
    """Событие использования платформы (для контроля вовлечённости).

    kind: login | view_call | next_step | listen. call_id — если событие
    привязано к звонку (напр. просмотр карточки).
    """

    __tablename__ = "activity_events"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False)
    call_id = db.Column(db.Integer, db.ForeignKey("calls.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


class Setting(db.Model):
    """Настройки приложения (key-value), редактируемые из интерфейса.

    Имеют приоритет над переменными окружения (fallback — env).
    """

    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False, index=True)
    value = db.Column(db.Text)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class AmoToken(db.Model):
    """OAuth-токены amoCRM (единственная строка)."""

    __tablename__ = "amo_tokens"

    id = db.Column(db.Integer, primary_key=True)
    access_token = db.Column(db.Text)
    refresh_token = db.Column(db.Text)
    expires_at = db.Column(db.DateTime)
    base_domain = db.Column(db.String(255))


# =========================================================================
# Модуль «Операционный пульт» (Ops Metrics) — Этап 1: сбор данных.
# ВНИМАНИЕ: таблица сырых действий называется ops_activity_events (а НЕ
# activity_events из §5 ТЗ), потому что имя activity_events уже занято
# существующей ActivityEvent (трекинг использования платформы). Так мы не
# ломаем существующий функционал.
# =========================================================================

class OpsActivityEvent(db.Model):
    """Сырое действие менеджера из amoCRM (звонок/сообщение/задача/примечание)."""

    __tablename__ = "ops_activity_events"

    id = db.Column(db.Integer, primary_key=True)
    # дедупликация; namespace: 'note:<id>' | 'event:<id>'
    amo_event_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    amo_user_id = db.Column(db.BigInteger, nullable=True, index=True)
    # call_out | call_in | msg_out | msg_in | task_done | note
    type = db.Column(db.String(20), nullable=False, index=True)
    contact_id = db.Column(db.BigInteger, nullable=True, index=True)
    lead_id = db.Column(db.BigInteger, nullable=True, index=True)
    occurred_at = db.Column(db.DateTime, nullable=False, index=True)
    duration_sec = db.Column(db.Integer, nullable=True)
    call_status = db.Column(db.Integer, nullable=True)
    is_connected = db.Column(db.Boolean, nullable=False, default=False)
    call_id = db.Column(db.Integer, db.ForeignKey("calls.id"), nullable=True)
    raw = db.Column(db.JSON)

    def __repr__(self) -> str:
        return f"<OpsActivityEvent {self.type} {self.amo_event_id}>"


class FunnelStageMap(db.Model):
    """Маппинг этапа воронки amoCRM → шаг воронки модуля."""

    __tablename__ = "funnel_stage_map"

    id = db.Column(db.Integer, primary_key=True)
    pipeline_id = db.Column(db.BigInteger, nullable=False)
    status_id = db.Column(db.Integer, nullable=False)
    # none | qualified | meeting_set | meeting_held | invoice | won | lost
    step = db.Column(db.String(20), nullable=False, default="none")
    step_order = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        db.UniqueConstraint("pipeline_id", "status_id", name="uq_funnel_stage_map_pl_st"),
    )

    def __repr__(self) -> str:
        return f"<FunnelStageMap {self.pipeline_id}/{self.status_id}={self.step}>"


class FunnelEvent(db.Model):
    """Первое достижение шага воронки лидом (когортная база L2/L3)."""

    __tablename__ = "funnel_events"

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.BigInteger, nullable=False, index=True)
    step = db.Column(db.String(20), nullable=False, index=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    occurred_at = db.Column(db.DateTime, nullable=False, index=True)
    amount = db.Column(db.Numeric, nullable=True)
    pipeline_id = db.Column(db.BigInteger, nullable=True)
    client_key = db.Column(db.String(64), nullable=True, index=True)
    is_first_revenue = db.Column(db.Boolean, nullable=False, default=False)

    __table_args__ = (
        db.UniqueConstraint("lead_id", "step", name="uq_funnel_events_lead_step"),
    )

    def __repr__(self) -> str:
        return f"<FunnelEvent lead={self.lead_id} {self.step}>"


class OpsSetting(db.Model):
    """Настройки модуля Ops (key/value). Отдельно от общих Setting."""

    __tablename__ = "ops_settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False, index=True)
    value = db.Column(db.Text)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class OpsSyncLog(db.Model):
    """Журнал синхронизаций событий amoCRM."""

    __tablename__ = "ops_sync_log"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(20), nullable=False)       # incremental | backfill
    status = db.Column(db.String(20), nullable=False)     # running | ok | error
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    cursor_from = db.Column(db.DateTime, nullable=True)
    cursor_to = db.Column(db.DateTime, nullable=True)
    counts = db.Column(db.JSON)                            # {calls, messages, status_changes, ...}
    message = db.Column(db.Text, nullable=True)


class WorkCalendar(db.Model):
    """Производственный календарь: рабочий день или нет."""

    __tablename__ = "work_calendar"

    date = db.Column(db.Date, primary_key=True)
    is_working_day = db.Column(db.Boolean, nullable=False, default=True)


# --- Ops Metrics Этап 2: материализованные агрегаты, планы, отсутствия --------

class ManagerDayStat(db.Model):
    """Дневные агрегаты метрик по менеджеру (материализованные, пересчёт upsert)."""

    __tablename__ = "manager_day_stats"

    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    date = db.Column(db.Date, primary_key=True)
    # L1 — ритм
    calls_out = db.Column(db.Integer, nullable=False, default=0)
    calls_connected = db.Column(db.Integer, nullable=False, default=0)
    talk_time_sec = db.Column(db.Integer, nullable=False, default=0)
    messages_out = db.Column(db.Integer, nullable=False, default=0)
    touches = db.Column(db.Integer, nullable=False, default=0)
    new_leads_contacted = db.Column(db.Integer, nullable=False, default=0)
    speed_to_lead_median_min = db.Column(db.Float, nullable=True)
    first_action_at = db.Column(db.DateTime, nullable=True)
    last_action_at = db.Column(db.DateTime, nullable=True)
    idle_gaps_count = db.Column(db.Integer, nullable=False, default=0)
    tasks_overdue = db.Column(db.Integer, nullable=False, default=0)
    # L2 — воронка
    qualified = db.Column(db.Integer, nullable=False, default=0)
    meetings_set = db.Column(db.Integer, nullable=False, default=0)
    meetings_held = db.Column(db.Integer, nullable=False, default=0)
    # L3 — результат
    invoices = db.Column(db.Integer, nullable=False, default=0)
    invoices_sum = db.Column(db.Numeric, nullable=False, default=0)
    payments = db.Column(db.Integer, nullable=False, default=0)
    payments_sum = db.Column(db.Numeric, nullable=False, default=0)
    # качество (из существующего ОКК)
    avg_call_score = db.Column(db.Float, nullable=True)
    is_working_day = db.Column(db.Boolean, nullable=False, default=True)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ManagerHourStat(db.Model):
    """Почасовые агрегаты (для тепловой карты)."""

    __tablename__ = "manager_hour_stats"

    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    date = db.Column(db.Date, primary_key=True)
    hour = db.Column(db.Integer, primary_key=True)
    calls_out = db.Column(db.Integer, nullable=False, default=0)
    calls_connected = db.Column(db.Integer, nullable=False, default=0)
    messages_out = db.Column(db.Integer, nullable=False, default=0)
    talk_time_sec = db.Column(db.Integer, nullable=False, default=0)


class ManagerPlan(db.Model):
    """План по менеджеру на месяц (1-е число месяца)."""

    __tablename__ = "manager_plans"

    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    month = db.Column(db.Date, primary_key=True)  # 1-е число месяца
    revenue_plan = db.Column(db.Numeric, nullable=True)
    qualified_plan = db.Column(db.Integer, nullable=True)
    meetings_plan = db.Column(db.Integer, nullable=True)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    manager = db.relationship("User")


class ManagerAbsence(db.Model):
    """Отсутствие менеджера (отпуск/больничный/прочее)."""

    __tablename__ = "manager_absences"

    id = db.Column(db.Integer, primary_key=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    date_from = db.Column(db.Date, nullable=False)
    date_to = db.Column(db.Date, nullable=False)
    kind = db.Column(db.String(20), nullable=False, default="vacation")  # vacation|sick|other

    manager = db.relationship("User")


# --- Ops Metrics Этап 4: затраты и окупаемость --------------------------------

class StaffCost(db.Model):
    """Затраты на сотрудника (менеджера/РОПа) за месяц — ввод вручную (§8.4)."""

    __tablename__ = "staff_costs"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    month = db.Column(db.Date, primary_key=True)  # 1-е число месяца
    cost_role = db.Column(db.String(10), nullable=False, default="manager")  # manager|rop
    salary_fixed = db.Column(db.Numeric, nullable=False, default=0)
    bonus_paid = db.Column(db.Numeric, nullable=False, default=0)
    payroll_tax_rate = db.Column(db.Numeric, nullable=False, default=0.302)
    overhead = db.Column(db.Numeric, nullable=False, default=0)
    lead_cost = db.Column(db.Numeric, nullable=True)
    comment = db.Column(db.String(500), nullable=True)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    user = db.relationship("User", foreign_keys=[user_id])


class RopCostAllocation(db.Model):
    """Рассчитанная доля затрат РОПа на менеджера за месяц (пересчитывается)."""

    __tablename__ = "rop_cost_allocations"

    month = db.Column(db.Date, primary_key=True)
    rop_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    weight = db.Column(db.Numeric, nullable=False, default=0)       # рабочие дни менеджера
    allocated_cost = db.Column(db.Numeric, nullable=False, default=0)

    rop = db.relationship("User", foreign_keys=[rop_user_id])
    manager = db.relationship("User", foreign_keys=[manager_id])


# --- Ops Metrics Этап 5: миссии и геймификация --------------------------------

class DailyMission(db.Model):
    """Дневная миссия менеджера: цели (тиры) и результат."""

    __tablename__ = "daily_missions"

    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    date = db.Column(db.Date, primary_key=True)
    level = db.Column(db.Integer, nullable=False, default=1)
    targets = db.Column(db.JSON)   # {metric: {bronze, silver, gold}}
    results = db.Column(db.JSON)    # {metric: value}
    tier_reached = db.Column(db.String(10), nullable=False, default="none")  # none|bronze|silver|gold
    xp_awarded = db.Column(db.Integer, nullable=False, default=0)
    finalized = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ManagerProgress(db.Model):
    """Прогресс геймификации менеджера: уровень, серия, заморозки, рекорды."""

    __tablename__ = "manager_progress"

    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    level = db.Column(db.Integer, nullable=False, default=1)
    streak_days = db.Column(db.Integer, nullable=False, default=0)
    streak_best = db.Column(db.Integer, nullable=False, default=0)
    freezes_available = db.Column(db.Integer, nullable=False, default=0)
    records = db.Column(db.JSON)   # {metric: {value, date}}
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class XpLedger(db.Model):
    """Единый журнал XP (идемпотентно по source+ref)."""

    __tablename__ = "xp_ledger"

    id = db.Column(db.Integer, primary_key=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    occurred_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    source = db.Column(db.String(30), nullable=False)
    amount = db.Column(db.Integer, nullable=False, default=0)
    ref = db.Column(db.String(80), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("source", "ref", name="uq_xp_ledger_source_ref"),
    )
