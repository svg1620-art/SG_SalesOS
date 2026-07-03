"""Сид отделов по умолчанию."""
from extensions import db
from models import Department

DEFAULT_DEPARTMENTS = ["Отдел продаж", "Отдел развития клиентов"]


def seed_default_departments(app) -> tuple[int, str]:
    """Создать отделы по умолчанию, если их ещё нет. Идемпотентно."""
    created = 0
    for name in DEFAULT_DEPARTMENTS:
        if Department.query.filter_by(name=name).first() is None:
            db.session.add(Department(name=name))
            created += 1
    db.session.commit()
    return created, f"Отделов создано: {created} (из {len(DEFAULT_DEPARTMENTS)})."
