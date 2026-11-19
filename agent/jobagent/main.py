"""Точка входа сервиса: HTTP для вебхуков Hirify + Telegram-бот + воркер + опрос фильтров.

Запуск: uv run python -m jobagent.main
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

import anthropic
import uvicorn
from aiogram import Bot
from fastapi import FastAPI

from .bot import Notifier, make_bot, make_dispatcher
from .config import AGENT_DIR, Settings
from .db import Database
from .facts import FactFileError
from .hirify import HirifyClient
from .ingest import reconcile_forever, webhook_router
from .llm import make_client
from .package import PackageBuilder
from .package_bot import PackageRunner
from .package_store import PackageStore
from .pdf import Contacts
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


def make_package_builder(settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> PackageBuilder | None:
    """Сборщик пакетов отклика, если заданы контакты кандидата для резюме и письма; иначе None.

    Общий для сервиса и команд `cli package` / `cli package-eval`.
    """
    if not (settings.candidate_email and settings.candidate_linkedin):
        log.warning("Пакеты отклика выключены: не заданы CANDIDATE_EMAIL и CANDIDATE_LINKEDIN")
        return None
    contacts = Contacts(
        email=settings.candidate_email,
        linkedin=settings.candidate_linkedin,
        telegram=settings.candidate_telegram,
        location_line="",  # строку места и языков подставляет сборщик по языку пакета
    )
    return PackageBuilder(
        settings.knowledge_dir,
        settings.scoring_model,
        settings.package_model,
        settings.package_check_model,
        contacts,
        client=client or make_client(settings.openrouter_api_key),
    )


def make_package_runner(settings: Settings, bot: Bot | None, db: Database) -> PackageRunner | None:
    """Сборка пакетов по кнопке в боте; None — кнопки нет.

    Ошибка разметки файла опыта выключает только пакеты: отбор вакансий работает дальше.
    В журнал идёт только класс ошибки, в её тексте цитата из файла опыта. Команды `cli package`
    её не ловят: в консоли нужен полный текст, чтобы найти место в файле.
    """
    try:
        builder = make_package_builder(settings)
    except FactFileError as err:
        log.warning("Пакеты отклика выключены: ошибка разметки файла опыта (%s)", type(err).__name__)
        return None
    if builder is None:
        return None
    store = PackageStore(db, settings.packages_dir, builder.fb, builder.contacts)
    return PackageRunner(bot, db, store, builder, settings.telegram_owner_id)


def create_app(settings: Settings) -> FastAPI:
    settings.require(
        "hirify_api_key", "hirify_webhook_secret", "telegram_bot_token", "telegram_owner_id"
    )
    db = Database(settings.db_path)
    hirify = HirifyClient(settings.hirify_api_key)
    bot = make_bot(settings.telegram_bot_token)
    package_runner = make_package_runner(settings, bot, db)
    notifier = Notifier(bot, settings.telegram_owner_id, package_button=package_runner is not None)
    scorer = Scorer(
        settings.knowledge_dir,
        settings.triage_model,
        settings.scoring_model,
        settings.scoring_effort,
        client=make_client(settings.openrouter_api_key),
        server_fallback=not settings.openrouter_api_key,
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
        if stale := await db.fail_stale_packages():
            log.info("Пакетов, сборка которых оборвалась при перезапуске: %d", stale)
        dp = make_dispatcher(db, hirify, settings.telegram_owner_id, package_runner)
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
            # Сборки пакетов тоже: оборванная останется building, при следующем старте станет failed.
            tasks += list(package_runner.tasks) if package_runner else []
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


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # На DEBUG aiosqlite пишет параметры SQL-запросов, а в них текст вакансии, резюме и письма.
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
