"""Дневной пульс в Telegram: краткий отчёт по менеджерам.

По каждому активному менеджеру за день: сколько диалогов, средняя длительность,
средний балл и строка-рекомендация. Отправляется ботом в 19:00 (TELEGRAM_HOUR)
получателям из TELEGRAM_CHAT_IDS. Идемпотентно в пределах дня.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from html import escape

import httpx

from extensions import db
from models import Call, User, Recommendation, DailyDigest

_ZONE_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
_PRIORITY_WEIGHT = {"high": 3, "med": 2, "low": 1}


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _zone_for_score(score, avg_zone_calls):
    # эмодзи по преобладающей зоне дня менеджера
    if not avg_zone_calls:
        return ""
    best = max(avg_zone_calls, key=avg_zone_calls.get)
    return _ZONE_EMOJI.get(best, "")


def _recommendation_line(call_ids):
    """Одна строка-рекомендация: самая приоритетная за день."""
    if not call_ids:
        return ""
    recs = Recommendation.query.filter(Recommendation.call_id.in_(call_ids)).all()
    if not recs:
        return ""
    recs.sort(key=lambda r: _PRIORITY_WEIGHT.get(r.priority, 1), reverse=True)
    top = recs[0]
    skill = (top.skill or "").strip()
    text = (top.text or "").strip()
    line = f"{skill}: {text}" if skill else text
    return line[:180]


def build_pulse(app, day) -> str:
    """Собрать текст пульса за день (HTML для Telegram)."""
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)

    calls = Call.query.filter(
        Call.status == "done",
        Call.started_at >= start,
        Call.started_at < end,
    ).all()

    by_manager = defaultdict(list)
    for c in calls:
        by_manager[c.manager_id].append(c)

    header = f"📊 <b>Пульс за {day:%d.%m.%Y}</b>"
    if not calls:
        return header + "\n\nЗа день нет обработанных звонков."

    def _avg(mcalls):
        s = [c.overall_score for c in mcalls if c.overall_score is not None]
        return sum(s) / len(s) if s else 0

    def _manager_line(manager, mcalls):
        name = (manager.full_name or manager.email) if manager else "Не назначен"
        dialogs = len({c.client_id for c in mcalls if c.client_id})
        durations = [c.duration_sec for c in mcalls if c.duration_sec]
        avg_dur = _fmt_duration(sum(durations) / len(durations)) if durations else "—"
        scored = [c.overall_score for c in mcalls if c.overall_score is not None]
        avg_score = round(sum(scored) / len(scored)) if scored else "—"
        zone_counts = defaultdict(int)
        for c in mcalls:
            if c.zone:
                zone_counts[c.zone] += 1
        emoji = _zone_for_score(avg_score, zone_counts)
        line = (
            f"\n👤 <b>{escape(name)}</b> — {dialogs} диал., "
            f"ср. {avg_dur}, балл {avg_score} {emoji}"
        )
        rec = _recommendation_line([c.id for c in mcalls])
        if rec:
            line += f"\n   💡 {escape(rec)}"
        return line

    # группировка менеджеров по отделам
    managers = {mid: db.session.get(User, mid) if mid else None for mid in by_manager}
    dept_groups = defaultdict(list)  # dept_name -> [(manager, mcalls)]
    for mid, mcalls in by_manager.items():
        manager = managers[mid]
        dept_name = (
            manager.department.name if manager and manager.department else "Без отдела"
        )
        dept_groups[dept_name].append((manager, mcalls))

    blocks = [header]
    for dept_name in sorted(dept_groups.keys()):
        rows = sorted(dept_groups[dept_name], key=lambda mm: _avg(mm[1]))
        blocks.append(f"\n\n🏢 <b>{escape(dept_name)}</b>")
        for manager, mcalls in rows:
            blocks.append(_manager_line(manager, mcalls))

    return "".join(blocks)


def _send_message(app, text: str) -> bool:
    from settings_store import telegram_token, telegram_chat_ids

    token = telegram_token(app)
    chat_ids = telegram_chat_ids(app)
    if not token or not chat_ids:
        app.logger.info("[telegram] не настроен (нет токена/получателей) — пропуск.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok_any = False
    for chat_id in chat_ids:
        try:
            resp = httpx.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
            if resp.status_code == 200:
                ok_any = True
            else:
                app.logger.warning(
                    "[telegram] chat %s: %s %s", chat_id, resp.status_code, resp.text[:200]
                )
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("[telegram] ошибка отправки в %s: %s", chat_id, exc)
    return ok_any


def send_daily_pulse(app, day=None, force: bool = False) -> bool:
    """Собрать и отправить пульс. Идемпотентно в пределах дня (если не force)."""
    from digest.daily import today_in_tz

    day = day or today_in_tz(app)
    digest = DailyDigest.query.filter_by(date=day).first()
    if not force and digest and (digest.content_json or {}).get("pulse_sent"):
        app.logger.info("[telegram] пульс за %s уже отправлен.", day)
        return False

    text = build_pulse(app, day)
    sent = _send_message(app, text)

    if sent:
        stamp = datetime.utcnow().isoformat(timespec="seconds")
        if digest is None:
            digest = DailyDigest(date=day, content_json={"pulse_sent": stamp})
            db.session.add(digest)
        else:
            content = dict(digest.content_json or {})
            content["pulse_sent"] = stamp
            digest.content_json = content
        db.session.commit()
    return sent
