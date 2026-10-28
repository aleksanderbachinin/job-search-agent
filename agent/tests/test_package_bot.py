"""Кнопки пакета отклика в боте: «Подготовить отклик», «Переделать», одна сборка за раз, без текста в логах."""

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from itertools import count

import anthropic
import httpx2
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendDocument, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from jobagent.bot import HELP, kb_new, make_dispatcher
from jobagent.db import Database
from jobagent.facts import parse_facts
from jobagent.factcheck import Violation
from jobagent.package_bot import PackageRunner
from jobagent.package_store import PackageStore
from jobagent.pdf import PackageError
from jobagent.scoring import Usage
from test_package_store import CONTACTS, DETAIL_WORD, EXPERIENCE, LETTER_WORD, RESUME_WORD, WARNING_WORD, built
from test_service import CARD, detail

OWNER = 42
STRANGER = 666
VID = CARD["vacancy_id"]
SLUG = CARD["slug"]
CARD_MSG = 777
ROLE = "AI Product Builder"
USAGE = Usage(model="claude-opus-5-5", input_tokens=10, output_tokens=5, cache_read_tokens=3)


# ---------- фальшивый Telegram ----------
# TODO после слияния с веткой, где есть test_bot.py: взять FakeSession и фикстуру db оттуда.


class FakeSession(BaseSession):
    """Записывает вызовы методов Bot API и отвечает правдоподобными объектами."""

    def __init__(self):
        super().__init__()
        self.calls: list = []
        self._ids = count(1000)

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendDocument)):
            return Message(
                message_id=next(self._ids),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                text=getattr(method, "text", None),
            )
        return True

    async def close(self):
        pass

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b""

    def of(self, kind) -> list:
        return [c for c in self.calls if isinstance(c, kind)]

    def answers(self) -> list[str | None]:
        return [c.text for c in self.of(AnswerCallbackQuery)]


class FakeBuilder:
    """Вместо PackageBuilder: отдаёт заданный пакет или бросает исключение; может ждать Event."""

    def __init__(self, result=None, error: Exception | None = None, gate: asyncio.Event | None = None):
        self.result = result or built()
        self.error = error
        self.gate = gate
        self.calls: list[dict] = []

    async def build(self, v, feedback=None, previous=None):
        self.calls.append({"slug": v.slug, "feedback": feedback, "previous": previous})
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.result, [("package_map", USAGE), ("package_resume", USAGE)]


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.sqlite3")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def store(db, tmp_path):
    return PackageStore(db, tmp_path / "packages", parse_facts(EXPERIENCE), CONTACTS)


async def add_vacancy(db, status="shown", with_detail=True):
    await db.upsert_card(CARD, [], "webhook")
    d = detail(company="Acme", title=ROLE) if with_detail else None
    await db.update(
        SLUG, status=status, detail=d, assessment={"role": ROLE, "company": "Acme"}, tg_message_id=CARD_MSG
    )
    return await db.get(SLUG)


class Harness:
    def __init__(self, db, store, builder):
        self.db = db
        self.store = store
        self.builder = builder
        self.session = FakeSession()
        self.bot = Bot("123456:TESTTOKEN", session=self.session)
        self.runner = PackageRunner(self.bot, db, store, builder, OWNER)
        self.dp = make_dispatcher(db, None, OWNER, package_runner=self.runner)
        self._updates = count(1)

    async def press(self, data: str, user_id: int = OWNER) -> None:
        user = User(id=user_id, is_bot=False, first_name="A")
        msg = Message(message_id=CARD_MSG, date=datetime.now(UTC), chat=Chat(id=user_id, type="private"), text="card")
        cb = CallbackQuery(id=f"cb{next(self._updates)}", from_user=user, chat_instance="ci", message=msg, data=data)
        await self.dp.feed_update(self.bot, Update(update_id=next(self._updates), callback_query=cb))

    async def reply(self, to_message_id: int, text: str, user_id: int = OWNER) -> None:
        user = User(id=user_id, is_bot=False, first_name="A")
        chat = Chat(id=user_id, type="private")
        original = Message(message_id=to_message_id, date=datetime.now(UTC), chat=chat, text="?")
        msg = Message(
            message_id=next(self._updates) + 5000, date=datetime.now(UTC), chat=chat, from_user=user,
            text=text, reply_to_message=original,
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(self._updates), message=msg))

    async def finish(self) -> None:
        await asyncio.gather(*self.runner.tasks)


