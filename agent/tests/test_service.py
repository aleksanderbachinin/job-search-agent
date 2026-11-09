import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobagent import rules
from jobagent.cards import DETAILS_HEADER, posted_ago, render_card, render_full, render_stats
from jobagent.db import Database, vacancy_id_from_slug
from jobagent.ingest import verify_signature, webhook_router
from jobagent.pipeline import Pipeline
from jobagent.scoring import Assessment, Triage, Usage

SECRET = "test-secret"

CARD = {
    "vacancy_id": 1071419,
    "title": "Product Manager (EdTech)",
    "slug": "1071419-product-manager-edtech",
    "company_masked": False,
    "can_apply_directly": True,
    "skills": ["ai", "analytics", "edtech"],
    "specializations": [{"code": "product_management", "name_en": "Product Management"}],
    "grades": ["middle", "senior"],
    "regions": [],
    "cities": [],
    "remote_type": "global",
    "work_format": ["remote"],
    "salary": {"currency": "RUB", "min": 150000, "max": 250000},
    "vacancy_language": "ru",
    "english_level": "b2",
    "has_applied": False,
    "in_tracker": False,
}


def detail(**over):
    d = dict(CARD)
    d.update(
        company="Remoteria",
        url="https://hirify.me/jobs/1071419-product-manager-edtech?utm_source=agent-api",
        created_at=datetime.now(UTC).isoformat(),
        description="Ищем PM. Полностью удалённо, можно не из РФ. Обязательно: A/B-тесты.",
        application_instructions=None,
    )
    d.update(over)
    return d


def assessment(score=4):
    return Assessment(
        company="Remoteria",
        role="Product Manager (EdTech)",
        grade="middle/senior",
        location_format="удалёнка, можно не из РФ",
        salary="150–250k RUB в месяц",
        format_priority="remote_from_russia",
        must_haves=["A/B-тесты"],
        missing_must_haves=["Яндекс.Метрика"],
        red_flags=[],
        ru_label="indirect",
        ru_summary="текст на русском",
        ru_linkedin="CEO Иван Петров",
        ru_evidence="текст на русском, CEO Иван Петров из Минска",
        score=score,
        verdict="Стоит откликнуться",
        pros=["A/B-тесты → программа экспериментов в MPSTATS", "удалёнка из России"],
        con="EdTech-опыта нет",
        questions=["вилка в USD"],
    )


def sign(body: bytes, ts: int | None = None) -> dict[str, str]:
    ts = ts or int(time.time())
    sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {
        "X-Hirify-Timestamp": str(ts),
        "X-Hirify-Signature": f"sha256={sig}",
        "X-Hirify-Event": "vacancy.matched",
        "X-Hirify-Delivery": "d-1",
    }


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.sqlite3")
    await d.connect()
    yield d
    await d.close()


# --- подпись и вебхук ---


def test_signature_valid_and_tampered():
    body = b'{"event":"vacancy.matched"}'
    h = sign(body)
    assert verify_signature(SECRET, h["X-Hirify-Timestamp"], body, h["X-Hirify-Signature"], 300)
    assert not verify_signature(SECRET, h["X-Hirify-Timestamp"], body + b" ", h["X-Hirify-Signature"], 300)
    assert not verify_signature("other", h["X-Hirify-Timestamp"], body, h["X-Hirify-Signature"], 300)


def test_signature_old_timestamp_rejected():
    body = b"{}"
    old = int(time.time()) - 3600
    h = sign(body, ts=old)
    assert not verify_signature(SECRET, h["X-Hirify-Timestamp"], body, h["X-Hirify-Signature"], 300)


