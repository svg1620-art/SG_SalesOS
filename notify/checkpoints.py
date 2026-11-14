"""Чекпоинты РОПу и личные сообщения менеджерам (Этап 6b, §10.1–10.2).

РОП (в общий чат TELEGRAM_CHAT_IDS):
- 11:00 — кто не начал день + аномально низкий connect rate;
- 15:00 — светофор команды (🟢/🟡/🔴) + список красных с причиной.
Итог 19:00 встроен в существующий дневной пульс (ops-блок в notify/telegram.py).

Менеджеру (личный чат, с учётом тихих часов и notify_enabled):
- 09:30 — миссия дня (3 цели);
- 16:00 — финишная прямая, если до следующего тира ≤ порога.

Идемпотентность в пределах дня — через ops_settings (ключ с датой).
"""
from datetime import date
from html import escape

from extensions import db
from models import User
from notify.bot import send_to_chat, notify_manager


_ZONE_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def _rop_chats(app):
    from settings_store import telegram_chat_ids
    return telegram_chat_ids(app)


def _send_rop(app, text) -> bool:
    ok = False
    for cid in _rop_chats(app):
        ok = send_to_chat(app, cid, text) or ok
    return ok


def _once_guard(key: str) -> bool:
    """True, если сегодня это ещё не отправляли (и помечает отправленным)."""
    from ops_store import get_ops_setting, set_ops_setting
    today = date.today().isoformat()
    if get_ops_setting(key) == today:
        return False
    set_ops_setting(key, today)
    return True


# ---------- РОП: 11:00 ----------

def checkpoint_morning(app=None, day=None, force=False) -> bool:
    from flask import current_app
    from ops.board import board_rows
    from ops_store import ops_int
    app = app or current_app
    if not force and not _once_guard("tg_cp_morning_sent"):
        return False
    rows = board_rows(today=day)
    if not rows:
        return False
    not_started = [r for r in rows if not r["first_action_label"]]
    # аномально низкий connect rate: есть дозвоны, но cr < порога
    cr_min = ops_int("board_connect_rate_min_pct", 60)
    low_cr = [r for r in rows
              if r["calls_out"] >= 5 and (r["connect_rate_pct"] or 0) < cr_min]
    lines = ["☀️ <b>Чекпоинт 11:00</b>"]
    if not_started:
        names = ", ".join(escape(r["name"]) for r in not_started)
        lines.append(f"\n⚠️ <b>Не начали день:</b> {names}")
    else:
        lines.append("\n✅ Все менеджеры уже в работе.")
    if low_cr:
        lines.append("\n📉 <b>Низкий connect rate:</b>")
        for r in low_cr:
            lines.append(f"\n   {escape(r['name'])} — {r['connect_rate_pct']}% "
                         f"({r['calls_connected']}/{r['calls_out']})")
    return _send_rop(app, "".join(lines))


# ---------- РОП: 15:00 ----------

def checkpoint_afternoon(app=None, day=None, force=False) -> bool:
    from flask import current_app
    from ops.board import board_rows
    app = app or current_app
    if not force and not _once_guard("tg_cp_afternoon_sent"):
        return False
    rows = board_rows(today=day)
    if not rows:
        return False
    cnt = {"green": 0, "yellow": 0, "red": 0}
    for r in rows:
        cnt[r["status"]] = cnt.get(r["status"], 0) + 1
    lines = [f"🕒 <b>Чекпоинт 15:00</b>\n\nСветофор команды: "
             f"🟢 {cnt['green']} · 🟡 {cnt['yellow']} · 🔴 {cnt['red']}"]
    reds = [r for r in rows if r["status"] == "red"]
    if reds:
        lines.append("\n\n🔴 <b>Красная зона:</b>")
        for r in reds:
            reason = _red_reason(r)
            lines.append(f"\n   {escape(r['name'])} — {reason}")
    return _send_rop(app, "".join(lines))


