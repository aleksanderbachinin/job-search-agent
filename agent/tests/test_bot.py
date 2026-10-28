"""Обработчики кнопок бота: настоящие апдейты через Dispatcher, настоящая база, Telegram и Hirify подменены.

Кнопки — единственные действия агента наружу, поэтому проверяем, что в Hirify ушёл нужный вызов
для нужной вакансии, а в базе остались статус и событие.
"""

from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from jobagent.bot import make_dispatcher
from jobagent.hirify import HirifyError
from test_service import CARD, assessment, db, detail  # noqa: F401 (db — фикстура)

OWNER = 111
STRANGER = 222
SLUG = CARD["slug"]
VID = CARD["vacancy_id"]


class FakeSession(BaseSession):
    """Вместо сети записывает вызовы Telegram API."""

    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        return True

    async def close(self): ...

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


class FakeHirify:
    """Записывает вызовы; ошибки включаются по методам: errors={"track": HirifyError(...)}."""

    def __init__(self, errors=None, tracker_id=None):
        self.errors = errors or {}
        self.tracker_id = tracker_id
        self.calls = []

    def _call(self, name, **args):
        self.calls.append((name, args))
        if name in self.errors:
            raise self.errors[name]

    async def track(self, *, slug=None, tracker_id=None, stage=None):
        args = {"slug": slug, "tracker_id": tracker_id, "stage": stage}
        self._call("track", **{k: v for k, v in args.items() if v is not None})
        return {"tracker_id": self.tracker_id} if self.tracker_id else {}

    async def hide_vacancy(self, slug):
        self._call("hide_vacancy", slug=slug)

    async def hide_company(self, slug):
        self._call("hide_company", slug=slug)

    async def reveal(self, vacancy_id):
        self._call("reveal", vacancy_id=vacancy_id)
        contacts = [{"type": "telegram", "value": "@hr_docs"}]
        return {"contacts": contacts, "linkedin": None, "charged": True}, {"quota": {"remaining": 199}}


class Harness:
    """Бот с подменённой сессией и диспетчер с обработчиками из build_router."""

    def __init__(self, db, hirify):
        self.db, self.hirify = db, hirify
        self.session = FakeSession()
        self.bot = Bot("123456:" + "A" * 35, session=self.session)
        self.dp = make_dispatcher(db, hirify, OWNER)

    def _message(self, user_id, text, message_id=10):
        return Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="u"),
            text=text,
        )

    async def _feed(self, **update_fields):
        # Как приходит из Telegram: через валидацию с ботом в контексте, чтобы он был привязан ко всем вложенным объектам.
        raw = Update(update_id=1, **update_fields).model_dump(mode="json", exclude_none=True)
        update = Update.model_validate(raw, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, update)

    async def press(self, data, user_id=OWNER, card_text="Карточка"):
        cb = CallbackQuery(
            id="cb1",
            from_user=User(id=user_id, is_bot=False, first_name="u"),
            chat_instance="ci",
            data=data,
            message=self._message(OWNER, card_text),
        )
        await self._feed(callback_query=cb)

    async def send(self, text, user_id=OWNER):
        await self._feed(message=self._message(user_id, text, message_id=11))

    def sent(self, method_type):
        return [m for m in self.session.calls if isinstance(m, method_type)]

    @property
    def answer(self) -> AnswerCallbackQuery:
        (answer,) = self.sent(AnswerCallbackQuery)
        return answer

    @property
    def keyboard(self) -> list:
        """Кнопки последней правки сообщения: (текст, callback_data)."""
        edits = self.sent(EditMessageReplyMarkup) + self.sent(EditMessageText)
        markup = edits[-1].reply_markup
        return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def events(db):
    async with db.conn.execute("SELECT type, slug, data FROM events ORDER BY id") as cur:
        return [tuple(row) for row in await cur.fetchall()]


async def shown(db, **fields):
    """Вакансия в состоянии «карточка отправлена», с оценкой, как её оставляет воркер; fields перекрывают поля."""
    await db.upsert_card(CARD, [{"id": 1, "name": "PM remote"}], source="webhook", status="shown")
    await db.update(SLUG, **{"assessment": assessment().model_dump(), **fields})
    return await db.get(SLUG)


@pytest.fixture
def hirify():
    return FakeHirify()


@pytest.fixture
def h(db, hirify):
    return Harness(db, hirify)


# --- «✅ Откликнулся» ---
# Поломки: отклик уходит не для той вакансии; статус или событие не пишутся в базу;
# id записи трекера теряется (этап потом уйдёт не в ту запись); сбой Hirify выглядит как успех.