async def test_webhook_queues_once_and_ignores_duplicates(db):
    woke = []
    app = FastAPI()
    app.include_router(webhook_router(db, SECRET, 300, lambda: woke.append(1)))
    card = {k: v for k, v in CARD.items() if k not in ("vacancy_id", "has_applied", "in_tracker")}
    payload = {
        "event": "vacancy.matched",
        "delivery_id": "d-1",
        "delivered_at": "2026-10-26T10:00:00Z",
        "is_reopen": False,
        "feeds": [{"id": 31447, "name": "PM remote"}],
        "vacancy": card,
    }
    body = json.dumps(payload).encode()
    with TestClient(app) as client:
        r = client.post("/hirify/webhook", content=body, headers=sign(body))
        assert r.status_code == 200 and r.json() == {"ok": True}
        r = client.post("/hirify/webhook", content=body, headers=sign(body))
        assert r.json()["duplicate"] is True
        r = client.post("/hirify/webhook", content=body, headers={**sign(body), "X-Hirify-Signature": "sha256=00"})
        assert r.status_code == 401

    v = await db.get(CARD["slug"])
    assert v.status == "queued" and v.vacancy_id == 1071419 and v.source == "webhook"
    assert woke == [1]


async def test_same_vacancy_from_two_feeds_is_merged(db):
    assert await db.upsert_card(CARD, [{"id": 1, "name": "A"}], source="webhook")
    assert not await db.upsert_card(CARD, [{"id": 2, "name": "B"}], source="reconcile")
    v = await db.get(CARD["slug"])
    assert [f["id"] for f in v.feeds] == [1, 2]
    assert (await db.event_counts(None))["received"] == 1


def test_vacancy_id_from_slug():
    assert vacancy_id_from_slug("1071419-product-manager-edtech") == 1071419
    assert vacancy_id_from_slug("no-number") is None


# --- правила ---


@pytest.mark.parametrize(
    "title,rejected",
    [
        ("Senior Product Manager", False),
        ("Head of Growth", False),
        ("Product Marketing Manager", False),
        ("AI Product Builder", False),
        ("Продуктовый менеджер", False),
        ("Product Designer", True),
        ("Product Analyst", True),
        ("Senior Backend Developer", True),
        ("QA Engineer", True),
        ("Sales Manager", True),
        ("Chief of Staff", False),
    ],
)
def test_title_rules(title, rejected):
    assert (rules.check_card({**CARD, "title": title}) is not None) == rejected


def test_junior_and_applied_rejected():
    assert rules.check_card({**CARD, "grades": ["junior"]})
    assert rules.check_card({**CARD, "grades": ["junior", "middle"]}) is None
    assert rules.check_card({**CARD, "has_applied": True})


def test_ukrainian_vacancy_rejected():
    assert rules.check_card({**CARD, "vacancy_language": "uk"}) == "вакансия на украинском"
    ua = "Шукаємо продакт-менеджера. Ви будете відповідати за розвиток продукту, аналітику та команду."
    assert rules.check_detail(detail(description=ua), 7) == ("rules", "вакансия на украинском")
    ru = "Ищем продакт-менеджера в Київ и Москву. Отвечаете за развитие продукта, аналитику и команду."
    assert rules.check_detail(detail(description=ru), 7) is None
    assert rules.check_detail(detail(description="Product Manager, Kyiv office"), 7) is None


@pytest.mark.parametrize(
    "salary,rejected",
    [
        ({"currency": "RUB", "min": 120000, "max": 220000}, True),
        ({"currency": "RUB", "min": 150000, "max": None}, True),
        ({"currency": "RUB", "min": 200000, "max": 250000}, False),
        ({"currency": "RUB", "min": 250000, "max": None}, False),
        ({"currency": "RUB", "min": None, "max": None}, False),
        ({"currency": "RUB", "min": None, "max": 160000}, True),
        ({"currency": "RUB", "min": "150000", "max": None}, False),
        ({"currency": "USD", "min": 150, "max": 225}, False),
        ("150000 RUB", False),
        (None, False),
    ],
)
def test_low_rub_salary_rejected(salary, rejected):
    reason = rules.check_card({**CARD, "salary": salary})
    assert (reason is not None) == rejected
    if rejected:
        assert reason.startswith("зарплата до ")


def test_stale_detail():
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    assert rules.check_detail(detail(created_at=old), 7)[0] == "stale"
    assert rules.check_detail(detail(), 7) is None


