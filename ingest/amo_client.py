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

    def download_recording(self, url: str) -> bytes | None:
        """Скачать запись. Пробуем без авторизации (внешняя ссылка Мегафона),
        затем с Bearer (если ссылка на amoCRM)."""
        for headers in ({}, self._headers()):
            try:
                r = httpx.get(url, headers=headers, timeout=90, follow_redirects=True)
                if r.status_code == 200 and r.content:
                    return r.content
            except Exception:  # noqa: BLE001
                continue
        return None
