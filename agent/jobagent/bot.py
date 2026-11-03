"""Telegram-бот: карточки вакансий, кнопки решений, /stats. Отвечает только владельцу."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .cards import DETAILS_HEADER, SKIP_REASONS, STAGES, contact_url, render_full, render_stats, vacancy_url
from .db import Database, Vacancy
from .hirify import HirifyClient, HirifyError, tracker_id_from
from .package_bot import PackageRunner, build_package_router

log = logging.getLogger(__name__)

HELP = """Я присылаю вакансии из ваших фильтров Hirify, которые прошли отсев.

Под каждой карточкой:
📋 Подробнее — требования, RU-проверка с людьми из LinkedIn, флаги
📇 Контакт — раскрою в Hirify, как откликнуться (тратит одно раскрытие из 200 в день)
✅ Откликнулся — запишу отклик в трекер Hirify
⏭ Пропустить — спрошу причину и скрою вакансию в Hirify
📝 Подготовить отклик — соберу PDF-резюме и письмо под вакансию, пришлю ответом на карточку; ✏️ Переделать под пакетом — пересоберу с вашей правкой
После отклика можно отметить интервью, оффер или отказ.

/stats — воронка за 7 дней и за всё время
/stats 30 — за 30 дней"""


def make_bot(token: str) -> Bot:
    return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


# --- клавиатуры ---


def _details_button(b: InlineKeyboardBuilder, v: Vacancy, expanded: bool) -> None:
    if expanded:
        b.button(text="📋 Свернуть", callback_data=f"cl:{v.vacancy_id}")
    else:
        b.button(text="📋 Подробнее", callback_data=f"dt:{v.vacancy_id}")


def _contact_button(b: InlineKeyboardBuilder, v: Vacancy) -> int:
    """До раскрытия — кнопка раскрыть, после — ссылка на отклик. Возвращает число кнопок."""
    if (v.detail or {}).get("contact") is None:
        b.button(text="📇 Контакт", callback_data=f"ct:{v.vacancy_id}")
        return 1
    if url := contact_url(v.detail):
        b.button(text="📇 Откликнуться по контакту", url=url)
        return 1
    return 0


def kb_new(v: Vacancy, expanded: bool = False, package: bool = False) -> InlineKeyboardMarkup:
    """package — показывать «Подготовить отклик»: сборщик пакетов есть, только если заданы контакты кандидата."""
    b = InlineKeyboardBuilder()
    b.button(text="🔗 Открыть", url=vacancy_url(v.slug, v.detail))
    _details_button(b, v, expanded)
    contact = _contact_button(b, v)
    b.button(text="✅ Откликнулся", callback_data=f"ap:{v.vacancy_id}")
    b.button(text="⏭ Пропустить", callback_data=f"sk:{v.vacancy_id}")
    if package:
        b.button(text="📝 Подготовить отклик", callback_data=f"pk:{v.vacancy_id}")
    b.adjust(*([2, 1, 2, 1] if contact else [2, 2, 1]))
    return b.as_markup()


def kb_skip_reasons(v: Vacancy) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for code, label in SKIP_REASONS.items():
        b.button(text=label, callback_data=f"sr:{v.vacancy_id}:{code}")
    b.button(text="↩ Назад", callback_data=f"bk:{v.vacancy_id}")
    b.adjust(2, 2, 1, 1)
    return b.as_markup()


def kb_status(v: Vacancy, status_label: str, with_stages: bool, expanded: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=status_label, callback_data="nop")
    b.button(text="🔗 Открыть", url=vacancy_url(v.slug, v.detail))
    _details_button(b, v, expanded)
    sizes = [1, 2]
    if _contact_button(b, v):
        sizes.append(1)
    if with_stages:
        for code, (_, label) in STAGES.items():
            b.button(text=label, callback_data=f"st:{v.vacancy_id}:{code}")
        sizes.append(len(STAGES))
    b.adjust(*sizes)
    return b.as_markup()


def kb_for(v: Vacancy, expanded: bool, package: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура по текущему статусу вакансии: нужна, когда раскрываем или сворачиваем карточку."""
    if v.status == "skipped":
        return kb_status(v, f"⏭ Пропущено: {SKIP_REASONS.get(v.reason or '', v.reason or '')}", False, expanded)
    if v.status == "applied":
        if v.stage in STAGES:
            return kb_status(v, STAGES[v.stage][1], v.stage == "interview", expanded)
        return kb_status(v, "✅ Откликнулся", True, expanded)
    return kb_new(v, expanded, package)


