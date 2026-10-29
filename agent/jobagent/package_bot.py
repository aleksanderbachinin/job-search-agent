"""Пакет отклика в боте: кнопки «📝 Подготовить отклик» и «✏️ Переделать».

Сборка идёт в фоне, одна на весь сервис. Результат приходит ответом на карточку вакансии:
PDF документом, письмо отдельным сообщением, сводка проверки, если есть что сказать.
В логи и события идут только коды, номера версий и длины: ни текста вакансии, ни резюме,
ни письма, ни правки Александра, ни предупреждений. Сводка в Telegram — приватный канал
Александра: в ней место по-человечески и подробности нарушений.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict

import anthropic
from aiogram import Bot, F, Router
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ForceReply,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .db import Database, Vacancy
from .package import BuiltPackage, PackageBuilder
from .package_store import PackageStore
from .package_rules import EMPLOYER_NAMES
from .pdf import PackageError
from .scoring import Usage

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096

STARTED = "Собираю пакет, это займёт пару минут"
BUSY = "Уже собираю"
BUSY_OTHER = "Собираю пакет для другой вакансии, нажми после него"
NOT_SHOWN = "Пакет собираю только для новых карточек"
NO_DETAIL = "Не соберу: нет полного текста вакансии"


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Режет длинный текст на части не длиннее limit: по абзацам, строкам, пробелам. Ничего не теряет."""
    text = text.strip()
    parts = []
    while len(text) > limit:
        cut = next((i for sep in ("\n\n", "\n", " ") if (i := text.rfind(sep, 0, limit)) > 0), limit)
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


# Коды, у которых detail говорит, что именно не так: число, название, шаблон, причина.
_DETAIL_CODES = frozenset({"number", "term", "unknown_term", "forbidden", "semantic"})
_PATH = re.compile(r"(?P<part>jobs|summary|skills|letter)\[(?P<i>\d+)\](?:\.bullets\[(?P<j>\d+)\])?$")
_LETTER_PARTS = {"letter.subject": "письмо, тема", "letter.closing": "письмо, финал", "letter": "письмо"}


def _where(path: str, built: BuiltPackage) -> str:
    """Место нарушения по-человечески: «MPSTATS, пункт 2», «summary, фраза 1», «письмо, финал»."""
    if path in _LETTER_PARTS:
        return _LETTER_PARTS[path]
    m = _PATH.match(path)
    if m is None:
        return path
    i = int(m["i"])
    if m["part"] == "summary":
        return f"summary, фраза {i + 1}"
    if m["part"] == "skills":
        return f"навыки, пункт {i + 1}"
    if m["part"] == "letter":
        return f"письмо, предложение {i + 1}"
    # Пути jobs[i] — по черновику до удалений; у пакета, сохранённого раньше, — по итоговому резюме.
    if i < len(built.job_employers):
        code = built.job_employers[i]
    elif i < len(built.resume.jobs):
        code = built.resume.jobs[i].employer
    else:
        return path
    name = EMPLOYER_NAMES.get(code, code)
    return f"{name}, пункт {int(m['j']) + 1}" if m["j"] is not None else name


def render_summary(built: BuiltPackage) -> str:
    """Сводка проверки для Александра: что убрано, что проверить, что сокращено под страницу.

    Только для Telegram и консоли: в логи и события сводка не идёт."""
    lines = [
        f"убрал: {_where(x.path, built)} ({x.code}"
        + (f": {x.detail}" if x.code in _DETAIL_CODES and x.detail else "") + ")"
        for x in built.removed
    ]
    lines += built.warnings
    if built.cut_steps:
        lines.append("сократил под страницу: " + ", ".join(built.cut_steps))
    return "\n".join(lines)