# --- воркер ---


class FakeHirify:
    def __init__(self, d):
        self.d = d
        self.reads = 0

    async def read_vacancy(self, vacancy_id):
        self.reads += 1
        return self.d


class FakeScorer:
    def __init__(self, relevant=True, score=4, prefilter_relevant=True):
        self.relevant = relevant
        self.score = score
        self.prefilter_relevant = prefilter_relevant
        self.assessed = 0

    async def triage(self, card):
        return Triage(relevant=self.relevant, reason="не PM"), Usage("haiku", 100, 10, 0)

    async def prefilter(self, d, feeds):
        reason = "гибрид в Москве, релокации нет"
        return Triage(relevant=self.prefilter_relevant, reason=reason), Usage("haiku", 1200, 60, 3000, 0)

    async def assess(self, d, feeds):
        self.assessed += 1
        return assessment(self.score), Usage("sonnet", 5000, 400, 4000)


class FakeSink:
    def __init__(self):
        self.cards = []
        self.alerts = []

    async def send_card(self, v):
        self.cards.append(v)
        return 777

    async def alert(self, text):
        self.alerts.append(text)


async def run_one(db, scorer, d=None, prefilter=True):
    await db.upsert_card(CARD, [{"id": 1, "name": "PM remote"}], source="webhook")
    hirify, sink = FakeHirify(d or detail()), FakeSink()
    p = Pipeline(db, hirify, scorer, sink, min_score=4, max_age_days=7, prefilter=prefilter)
    await p.process_safely(await db.next_queued())
    return await db.get(CARD["slug"]), hirify, sink


async def test_pipeline_shows_good_vacancy(db):
    v, hirify, sink = await run_one(db, FakeScorer(score=5))
    assert v.status == "shown" and v.tg_message_id == 777 and v.score == 5
    assert v.company == "Remoteria" and len(sink.cards) == 1
    counts = await db.event_counts(None)
    assert counts["shown"] == 1 and counts["llm"] == 3


async def test_pipeline_triage_reject_does_not_open_vacancy(db):
    v, hirify, sink = await run_one(db, FakeScorer(relevant=False))
    assert v.status == "rejected" and v.stage == "triage"
    assert hirify.reads == 0 and not sink.cards


async def test_pipeline_prefilter_reject_skips_sonnet(db):
    scorer = FakeScorer(prefilter_relevant=False)
    v, hirify, sink = await run_one(db, scorer)
    assert v.status == "rejected" and v.stage == "prefilter"
    assert v.reason == "гибрид в Москве, релокации нет"
    assert hirify.reads == 1 and scorer.assessed == 0 and not sink.cards
    assert (await db.event_counts(None))["rejected:prefilter"] == 1


async def test_pipeline_prefilter_can_be_disabled(db):
    scorer = FakeScorer(prefilter_relevant=False, score=5)
    v, _, sink = await run_one(db, scorer, prefilter=False)
    assert v.status == "shown" and scorer.assessed == 1
    assert (await db.event_counts(None))["llm"] == 2


async def test_pipeline_logs_cache_writes(db):
    await run_one(db, FakeScorer(score=5))
    async with db.conn.execute("SELECT data FROM events WHERE type = 'llm'") as cur:
        steps = {json.loads(r["data"])["step"]: json.loads(r["data"]) async for r in cur}
    assert set(steps) == {"triage", "prefilter", "scoring"}
    assert steps["prefilter"]["cache_write_tokens"] == 0 and "cache_write_tokens" in steps["scoring"]


async def test_pipeline_low_score_rejected(db):
    v, _, sink = await run_one(db, FakeScorer(score=3))
    assert v.status == "rejected" and v.stage == "score" and v.reason.startswith("3/5")
    assert not sink.cards


async def test_pipeline_foreign_team_rejected_even_with_high_score(db):
    class Foreign(FakeScorer):
        async def assess(self, d, feeds):
            self.assessed += 1
            return assessment(5).model_copy(update={"ru_label": "foreign"}), Usage("sonnet", 5000, 400, 4000)

    v, _, sink = await run_one(db, Foreign())
    assert v.status == "rejected" and v.stage == "foreign" and v.score == 5
    assert not sink.cards


