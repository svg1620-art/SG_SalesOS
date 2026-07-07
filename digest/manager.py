"""Персональная AI-сводка по конкретному менеджеру (по запросу, не по cron).

Собирает звонки менеджера за период, частые упущения и рекомендации по навыкам,
просит Claude оформить короткую сводку «на что обратить внимание именно этому
менеджеру» + конкретные фокусы. Не сохраняется в БД — генерится по кнопке.
"""
from collections import defaultdict
from datetime import datetime

from flask import current_app

from extensions import db
from models import Call, User, MissedMoment, Recommendation
from claude_client import claude_complete
from utils import extract_json

_PRIORITY_WEIGHT = {"high": 3, "med": 2, "low": 1}


def compute_manager_stats(manager_id, date_from, date_to) -> dict:
    """Статистика по менеджеру за период (без исключённых звонков)."""
    calls = Call.query.filter(
        Call.manager_id == manager_id,
        Call.status == "done",
        Call.excluded.isnot(True),
        Call.started_at >= date_from,
        Call.started_at <= date_to,
    ).all()

    zones = {"green": 0, "yellow": 0, "red": 0}
    for c in calls:
        if c.zone in zones:
            zones[c.zone] += 1

    scored = [c.overall_score for c in calls if c.overall_score is not None]
    avg = round(sum(scored) / len(scored), 1) if scored else None

    # исторический средний до периода — для сравнения
    hist = [
        c.overall_score
        for c in Call.query.filter(
            Call.manager_id == manager_id,
            Call.status == "done",
            Call.started_at < date_from,
        ).all()
        if c.overall_score is not None
    ]
    hist_avg = round(sum(hist) / len(hist), 1) if hist else None

    call_ids = [c.id for c in calls]

    # частые упущения
    missed_counts = defaultdict(int)
    if call_ids:
        for mm in MissedMoment.query.filter(MissedMoment.call_id.in_(call_ids)).all():
            if mm.label:
                missed_counts[mm.label.strip()] += 1
    top_missed = sorted(
        ({"label": k, "count": v} for k, v in missed_counts.items()),
        key=lambda x: x["count"], reverse=True,
    )[:6]

    # рекомендации по навыкам (агрегат по весу приоритета)
    skills = defaultdict(lambda: {"skill": "", "count": 0, "weight": 0, "example": ""})
    if call_ids:
        for r in Recommendation.query.filter(Recommendation.call_id.in_(call_ids)).all():
            key = (r.skill or "Общее").strip() or "Общее"
            item = skills[key]
            item["skill"] = key
            item["count"] += 1
            item["weight"] += _PRIORITY_WEIGHT.get(r.priority, 1)
            if not item["example"] and r.text:
                item["example"] = r.text.strip()
    top_skills = sorted(skills.values(), key=lambda s: s["weight"], reverse=True)[:6]

    red_calls = [
        {
            "id": c.id,
            "client": (c.client.name or c.client.phone_normalized) if c.client else "—",
            "score": c.overall_score,
        }
        for c in calls if c.zone == "red"
    ]

    return {
        "calls": len(calls),
        "dialogs": len({c.client_id for c in calls if c.client_id}),
        "zones": zones,
        "avg": avg,
        "hist_avg": hist_avg,
        "top_missed": top_missed,
        "top_skills": top_skills,
        "red_calls": red_calls,
    }


def _summary_from_claude(name, period_label, stats: dict) -> dict:
    lines = [
        f"Менеджер: {name}",
        f"Период: {period_label}",
        f"Звонков: {stats['calls']}, диалогов: {stats['dialogs']}",
        f"Средний балл за период: {stats['avg']} (исторический: {stats['hist_avg']})",
        f"Зоны — зелёная: {stats['zones']['green']}, жёлтая: {stats['zones']['yellow']}, "
        f"красная: {stats['zones']['red']}",
    ]
    if stats["top_missed"]:
        lines.append("Частые упущения: " + ", ".join(
            f"{m['label']} (×{m['count']})" for m in stats["top_missed"]))
    if stats["top_skills"]:
        lines.append("Навыки для роста:")
        for s in stats["top_skills"]:
            ex = f" — например: {s['example']}" if s["example"] else ""
            lines.append(f"  - {s['skill']} (упоминаний {s['count']}){ex}")

    prompt = (
        "Ты — наставник (коуч) отдела продаж. По данным одного менеджера ниже "
        "сделай короткую персональную сводку: что у него/неё в порядке и что "
        "срочно подтянуть. Пиши конкретно и по делу, обращайся по имени.\n\n"
        + "\n".join(lines)
        + "\n\nВерни СТРОГО JSON: {\"summary\": \"3-5 предложений человеческим языком\", "
        "\"focuses\": [\"2-4 конкретных фокуса/рекомендации для этого менеджера\"]}"
    )
    raw = claude_complete(
        prompt,
        system="Ты лаконичный наставник по продажам. Отвечай на русском, строго JSON.",
        max_tokens=1200,
    )
    data = extract_json(raw)
    return {
        "summary": (data.get("summary") or "").strip(),
        "focuses": [f for f in (data.get("focuses") or []) if isinstance(f, str)][:4],
    }


def generate_manager_digest(app, manager_id, date_from, date_to) -> dict:
    """Собрать персональную сводку по менеджеру за период (по запросу)."""
    app = app or current_app
    manager = db.session.get(User, manager_id)
    if manager is None:
        return {"ok": False, "error": "Менеджер не найден."}

    name = manager.full_name or manager.email
    stats = compute_manager_stats(manager_id, date_from, date_to)
    period_label = f"{date_from:%d.%m.%Y}–{date_to:%d.%m.%Y}"

    narrative = {"summary": "", "focuses": []}
    if stats["calls"] == 0:
        narrative["summary"] = "За выбранный период у менеджера нет обработанных звонков."
    else:
        try:
            narrative = _summary_from_claude(name, period_label, stats)
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("[manager-digest] Claude недоступен: %s", exc)
            narrative["summary"] = (
                f"Звонков: {stats['calls']}, средний балл {stats['avg']}, "
                f"красных: {len(stats['red_calls'])}. AI-сводка недоступна "
                "(проверьте ANTHROPIC_API_KEY/CLAUDE_MODEL)."
            )

    return {
        "ok": True,
        "manager": name,
        "period_label": period_label,
        "stats": stats,
        "summary": narrative["summary"],
        "focuses": narrative["focuses"],
    }