@pytest.fixture
def harness(db, store):
    def make(builder=None):
        return Harness(db, store, builder or FakeBuilder())
    return make


async def events(db, prefix="") -> list[tuple[str, dict]]:
    async with db.conn.execute("SELECT type, data FROM events ORDER BY id") as cur:
        rows = [(r["type"], json.loads(r["data"]) if r["data"] else {}) for r in await cur.fetchall()]
    return [r for r in rows if r[0].startswith(prefix)]


def buttons(markup) -> list[str]:
    return [b.callback_data for row in (markup.inline_keyboard if markup else []) for b in row]


# ---------- тесты ----------


async def test_card_has_package_button(db):
    v = await add_vacancy(db)
    rows = kb_new(v).inline_keyboard
    assert [b.callback_data for b in rows[-1]] == [f"pk:{VID}"]
    assert rows[-1][0].text == "📝 Подготовить отклик"
    assert "Подготовить отклик" in HELP


async def test_package_button_builds_and_sends(db, store, harness):
    await add_vacancy(db)
    h = harness()
    await h.press(f"pk:{VID}")
    assert h.session.answers() == ["Собираю пакет, это займёт пару минут"]
    await h.finish()

    assert h.builder.calls == [{"slug": SLUG, "feedback": None, "previous": None}]
    (doc,) = h.session.of(SendDocument)
    pkg = built()
    assert doc.document.filename == pkg.filename
    assert doc.caption == f"Резюме v1 · {ROLE}"
    assert doc.parse_mode is None
    assert doc.reply_parameters.message_id == CARD_MSG
    letter_msg, summary_msg = h.session.of(SendMessage)
    assert letter_msg.text == pkg.letter_text.strip()
    assert letter_msg.parse_mode is None
    assert letter_msg.reply_parameters.message_id == CARD_MSG
    assert "убрал: jobs[0].bullets[1] (unknown_fact)" in summary_msg.text
    assert pkg.warnings[0] in summary_msg.text
    assert "drop:AIC" in summary_msg.text
    assert summary_msg.parse_mode is None
    assert buttons(summary_msg.reply_markup) == [f"rd:{VID}:1"]
    assert buttons(letter_msg.reply_markup) == []

    row = await db.get_package(SLUG, 1)
    assert row["status"] == "ready"
    types = [t for t, _ in await events(db)]
    assert "package_requested" in types and "package_ready" in types
    assert dict(await events(db, "package_requested"))["package_requested"] == {"version": 1}
    llm = [d for t, d in await events(db) if t == "llm"]
    assert [d["step"] for d in llm] == ["package_map", "package_resume"]
    assert llm[0]["model"] == USAGE.model and llm[0]["input_tokens"] == 10
    assert (await db.get(SLUG)).status == "shown"  # пакет не отклик


async def test_redo_button_on_letter_when_summary_empty(db, harness):
    await add_vacancy(db)
    h = harness(FakeBuilder(replace(built(), removed=[], warnings=[], cut_steps=[])))
    await h.press(f"pk:{VID}")
    await h.finish()
    (letter_msg,) = h.session.of(SendMessage)
    assert buttons(letter_msg.reply_markup) == [f"rd:{VID}:1"]


async def test_second_press_while_building_says_already(db, harness):
    await add_vacancy(db)
    gate = asyncio.Event()
    h = harness(FakeBuilder(gate=gate))
    # два быстрых нажатия одновременно: ровно одна сборка
    await asyncio.gather(h.press(f"pk:{VID}"), h.press(f"pk:{VID}"))
    await h.press(f"pk:{VID}")
    await asyncio.sleep(0)
    assert sorted(h.session.answers()) == sorted(
        ["Собираю пакет, это займёт пару минут", "Уже собираю", "Уже собираю"]
    )
    gate.set()
    await h.finish()
    assert len(h.builder.calls) == 1
    assert await db.get_package(SLUG, 2) is None
    assert not h.runner.lock.locked()

    # после окончания сборки можно снова
    await h.press(f"pk:{VID}")
    await h.finish()
    assert len(h.builder.calls) == 2