async def test_pipeline_rejects_company_skipped_before(db):
    other = {**CARD, "slug": "1000-other", "vacancy_id": 1000}
    await db.upsert_card(other, [], source="webhook")
    await db.update(other["slug"], status="skipped", reason="company", company="REMOTERIA ")
    scorer = FakeScorer(score=5)
    v, hirify, sink = await run_one(db, scorer)
    assert v.status == "rejected" and v.stage == "company"
    assert hirify.reads == 1 and scorer.assessed == 0 and not sink.cards


async def test_pipeline_drops_card_when_company_skipped_during_scoring(db):
    class SkipWhileScoring(FakeScorer):
        async def assess(self, d, feeds):
            other = {**CARD, "slug": "1000-other", "vacancy_id": 1000}
            await db.upsert_card(other, [], source="webhook")
            await db.update(other["slug"], status="skipped", reason="company", company="Remoteria")
            return await super().assess(d, feeds)

    v, _, sink = await run_one(db, SkipWhileScoring(score=5))
    assert v.status == "rejected" and v.stage == "company" and not sink.cards


async def test_pipeline_other_skip_reasons_do_not_block_company(db):
    other = {**CARD, "slug": "1000-other", "vacancy_id": 1000}
    await db.upsert_card(other, [], source="webhook")
    await db.update(other["slug"], status="skipped", reason="role", company="Remoteria")
    v, _, _ = await run_one(db, FakeScorer(score=5))
    assert v.status == "shown"


async def test_pipeline_error_retries_then_alerts(db):
    class Broken(FakeScorer):
        async def assess(self, d, feeds):
            raise RuntimeError("boom")

    await db.upsert_card(CARD, [], source="webhook")
    sink = FakeSink()
    p = Pipeline(db, FakeHirify(detail()), Broken(), sink, 4, 7)
    for _ in range(3):
        await p.process_safely(await db.get(CARD["slug"]))
    v = await db.get(CARD["slug"])
    assert v.status == "error" and v.attempts == 3 and len(sink.alerts) == 1


async def test_pipeline_hirify_outage_keeps_vacancy_queued(db):
    from jobagent.hirify import HirifyError

    class Down(FakeHirify):
        async def read_vacancy(self, vacancy_id):
            raise HirifyError(503, "http_error", "Service Unavailable")

    await db.upsert_card(CARD, [], source="webhook")
    p = Pipeline(db, Down(detail()), FakeScorer(), FakeSink(), 4, 7)
    pause = await p.process_safely(await db.next_queued())
    v = await db.get(CARD["slug"])
    assert pause == 120 and v.status == "queued" and v.attempts == 0


async def test_hirify_client_retries_503(monkeypatch):
    import httpx

    from jobagent import hirify as hmod

    monkeypatch.setattr(hmod, "RETRY_DELAYS", (0, 0, 0))
    calls = []

    def handler(request):
        calls.append(request.method)
        if len(calls) < 3:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={"ok": True, "data": {"plan": "plus"}, "meta": {}})

    client = hmod.HirifyClient("k")
    client._http = httpx.AsyncClient(base_url=hmod.BASE_URL, transport=httpx.MockTransport(handler))
    assert (await client.me())["plan"] == "plus" and len(calls) == 3

    calls.clear()
    with pytest.raises(hmod.HirifyError) as e:
        await client.create_webhook("x", "https://example.com")  # не повторяется
    assert e.value.status == 503 and len(calls) == 1
    await client.aclose()


