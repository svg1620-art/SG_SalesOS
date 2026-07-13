"""Опрос успешных сделок amoCRM + геймификация (XP, поздравления в Telegram).

Успешная сделка в amoCRM — системный статус 142 («Успешно реализовано»), общий
для всех воронок. Тянем такие сделки, привязываем к менеджеру по
User.amo_user_id ↔ responsible_user_id, храним сумму (price) и дату закрытия.

Месяц выручки определяется по `closed_at` — дате перемещения сделки в статус
«успешно» (а НЕ по дате последнего изменения). Так сделка попадает ровно в тот
месяц, когда была закрыта.

XP: +50 за каждые 50 000 руб выручки менеджера за месяц (накопительно в рамках
месяца). Поздравление шлётся только за реально свежие закрытия — при первичной
загрузке (бэкфилле) истории поздравления НЕ отправляются, чтобы не спамить.
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

WON_STATUS_ID = 142   # «Успешно реализовано» (системный статус amoCRM)
LOST_STATUS_ID = 143  # «Закрыто и не реализовано»
XP_STEP_RUB = 50000
XP_PER_STEP = 50
_FIRST_RUN_DAYS = 120  # на первом опросе — сделки, обновлённые за последние N дней
# поздравляем только за закрытия не старше N дней (защита от старых сделок,
# которые «всплыли» из-за постороннего редактирования)
_CONGRATS_MAX_AGE_DAYS = 2
# страховочный лимит поздравлений за один прогон (от лавины при сбоях)
_MAX_CONGRATS_PER_RUN = 12


def manager_revenue(manager_id: int) -> int:
    """Суммарная выручка менеджера по всем ВЫИГРАННЫМ сделкам (руб)."""
    total = 0
    for d in Deal.query.filter_by(manager_id=manager_id, outcome="won").all():
        total += d.price or 0
    return total


def xp_for_revenue(revenue: int) -> int:
    return (revenue // XP_STEP_RUB) * XP_PER_STEP


def manager_revenue_in_month(manager_id: int, year: int, month: int) -> int:
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    total = 0
    for d in Deal.query.filter(
        Deal.manager_id == manager_id, Deal.outcome == "won",
        Deal.won_at >= start, Deal.won_at < end,
    ).all():
        total += d.price or 0
    return total


def _main_contact_id(lead) -> int | None:
    """ID основного контакта сделки из _embedded.contacts."""
    contacts = ((lead.get("_embedded") or {}).get("contacts")) or []
    if not contacts:
        return None
    for c in contacts:
        if c.get("is_main"):
            return c.get("id")
    return contacts[0].get("id")


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


def poll_deals(app=None, congratulate=None) -> dict:
    """Опросить успешные сделки, завести новые, начислить XP и поздравить.

    congratulate=None → авто: на первом (бэкфилл) прогоне не поздравляем, дальше
    поздравляем. Явное True/False переопределяет.
    """
    app = app or current_app
    if not amo_configured(app):
        return {"ok": False, "error": "amoCRM не настроен", "new": 0}

    client = AmoClient(amo_base_domain(app), amo_access_token(app))
    since = get_setting("amo_deals_last_sync")
    first_run = not (since and str(since).isdigit())
    since_ts = None if first_run else int(since)
    if since_ts is None:
        since_ts = int((datetime.utcnow() - timedelta(days=_FIRST_RUN_DAYS)).timestamp())
    # на бэкфилле истории не поздравляем — только импортируем
    if congratulate is None:
        congratulate = not first_run

    from settings_store import leaderboard_pipeline_id
    target_pipeline = leaderboard_pipeline_id(app)  # None → все воронки

    congrats_after = datetime.utcnow() - timedelta(days=_CONGRATS_MAX_AGE_DAYS)
    new_count, congrats_sent, removed_count, max_updated = 0, 0, 0, since_ts or 0
    try:
        leads = list(client.iter_leads(since_ts))
    except AmoError as exc:
        app.logger.warning("[deals] опрос не удался: %s", exc)
        return {"ok": False, "error": str(exc), "new": 0}

    for lead in leads:
        try:
            updated = int(lead.get("updated_at") or 0)
            max_updated = max(max_updated, updated)
            lead_id = lead.get("id")
            status = int(lead.get("status_id") or 0)
            closed_ts = int(lead.get("closed_at") or 0)
            outcome = (
                "won" if status == WON_STATUS_ID
                else "lost" if status == LOST_STATUS_ID
                else None
            )
            # воронка: если задана целевая — считаем только её сделки
            in_pipeline = (
                target_pipeline is None
                or int(lead.get("pipeline_id") or 0) == target_pipeline
            )
            # учитываем закрытые сделки (выигранные/проигранные) с датой закрытия
            if outcome is None or not closed_ts or not in_pipeline:
                # самоочистка: сделка снова открыта / чужая воронка — убираем
                if lead_id:
                    stale = Deal.query.filter_by(amo_lead_id=lead_id).first()
                    if stale is not None:
                        db.session.delete(stale)
                        db.session.commit()
                        removed_count += 1
                continue
            existing = Deal.query.filter_by(amo_lead_id=lead_id).first() if lead_id else None
            if existing is not None:
                # исход мог измениться (выиграли/проиграли заново) — обновим
                if existing.outcome != outcome:
                    existing.outcome = outcome
                    existing.status_id = status
                    existing.won_at = datetime.utcfromtimestamp(closed_ts)
                    db.session.commit()
                continue

            price = int(lead.get("price") or 0)
            responsible = lead.get("responsible_user_id")
            manager = (
                User.query.filter_by(amo_user_id=responsible).first()
                if responsible else None
            )
            won_at = datetime.utcfromtimestamp(closed_ts)  # дата закрытия

            deal = Deal(
                amo_lead_id=lead_id,
                manager_id=manager.id if manager else None,
                amo_contact_id=_main_contact_id(lead),
                price=price,
                name=(lead.get("name") or "")[:500],
                pipeline_id=lead.get("pipeline_id"),
                status_id=status,
                outcome=outcome,
                won_at=won_at,
            )
            db.session.add(deal)
            db.session.commit()
            new_count += 1

            # XP и поздравление — только за свежие ВЫИГРАННЫЕ сделки
            eligible = (
                outcome == "won"
                and congratulate and manager and price > 0
                and won_at >= congrats_after
                and congrats_sent < _MAX_CONGRATS_PER_RUN
            )
            if eligible:
                total_after = manager_revenue_in_month(
                    manager.id, won_at.year, won_at.month
                )
                total_before = total_after - price
                xp_gain = xp_for_revenue(total_after) - xp_for_revenue(total_before)
                if xp_gain > 0:
                    _send_congrats(app, manager, price, xp_gain)
                    congrats_sent += 1
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            app.logger.warning("[deals] сделка %s пропущена: %s", lead.get("id"), exc)

    if max_updated:
        set_setting("amo_deals_last_sync", max_updated)
    app.logger.info(
        "[deals] опрос завершён: новых %s, удалено (уехали из «оплата получена») %s, "
        "поздравлений %s (backfill=%s)",
        new_count, removed_count, congrats_sent, first_run,
    )
    return {
        "ok": True, "new": new_count, "removed": removed_count,
        "congrats": congrats_sent, "backfill": first_run, "last_sync": max_updated,
    }


def resync_deals(app=None) -> dict:
    """Полностью пересобрать сделки: удалить все, сбросить курсор, загрузить
    заново по дате закрытия — БЕЗ поздравлений. Чинит неверные месяцы/спам.

    Безопасно: перед удалением проверяем связь с amoCRM (get_account). Если
    amoCRM недоступен — НЕ удаляем, чтобы не остаться с пустым лидербордом.
    """
    app = app or current_app
    if not amo_configured(app):
        return {"ok": False, "error": "amoCRM не настроен."}

    client = AmoClient(amo_base_domain(app), amo_access_token(app))
    try:
        client.get_account()  # проверка связи ДО удаления
    except AmoError as exc:
        app.logger.warning("[deals] пересбор отменён — amoCRM недоступен: %s", exc)
        return {"ok": False, "error": f"amoCRM недоступен, данные не тронуты: {exc}"}

    deleted = Deal.query.delete()
    set_setting("amo_deals_last_sync", "")
    db.session.commit()
    app.logger.info("[deals] пересинхронизация: удалено %s сделок", deleted)
    result = poll_deals(app, congratulate=False)
    result["deleted"] = deleted
    return result