def _red_reason(r) -> str:
    bits = []
    if not r["first_action_label"]:
        bits.append("не начал день")
    if r["norm_calls"] and (r["calls_pct_of_norm"] or 0) < 50:
        bits.append(f"дозвоны {r['calls_connected']}/{r['norm_calls']}")
    if r["connect_rate_pct"] is not None and r["connect_rate_pct"] < 40:
        bits.append(f"connect {r['connect_rate_pct']}%")
    if r["minutes_since_last"] and r["minutes_since_last"] > 120:
        bits.append(f"тишина {r['last_action_label']}")
    return ", ".join(bits) or "низкая активность"


# ---------- Менеджеру: 09:30 миссия ----------

def send_manager_missions(app=None, day=None, force=False) -> dict:
    from flask import current_app
    from datetime import date as _date
    from ops.missions import (generate_mission, LEVEL_METRICS, LEVEL_NAMES,
                              METRIC_LABELS)
    from ops.metrics import _is_working_day
    app = app or current_app
    day = day or _date.today()
    if not _is_working_day(day):
        return {"sent": 0, "skip": "weekend"}
    if not force and not _once_guard("tg_mission_sent"):
        return {"sent": 0, "skip": "already"}
    sent = 0
    for m in User.query.filter_by(role="manager", is_active=True).all():
        if not m.telegram_chat_id or not m.notify_enabled:
            continue
        try:
            mission = generate_mission(m, day)
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            app.logger.warning("[tg-mission] m=%s: %s", m.id, exc)
            continue
        metrics = LEVEL_METRICS.get(mission.level, LEVEL_METRICS[1])
        targets = mission.targets or {}
        goals = []
        for mk in metrics:
            t = targets.get(mk, {})
            silver = t.get("silver") or t.get("bronze") or 0
            goals.append(f"• {METRIC_LABELS.get(mk, mk)}: {silver}")
        text = (f"🎯 <b>Миссия на сегодня</b> "
                f"(ур.{mission.level} · {LEVEL_NAMES.get(mission.level, '')})\n\n"
                + "\n".join(goals) +
                "\n\nВзять серебро — норма дня. Золото — личный максимум 💪")
        if notify_manager(app, m, text):
            sent += 1
    return {"sent": sent}


# ---------- Менеджеру: 16:00 финишная прямая ----------

def send_finish_line(app=None, day=None, force=False) -> dict:
    from flask import current_app
    from datetime import date as _date
    from ops.missions import (evaluate_mission, LEVEL_METRICS, METRIC_LABELS, RANK)
    from ops.metrics import _is_working_day
    from ops_store import ops_int
    app = app or current_app
    day = day or _date.today()
    if not _is_working_day(day):
        return {"sent": 0, "skip": "weekend"}
    if not force and not _once_guard("tg_finish_sent"):
        return {"sent": 0, "skip": "already"}
    within = ops_int("tg_finish_within_pct", 25)
    sent = 0
    for m in User.query.filter_by(role="manager", is_active=True).all():
        if not m.telegram_chat_id or not m.notify_enabled:
            continue
        try:
            mission = evaluate_mission(m, day, finalize=False)
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            app.logger.warning("[tg-finish] m=%s: %s", m.id, exc)
            continue
        metrics = LEVEL_METRICS.get(mission.level, LEVEL_METRICS[1])
        targets = mission.targets or {}
        results = mission.results or {}
        # ближайшая невзятая цель следующего тира, до которой осталось ≤ within%
        hints = []
        for mk in metrics:
            t = targets.get(mk, {})
            val = int(results.get(mk, 0))
            for tier in ("gold", "silver", "bronze"):
                goal = int(t.get(tier, 0) or 0)
                if goal and val < goal:
                    need = goal - val
                    if need <= max(1, round(goal * within / 100)):
                        hints.append(f"• {METRIC_LABELS.get(mk, mk)}: ещё {need} до "
                                     f"{_tier_ru(tier)}")
                    break
        if not hints:
            continue
        text = ("🏁 <b>Финишная прямая</b>\nСовсем чуть-чуть до цели:\n\n"
                + "\n".join(hints))
        if notify_manager(app, m, text):
            sent += 1
    return {"sent": sent}


def _tier_ru(tier):
    return {"gold": "🥇 золота", "silver": "🥈 серебра", "bronze": "🥉 бронзы"}.get(tier, tier)