async def test_baseline_runs_once_and_marks_cards(db):
    from jobagent.ingest import baseline_if_empty

    class Feeds:
        calls = 0

        async def list_feeds(self):
            return [{"id": 1, "name": "A", "is_active": True}]

        async def feed_vacancies(self, feed_id, page, per_page):
            Feeds.calls += 1
            return [CARD], {"last_page": 1}

    # вакансия из вебхука пришла раньше разметки: разметка всё равно должна пройти
    await db.upsert_card({**CARD, "slug": "1-other", "vacancy_id": 1}, [], source="webhook")
    await baseline_if_empty(db, Feeds(), [], 2, 50)
    await baseline_if_empty(db, Feeds(), [], 2, 50)
    assert Feeds.calls == 1
    assert (await db.get(CARD["slug"])).status == "baseline"


class Webhooks:
    """Hirify с вебхуками аккаунта: наш отключён после простоя, чужой тоже выключен."""

    def __init__(self, hooks):
        self.hooks = hooks
        self.patched = []

    async def list_webhooks(self):
        return self.hooks

    async def update_webhook(self, webhook_id, **fields):
        self.patched.append((webhook_id, fields))
        return {"id": webhook_id, "state": "active", "is_active": True}


async def test_disabled_webhook_is_reactivated(db):
    from jobagent.ingest import ensure_webhook_active

    h = Webhooks([
        {"id": 26, "url": "https://193-42-112-144.sslip.io/hirify/webhook", "state": "disabled", "is_active": False},
        {"id": 7, "url": "https://example.com/other", "state": "disabled", "is_active": False},
    ])
    assert await ensure_webhook_active(db, h) == [26]
    assert h.patched == [(26, {"is_active": True})]
    assert await db.has_event("webhook_reactivated")


async def test_active_webhook_left_alone(db):
    from jobagent.ingest import ensure_webhook_active

    h = Webhooks([{"id": 26, "url": "https://x.sslip.io/hirify/webhook", "state": "active", "is_active": True}])
    assert await ensure_webhook_active(db, h) == []
    assert h.patched == [] and not await db.has_event("webhook_reactivated")


async def test_webhook_check_failure_does_not_raise(db):
    from jobagent import hirify as hmod
    from jobagent.ingest import check_webhook

    class Down:
        async def list_webhooks(self):
            raise hmod.HirifyError(503, "unavailable", "down")

    await check_webhook(db, Down())  # сбой Hirify не роняет опрос фильтров


async def test_hirify_update_webhook_sends_patch():
    import httpx

    from jobagent import hirify as hmod

    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "data": {"id": 26, "is_active": True}, "meta": {}})

    client = hmod.HirifyClient("k")
    client._http = httpx.AsyncClient(base_url=hmod.BASE_URL, transport=httpx.MockTransport(handler))
    assert (await client.update_webhook(26, is_active=True))["is_active"] is True
    assert calls == [("PATCH", "/api/agent/webhooks/26", {"is_active": True})]
    await client.aclose()


# --- карточка и статистика ---


def test_card_render_escapes_and_contains_key_parts():
    a = assessment().model_dump()
    a["con"] = "нет опыта <EdTech> & LMS"
    text = render_card("t", a, [{"id": 1, "name": "PM remote"}], detail())
    assert "4/5 · Product Manager (EdTech)" in text
    assert "RU 🟡 текст на русском" in text and "&lt;EdTech&gt; &amp; LMS" in text
    assert "🔎 LinkedIn: CEO Иван Петров" in text
    assert "💰 150–250k RUB в месяц" in text and "🕒 сегодня" in text
    assert "❓ Уточнить: вилка в USD" in text
    # Подробности только в раскрытой карточке.
    assert DETAILS_HEADER not in text and "Яндекс.Метрика" not in text


def test_card_details_show_requirements_linkedin_people_and_channel():
    a = assessment().model_dump()
    research = {
        "people": [{"title": "Иван Петров - CEO - Remoteria | LinkedIn", "url": "https://linkedin.com/in/ip", "snippet": "Минск · БГУ"}],
        "company": {"name": "Remoteria", "url": "https://linkedin.com/company/r", "headquarters": "Limassol", "size": "11-50"},
    }
    text = render_full("t", a, [{"id": 1, "name": "PM remote"}], detail(linkedin_research=research), expanded=True)
    assert DETAILS_HEADER in text
    assert "✓ A/B-тесты" in text and "✗ Яндекс.Метрика" in text
    assert "CEO Иван Петров из Минска" in text
    assert '<a href="https://linkedin.com/in/ip">Иван Петров - CEO - Remoteria</a> — Минск · БГУ' in text
    assert "Limassol, 11-50" in text and "кнопка на Hirify" in text and "PM remote" in text


