"""Расширения Flask, инициализируемые в фабрике приложения.

Держим их отдельно, чтобы избежать циклических импортов между app.py и models.py.
"""
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from apscheduler.schedulers.background import BackgroundScheduler

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()

# Планировщик пока не запускает задач (Этапы 3/8/9), но создаём его тут,
# чтобы фабрика могла его сконфигурировать и стартовать при необходимости.
scheduler = BackgroundScheduler()

# Куда редиректить неавторизованных
login_manager.login_view = "auth.login"
login_manager.login_message = "Требуется вход в систему."
login_manager.login_message_category = "warning"
