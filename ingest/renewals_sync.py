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
    """Терминальность сделки по этапу: оплатил / отказался / открыта.

    Этапы как операционный статус НЕ используем (они не поддерживаются). Берём
    только факт исхода: системный won/lost или имя терминального этапа.
    Статус-светофор открытых сделок считается по данным (дата+касания) при показе.
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
    return "open", True


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
        return _to_date(vals[0].get("value"))
    return None


def _to_date(raw):
    """Значение поля даты amoCRM → date. Поддержка unix-ts (int/str) и строк."""
    if raw is None or raw == "":
        return None
    # unix timestamp (секунды) — основной формат date-полей amoCRM
    try:
        ts = int(raw)
        if ts > 10 ** 8:  # похоже на unix-время, а не на «12»
            return datetime.utcfromtimestamp(ts).date()
    except (TypeError, ValueError):
        pass
    s = str(raw).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
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


def sample_custom_fields(app=None, limit_leads=10) -> dict:
    """Диагностика: какие доп-поля есть у сделок воронки и их значения.

    Помогает найти точный id/имя поля «дата окончания подписки».
    """
    app = app or current_app
    if not amo_configured(app):
        return {"error": "amoCRM не настроен"}
    pid = ops_int("renewals_pipeline_id", 0)
    if not pid:
        return {"error": "Не задана воронка продлений"}
    try:
        client = _client()
        fields, n = {}, 0
        for lead in client.iter_leads(pipeline_id=pid, max_pages=1, order="desc"):
            for cf in (lead.get("custom_fields_values") or []):
                fid = cf.get("field_id")
                ent = fields.setdefault(fid, {
                    "id": fid, "name": cf.get("field_name") or "",
                    "type": cf.get("field_type") or "", "examples": [], "looks_date": False,
                })
                vals = cf.get("values") or []
                raw = vals[0].get("value") if vals else None
                d = _to_date(raw)
                disp = (d.strftime("%d.%m.%Y") if d else str(raw))
                if d:
                    ent["looks_date"] = True
                if disp and disp not in ent["examples"] and len(ent["examples"]) < 3:
                    ent["examples"].append(disp)
            n += 1
            if n >= limit_leads:
                break
        rows = sorted(fields.values(), key=lambda f: (not f["looks_date"], str(f["name"])))
        return {"ok": True, "leads": n, "fields": rows}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}


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
