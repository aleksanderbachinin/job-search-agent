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
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request

from .db import Database
from .hirify import HirifyClient, HirifyError

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/hirify/webhook"


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

    @router.post(WEBHOOK_PATH)
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


async def ensure_webhook_active(db: Database, hirify: HirifyClient) -> list[int]:
    """Включает обратно наш вебхук, если Hirify его отключил. Возвращает id включённых.

    После серии неудачных доставок (сервис лежал, адрес отдавал 502) Hirify молча
    переводит вебхук в disabled, привязка к фильтрам остаётся. Так было 4 ноября 2026:
    после двух суток простоя вакансии пять дней шли только через опрос фильтров.
    Наш вебхук узнаём по пути адреса. Чужие вебхуки аккаунта не трогаем.
    """
    reactivated = []
    for hook in await hirify.list_webhooks():
        if urlparse(hook.get("url") or "").path != WEBHOOK_PATH:
            continue
        if hook.get("is_active") and hook.get("state", "active") == "active":
            continue
        updated = await hirify.update_webhook(hook["id"], is_active=True)
        if updated.get("state", "active") != "active":
            log.error("Вебхук Hirify %s не включился: state=%s", hook["id"], updated.get("state"))
            continue
        await db.log_event("webhook_reactivated", webhook_id=hook["id"], state=hook.get("state"))
        log.warning("Вебхук Hirify %s был отключён (state=%s), включил обратно", hook["id"], hook.get("state"))
        reactivated.append(hook["id"])
    return reactivated


async def check_webhook(db: Database, hirify: HirifyClient) -> None:
    """Проверка — подстраховка: любой её сбой не должен останавливать опрос фильтров."""
    try:
        await ensure_webhook_active(db, hirify)
    except (HirifyError, httpx.TransportError) as e:
        log.warning("Проверка вебхука Hirify не удалась: %s", e)
    except Exception:
        log.exception("Проверка вебхука Hirify упала")


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

    # При старте: после простоя вебхук мог быть отключён, ждать первого опроса полсуток незачем.
    await check_webhook(db, hirify)
    while True:
        await asyncio.sleep(every_hours * 3600)
        await check_webhook(db, hirify)
        try:
            n = await sync_feeds(db, hirify, feed_ids, pages, per_page, status="queued", source="reconcile")
            await db.log_event("reconcile", added=n)
            if n:
                log.info("Опрос фильтров добавил %d вакансий", n)
                on_new()
        except (HirifyError, httpx.TransportError) as e:
            log.warning("Опрос фильтров не удался: %s", e)