async def test_redo_uses_requested_version(db, store, harness):
    await add_vacancy(db)
    v1 = replace(built(), filename="Bachinin_Alexander_Acme_V1.pdf")
    v2 = replace(built(), filename="Bachinin_Alexander_Acme_V2.pdf")
    await store.save(SLUG, await store.start(SLUG), v1, None)
    await store.save(SLUG, await store.start(SLUG), v2, None)
    h = harness()

    await h.press(f"rd:{VID}:1")
    (ask,) = h.session.of(SendMessage)
    assert ask.text == "Что поправить в версии 1?"
    assert ask.reply_markup.force_reply is True
    ask_id = next(mid for mid, target in h.runner.pending_redo.items() if target == (SLUG, 1))

    await h.reply(ask_id, "письмо короче")
    await h.finish()
    (call,) = h.builder.calls
    assert call["feedback"] == "письмо короче"
    assert call["previous"] == await store.load(SLUG, 1)
    assert (await db.get_package(SLUG, 3))["status"] == "ready"
    (doc,) = h.session.of(SendDocument)
    assert doc.caption.startswith("Резюме v3 · ")
    assert f"rd:{VID}:3" in buttons(h.session.of(SendMessage)[-1].reply_markup)
    assert ask_id not in h.runner.pending_redo
    assert dict(await events(db, "package_redo"))["package_redo"] == {
        "version": 3, "base_version": 1, "feedback_len": len("письмо короче"),
    }


async def test_redo_feedback_not_logged(db, store, harness, caplog):
    caplog.set_level(logging.DEBUG)
    await add_vacancy(db)
    await store.save(SLUG, await store.start(SLUG), built(), None)
    h = harness()
    await h.press(f"rd:{VID}:1")
    ask_id = next(iter(h.runner.pending_redo))
    feedback = "Напиши Анне, что жду 5000 USD"
    await h.reply(ask_id, feedback)
    await h.finish()

    assert h.builder.calls[0]["feedback"] == feedback
    # "5000" отдельно не ищем: оно есть в зарплате вакансии (150000) в отладочном логе aiosqlite
    assert "Анне" not in caplog.text and "жду 5000" not in caplog.text
    dump = json.dumps(await events(db), ensure_ascii=False)
    assert "Анне" not in dump and "жду 5000" not in dump
    assert dict(await events(db, "package_redo"))["package_redo"]["feedback_len"] == len(feedback)
    # и текст пакета (письмо, резюме, предупреждения, подробности нарушений) тоже
    for word in (LETTER_WORD, RESUME_WORD, WARNING_WORD, DETAIL_WORD):
        assert word not in caplog.text and word not in dump


async def test_stranger_cannot_build_or_redo(db, store, harness):
    await add_vacancy(db)
    await store.save(SLUG, await store.start(SLUG), built(), None)
    h = harness()
    await h.press(f"pk:{VID}", user_id=STRANGER)
    await h.press(f"rd:{VID}:1", user_id=STRANGER)
    assert h.session.calls == []
    assert h.runner.pending_redo == {}

    await h.press(f"rd:{VID}:1")
    ask_id = next(iter(h.runner.pending_redo))
    await h.reply(ask_id, "убери RLY", user_id=STRANGER)
    await h.finish()
    assert h.builder.calls == []
    assert await db.get_package(SLUG, 2) is None


async def test_build_error_reports_code(db, harness, caplog):
    caplog.set_level(logging.DEBUG)
    await add_vacancy(db)
    err = PackageError("после пересборки: composition (jobs[0] AEZ: секретный текст)", code="composition")
    err.usages = [("package_map", USAGE)]
    h = harness(FakeBuilder(error=err))
    await h.press(f"pk:{VID}")
    await h.finish()

    (msg,) = h.session.of(SendMessage)
    assert msg.text == "Пакет не собрался: composition"
    assert msg.reply_parameters.message_id == CARD_MSG
    assert h.session.of(SendDocument) == []
    assert (await db.get_package(SLUG, 1))["status"] == "failed"
    assert dict(await events(db, "package_failed"))["package_failed"] == {"version": 1, "code": "composition"}
    assert [d["step"] for t, d in await events(db) if t == "llm"] == ["package_map"]
    assert "секретный текст" not in caplog.text
    assert not h.runner.lock.locked()


