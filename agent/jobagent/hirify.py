"""Клиент Hirify Agent REST API.

Контракт: https://api.hirify.me/api/agent/openapi.json
Ответы приходят в конверте {"ok": true, "data": ..., "meta": ...}
или {"ok": false, "error": {"code", "message", "retry_after", ...}}.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.hirify.me/api/agent"
RETRY_STATUSES = {502, 503, 504}
RETRY_DELAYS = (2.0, 5.0, 15.0)


class HirifyError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None):
        super().__init__(f"Hirify {status} {code}: {message}")
        self.status = status
        self.code = code
        self.retry_after = retry_after

    @property
    def is_limit(self) -> bool:
        return self.status == 429 or self.code in {"quota_exhausted", "rate_limited"}

    @property
    def is_transient(self) -> bool:
        """Временный сбой на стороне Hirify: стоит повторить позже."""
        return self.status >= 500 or self.code in {"external_unavailable", "internal_error"}

    @property
    def needs_person(self) -> bool:
        """Ошибки, которые решает только Александр: уведомление, доступ, ключ."""
        return self.code in {"action_required", "access_restricted", "unauthenticated", "missing_ability"}


class HirifyClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = 30.0):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self, method: str, path: str, *, retry: bool | None = None, **kwargs: Any
    ) -> tuple[Any, dict[str, Any]]:
        """Запрос с повтором при 502/503/504 и обрыве соединения.

        Один из серверов Hirify периодически отвечает 503, поэтому чтения и идемпотентные
        записи (track, hide, delivery) повторяются. Создание сущностей не повторяется.
        """
        if retry is None:
            retry = method == "GET"
        attempts = len(RETRY_DELAYS) + 1 if retry else 1
        for attempt in range(attempts):
            try:
                resp = await self._http.request(method, path, **kwargs)
            except httpx.TransportError:
                if attempt == attempts - 1:
                    raise
            else:
                if resp.status_code not in RETRY_STATUSES or attempt == attempts - 1:
                    return self._parse(resp)
            delay = RETRY_DELAYS[attempt]
            log.info("Hirify %s %s: сбой, повтор через %.0f с", method, path, delay)
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _parse(resp: httpx.Response) -> tuple[Any, dict[str, Any]]:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.is_success and body.get("ok", True):
            return body.get("data"), body.get("meta") or {}
        err = body.get("error") or {}
        retry_after = err.get("retry_after")
        if retry_after is None and resp.headers.get("Retry-After", "").isdigit():
            retry_after = int(resp.headers["Retry-After"])
        raise HirifyError(
            resp.status_code,
            err.get("code", "http_error"),
            err.get("message", resp.reason_phrase),
            retry_after,
        )

    # --- аккаунт и фильтры ---

    async def me(self) -> dict[str, Any]:
        data, _ = await self._request("GET", "/me")
        return data

    async def list_feeds(self) -> list[dict[str, Any]]:
        data, _ = await self._request("GET", "/feeds")
        return data or []

    async def feed_vacancies(
        self, feed_id: int, page: int = 1, per_page: int = 50
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        data, meta = await self._request(
            "GET", f"/feeds/{feed_id}/vacancies", params={"page": page, "per_page": per_page}
        )
        return data or [], meta

    # --- вакансии ---

    async def read_vacancy(self, vacancy_id: int) -> dict[str, Any]:
        """Полный текст вакансии. Тратит одно открытие из дневного лимита (повтор за день бесплатен)."""
        data, _ = await self._request("GET", f"/vacancies/{vacancy_id}")
        return data

    async def reveal(self, vacancy_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """Как откликнуться: ссылка, Telegram, email. Тратит одно раскрытие из дневного лимита.

        Не повторяется: при обрыве раскрытие могло засчитаться. Пример ответа (DocsInBox, 2026-10-27):
        {"contacts": [{"type": "url", "value": "https://hh.ru/vacancy/138883370"}], "linkedin": null,
        "charged": true}; в meta.quota остаток лимита.
        """
        data, meta = await self._request("POST", f"/vacancies/{vacancy_id}/reveal")
        return data or {}, meta

    async def hide_vacancy(self, slug: str) -> None:
        await self._request("POST", "/vacancies/hide", json={"slugs": [slug]}, retry=True)

    async def hide_company(self, slug: str) -> None:
        await self._request("POST", "/companies/hide", json={"slugs": [slug]}, retry=True)

    # --- трекер откликов ---

    async def track(
        self, *, slug: str | None = None, tracker_id: int | None = None, stage: str | None = None
    ) -> dict[str, Any]:
        """Записать отклик в трекер Hirify (по slug) или сменить этап (по tracker_id).

        Этапы: Applied, Viewed, No Response, HR Interview, Technical Interview,
        Test Task, Final Interview, Offer, Rejected.
        """
        body: dict[str, Any] = {}
        if tracker_id is not None:
            body["tracker_id"] = tracker_id
        elif slug is not None:
            body["slug"] = slug
        else:
            raise ValueError("Нужен slug или tracker_id")
        if stage is not None:
            body["stage"] = stage
        data, _ = await self._request("POST", "/applications/track", json=body, retry=True)
        return data or {}

    # --- вебхуки ---

    async def create_webhook(self, name: str, url: str) -> dict[str, Any]:
        data, _ = await self._request("POST", "/webhooks", json={"name": name, "url": url})
        return data

    async def list_webhooks(self) -> list[dict[str, Any]]:
        data, _ = await self._request("GET", "/webhooks")
        return data or []

    async def set_feed_webhook(self, feed_id: int, webhook_id: int | None) -> dict[str, Any]:
        data, _ = await self._request(
            "PUT", f"/feeds/{feed_id}/delivery", json={"webhook_endpoint_id": webhook_id}, retry=True
        )
        return data

    async def rotate_webhook_secret(self, webhook_id: int) -> dict[str, Any]:
        data, _ = await self._request("POST", f"/webhooks/{webhook_id}/secret", retry=True)
        return data

    async def test_webhook(self, webhook_id: int) -> dict[str, Any]:
        data, _ = await self._request("POST", f"/webhooks/{webhook_id}/test", retry=True)
        return data

    async def webhook_deliveries(self, webhook_id: int) -> Any:
        data, _ = await self._request("GET", f"/webhooks/{webhook_id}/deliveries")
        return data


def tracker_id_from(data: dict[str, Any]) -> int | None:
    """Ответ applications.track не описан в OpenAPI подробно; берём id записи трекера, если он есть."""
    for key in ("tracker_id", "id"):
        value = data.get(key)
        if isinstance(value, int):
            return value
    entry = data.get("entry") or data.get("tracker")
    if isinstance(entry, dict):
        return tracker_id_from(entry)
    return None
