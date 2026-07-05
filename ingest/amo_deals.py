"""Опрос успешных сделок amoCRM + геймификация (XP, поздравления в Telegram).

Успешная сделка в amoCRM — системный статус 142 («Успешно реализовано»), общий
для всех воронок. Тянем такие сделки, привязываем к менеджеру по
User.amo_user_id ↔ responsible_user_id, храним сумму (price) и дату закрытия.

XP: +50 за каждые 50 000 руб выручки менеджера (накопительно). При начислении
XP шлём поздравление в бота.
"""
from datetime import datetime, timedelta
from html import escape

from flask import current_app

from extensions import db
from models import Deal, User
from settings_store import (
    amo_base_domain, amo_access_token, amo_configured, get_setting, set_setting,
)
from ingest.amo_client import AmoClient, AmoError

WON_STATUS_ID = 142  # «Успешно реализовано» (системный статус amoCRM)
XP_STEP_RUB = 50000
XP_PER_STEP = 50
_FIRST_RUN_DAYS = 45  # на первом опросе — сделки за последние N дней


def manager_revenue(manager_id: int) -> int:
    """Суммарная выручка менеджера по всем успешным сделкам (руб)."""
    total = 0
    for d in Deal.query.filter_by(manager_id=manager_id).all():
        total += d.price or 0
    return total


def xp_for_revenue(revenue: int) -> int:
    return (revenue // XP_STEP_RUB) * XP_PER_STEP


def manager_revenue_in_month(manager_id: int, year: int, month: int) -> int:
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    total = 0
    for d in Deal.query.filter(
        Deal.manager_id == manager_id, Deal.won_at >= start, Deal.won_at < end
    ).all():
        total += d.price or 0
    return total


def _fmt_money(rub: int) -> str:
    return f"{rub:,}".replace(",", " ")


def _send_congrats(app, manager, price: int, xp_gain: int) -> None:
    name = manager.full_name or manager.email
    text = (
        f"🔥 <b>{escape(name)}</b>  ✅ {_fmt_money(price)} руб  "
        f"+{xp_gain} Xp 🚀 Поздравляем!"
    )
    try:
        from notify.telegram import _send_message
        _send_message(app, text)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("[deals] не удалось отправить поздравление: %s", exc)


def poll_deals(app=None) -> dict:
    """Опросить успешные сделки, завести новые, начислить XP и поздравить."""
    app = app or current_app
    if not amo_configured(app):
        return {"ok": False, "error": "amoCRM не настроен", "new": 0}

    client = AmoClient(amo_base_domain(app), amo_access_token(app))
    since = get_setting("amo_deals_last_sync")
    since_ts = int(since) if since and str(since).isdigit() else None
    if since_ts is None:
        since_ts = int((datetime.utcnow() - timedelta(days=_FIRST_RUN_DAYS)).timestamp())

    new_count, max_updated = 0, since_ts or 0
    try:
        leads = list(client.iter_leads(since_ts))
    except AmoError as exc:
        app.logger.warning("[deals] опрос не удался: %s", exc)
        return {"ok": False, "error": str(exc), "new": 0}

    for lead in leads:
        try:
            updated = int(lead.get("updated_at") or 0)
            max_updated = max(max_updated, updated)
            if lead.get("status_id") != WON_STATUS_ID:
                continue  # только выигранные
            lead_id = lead.get("id")
            if lead_id and Deal.query.filter_by(amo_lead_id=lead_id).first():
                continue  # уже учтена

            price = int(lead.get("price") or 0)
            responsible = lead.get("responsible_user_id")
            manager = (
                User.query.filter_by(amo_user_id=responsible).first()
                if responsible else None
            )
            won_ts = int(lead.get("closed_at") or updated or 0)
            won_at = datetime.utcfromtimestamp(won_ts) if won_ts else datetime.utcnow()

            deal = Deal(
                amo_lead_id=lead_id,
                manager_id=manager.id if manager else None,
                price=price,
                name=(lead.get("name") or "")[:500],
                pipeline_id=lead.get("pipeline_id"),
                status_id=WON_STATUS_ID,
                won_at=won_at,
            )
            db.session.add(deal)
            db.session.commit()
            new_count += 1

            # XP и поздравление — по выручке за месяц закрытия сделки
            # (лидерборд считается помесячно, поздравление ему соответствует)
            if manager and price > 0:
                total_after = manager_revenue_in_month(
                    manager.id, won_at.year, won_at.month
                )
                total_before = total_after - price
                xp_gain = xp_for_revenue(total_after) - xp_for_revenue(total_before)
                if xp_gain > 0:
                    _send_congrats(app, manager, price, xp_gain)
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            app.logger.warning("[deals] сделка %s пропущена: %s", lead.get("id"), exc)

    if max_updated:
        set_setting("amo_deals_last_sync", max_updated)
    app.logger.info("[deals] опрос завершён: новых успешных сделок %s", new_count)
    return {"ok": True, "new": new_count, "last_sync": max_updated}