def is_expanded(message: Message | None) -> bool:
    return bool(message and message.text and DETAILS_HEADER in message.text)


# --- отправка ---


class Notifier:
    """Отправляет владельцу карточки и алерты."""

    def __init__(self, bot: Bot, owner_id: int, package_button: bool = False):
        self.bot = bot
        self.owner_id = owner_id
        self.package_button = package_button

    async def send_card(self, v: Vacancy) -> int:
        text = render_full(v.title, v.assessment or {}, v.feeds, v.detail, expanded=False)
        msg = await self.bot.send_message(
            self.owner_id, text, reply_markup=kb_new(v, package=self.package_button), disable_web_page_preview=True
        )
        return msg.message_id

    async def alert(self, text: str) -> None:
        try:
            await self.bot.send_message(self.owner_id, f"⚠️ {text}", parse_mode=None)
        except Exception:  # алерт не должен ронять воркер
            log.exception("Не удалось отправить алерт")


# --- обработчики ---


def build_router(db: Database, hirify: HirifyClient, owner_id: int, package_button: bool = False) -> Router:
    router = Router()
    router.message.filter(F.from_user.id == owner_id)
    router.callback_query.filter(F.from_user.id == owner_id)

    async def load(cb: CallbackQuery, raw_id: str) -> Vacancy | None:
        v = await db.get_by_id(int(raw_id))
        if v is None:
            await cb.answer("Вакансия не найдена в базе", show_alert=True)
        return v

    @router.message(CommandStart())
    @router.message(Command("help"))
    async def on_start(message: Message) -> None:
        await message.answer(HELP, parse_mode=None)

    @router.message(Command("stats"))
    async def on_stats(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip()
        if arg.isdigit():
            days = int(arg)
            text = render_stats(
                await db.event_counts(days), await db.skip_reasons(days), f"за {days} дн.",
                await db.llm_usage(days, "package_"),
            )
        else:
            text = "\n\n".join(
                [
                    render_stats(
                        await db.event_counts(7), await db.skip_reasons(7), "за 7 дней",
                        await db.llm_usage(7, "package_"),
                    ),
                    render_stats(
                        await db.event_counts(None), await db.skip_reasons(None), "за всё время",
                        await db.llm_usage(None, "package_"),
                    ),
                ]
            )
        await message.answer(text)

    @router.callback_query(F.data == "nop")
    async def on_nop(cb: CallbackQuery) -> None:
        await cb.answer()

    @router.callback_query(F.data.startswith(("dt:", "cl:")))
    async def on_toggle_details(cb: CallbackQuery) -> None:
        action, raw_id = cb.data.split(":", 1)
        v = await load(cb, raw_id)
        if v is None:
            return
        expanded = action == "dt"
        text = render_full(v.title, v.assessment or {}, v.feeds, v.detail, expanded)
        await cb.message.edit_text(text, reply_markup=kb_for(v, expanded, package_button), disable_web_page_preview=True)
        await cb.answer()

    @router.callback_query(F.data.startswith("ct:"))
    async def on_contact(cb: CallbackQuery) -> None:
        v = await load(cb, cb.data.split(":")[1])
        if v is None or v.vacancy_id is None:
            return
        detail = dict(v.detail or {})
        note = "Контакт уже раскрыт"
        if detail.get("contact") is None:
            try:
                data, meta = await hirify.reveal(v.vacancy_id)
            except HirifyError as e:
                log.warning("reveal failed for %s: %s", v.slug, e)
                why = "лимит раскрытий на сегодня исчерпан" if e.is_limit else str(e)
                await cb.answer(f"Hirify не раскрыл контакт: {why}"[:200], show_alert=True)
                return
            detail["contact"] = {"contacts": data.get("contacts") or [], "linkedin": data.get("linkedin")}
            await db.update(v.slug, detail=detail)
            await db.log_event("revealed", v.slug, charged=bool(data.get("charged")))
            remaining = ((meta.get("quota") or {}).get("remaining"))
            note = "Контакт раскрыт" + (f", раскрытий на сегодня осталось {remaining}" if remaining is not None else "")
        v.detail = detail
        expanded = is_expanded(cb.message)
        text = render_full(v.title, v.assessment or {}, v.feeds, v.detail, expanded)
        await cb.message.edit_text(text, reply_markup=kb_for(v, expanded, package_button), disable_web_page_preview=True)
        await cb.answer(note)

    @router.callback_query(F.data.startswith("ap:"))
    async def on_applied(cb: CallbackQuery) -> None:
        v = await load(cb, cb.data.split(":")[1])
        if v is None:
            return
        note = "Записал в трекер Hirify"
        tracker_id = v.tracker_id
        try:
            tracker_id = tracker_id_from(await hirify.track(slug=v.slug)) or tracker_id
        except HirifyError as e:
            log.warning("track failed for %s: %s", v.slug, e)
            note = "Отметил у себя, но в трекер Hirify записать не удалось"
        await db.update(v.slug, status="applied", tracker_id=tracker_id)
        await db.log_event("applied", v.slug)
        await cb.message.edit_reply_markup(reply_markup=kb_status(v, "✅ Откликнулся", True, is_expanded(cb.message)))
        await cb.answer(note)

    @router.callback_query(F.data.startswith("sk:"))
    async def on_skip(cb: CallbackQuery) -> None:
        v = await load(cb, cb.data.split(":")[1])
        if v is None:
            return
        await cb.message.edit_reply_markup(reply_markup=kb_skip_reasons(v))
        await cb.answer("Почему пропускаем?")

    @router.callback_query(F.data.startswith("bk:"))
    async def on_back(cb: CallbackQuery) -> None:
        v = await load(cb, cb.data.split(":")[1])
        if v is None:
            return
        await cb.message.edit_reply_markup(reply_markup=kb_new(v, is_expanded(cb.message), package_button))
        await cb.answer()

    @router.callback_query(F.data.startswith("sr:"))
    async def on_skip_reason(cb: CallbackQuery) -> None:
        _, raw_id, code = cb.data.split(":", 2)
        v = await load(cb, raw_id)
        if v is None:
            return
        note = "Пропустил и скрыл в Hirify"
        try:
            if code == "company":
                await hirify.hide_company(v.slug)
                note = "Пропустил и скрыл все вакансии компании в Hirify"
            else:
                await hirify.hide_vacancy(v.slug)
        except HirifyError as e:
            log.warning("hide failed for %s: %s", v.slug, e)
            note = "Пропустил, но скрыть в Hirify не удалось"
        await db.update(v.slug, status="skipped", reason=code)
        await db.log_event("skipped", v.slug, reason=code)
        label = f"⏭ Пропущено: {SKIP_REASONS.get(code, code)}"
        await cb.message.edit_reply_markup(reply_markup=kb_status(v, label, False, is_expanded(cb.message)))
        # Компания приходит пачкой: остальные её карточки без решения закрываем тем же нажатием.
        if code == "company" and v.company:
            others = await db.shown_of_company(v.company)
            for other in others:
                await db.update(other.slug, status="skipped", reason=code)
                await db.log_event("skipped", other.slug, reason=code, with_slug=v.slug)
                if other.tg_message_id:
                    try:
                        await cb.bot.edit_message_reply_markup(
                            chat_id=cb.message.chat.id,
                            message_id=other.tg_message_id,
                            reply_markup=kb_status(other, label, False),
                        )
                    except TelegramAPIError as e:
                        log.warning("edit failed for %s: %s", other.slug, e)
            if others:
                note += f". Другие её карточки в чате тоже пропущены: {len(others)}"
        await cb.answer(note)

    @router.callback_query(F.data.startswith("st:"))
    async def on_stage(cb: CallbackQuery) -> None:
        _, raw_id, code = cb.data.split(":", 2)
        v = await load(cb, raw_id)
        if v is None or code not in STAGES:
            return
        hirify_stage, label = STAGES[code]
        note = "Обновил этап в трекере Hirify"
        try:
            if v.tracker_id:
                await hirify.track(tracker_id=v.tracker_id, stage=hirify_stage)
            else:
                await hirify.track(slug=v.slug, stage=hirify_stage)
        except HirifyError as e:
            log.warning("stage update failed for %s: %s", v.slug, e)
            note = "Отметил у себя, но в трекере Hirify обновить не удалось"
        await db.update(v.slug, stage=code)
        await db.log_event(code, v.slug)
        await cb.message.edit_reply_markup(
            reply_markup=kb_status(v, label, code == "interview", is_expanded(cb.message))
        )
        await cb.answer(note)

    return router


def make_dispatcher(
    db: Database, hirify: HirifyClient, owner_id: int, package_runner: PackageRunner | None = None
) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(db, hirify, owner_id, package_button=package_runner is not None))
    if package_runner is not None:
        dp.include_router(build_package_router(db, package_runner, owner_id))
    return dp