async def test_applied_tracks_this_vacancy_and_saves_status_event_and_tracker_id(db, hirify, h):
    hirify.tracker_id = 55
    await shown(db)
    await h.press(f"ap:{VID}")

    assert hirify.calls == [("track", {"slug": SLUG})]
    v = await db.get(SLUG)
    assert (v.status, v.tracker_id) == ("applied", 55)
    assert await events(db) == [("applied", SLUG, None)]
    assert "не удалось" not in h.answer.text
    # Под карточкой появились кнопки этапов этой же вакансии.
    assert ("🎤 Позвали на интервью", f"st:{VID}:interview") in h.keyboard


async def test_applied_hirify_conflict_is_still_recorded_locally_and_says_so(db, h, hirify):
    # Сейчас любой HirifyError, в том числе 409 conflict, считается «в трекер не записали»:
    # у себя вакансия помечается, tracker_id не меняется, пользователю сказано, что трекер не обновлён.
    hirify.errors["track"] = HirifyError(409, "conflict", "already tracked")
    await shown(db, tracker_id=7)
    await h.press(f"ap:{VID}")

    assert hirify.calls == [("track", {"slug": SLUG})]
    v = await db.get(SLUG)
    assert (v.status, v.tracker_id) == ("applied", 7)
    assert await events(db) == [("applied", SLUG, None)]
    assert "не удалось" in h.answer.text


async def test_applied_for_unknown_vacancy_touches_nothing(db, h, hirify):
    await shown(db)
    await h.press("ap:999")

    assert hirify.calls == []
    assert (await db.get(SLUG)).status == "shown" and await events(db) == []
    assert h.answer.show_alert is True


# --- «⏭ Пропустить» ---
# Поломки: нажатие «Пропустить» сразу скрывает вакансию, не дождавшись причины; при причине
# «Компания неинтересна» скрывается одна вакансия (или при другой причине — вся компания);
# причина не пишется; сбой Hirify роняет обработчик или маскируется под успех.


async def test_skip_button_only_asks_for_reason(db, h, hirify):
    await shown(db)
    await h.press(f"sk:{VID}")

    assert hirify.calls == []
    assert (await db.get(SLUG)).status == "shown" and await events(db) == []
    assert (f"Условия или ЗП", f"sr:{VID}:terms") in h.keyboard


async def test_skip_company_reason_hides_whole_company_not_just_vacancy(db, h, hirify):
    await shown(db)
    await h.press(f"sr:{VID}:company")

    assert hirify.calls == [("hide_company", {"slug": SLUG})]
    v = await db.get(SLUG)
    assert (v.status, v.reason) == ("skipped", "company")
    assert await events(db) == [("skipped", SLUG, '{"reason": "company"}')]
    assert h.keyboard[0] == ("⏭ Пропущено: Компания неинтересна", "nop")


@pytest.mark.parametrize("code", ["role", "terms", "ru", "other"])
async def test_skip_other_reasons_hide_only_the_vacancy(db, h, hirify, code):
    await shown(db)
    await h.press(f"sr:{VID}:{code}")

    assert hirify.calls == [("hide_vacancy", {"slug": SLUG})]
    v = await db.get(SLUG)
    assert (v.status, v.reason) == ("skipped", code)
    assert await events(db) == [("skipped", SLUG, f'{{"reason": "{code}"}}')]


@pytest.mark.parametrize("code,hide", [("company", "hide_company"), ("terms", "hide_vacancy")])
async def test_skip_marks_skipped_even_when_hirify_fails_and_says_not_hidden(db, hirify, h, code, hide):
    hirify.errors[hide] = HirifyError(503, "external_unavailable", "down")
    await shown(db)
    await h.press(f"sr:{VID}:{code}")

    assert [name for name, _ in hirify.calls] == [hide]
    v = await db.get(SLUG)
    assert (v.status, v.reason) == ("skipped", code)
    assert await events(db) == [("skipped", SLUG, f'{{"reason": "{code}"}}')]
    assert "не удалось" in h.answer.text
    assert h.keyboard[0][0].startswith("⏭ Пропущено")


# --- этапы после отклика ---
# Поломки: этап уходит в Hirify с чужим названием (кнопка «Оффер» ставит Rejected);
# при известном tracker_id этап шлётся по slug, а не в запись трекера; событие и этап не пишутся;
# неизвестный код этапа доходит до Hirify.


@pytest.mark.parametrize(
    "code,hirify_stage",
    [("interview", "HR Interview"), ("offer", "Offer"), ("rejected_by_employer", "Rejected")],
)
async def test_stage_goes_to_tracker_record_and_event_is_logged(db, h, hirify, code, hirify_stage):
    await shown(db, status="applied", tracker_id=55)
    await h.press(f"st:{VID}:{code}")

    assert hirify.calls == [("track", {"tracker_id": 55, "stage": hirify_stage})]
    v = await db.get(SLUG)
    assert (v.status, v.stage) == ("applied", code)
    assert await events(db) == [(code, SLUG, None)]
    assert "не удалось" not in h.answer.text