def test_old_assessment_without_new_fields_still_renders():
    a = assessment().model_dump()
    for k in ("ru_summary", "ru_linkedin", "questions"):
        a.pop(k)
    a["what_would_change"] = "уточнить вилку"
    text = render_full("t", a, [], detail(salary=None), expanded=True)
    assert "RU 🟡 текст на русском, CEO" in text and "❓ уточнить вилку" in text


def test_posted_ago():
    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    assert posted_ago("2026-10-27T01:00:00.000000Z", now) == "сегодня"
    assert posted_ago("2026-10-26T23:00:00Z", now) == "вчера"
    assert posted_ago("2026-10-22T10:00:00Z", now) == "5 дн. назад"
    assert posted_ago(None, now) == "" and posted_ago("bad", now) == ""


def test_card_is_cut_to_telegram_limit():
    a = assessment().model_dump()
    a["red_flags"] = ["флаг " * 60] * 30
    assert len(render_full("t", a, [], detail(), expanded=True)) <= 4096


def test_stats_render():
    counts = {"received": 10, "rejected:rules": 3, "rejected:score": 4, "shown": 3, "applied": 2, "interview": 1}
    text = render_stats(counts, {"terms": 1}, "за 7 дней")
    assert "Получено: 10" in text and "Отсеяно: 7 (70%)" in text
    assert "Показано: 3 (30%)" in text and "Интервью: 1 (50%)" in text
    assert "Условия или ЗП: 1" in text
    text = render_stats({"received": 2, "rejected:triage": 1, "rejected:prefilter": 1}, {}, "за 7 дней")
    assert "быстрая модель по карточке: 1" in text and "быстрая модель по тексту: 1" in text
    assert "Пакеты" not in text


def llm_usage(model, **tokens):
    base = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    return {"model": model, **base, **tokens}


def test_stats_render_packages():
    usages = [
        llm_usage("claude-opus-5-5", input_tokens=1_000_000),  # $4
        llm_usage("claude-haiku-5-5", output_tokens=1_000_000),  # $0.50
    ]
    text = render_stats({"package_ready": 2, "package_redo": 1}, {}, "за 7 дней", usages)
    assert "Пакеты: 2 собрано, 1 переделок, средняя стоимость $2.25" in text
    # Модель без цены (задана переменной окружения): средняя по известным ценам, её затраты — токенами.
    usages.append(llm_usage("claude-next", input_tokens=5, output_tokens=7, cache_read_tokens=11))
    usages.append(llm_usage("claude-next", input_tokens=1))
    text = render_stats({"package_ready": 2}, {}, "за 7 дней", usages)
    assert (
        "Пакеты: 2 собрано, 0 переделок, средняя стоимость $2.25 без claude-next; "
        "токены без цены: ввод 6, вывод 7, кеш чтение 11, кеш запись 0"
    ) in text
    # Сборки были, но ни одна не дошла до конца: средней нет.
    text = render_stats({"package_requested": 1}, {}, "за 7 дней", usages[:1])
    assert "Пакеты: 0 собрано, 0 переделок, потрачено $4.00" in text