@pytest.mark.parametrize(
    "error, code",
    [
        (RuntimeError("секретный текст"), "error"),
        (anthropic.APIConnectionError(message="секретный текст", request=httpx2.Request("POST", "https://x")), "api_error"),
    ],
)
async def test_unexpected_error_reports_code_without_text(db, harness, caplog, error, code):
    caplog.set_level(logging.DEBUG)
    await add_vacancy(db)
    h = harness(FakeBuilder(error=error))
    await h.press(f"pk:{VID}")
    await h.finish()
    (msg,) = h.session.of(SendMessage)
    assert msg.text == f"Пакет не собрался: {code}"
    assert dict(await events(db, "package_failed"))["package_failed"] == {"version": 1, "code": code}
    assert "секретный текст" not in caplog.text
    assert "секретный текст" not in json.dumps(await events(db), ensure_ascii=False)


async def test_save_error_fails_package(db, store, harness, monkeypatch):
    await add_vacancy(db)

    async def broken_save(*args, **kwargs):
        raise OSError("диск полон")

    monkeypatch.setattr(store, "save", broken_save)
    h = harness()
    await h.press(f"pk:{VID}")
    await h.finish()
    assert (await db.get_package(SLUG, 1))["status"] == "failed"
    assert dict(await events(db, "package_failed"))["package_failed"] == {"version": 1, "code": "save_error"}
    assert [m.text for m in h.session.of(SendMessage)] == ["Пакет не собрался: save_error"]


async def test_caption_and_texts_within_telegram_limits(db, harness):
    await add_vacancy(db)
    await db.update(SLUG, assessment={"role": "R" * 2000})
    paragraph = "Слово " * 300  # ~1800 символов
    long_letter = "\n\n".join(paragraph.strip() for _ in range(6))
    pkg = replace(built(), letter_text=long_letter, warnings=[f"⚠️ проверь: {i} " + "x" * 300 for i in range(30)])
    h = harness(FakeBuilder(pkg))
    await h.press(f"pk:{VID}")
    await h.finish()

    (doc,) = h.session.of(SendDocument)
    assert len(doc.caption) <= 1024
    msgs = h.session.of(SendMessage)
    assert all(len(m.text) <= 4096 for m in msgs)
    letter_parts = [m.text for m in msgs if m.text.startswith("Слово")]
    assert len(letter_parts) >= 3
    assert "\n".join(letter_parts).split() == long_letter.split()  # ничего не обрезано
    assert all(f"⚠️ проверь: {i} " in "".join(m.text for m in msgs) for i in range(30))
    assert buttons(msgs[-1].reply_markup) == [f"rd:{VID}:1"]


async def test_vacancy_without_detail_is_not_built(db, harness):
    await add_vacancy(db, with_detail=False)
    h = harness()
    await h.press(f"pk:{VID}")
    await h.finish()
    assert "нет полного текста вакансии" in h.session.answers()[0]
    assert h.builder.calls == []
    assert await db.get_package(SLUG, 1) is None


async def test_only_shown_vacancy_is_built(db, harness):
    await add_vacancy(db, status="applied")
    h = harness()
    await h.press(f"pk:{VID}")
    await h.finish()
    assert h.builder.calls == []
    assert h.session.answers() and h.session.answers()[0] != "Собираю пакет, это займёт пару минут"
    assert await db.get_package(SLUG, 1) is None


async def test_redo_of_missing_version_is_refused(db, harness):
    await add_vacancy(db)
    h = harness()
    await h.press(f"rd:{VID}:5")
    assert h.runner.pending_redo == {}
    assert h.session.of(SendMessage) == []
    assert h.session.answers()[0]


def test_dispatcher_without_runner_has_no_package_router(db):
    dp = make_dispatcher(db, None, OWNER)
    assert len(dp.sub_routers) == 1
