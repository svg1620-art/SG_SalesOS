"""Еженедельная AI-диагностика менеджера (§11).

Собираем JSON с метриками L1–L3 за 4 недели, конверсиями vs команда, факторной
диагностикой (§8.3), трендом балла качества, топ-3 повторяющимися упущениями и
историей уровней/серий. Отдаём в LLM через ЕДИНЫЙ слой провайдера SalesOS
(`claude_complete` → DeepSeek/Claude по настройке LLM_PROVIDER). Ответ — строгий
JSON {main_cause, evidence[], action_for_manager, question_for_rop, risk_level}.

ВАЖНО: анализ идёт через настраиваемый провайдер (по умолчанию DeepSeek на этом
проекте), модель Claude не хардкодится. Падение разбора по одному менеджеру не
останавливает остальных (failure isolation, §14).
"""
from collections import Counter
from datetime import date, datetime, timedelta

from extensions import db
from models import (Call, ManagerDayStat, ManagerProgress, MissedMoment,
                    ManagerDiagnosis, User)

_RISK = {"low", "medium", "high"}


def week_start_for(today: date = None) -> date:
    """Понедельник недели (ISO)."""
    today = today or date.today()
    return today - timedelta(days=today.weekday())


# ---------- сбор входа ----------

def _sum_window(manager_id, d_from, d_to):
    rows = ManagerDayStat.query.filter(
        ManagerDayStat.manager_id == manager_id,
        ManagerDayStat.date >= d_from, ManagerDayStat.date <= d_to,
    ).all()
    agg = {k: 0 for k in (
        "calls_out", "calls_connected", "talk_time_sec", "messages_out",
        "qualified", "meetings_set", "meetings_held", "invoices", "payments",
    )}
    rev = 0.0
    scores = []
    for r in rows:
        for k in agg:
            agg[k] += int(getattr(r, k) or 0)
        rev += float(r.payments_sum or 0)
        if r.avg_call_score is not None:
            scores.append(r.avg_call_score)
    agg["payments_sum"] = round(rev)
    agg["avg_call_score"] = round(sum(scores) / len(scores)) if scores else None
    return agg, scores


def _conv(a, b):
    return round(a / b * 100) if b else None


def _conversions(agg):
    return {
        "connect_rate": _conv(agg["calls_connected"], agg["calls_out"]),
        "connect_to_qual": _conv(agg["qualified"], agg["calls_connected"]),
        "qual_to_meeting": _conv(agg["meetings_set"], agg["qualified"]),
        "show_rate": _conv(agg["meetings_held"], agg["meetings_set"]),
        "meeting_to_invoice": _conv(agg["invoices"], agg["meetings_held"]),
        "invoice_to_payment": _conv(agg["payments"], agg["invoices"]),
    }


def _team_conversions(d_from, d_to, exclude_id=None):
    """Средние конверсии команды за окно (агрегат по сумме, не среднее средних)."""
    team = {k: 0 for k in (
        "calls_out", "calls_connected", "qualified", "meetings_set",
        "meetings_held", "invoices", "payments")}
    for m in User.query.filter_by(role="manager", is_active=True).all():
        if exclude_id and m.id == exclude_id:
            continue
        agg, _ = _sum_window(m.id, d_from, d_to)
        for k in team:
            team[k] += agg[k]
    return _conversions(team)


def _quality_trend(manager_id, week_start):
    """Средний балл по 4 неделям (старая→свежая)."""
    out = []
    for i in range(4, 0, -1):
        # окно недели i назад: [week_start-7*i .. +6]
        wf = week_start - timedelta(days=7 * i)
        _agg, scores = _sum_window(manager_id, wf, wf + timedelta(days=6))
        out.append(round(sum(scores) / len(scores)) if scores else None)
    return out  # [4 недели назад ... прошлая неделя]


def _top_missed(manager_id, d_from, d_to, limit=3):
    calls = Call.query.filter(
        Call.manager_id == manager_id, Call.status == "done",
        Call.started_at >= datetime.combine(d_from, datetime.min.time()),
        Call.started_at <= datetime.combine(d_to, datetime.max.time()),
    ).all()
    ids = [c.id for c in calls]
    if not ids:
        return []
    mm = MissedMoment.query.filter(MissedMoment.call_id.in_(ids)).all()
    cnt = Counter((m.label or "").strip() for m in mm if (m.label or "").strip())
    return [{"label": lbl, "count": n} for lbl, n in cnt.most_common(limit)]


def build_input(manager, week_start) -> dict:
    """Входной JSON для LLM по менеджеру за 4 недели до начала недели разбора."""
    from ops.payback import factor_diagnosis, month_first
    d_to = week_start - timedelta(days=1)       # прошлая неделя — последняя полная
    d_from = d_to - timedelta(days=27)          # 4 недели
    agg, _scores = _sum_window(manager.id, d_from, d_to)
    conv = _conversions(agg)
    team = _team_conversions(d_from, d_to, exclude_id=manager.id)
    try:
        fd = factor_diagnosis(manager.id, month_first(d_to))
        factors = [
            {"factor": r["label"], "now": round(r["m"], 2), "base": round(r["b"], 2),
             "contribution": round(r.get("contribution", 0))}
            for r in fd.get("rows", [])
        ]
        rev_delta = round(fd.get("delta", 0))
    except Exception:  # noqa: BLE001
        db.session.rollback()
        factors, rev_delta = [], 0
    prog = db.session.get(ManagerProgress, manager.id)
    return {
        "manager": manager.full_name or manager.email,
        "window": {"from": d_from.isoformat(), "to": d_to.isoformat()},
        "totals_4w": agg,
        "conversions_pct": conv,
        "team_conversions_pct": team,
        "factor_decomposition": factors,
        "revenue_delta_month": rev_delta,
        "quality_score_by_week": _quality_trend(manager.id, week_start),
        "top_missed_moments": _top_missed(manager.id, d_from, d_to),
        "gamification": {
            "level": prog.level if prog else 1,
            "streak_days": prog.streak_days if prog else 0,
            "streak_best": prog.streak_best if prog else 0,
        },
    }


