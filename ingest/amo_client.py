"""Клиент amoCRM (API v4) на долгосрочном токене.

Долгоживущий access-token задаётся в настройках/окружении — не требует OAuth-обмена
и обновления. Используется для опроса примечаний-звонков и скачивания записей.
"""
import httpx


class AmoError(Exception):
    pass


class AmoClient:
    def __init__(self, base_domain: str, token: str):
        self.base = f"https://{base_domain.rstrip('/')}"
        self.token = token

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def get_account(self) -> dict:
        """Проверка подключения — вернёт инфо об аккаунте (или бросит AmoError)."""
        try:
            r = httpx.get(
                f"{self.base}/api/v4/account", headers=self._headers(), timeout=30
            )
        except Exception as exc:  # noqa: BLE001
            raise AmoError(f"сеть: {exc}")
        if r.status_code == 401:
            raise AmoError("401 — неверный или истёкший токен.")
        if r.status_code != 200:
            raise AmoError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def iter_call_notes(self, entity: str, since_ts: int | None = None, max_pages: int = 50):
        """Итератор по примечаниям-звонкам (call_in/call_out) сущности.

        entity: 'contacts' | 'leads'. since_ts — фильтр по updated_at (unix).
        """
        page = 1
        while page <= max_pages:
            params = [
                ("filter[note_type][]", "call_in"),
                ("filter[note_type][]", "call_out"),
                ("order[updated_at]", "asc"),
                ("page", page),
                ("limit", 250),
            ]
            if since_ts:
                params.append(("filter[updated_at][from]", int(since_ts)))
            r = httpx.get(
                f"{self.base}/api/v4/{entity}/notes",
                headers=self._headers(), params=params, timeout=30,
            )
            if r.status_code == 204:  # нет данных
                return
            if r.status_code != 200:
                raise AmoError(f"notes HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            notes = (data.get("_embedded") or {}).get("notes") or []
            if not notes:
                return
            for note in notes:
                yield note
            if not (data.get("_links") or {}).get("next"):
                return
            page += 1

    def get_users(self, max_pages: int = 20) -> list[dict]:
        """Все пользователи amoCRM: [{id, name, email}]."""
        users, page = [], 1
        while page <= max_pages:
            r = httpx.get(
                f"{self.base}/api/v4/users",
                headers=self._headers(), params={"page": page, "limit": 250}, timeout=30,
            )
            if r.status_code == 204:
                break
            if r.status_code != 200:
                raise AmoError(f"users HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            batch = (data.get("_embedded") or {}).get("users") or []
            if not batch:
                break
            for u in batch:
                users.append({
                    "id": u.get("id"),
                    "name": u.get("name") or "",
                    "email": (u.get("email") or "").strip().lower(),
                })
            if not (data.get("_links") or {}).get("next"):
                break
            page += 1
        return users

    def iter_leads(self, since_ts: int | None = None, max_pages: int = 50):
        """Итератор по сделкам (leads), обновлённым с since_ts."""
        page = 1
        while page <= max_pages:
            params = [
                ("order[updated_at]", "asc"),
                ("page", page),
                ("limit", 250),
            ]
            if since_ts:
                params.append(("filter[updated_at][from]", int(since_ts)))
            r = httpx.get(
                f"{self.base}/api/v4/leads",
                headers=self._headers(), params=params, timeout=30,
            )
            if r.status_code == 204:
                return
            if r.status_code != 200:
                raise AmoError(f"leads HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            leads = (data.get("_embedded") or {}).get("leads") or []
            if not leads:
                return
            for lead in leads:
                yield lead
            if not (data.get("_links") or {}).get("next"):
                return
            page += 1

    def add_note(self, entity: str, entity_id: int, text: str) -> dict:
        """Добавить примечание (note_type=common) в ленту сущности amoCRM.

        entity: 'contacts' | 'leads'. Возвращает ответ API (или бросает AmoError).
        """
        payload = [{"note_type": "common", "params": {"text": text}}]
        try:
            r = httpx.post(
                f"{self.base}/api/v4/{entity}/{entity_id}/notes",
                headers=self._headers(), json=payload, timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            raise AmoError(f"сеть: {exc}")
        if r.status_code == 401:
            raise AmoError("401 — неверный или истёкший токен.")
        if r.status_code not in (200, 201):
            raise AmoError(f"note HTTP {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    def _try(self, url, headers, follow=True, proxy=None):
        try:
            with httpx.Client(proxy=proxy, timeout=30, follow_redirects=follow) as cl:
                return cl.get(url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            return exc

    def download_recording(self, url: str, proxy: str | None = None):
        """Скачать запись. Возвращает (bytes|None, diag:str).

        Порядок: без авторизации; затем с Bearer, но при 3xx-редиректе целевой
        URL тянем БЕЗ auth-заголовка (подписанные ссылки провайдера часто
        отвергают лишний Authorization). proxy — для РФ-IP (Мегафон блокирует
        иностранные адреса).
        """
        diag = []
        if proxy:
            diag.append("proxy")

        # 1) прямая ссылка без авторизации (внешний провайдер/Мегафон)
        r = self._try(url, {}, follow=True, proxy=proxy)
        if isinstance(r, Exception):
            diag.append(f"noauth:err({type(r).__name__})")
        else:
            diag.append(f"noauth:{r.status_code}")
            if r.status_code == 200 and r.content:
                return r.content, "ok(noauth)"

        # 2) с Bearer, редирект обрабатываем вручную
        r = self._try(url, self._headers(), follow=False, proxy=proxy)
        if isinstance(r, Exception):
            diag.append(f"bearer:err({type(r).__name__})")
        else:
            diag.append(f"bearer:{r.status_code}")
            if r.status_code == 200 and r.content:
                return r.content, "ok(bearer)"
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location")
                if loc:
                    r2 = self._try(loc, {}, follow=True, proxy=proxy)  # цель — без auth
                    if isinstance(r2, Exception):
                        diag.append(f"redir:err({type(r2).__name__})")
                    else:
                        diag.append(f"redir:{r2.status_code}")
                        if r2.status_code == 200 and r2.content:
                            return r2.content, "ok(redirect)"

        return None, "; ".join(diag)
