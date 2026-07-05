"""Агент дневной сводки РОПа (Claude, cron).

Раз в день агрегирует звонки за день и формирует сводку «на что обратить
внимание»: кто просел, красные звонки, частые упущения, фокусы на завтра.
Сохраняется в DailyDigest (одна строка на дату), показывается на дашборде.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import current_app

from extensions import db
from models import Call, DailyDigest, User, MissedMoment
from claude_client import claude_complete
from utils import extract_json

_DROP_EPS = 5  # падение среднего балла, считающееся «просадкой»


def today_in_tz(app=None) -> "datetime.date":
    app = app or current_app
    tz = app.config.get("TZ") or "UTC"
    try:
        return datetime.now(ZoneInfo(tz)).date()
    except Exception:
        return datetime.utcnow().date()


def compute_day_stats(day) -> dict:
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)

    calls = Call.query.filter(
        Call.status == "done",
        Call.excluded.isnot(True),
        Call.started_at >= start,
        Call.started_at < end,
    ).all()

    zones = {"green": 0, "yellow": 0, "red": 0}
    by_manager = defaultdict(list)
    for c in calls:
        if c.zone in zones:
            zones[c.zone] += 1
        by_manager[c.manager_id].append(c)

    managers = []
    for mid, mcalls in by_manager.items():
        manager = db.session.get(User, mid) if mid else None
        scored = [c.overall_score for c in mcalls if c.overall_score is not None]
        avg_today = round(sum(scored) / len(scored), 1) if scored else None
        # исторический средний до этого дня
        hist = [
            c.overall_score
            for c in Call.query.filter(
                Call.manager_id == mid,
                Call.status == "done",
                Call.started_at < start,
            ).all()
            if c.overall_score is not None
        ]
        hist_avg = round(sum(hist) / len(hist), 1) if hist else None
        dropped = (
            avg_today is not None
            and hist_avg is not None
            and avg_today < hist_avg - _DROP_EPS
        )
        managers.append({
            "name": (manager.full_name or manager.email) if manager else "Не назначен",
            "calls": len(mcalls),
            "avg": avg_today,
            "hist_avg": hist_avg,
            "dropped": dropped,
        })
    managers.sort(key=lambda m: (m["avg"] is not None, m["avg"] or 0))

    red_calls = [
        {
            "id": c.id,
            "manager": (c.manager.full_name or c.manager.email) if c.manager else "—",
            "client": (c.client.name or c.client.phone_normalized) if c.client else "—",
            "score": c.overall_score,
        }
        for c in calls if c.zone == "red"
    ]

    call_ids = [c.id for c in calls]
    missed_counts = defaultdict(int)
    if call_ids:
        for mm in MissedMoment.query.filter(MissedMoment.call_id.in_(call_ids)).all():
            if mm.label:
                missed_counts[mm.label.strip()] += 1
    top_missed = sorted(
        ({"label": k, "count": v} for k, v in missed_counts.items()),
        key=lambda x: x["count"], reverse=True,
    )[:5]

    return {
        "calls": len(calls),
        "dialogs": len({c.client_id for c in calls if c.client_id}),
        "zones": zones,
        "managers": managers,
        "red_calls": red_calls,
        "top_missed": top_missed,
    }


def _narrative_from_claude(day, stats: dict) -> dict:
    """Попросить Claude оформить сводку. Возвращает {summary, focuses}."""
    lines = [f"Дата: {day:%d.%m.%Y}", f"Звонков: {stats['calls']}, диалогов: {stats['dialogs']}",
             f"Зоны — зелёная: {stats['zones']['green']}, жёлтая: {stats['zones']['yellow']}, красная: {stats['zones']['red']}"]
    if stats["managers"]:
        lines.append("Менеджеры:")
        for m in stats["managers"]:
            drop = " (ПРОСАДКА)" if m["dropped"] else ""
            lines.append(f"  - {m['name']}: {m['calls']} звонков, средний {m['avg']}{drop} (было {m['hist_avg']})")
    if stats["red_calls"]:
        lines.append(f"Красные звонки: {len(stats['red_calls'])}")
    if stats["top_missed"]:
        lines.append("Частые упущения: " + ", ".join(f"{m['label']} (×{m['count']})" for m in stats["top_missed"]))

    prompt = (
        "Ты — руководитель отдела продаж. По сводным данным за день ниже сделай "
        "короткую сводку для РОПа «на что обратить внимание».\n\n"
        + "\n".join(lines)
        + "\n\nВерни СТРОГО JSON: {\"summary\": \"3-5 предложений человеческим языком\", "
        "\"focuses\": [\"2-3 конкретных фокуса на завтра\"]}"
    )
    raw = claude_complete(
        prompt,
        system="Ты лаконичный руководитель отдела продаж. Отвечай на русском, строго JSON.",
        max_tokens=1200,
    )
    data = extract_json(raw)
    return {
        "summary": (data.get("summary") or "").strip(),
        "focuses": [f for f in (data.get("focuses") or []) if isinstance(f, str)][:3],
    }


def generate_daily_digest(app, day=None) -> DailyDigest:
    """Сформировать/обновить сводку за день. Возвращает DailyDigest."""
    day = day or today_in_tz(app)
    stats = compute_day_stats(day)

    narrative = {"summary": "", "focuses": []}
    if stats["calls"] == 0:
        narrative["summary"] = "За день нет обработанных звонков."
    else:
        try:
            narrative = _narrative_from_claude(day, stats)
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("[digest] Claude недоступен: %s", exc)
            narrative["summary"] = (
                f"Звонков: {stats['calls']}, красных: {len(stats['red_calls'])}. "
                "AI-сводка недоступна (проверьте ANTHROPIC_API_KEY/CLAUDE_MODEL)."
            )

    content = {
        "date": day.isoformat(),
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "stats": stats,
        "summary": narrative["summary"],
        "focuses": narrative["focuses"],
    }

    digest = DailyDigest.query.filter_by(date=day).first()
    if digest is None:
        digest = DailyDigest(date=day, content_json=content)
        db.session.add(digest)
    else:
        # сохраняем метку отправки пульса, если была
        if digest.content_json and digest.content_json.get("pulse_sent"):
            content["pulse_sent"] = digest.content_json["pulse_sent"]
        digest.content_json = content
    db.session.commit()
    return digest
