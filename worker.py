"""Entrypoint для отдельного Railway-сервиса `worker` (фоновый планировщик).

Разносит APScheduler из веб-процесса в собственный процесс, чтобы тяжёлые
джобы (синхронизация amoCRM-метрик, LLM-диагностика, продления, Telegram) не
конкурировали с веб-запросами за GIL и пул соединений.

Запуск на Railway: Custom Start Command = `python worker.py`.
Volume /data к этому сервису НЕ монтируется — аудио-пайплайн (скачивание
записей + плеер) остаётся на `web` (см. SCHEDULER_ROLE).

Переменные окружения (этого сервиса):
  SCHEDULER_ENABLED=1   (worker.py выставит сам, если забыли)
  SCHEDULER_ROLE=worker (по умолчанию здесь — только «воркерные» джобы)
  + те же DATABASE_URL / ключи / TZ, что и у web.
"""
import os
import sys
import time

# Должно стоять ДО импорта app (Config читает env на импорте).
os.environ.setdefault("SCHEDULER_ENABLED", "1")
os.environ.setdefault("SCHEDULER_ROLE", "worker")

from app import app  # noqa: E402 — импорт создаёт app и стартует планировщик
from extensions import scheduler  # noqa: E402


def main() -> int:
    if not scheduler.running:
        app.logger.error(
            "[worker] планировщик не запущен — проверь SCHEDULER_ENABLED=1 "
            "и SCHEDULER_ROLE=worker в переменных сервиса."
        )
        return 1
    jobs = [j.id for j in scheduler.get_jobs()]
    app.logger.info("[worker] старт: роль=%s, джоб=%d %s",
                    app.config.get("SCHEDULER_ROLE"), len(jobs), jobs)
    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        app.logger.info("[worker] остановка планировщика")
        scheduler.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
