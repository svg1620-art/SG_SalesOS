"""Скоринг звонка: веса → итог → зона, применение анализа Claude к моделям.

Правила (раздел 5.2 и 12 ТЗ):
- overall = Σ(score/max_score * weight) по критериям; бэкенд считает сам, не
  доверяя числу от модели.
- зона по порогам активного чек-листа: ≥ green_min → green; ≥ yellow_min →
  yellow; иначе red.
- критичный критерий с 0/10 капает зону в yellow (не может быть green).
"""
from extensions import db
from models import CallCriterionScore, Recommendation, MissedMoment


def zone_for(score: int, checklist, critical_failed: bool) -> str:
    if score >= checklist.zone_green_min:
        zone = "green"
    elif score >= checklist.zone_yellow_min:
        zone = "yellow"
    else:
        zone = "red"
    if critical_failed and zone == "green":
        zone = "yellow"  # правило критичных критериев
    return zone


def _to_int(value, default=0):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def apply_analysis(call, checklist, data: dict) -> None:
    """Записать результат анализа в call и связанные таблицы, посчитать зону.

    Идемпотентно: удаляет прежние оценки/рекомендации/упущения (для повторного
    прогона) и создаёт заново. Коммит — на стороне воркера.
    """
    # очистка прошлого результата (reprocess)
    CallCriterionScore.query.filter_by(call_id=call.id).delete()
    Recommendation.query.filter_by(call_id=call.id).delete()
    MissedMoment.query.filter_by(call_id=call.id).delete()

    criteria_by_id = {c.id: c for c in checklist.criteria}

    total = 0.0
    critical_failed = False
    for item in data.get("criteria") or []:
        if not isinstance(item, dict):
            continue
        criterion = criteria_by_id.get(item.get("criterion_id"))
        max_score = _to_int(item.get("max_score"), 10) or 10
        score = max(0, min(_to_int(item.get("score"), 0), max_score))

        db.session.add(
            CallCriterionScore(
                call_id=call.id,
                criterion_id=criterion.id if criterion else None,
                score=score,
                max_score=max_score,
                evidence=(item.get("evidence") or "").strip(),
                comment=(item.get("comment") or "").strip(),
                is_missed=bool(item.get("is_missed")),
            )
        )
        if criterion:
            total += (score / max_score) * criterion.weight
            if criterion.is_critical and score == 0:
                critical_failed = True

    overall = max(0, min(100, _to_int(total, 0)))

    for rec in data.get("recommendations") or []:
        if not isinstance(rec, dict):
            continue
        text = (rec.get("text") or "").strip()
        if not text:
            continue
        priority = (rec.get("priority") or "med").strip().lower()
        if priority not in {"high", "med", "low"}:
            priority = "med"
        db.session.add(
            Recommendation(
                call_id=call.id,
                manager_id=call.manager_id,
                skill=(rec.get("skill") or "").strip()[:255],
                text=text,
                priority=priority,
            )
        )

    for mm in data.get("missed_moments") or []:
        if not isinstance(mm, dict):
            continue
        label = (mm.get("label") or "").strip()
        if not label:
            continue
        db.session.add(
            MissedMoment(
                call_id=call.id,
                transcript_span_start=_to_int(mm.get("span_start"), 0),
                transcript_span_end=_to_int(mm.get("span_end"), 0),
                label=label[:255],
                explanation=(mm.get("explanation") or "").strip(),
                quote=(mm.get("quote") or "").strip(),
            )
        )

    call.summary = (data.get("summary") or "").strip()
    call.overall_score = overall
    call.zone = zone_for(overall, checklist, critical_failed)
