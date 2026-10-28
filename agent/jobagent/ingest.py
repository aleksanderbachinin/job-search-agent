"""Приём вакансий: вебхук Hirify и страховочный опрос первых страниц фильтров."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

from .db import Database
from .hirify import HirifyClient, HirifyError

log = logging.getLogger(__name__)


def verify_signature(
    secret: str, timestamp: str, body: bytes, signature: str, tolerance_sec: int, now: float | None = None
) -> bool:
    """HMAC-SHA256 над "<timestamp>.<raw body>", заголовок вида sha256=<hex>."""
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > tolerance_sec:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


def webhook_router(
    db: Database, secret: str, tolerance_sec: int, on_new: Callable[[], None]
) -> APIRouter:
    router = APIRouter()

    @router.post("/hirify/webhook")
    async def hirify_webhook(request: Request) -> dict[str, Any]:
        body = await request.body()
        h = request.headers
        if not verify_signature(
            secret,
            h.get("X-Hirify-Timestamp", ""),
            body,
            h.get("X-Hirify-Signature", ""),
            tolerance_sec,
        ):
            raise HTTPException(status_code=401, detail="bad signature")

        payload = json.loads(body)
        event = h.get("X-Hirify-Event") or payload.get("event", "")
        delivery_id = h.get("X-Hirify-Delivery") or payload.get("delivery_id", "")
        if not await db.remember_delivery(delivery_id, event):
            return {"ok": True, "duplicate": True}

        if event == "webhook.test":
            await db.log_event("webhook_test", endpoint=payload.get("endpoint"))
            log.info("Получен тестовый вебхук")
        elif event == "vacancy.matched":
            card = payload["vacancy"]
            feeds = payload.get("feeds") or []
            if await db.upsert_card(card, feeds, source="webhook"):
                on_new()
        else:
            log.info("Неизвестное событие Hirify: %s", event)
        return {"ok": True}

    return router


async def resolve_feed_ids(hirify: HirifyClient, configured: list[int]) -> list[dict[str, Any]]:
    feeds = await hirify.list_feeds()
    if configured:
        return [f for f in feeds if f["id"] in configured]
    return [f for f in feeds if f.get("is_active")]


async def sync_feeds(
    db: Database,
    hirify: HirifyClient,
    feed_ids: list[int],
    pages: int,
    per_page: int,
    status: str,
    source: str,
) -> int:
    """Читает первые `pages` страниц каждого фильтра. Возвращает число новых вакансий.

    Весь фильтр не листаем: Hirify считает массовое листание без открытий копированием каталога.
    """
    added = 0
    for feed in await resolve_feed_ids(hirify, feed_ids):
        ref = [{"id": feed["id"], "name": feed.get("name")}]
        for page in range(1, pages + 1):
            cards, meta = await hirify.feed_vacancies(feed["id"], page=page, per_page=per_page)
            for card in cards:
                if await db.upsert_card(card, ref, source=source, status=status):
                    added += 1
            if page >= (meta.get("last_page") or page):
                break
    return added


async def baseline_if_empty(db: Database, hirify: HirifyClient, feed_ids: list[int], pages: int, per_page: int) -> None:
    """При первом запуске помечаем текущее содержимое фильтров как «уже было».

    Признак завершения — событие baseline: если разметка оборвалась на середине,
    при следующем запуске она пройдёт заново (уже размеченное повторно не добавится).
    """
    if await db.has_event("baseline"):
        return
    n = await sync_feeds(db, hirify, feed_ids, pages, per_page, status="baseline", source="baseline")
    await db.log_event("baseline", count=n)
    log.info("Первый запуск: %d вакансий помечены как уже виденные", n)


async def reconcile_forever(
    db: Database,
    hirify: HirifyClient,
    feed_ids: list[int],
    pages: int,
    per_page: int,
    every_hours: float,
    on_new: Callable[[], None],
) -> None:
    """Сначала первичная разметка (повторяется, пока Hirify не ответит), затем регулярный опрос.

    Работает в фоне: сбои Hirify не мешают принимать вебхуки и отвечать в боте.
    """
    delay = 30.0
    while True:
        try:
            await baseline_if_empty(db, hirify, feed_ids, pages, per_page)
            break
        except (HirifyError, httpx.TransportError) as e:
            log.warning("Первичная разметка не удалась (%s), повтор через %.0f с", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 1800)

    while True:
        await asyncio.sleep(every_hours * 3600)
        try:
            n = await sync_feeds(db, hirify, feed_ids, pages, per_page, status="queued", source="reconcile")
            await db.log_event("reconcile", added=n)
            if n:
                log.info("Опрос фильтров добавил %d вакансий", n)
                on_new()
        except (HirifyError, httpx.TransportError) as e:
            log.warning("Опрос фильтров не удался: %s", e)
