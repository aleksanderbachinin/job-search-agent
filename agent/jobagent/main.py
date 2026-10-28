"""Точка входа сервиса: HTTP для вебхуков Hirify + Telegram-бот + воркер + опрос фильтров.

Запуск: uv run python -m jobagent.main
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI

from .bot import Notifier, make_bot, make_dispatcher
from .config import AGENT_DIR, Settings
from .db import Database
from .hirify import HirifyClient
from .ingest import reconcile_forever, webhook_router
from .pipeline import Pipeline
from .research import LinkedInResearch
from .scoring import Scorer

log = logging.getLogger("jobagent")

# Коммит, который выложил deploy.sh. По /healthz видно, что работает на сервере.
VERSION_FILE = AGENT_DIR / "VERSION"


def read_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except FileNotFoundError:
        return "unknown"


def create_app(settings: Settings) -> FastAPI:
    settings.require(
        "hirify_api_key", "hirify_webhook_secret", "telegram_bot_token", "telegram_owner_id"
    )
    db = Database(settings.db_path)
    hirify = HirifyClient(settings.hirify_api_key)
    bot = make_bot(settings.telegram_bot_token)
    notifier = Notifier(bot, settings.telegram_owner_id)
    scorer = Scorer(
        settings.knowledge_dir,
        settings.triage_model,
        settings.scoring_model,
        settings.scoring_effort,
    )
    research = LinkedInResearch(settings.scrapecreators_api_key) if settings.scrapecreators_api_key else None
    pipeline = Pipeline(
        db,
        hirify,
        scorer,
        notifier,
        settings.min_score,
        settings.max_age_days,
        research,
        prefilter=settings.prefilter,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.connect()
        dp = make_dispatcher(db, hirify, settings.telegram_owner_id)
        tasks = [
            asyncio.create_task(pipeline.run_forever(), name="pipeline"),
            asyncio.create_task(
                reconcile_forever(
                    db,
                    hirify,
                    settings.feed_ids,
                    settings.reconcile_pages,
                    settings.reconcile_per_page,
                    settings.reconcile_hours,
                    pipeline.wake.set,
                ),
                name="reconcile",
            ),
            asyncio.create_task(dp.start_polling(bot, handle_signals=False), name="bot"),
        ]
        log.info("Сервис запущен")
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await t
            await bot.session.close()
            await hirify.aclose()
            if research:
                await research.aclose()
            await db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(
        webhook_router(db, settings.hirify_webhook_secret, settings.webhook_tolerance_sec, pipeline.wake.set)
    )

    version = read_version()

    @app.get("/healthz")
    async def healthz() -> dict[str, bool | str]:
        return {"ok": True, "version": version}

    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
