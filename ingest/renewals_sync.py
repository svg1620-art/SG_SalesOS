"""Сбор воронки продлений («Аккаунтинг»), Этап A.

Тянем ВСЕ сделки заданной воронки (открытые + терминальные), читаем поле
«дата окончания подписки» и по этапу определяем зону 🟢/🟡/🔴 / оплатил / отвал.
Пишем в таблицу renewals (полный снимок воронки).
"""
from datetime import datetime, date

from flask import current_app

from extensions import db
from models import Renewal, User
from settings_store import amo_base_domain, amo_access_token, amo_configured
from ops_store import get_ops_setting, ops_int


def _client():
    from ingest.amo_client import AmoClient
    return AmoClient(amo_base_domain(), amo_access_token())


def _manager_map() -> dict:
    """{amo_user_id: User.id} по активным/всем пользователям с amo_user_id."""
    rows = User.query.filter(User.amo_user_id.isnot(None)).all()
    return {int(u.amo_user_id): u.id for u in rows}


def _status_index(client, pipeline_id):
    """{status_id: (name, type)} для воронки. type: 1=won(оплатил),2=lost(отвал)."""
    idx = {}
    try:
        for p in client.get_pipelines():
            if int(p.get("id") or 0) != int(pipeline_id):
                continue
            for st in (p.get("statuses") or []):
                idx[int(st.get("id"))] = (st.get("name") or "", int(st.get("type") or 0))
    except Exception:  # noqa: BLE001
        pass
    return idx


def _kw_list(key):
    return [s.strip().lower() for s in (get_ops_setting(key) or "").split(",") if s.strip()]


def _zone_for(status_name, status_type, kw):
    """Зона по этапу. Терминальные (оплатил/отказался) закрывают сделку.

    Приоритет: системный тип статуса (won/lost) → имя этапа (оплатил/отказался)
    → риск-слова (🔴/🟡) → иначе 🟢 открытая.
    """
    if status_type == 1:
        return "paid", False
    if status_type == 2:
        return "churn", False
    nm = (status_name or "").lower()
    if any(k in nm for k in kw["churn"]):
        return "churn", False
    if any(k in nm for k in kw["paid"]):
        return "paid", False
    if any(k in nm for k in kw["red"]):
        return "red", True
    if any(k in nm for k in kw["yellow"]):
        return "yellow", True
    return "green", True


def _parse_date_field(lead, field_cfg):
    """Дата окончания подписки из custom_fields_values. field_cfg — id или имя."""
    if not field_cfg:
        return None
    cfg = str(field_cfg).strip()
    by_id = cfg.isdigit()
    for cf in (lead.get("custom_fields_values") or []):
        fid = str(cf.get("field_id") or "")
        fname = (cf.get("field_name") or "").strip().lower()
        match = (fid == cfg) if by_id else (cfg.lower() in fname)
        if not match:
            continue
        vals = cf.get("values") or []
        if not vals:
            return None
        raw = vals[0].get("value")
        try:
            ts = int(raw)
            return datetime.utcfromtimestamp(ts).date()
        except (TypeError, ValueError):
            # иногда дата строкой YYYY-MM-DD
            try:
                return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
            except ValueError:
                return None
    return None


def _company_name(lead):
    comps = ((lead.get("_embedded") or {}).get("companies")) or []
    return (comps[0].get("name") if comps else None)


def _main_contact_id(lead):
    cts = ((lead.get("_embedded") or {}).get("contacts")) or []
    if not cts:
        return None
    for c in cts:
        if c.get("is_main"):
            return c.get("id")
    return cts[0].get("id")


def sync_renewals(app=None) -> dict:
    """Полный снимок воронки продлений в таблицу renewals."""
    app = app or current_app
    if not amo_configured(app):
        return {"ok": False, "error": "amoCRM не настроен"}
    pid = ops_int("renewals_pipeline_id", 0)
    if not pid:
        return {"ok": False, "error": "Не задана воронка продлений (renewals_pipeline_id)"}
    field_cfg = get_ops_setting("renewals_date_field")
    kw = {
        "paid": _kw_list("renewal_zone_paid"),
        "churn": _kw_list("renewal_zone_churn"),
        "red": _kw_list("renewal_zone_red"),
        "yellow": _kw_list("renewal_zone_yellow"),
    }

    client = _client()
    mgr_map = _manager_map()
    st_idx = _status_index(client, pid)

    seen, n, no_date = set(), 0, 0
    try:
        for lead in client.iter_leads(pipeline_id=pid, max_pages=40, order="desc"):
            lead_id = lead.get("id")
            if lead_id is None:
                continue
            status_id = int(lead.get("status_id") or 0)
            st_name, st_type = st_idx.get(status_id, ("", 0))
            zone, is_open = _zone_for(st_name, st_type, kw)
            rend = _parse_date_field(lead, field_cfg)
            if rend is None:
                no_date += 1
            resp = lead.get("responsible_user_id") or None
            upd = lead.get("updated_at")
            row = db.session.get(Renewal, int(lead_id)) or Renewal(amo_lead_id=int(lead_id))
            row.manager_id = mgr_map.get(int(resp)) if resp else None
            row.amo_responsible_id = int(resp) if resp else None
            row.name = (lead.get("name") or "")[:500]
            row.company = (_company_name(lead) or "")[:500] or None
            row.amo_contact_id = _main_contact_id(lead)
            comps = ((lead.get("_embedded") or {}).get("companies")) or []
            row.amo_company_id = comps[0].get("id") if comps else None
            row.price = int(lead.get("price") or 0)
            row.pipeline_id = int(lead.get("pipeline_id") or pid)
            row.status_id = status_id
            row.status_name = st_name[:160]
            row.zone = zone
            row.is_open = is_open
            row.renewal_end = rend
            row.amo_updated_at = datetime.utcfromtimestamp(int(upd)) if upd else None
            row.synced_at = datetime.utcnow()
            db.session.add(row)
            seen.add(int(lead_id))
            n += 1
            if n % 500 == 0:
                db.session.commit()
        db.session.commit()
        # полный снимок: убрать сделки воронки, которых больше нет
        stale = (
            Renewal.query.filter(Renewal.pipeline_id == pid, Renewal.amo_lead_id.notin_(seen))
            .all() if seen else []
        )
        removed = len(stale)
        for s in stale:
            db.session.delete(s)
        if stale:
            db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        app.logger.warning("[renewals] синк упал: %s", exc)
        return {"ok": False, "error": str(exc)[:300], "synced": n}
    return {"ok": True, "synced": n, "removed": removed,
            "without_date": no_date, "pipeline_id": pid}