async def test_stage_without_tracker_id_falls_back_to_slug(db, h, hirify):
    await shown(db, status="applied")
    await h.press(f"st:{VID}:offer")

    assert hirify.calls == [("track", {"slug": SLUG, "stage": "Offer"})]


async def test_stage_is_saved_locally_when_hirify_fails_and_says_so(db, hirify, h):
    hirify.errors["track"] = HirifyError(503, "external_unavailable", "down")
    await shown(db, status="applied", tracker_id=55)
    await h.press(f"st:{VID}:interview")

    assert (await db.get(SLUG)).stage == "interview"
    assert await events(db) == [("interview", SLUG, None)]
    assert "не удалось" in h.answer.text


async def test_unknown_stage_code_is_ignored(db, h, hirify):
    await shown(db, status="applied", tracker_id=55)
    await h.press(f"st:{VID}:hired")

    assert hirify.calls == []
    assert (await db.get(SLUG)).stage is None and await events(db) == []


# --- «📇 Контакт» ---
# Поломки: контакт раскрывается без нажатия (лишний расход лимита 200 в день); повторное нажатие
# снова вызывает reveal; результат не сохраняется в базе и пропадает при следующем показе;
# исчерпанный лимит роняет обработчик или записывает пустой контакт.


async def test_reveal_happens_only_on_contact_press(db, h, hirify):
    await shown(db)
    await h.press(f"dt:{VID}")
    await h.press(f"sk:{VID}")
    await h.press(f"bk:{VID}")
    await h.press(f"ap:{VID}")

    assert "reveal" not in [name for name, _ in hirify.calls]
    assert (await db.get(SLUG)).detail is None


async def test_contact_press_reveals_once_and_saves_contact(db, h, hirify):
    await shown(db, detail=detail())
    await h.press(f"ct:{VID}")

    assert hirify.calls == [("reveal", {"vacancy_id": VID})]
    saved = (await db.get(SLUG)).detail["contact"]
    assert saved == {"contacts": [{"type": "telegram", "value": "@hr_docs"}], "linkedin": None}
    assert await events(db) == [("revealed", SLUG, '{"charged": true}')]
    assert "199" in h.answer.text  # остаток лимита из meta.quota


async def test_second_contact_press_does_not_spend_the_limit_again(db, h, hirify):
    await shown(db, detail=detail())
    await h.press(f"ct:{VID}")
    h.session.calls.clear()
    await h.press(f"ct:{VID}")

    assert hirify.calls == [("reveal", {"vacancy_id": VID})]  # только первое нажатие
    assert [e[0] for e in await events(db)] == ["revealed"]
    assert (await db.get(SLUG)).detail["contact"]["contacts"][0]["value"] == "@hr_docs"
    assert "уже раскрыт" in h.answer.text


async def test_contact_limit_error_saves_nothing_and_alerts(db, h, hirify):
    hirify.errors["reveal"] = HirifyError(429, "quota_exhausted", "limit")
    await shown(db, detail=detail())
    await h.press(f"ct:{VID}")

    assert "contact" not in (await db.get(SLUG)).detail and await events(db) == []
    assert h.answer.show_alert is True and "лимит" in h.answer.text
    assert h.sent(EditMessageText) == []


# --- чужой пользователь ---
# Поломка: убран или ослаблен фильтр по owner_id — любой, кто нашёл бота, двигает отклики
# владельца и тратит его лимиты в Hirify.


@pytest.mark.parametrize(
    "data",
    [f"ap:{VID}", f"sr:{VID}:company", f"sr:{VID}:terms", f"ct:{VID}", f"st:{VID}:offer", f"dt:{VID}"],
)
async def test_callback_from_stranger_reaches_neither_db_nor_hirify(db, h, hirify, data):
    await shown(db, status="applied", detail=detail())
    before = await db.get(SLUG)
    await h.press(data, user_id=STRANGER)

    assert hirify.calls == []
    assert await db.get(SLUG) == before and await events(db) == []
    assert h.session.calls == []


@pytest.mark.parametrize("text", ["/stats", "/start", "/help"])
async def test_message_from_stranger_gets_no_reply_and_no_stats(db, h, hirify, text):
    await shown(db)
    await db.log_event("applied", SLUG)
    await h.send(text, user_id=STRANGER)

    assert h.session.calls == [] and hirify.calls == []


async def test_message_from_owner_is_answered(db, h):
    # Контроль к предыдущему тесту: тот же сценарий для владельца отвечает, значит молчание
    # для чужого — результат фильтра, а не сломанного стенда.
    await h.send("/start")

    assert len(h.session.calls) == 1
