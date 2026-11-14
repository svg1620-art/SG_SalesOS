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


def _start_health_server() -> None:
    """Мини-HTTP на $PORT, чтобы проходил общий healthcheck Railway (/healthz).

    У worker нет веб-сервера, но railway.toml задаёт healthcheckPath для обоих
    сервисов. Отдаём 200 на любой путь, чтобы Railway считал процесс живым.
    """
    port = os.environ.get("PORT")
    if not port:
        return
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):  # тише в логах
            return

    try:
        srv = HTTPServer(("0.0.0.0", int(port)), _H)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("[worker] health-сервер не поднялся: %s", exc)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app.logger.info("[worker] health-сервер на :%s", port)


def main() -> int:
    _start_health_server()
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