def test_price_of_usage_counts_cache_tokens():
    from jobagent.pricing import usage_cost

    u = Usage("claude-sonnet-5-5", input_tokens=1_000_000, output_tokens=1_000_000,
              cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    assert usage_cost(u) == pytest.approx(2 + 10 + 0.20 + 4)
    assert usage_cost(llm_usage("claude-opus-5-5", cache_write_tokens=1_000_000)) == pytest.approx(8)
    assert usage_cost(llm_usage("claude-next", input_tokens=1)) is None


async def test_package_llm_usage_only_package_steps(db):
    await db.log_event("llm", "s1", step="scoring", **llm_usage("claude-sonnet-5-5", input_tokens=7))
    await db.log_event("llm", "s1", step="package_resume", **llm_usage("claude-opus-5-5", input_tokens=9))
    await db.log_event("package_ready", "s1", version=1)
    assert await db.llm_usage(None, step_prefix="package_") == [llm_usage("claude-opus-5-5", input_tokens=9)]
    assert await db.llm_usage(7, step_prefix="package_") == [llm_usage("claude-opus-5-5", input_tokens=9)]


def test_keyboard_keeps_status_when_details_toggle():
    from jobagent.bot import kb_for
    from jobagent.db import Vacancy

    def v(status, stage=None, reason=None):
        return Vacancy(CARD["slug"], 1071419, "t", None, [], CARD, detail(), None, 4, status, stage, reason, "webhook", 0, 1, None)

    def texts(markup):
        return [b.text for row in markup.inline_keyboard for b in row]

    assert texts(kb_for(v("shown"), True, package=True)) == [
        "🔗 Открыть", "📋 Свернуть", "📇 Контакт", "✅ Откликнулся", "⏭ Пропустить", "📝 Подготовить отклик"
    ]
    # Без контактов кандидата сборщика пакетов нет, и кнопки тоже.
    assert "📝 Подготовить отклик" not in texts(kb_for(v("shown"), True))
    assert "📋 Подробнее" in texts(kb_for(v("shown"), False))
    applied = texts(kb_for(v("applied"), True))
    assert applied[0] == "✅ Откликнулся" and "🎤 Позвали на интервью" in applied and "📋 Свернуть" in applied
    assert texts(kb_for(v("applied", stage="offer"), False))[0] == "🎉 Оффер"
    assert texts(kb_for(v("skipped", reason="terms"), False))[0] == "⏭ Пропущено: Условия или ЗП"


def test_contact_line_and_button_after_reveal():
    from jobagent.bot import kb_for
    from jobagent.cards import contact_line
    from jobagent.db import Vacancy

    contact = {"contacts": [{"type": "url", "value": "https://hh.ru/vacancy/138883370"},
                            {"type": "telegram", "value": "@hr_docs"}], "linkedin": None}
    d = detail(contact=contact)
    assert contact_line(detail()) == ""
    line = contact_line(d)
    assert '<a href="https://hh.ru/vacancy/138883370">hh.ru/vacancy/138883370</a>' in line
    assert '<a href="https://t.me/hr_docs">@hr_docs</a>' in line
    assert line in render_card("t", assessment().model_dump(), [], d)
    assert contact_line(detail(contact={"contacts": [], "linkedin": None})) == "📇 Контакт: Hirify не вернул контакт"

    v = Vacancy(CARD["slug"], 1071419, "t", None, [], CARD, d, None, 4, "shown", None, None, "webhook", 0, 1, None)
    buttons = [b for row in kb_for(v, False).inline_keyboard for b in row]
    contact_button = [b for b in buttons if b.text.startswith("📇")][0]
    assert contact_button.url == "https://hh.ru/vacancy/138883370" and contact_button.callback_data is None


async def test_hirify_reveal_returns_contacts_and_quota():
    import httpx

    from jobagent import hirify as hmod

    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={
            "ok": True,
            "data": {"contacts": [{"type": "url", "value": "https://hh.ru/vacancy/1"}], "linkedin": None, "charged": True},
            "meta": {"quota": {"remaining": 199}},
        })

    client = hmod.HirifyClient("k")
    client._http = httpx.AsyncClient(base_url=hmod.BASE_URL, transport=httpx.MockTransport(handler))
    data, meta = await client.reveal(1323476)
    assert calls == [("POST", "/api/agent/vacancies/1323476/reveal")]
    assert data["contacts"][0]["value"] == "https://hh.ru/vacancy/1" and meta["quota"]["remaining"] == 199
    await client.aclose()
