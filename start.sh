#!/usr/bin/env bash
# Единая стартап-команда для обоих Railway-сервисов из одного репозитория.
# Режим выбирается по SCHEDULER_ROLE:
#   worker → фоновый планировщик (python worker.py), без gunicorn/миграций;
#   иначе (web/all/пусто) → миграции + веб-сервер gunicorn.
# Так общий railway.toml не мешает: config-as-code задаёт ОДНУ команду, а она
# сама ветвится по переменной сервиса.
set -e

if [ "${SCHEDULER_ROLE}" = "worker" ]; then
  echo "[start] SCHEDULER_ROLE=worker → python worker.py (фоновый планировщик)"
  exec python worker.py
else
  echo "[start] SCHEDULER_ROLE=${SCHEDULER_ROLE:-all} → миграции + gunicorn (web)"
  flask --app app db upgrade
  exec gunicorn app:app --bind "0.0.0.0:${PORT}" --workers 1 --threads 4 --timeout 120
fi
