"""Заглушка дашборда. Данные появятся на Этапах 5-9.

Админ видит дашборд РОПа, менеджер редиректится в свой кабинет (тоже заглушка).
"""
from flask import Blueprint, render_template, redirect, url_for
from flask_login import login_required, current_user

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@login_required
def index():
    # Менеджеры пока попадают на ту же заглушку, но с ролевой пометкой.
    if current_user.is_admin:
        return render_template("dashboard/index.html")
    return redirect(url_for("dashboard.manager_home"))


@dashboard_bp.route("/me")
@login_required
def manager_home():
    return render_template("dashboard/manager.html")
