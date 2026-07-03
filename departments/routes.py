"""Управление отделами (только админ): список, создание, переименование, удаление."""
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort

from extensions import db
from models import Department, User
from auth.decorators import admin_required

departments_bp = Blueprint("departments", __name__, url_prefix="/departments")


@departments_bp.route("/")
@admin_required
def index():
    departments = Department.query.order_by(Department.name).all()
    counts = {
        d.id: User.query.filter_by(department_id=d.id).count() for d in departments
    }
    return render_template(
        "departments/index.html", departments=departments, counts=counts
    )


@departments_bp.route("/new", methods=["POST"])
@admin_required
def create():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Укажите название отдела.", "error")
    elif Department.query.filter_by(name=name).first() is not None:
        flash("Отдел с таким названием уже есть.", "error")
    else:
        db.session.add(Department(name=name[:255]))
        db.session.commit()
        flash("Отдел создан.", "success")
    return redirect(url_for("departments.index"))


@departments_bp.route("/<int:department_id>/edit", methods=["POST"])
@admin_required
def edit(department_id):
    dept = db.session.get(Department, department_id) or abort(404)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Название не может быть пустым.", "error")
    else:
        dept.name = name[:255]
        db.session.commit()
        flash("Отдел сохранён.", "success")
    return redirect(url_for("departments.index"))


@departments_bp.route("/<int:department_id>/delete", methods=["POST"])
@admin_required
def delete(department_id):
    dept = db.session.get(Department, department_id) or abort(404)
    # у пользователей отдела просто снимаем привязку
    User.query.filter_by(department_id=dept.id).update({"department_id": None})
    db.session.delete(dept)
    db.session.commit()
    flash("Отдел удалён, привязка сотрудников снята.", "success")
    return redirect(url_for("departments.index"))