# ---------- вызов LLM ----------

_SYSTEM = ("Ты старший РОП-аналитик отдела продаж. Отвечаешь кратко, по делу, "
           "на русском, строго JSON без markdown. Опираешься только на цифры из "
           "входных данных, не выдумываешь.")


def _prompt(payload: dict) -> str:
    import json
    return (
        "Проанализируй недельную динамику менеджера по продажам. Данные (JSON):\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=1)}\n\n"
        "Задача: определи ОДНУ главную причину просадки или роста, ОДНО действие "
        "для менеджера на неделю и ОДИН вопрос для РОПа на встречу 1:1. Без воды, "
        "суммарно ≤ 120 слов. Сравнивай конверсии менеджера с командными, смотри "
        "на вклад факторов (factor_decomposition), тренд качества и упущения.\n\n"
        "Верни СТРОГО JSON:\n"
        "{\n"
        '  "main_cause": "одна главная причина, 1-2 предложения",\n'
        '  "evidence": ["факт из цифр", "факт из цифр"],\n'
        '  "action_for_manager": "одно конкретное действие на неделю",\n'
        '  "question_for_rop": "один вопрос для 1:1",\n'
        '  "risk_level": "low|medium|high"\n'
        "}"
    )


def diagnose_one(app, manager, week_start, force=False) -> dict:
    """Собрать разбор одного менеджера и сохранить (идемпотентно по неделе)."""
    from flask import current_app
    from claude_client import claude_complete
    from settings_store import llm_provider
    from utils import extract_json
    app = app or current_app

    existing = ManagerDiagnosis.query.filter_by(
        manager_id=manager.id, week_start=week_start).first()
    if existing is not None and not force and not existing.error:
        return {"ok": True, "skip": "exists"}

    payload = build_input(manager, week_start)
    provider = llm_provider(app)

    def _store(fields):
        nonlocal existing
        if existing is None:
            existing = ManagerDiagnosis(manager_id=manager.id, week_start=week_start)
            db.session.add(existing)
        existing.provider = provider
        existing.created_at = datetime.utcnow()
        for k, v in fields.items():
            setattr(existing, k, v)
        db.session.commit()

    try:
        raw = claude_complete(_prompt(payload), system=_SYSTEM, max_tokens=600,
                              require_complete=True)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("[diagnosis] LLM m=%s: %s", manager.id, exc)
        _store({"error": f"LLM недоступен: {exc}"[:500], "main_cause": None,
                "action_for_manager": None, "question_for_rop": None,
                "evidence_json": None, "risk_level": None})
        return {"ok": False, "error": str(exc)}
    try:
        data = extract_json(raw)
    except Exception as exc:  # noqa: BLE001
        _store({"error": f"Разбор ответа не удался: {exc}"[:500]})
        return {"ok": False, "error": "parse"}

    risk = str(data.get("risk_level") or "").strip().lower()
    if risk not in _RISK:
        risk = "medium"
    ev = data.get("evidence") or []
    if isinstance(ev, str):
        ev = [ev]
    _store({
        "main_cause": (data.get("main_cause") or "").strip()[:600] or None,
        "evidence_json": [str(x).strip()[:300] for x in ev][:5],
        "action_for_manager": (data.get("action_for_manager") or "").strip()[:600] or None,
        "question_for_rop": (data.get("question_for_rop") or "").strip()[:600] or None,
        "risk_level": risk, "error": None,
    })
    return {"ok": True, "risk": risk}


def diagnose_all(app=None, week_start=None, force=False) -> dict:
    """Разбор по всем активным менеджерам (failure isolation)."""
    from flask import current_app
    app = app or current_app
    week_start = week_start or week_start_for()
    ok = fail = 0
    for m in User.query.filter_by(role="manager", is_active=True).all():
        try:
            res = diagnose_one(app, m, week_start, force=force)
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            app.logger.warning("[diagnosis] m=%s: %s", m.id, exc)
            fail += 1
            continue
        if res.get("ok"):
            ok += 1
        elif not res.get("skip"):
            fail += 1
    return {"ok": ok, "fail": fail, "week": week_start.isoformat()}


# ---------- чтение для UI ----------

def latest_diagnosis(manager_id):
    """Самый свежий разбор менеджера (для карточки/Фокуса недели)."""
    return (ManagerDiagnosis.query.filter_by(manager_id=manager_id)
            .order_by(ManagerDiagnosis.week_start.desc()).first())


def diagnosis_map(manager_ids):
    """{manager_id: самый свежий ManagerDiagnosis} для списка менеджеров."""
    out = {}
    for mid in manager_ids:
        d = latest_diagnosis(mid)
        if d is not None:
            out[mid] = d
    return out
