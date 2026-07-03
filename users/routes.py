"""Управление пользователями (только админ): список, создание, редактирование.

Созданные менеджеры автоматически появляются в поле «Менеджер» при загрузке
звонка (форма подтягивает всех активных пользователей).
"""
from flask import (
    Blueprint,
    render_template,
    redirect,
    url_for,
    request,
    flash,
    abort,
)
from flask_login import current_user

from extensions import db
from models import User, Department
from auth.decorators import admin_required

users_bp = Blueprint("users", __name__, url_prefix="/users")

ROLES = ("admin", "manager")
MIN_PASSWORD = 6


def _parse_amo_user_id(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_department_id(raw):
    raw = (raw or "").strip()
    if raw.isdigit() and db.session.get(Department, int(raw)):
        return int(raw)
    return None


def _departments():
    return Department.query.order_by(Department.name).all()


@users_bp.route("/")
@admin_required
def index():
    users = User.query.order_by(User.role, User.full_name, User.email).all()
    return render_template("users/index.html", users=users)


@users_bp.route("/new", methods=["GET", "POST"])
@admin_required
def create():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        full_name = (request.form.get("full_name") or "").strip()
        role = request.form.get("role") or "manager"
        password = request.form.get("password") or ""

        error = None
        if not email:
            error = "Укажите email."
        elif role not in ROLES:
            error = "Некорректная роль."
        elif len(password) < MIN_PASSWORD:
            error = f"Пароль не короче {MIN_PASSWORD} символов."
        elif User.query.filter_by(email=email).first() is not None:
            error = "Пользователь с таким email уже есть."

        if error:
            flash(error, "error")
            return render_template("users/form.html", user=None, form=request.form,
                                   departments=_departments()), 400

        user = User(
            email=email,
            full_name=full_name or None,
            role=role,
            is_active=True,
            department_id=_parse_department_id(request.form.get("department_id")),
            amo_user_id=_parse_amo_user_id(request.form.get("amo_user_id")),
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash(f"Пользователь {email} создан.", "success")
        return redirect(url_for("users.index"))

    return render_template("users/form.html", user=None, form={}, departments=_departments())


@users_bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit(user_id):
    user = db.session.get(User, user_id) or abort(404)

    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        role = request.form.get("role") or user.role
        is_active = bool(request.form.get("is_active"))
        password = request.form.get("password") or ""

        if role not in ROLES:
            flash("Некорректная роль.", "error")
            return render_template("users/form.html", user=user, form=request.form,
                                   departments=_departments()), 400

        # защита от самоблокировки
        if user.id == current_user.id:
            if role != "admin":
                flash("Нельзя понизить собственную роль.", "error")
                return render_template("users/form.html", user=user, form=request.form,
                                   departments=_departments()), 400
            if not is_active:
                flash("Нельзя деактивировать собственную учётку.", "error")
                return render_template("users/form.html", user=user, form=request.form,
                                   departments=_departments()), 400

        # не оставить систему без активных админов
        if user.role == "admin" and (role != "admin" or not is_active):
            other_admins = User.query.filter(
                User.role == "admin", User.is_active.is_(True), User.id != user.id
            ).count()
            if other_admins == 0:
                flash("Это последний активный админ — изменение заблокировано.", "error")
                return render_template("users/form.html", user=user, form=request.form,
                                   departments=_departments()), 400

        if password:
            if len(password) < MIN_PASSWORD:
                flash(f"Пароль не короче {MIN_PASSWORD} символов.", "error")
                return render_template("users/form.html", user=user, form=request.form,
                                   departments=_departments()), 400
            user.set_password(password)

        user.full_name = full_name or None
        user.role = role
        user.is_active = is_active
        user.department_id = _parse_department_id(request.form.get("department_id"))
        user.amo_user_id = _parse_amo_user_id(request.form.get("amo_user_id"))
        db.session.commit()
        flash("Пользователь сохранён.", "success")
        return redirect(url_for("users.index"))

    return render_template("users/form.html", user=user, form={}, departments=_departments())