def kb_redo(v: Vacancy, version: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Переделать", callback_data=f"rd:{v.vacancy_id}:{version}")
    return b.as_markup()


class PackageRunner:
    """Запускает сборку пакета в фоне и присылает результат. Одна сборка за раз на весь сервис."""

    def __init__(self, bot: Bot, db: Database, store: PackageStore, builder: PackageBuilder, owner_id: int):
        self.bot = bot
        self.db = db
        self.store = store
        self.builder = builder
        self.owner_id = owner_id
        self.lock = asyncio.Lock()
        self.building: str | None = None  # slug вакансии, пакет которой сейчас собирается
        # message_id запроса правки (ForceReply) → (slug, версия). В памяти: перезапуск его забывает.
        self.pending_redo: dict[int, tuple[str, int]] = {}
        self.tasks: set[asyncio.Task] = set()  # сильные ссылки, чтобы задачу не собрал сборщик мусора

    async def request(self, v: Vacancy, feedback: str | None = None, base_version: int | None = None) -> str:
        """Ставит сборку; возвращает ответ для cb.answer."""
        if v.status != "shown":
            return NOT_SHOWN
        if not v.detail:
            return NO_DETAIL
        # Проверка и захват без await между ними: два быстрых нажатия дают одну сборку.
        if self.lock.locked():
            return BUSY if self.building == v.slug else BUSY_OTHER
        await self.lock.acquire()  # свободный замок берётся сразу, без переключения задач
        self.building = v.slug
        try:
            version = await self.store.start(v.slug)
            if feedback is None:
                await self.db.log_event("package_requested", v.slug, version=version)
            else:
                await self.db.log_event(
                    "package_redo", v.slug, version=version, base_version=base_version, feedback_len=len(feedback)
                )
        except BaseException:
            self.building = None
            self.lock.release()
            raise
        task = asyncio.create_task(self._run(v, version, feedback, base_version), name=f"package:{v.slug}")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return STARTED

    async def _run(self, v: Vacancy, version: int, feedback: str | None, base_version: int | None) -> None:
        try:
            if built := await self._build(v, version, feedback, base_version):
                try:
                    await self._send(v, version, built)
                except Exception as err:  # пакет сохранён, но до Александра не дошёл
                    log.warning("пакет %s v%d: не отправлен (%s)", v.slug, version, type(err).__name__)
                    await self._notify_unsent(v, version, err)
        except Exception as err:  # фоновая задача не должна падать молча и не пишет текст ошибки
            log.warning("пакет %s v%d: сбой (%s)", v.slug, version, type(err).__name__)
        finally:
            self.building = None
            self.lock.release()

    async def _notify_unsent(self, v: Vacancy, version: int, err: Exception) -> None:
        """Простое сообщение без ответа на карточку: отправка ответом могла и сломаться."""
        text = (
            f"Пакет v{version} собран, но не отправлен: {type(err).__name__}. "
            "Нажми «Подготовить отклик» ещё раз или возьми файлы через `cli`."
        )
        try:
            await self.bot.send_message(self.owner_id, text, parse_mode=None)
        except Exception as notify_err:
            log.warning("пакет %s v%d: не отправил и сообщение о сбое (%s)", v.slug, version, type(notify_err).__name__)

    async def _build(
        self, v: Vacancy, version: int, feedback: str | None, base_version: int | None
    ) -> BuiltPackage | None:
        """Сборка и сохранение; при сбое — package_failed и сообщение с кодом, возвращает None."""
        previous = None
        if base_version is not None:
            try:
                previous = await self.store.load(v.slug, base_version)
            except Exception as err:
                log.warning("пакет %s v%d: не прочитал v%d (%s)", v.slug, version, base_version, type(err).__name__)
                await self._fail(v, version, "load_error")
                return None
        try:
            built, usages = await self.builder.build(v, feedback=feedback, previous=previous)
        except PackageError as err:
            await self._log_usages(v.slug, err.usages)
            await self._fail(v, version, err.code)
            return None
        except Exception as err:
            code = "api_error" if isinstance(err, anthropic.APIError) else "error"
            log.warning("пакет %s v%d: сбой сборки (%s)", v.slug, version, type(err).__name__)
            await self._fail(v, version, code)
            return None
        await self._log_usages(v.slug, usages)
        try:
            await self.store.save(v.slug, version, built, feedback)
        except Exception as err:
            log.warning("пакет %s v%d: не сохранён (%s)", v.slug, version, type(err).__name__)
            await self._fail(v, version, "save_error")
            return None
        return built

    async def _log_usages(self, slug: str, usages: list[tuple[str, Usage]]) -> None:
        for step, usage in usages:
            await self.db.log_event("llm", slug, step=step, **asdict(usage))

    async def _fail(self, v: Vacancy, version: int, code: str) -> None:
        await self.store.fail(v.slug, version, code)
        await self.bot.send_message(
            self.owner_id, f"Пакет не собрался: {code}", parse_mode=None, reply_parameters=self._reply(v)
        )

    def _reply(self, v: Vacancy) -> ReplyParameters | None:
        if v.tg_message_id is None:
            return None
        return ReplyParameters(message_id=v.tg_message_id, allow_sending_without_reply=True)

    async def _send(self, v: Vacancy, version: int, built: BuiltPackage) -> None:
        """PDF, письмо, сводка — ответом на карточку; кнопка «Переделать» на последнем сообщении."""
        reply = self._reply(v)
        role = (v.assessment or {}).get("role") or v.title
        await self.bot.send_document(
            self.owner_id,
            BufferedInputFile(built.pdf, filename=built.filename),
            caption=f"Резюме v{version} · {role}"[:CAPTION_LIMIT],
            parse_mode=None,
            reply_parameters=reply,
        )
        texts = split_text(built.letter_text) + split_text(render_summary(built))
        last = None
        for i, text in enumerate(texts):
            markup = kb_redo(v, version) if i == len(texts) - 1 else None
            last = await self.bot.send_message(
                self.owner_id, text, parse_mode=None, reply_parameters=reply, reply_markup=markup
            )
        if last is not None:
            await self.db.set_package(v.slug, version, tg_message_id=last.message_id)
        log.info("пакет %s v%d отправлен", v.slug, version)


def build_package_router(db: Database, runner: PackageRunner, owner_id: int) -> Router:
    router = Router()
    router.message.filter(F.from_user.id == owner_id)
    router.callback_query.filter(F.from_user.id == owner_id)

    @router.callback_query(F.data.startswith("pk:"))
    async def on_package(cb: CallbackQuery) -> None:
        v = await db.get_by_id(int(cb.data.split(":")[1]))
        if v is None:
            await cb.answer("Вакансия не найдена в базе", show_alert=True)
            return
        await cb.answer(await runner.request(v))

    @router.callback_query(F.data.startswith("rd:"))
    async def on_redo(cb: CallbackQuery) -> None:
        _, raw_id, raw_version = cb.data.split(":", 2)
        v = await db.get_by_id(int(raw_id))
        version = int(raw_version)
        if v is None:
            await cb.answer("Вакансия не найдена в базе", show_alert=True)
            return
        row = await db.get_package(v.slug, version)
        if row is None or row["status"] != "ready":
            await cb.answer(f"Версии {version} нет", show_alert=True)
            return
        ask = await runner.bot.send_message(
            owner_id,
            f"Что поправить в версии {version}?",
            parse_mode=None,
            reply_markup=ForceReply(input_field_placeholder="например: письмо короче"),
        )
        runner.pending_redo[ask.message_id] = (v.slug, version)
        await cb.answer()

    def is_redo_answer(message: Message) -> bool:
        reply = message.reply_to_message
        return reply is not None and reply.message_id in runner.pending_redo

    @router.message(is_redo_answer)
    async def on_redo_text(message: Message) -> None:
        ask_id = message.reply_to_message.message_id
        slug, version = runner.pending_redo[ask_id]
        if not (message.text or "").strip():
            await message.answer("Напишите правку текстом", parse_mode=None)
            return
        v = await db.get(slug)
        if v is None:
            runner.pending_redo.pop(ask_id, None)
            await message.answer("Вакансия не найдена в базе", parse_mode=None)
            return
        note = await runner.request(v, feedback=message.text, base_version=version)
        if note == STARTED:
            runner.pending_redo.pop(ask_id, None)
        await message.answer(note, parse_mode=None)

    return router
