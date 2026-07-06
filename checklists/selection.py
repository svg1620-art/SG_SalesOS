"""Выбор активного чек-листа с учётом отдела.

Активный чек-лист — по одному на отдел (+ один общий, department_id=None).
Для звонка берём чек-лист отдела менеджера, иначе общий, иначе любой активный.
"""
from models import Checklist, User


def active_checklist_for_department(department_id):
    """Активный чек-лист отдела; при отсутствии — общий (department_id=None)."""
    if department_id is not None:
        own = Checklist.query.filter_by(
            department_id=department_id, is_active=True
        ).first()
        if own is not None:
            return own
    return Checklist.query.filter_by(department_id=None, is_active=True).first()


def resolve_checklist_for_call(call):
    """Чек-лист для оценки звонка.

    Приоритет: явно заданный на звонке → активный чек-лист отдела менеджера →
    общий активный → любой активный (фолбэк).
    """
    if call.checklist_id:
        chosen = Checklist.query.get(call.checklist_id)
        if chosen is not None:
            return chosen

    department_id = None
    if call.manager_id:
        manager = User.query.get(call.manager_id)
        if manager is not None:
            department_id = manager.department_id

    checklist = active_checklist_for_department(department_id)
    if checklist is None:
        checklist = Checklist.query.filter_by(is_active=True).first()
    return checklist
