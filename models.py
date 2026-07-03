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

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(255))
    role = db.Column(db.String(20), nullable=False, default="manager")  # admin|manager
    # для сопоставления с ответственным в amoCRM (Этап 8)
    amo_user_id = db.Column(db.BigInteger, nullable=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

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
    # пороги зон (см. раздел 12 ТЗ)
    zone_green_min = db.Column(db.Integer, nullable=False, default=80)
    zone_yellow_min = db.Column(db.Integer, nullable=False, default=60)
    is_active = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

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
    overall_score = db.Column(db.Integer)
    zone = db.Column(db.String(10))  # green|yellow|red
    diarization = db.Column(db.String(10))  # stereo|heuristic
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


class AmoToken(db.Model):
    """OAuth-токены amoCRM (единственная строка)."""

    __tablename__ = "amo_tokens"

    id = db.Column(db.Integer, primary_key=True)
    access_token = db.Column(db.Text)
    refresh_token = db.Column(db.Text)
    expires_at = db.Column(db.DateTime)
    base_domain = db.Column(db.String(255))
