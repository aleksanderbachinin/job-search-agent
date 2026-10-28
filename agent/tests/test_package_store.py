"""Хранение пакетов: версии в базе и файлы на диске, права, события без текста."""

import json
import stat

import pytest

from jobagent.config import AGENT_DIR
from jobagent.db import Database
from jobagent.facts import parse_facts
from jobagent.factcheck import Violation
from jobagent.package import BuiltPackage
from jobagent.package_models import Claim, Job, LetterDraft, LetterSentence, Requirement, RequirementMap, ResumeDraft
from jobagent.package_store import PackageStore
from jobagent.pdf import Contacts

EXPERIENCE = AGENT_DIR.parent / "experience" / "Bachinin_Alexander_Experience.md"
SLUG = "1071419-ai-product-builder-acme"

CONTACTS = Contacts(
    email="alexander@example.com",
    linkedin="https://www.linkedin.com/in/example/",
    telegram="https://t.me/example",
    location_line="UTC+3 · Remote",
)

# Слова, которых не должно быть в событиях: они живут только в тексте пакета и в правке.
RESUME_WORD = "zyxresumeword"
LETTER_WORD = "zyxletterword"
DETAIL_WORD = "zyxdetailword"
WARNING_WORD = "zyxwarningword"
FEEDBACK_WORD = "zyxfeedbackword"


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.sqlite3")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def store(db, tmp_path):
    return PackageStore(db, tmp_path / "packages", parse_facts(EXPERIENCE), CONTACTS)


def claim(text: str, *facts: str) -> Claim:
    return Claim(text=text, facts=list(facts))


def built() -> BuiltPackage:
    resume = ResumeDraft(
        headline="AI PM / AI Product Builder",
        summary=[claim(f"Launched an analytics tool, {RESUME_WORD}.", "MPS-1")],
        jobs=[Job(employer="MPS", title="Product Manager", bullets=[claim("Ran customer interviews.", "MPS-1")])],
        skills=[claim("SQL, GA4", "SKILL-3")],
    )
    letter = LetterDraft(
        subject="AI Product Builder at Acme",
        salutation="Dear Acme Hiring Team,",
        sentences=[
            LetterSentence(text=f"I launched a tool, {LETTER_WORD}.", facts=["MPS-1"], paragraph=1),
            LetterSentence(text="I am free for a call.", facts=[], paragraph=2),
        ],
        closing="Happy to talk.",
    )
    req_map = RequirementMap(
        role_type="builder",
        requirements=[
            Requirement(text="A/B tests", facts=["MPS-1"], status="closed"),
            Requirement(text="Strong Python", facts=[], status="gap"),
        ],
    )
    return BuiltPackage(
        lang="en",
        role_type="builder",
        req_map=req_map,
        resume=resume,
        letter=letter,
        letter_text=f"Subject: AI Product Builder at Acme\n\nDear Acme Hiring Team,\n\nI launched a tool, {LETTER_WORD}.\n",
        pdf=b"%PDF-1.4 fake bytes\x00\xff",
        filename="Bachinin_Alexander_Acme_AIProductBuilder.pdf",
        removed=[Violation(path="jobs[0].bullets[1]", code="unknown_fact", detail=f"GPTS-99 {DETAIL_WORD}")],
        warnings=[f"⚠️ проверь: stop phrase {WARNING_WORD}"],
        cut_steps=["drop:AIC"],
    )


async def test_versions_increment(db, store):
    assert await store.start(SLUG) == 1
    assert await store.start(SLUG) == 2
    assert await store.start("other-slug") == 1
    assert await db.building_package(SLUG) == 2
    row = await db.get_package(SLUG, 1)
    assert row["status"] == "building"
    assert row["path"] is None


async def test_save_and_load_roundtrip(db, store):
    v = await store.start(SLUG)
    original = built()
    path = await store.save(SLUG, v, original, f"make it shorter, {FEEDBACK_WORD}")

    assert path == store.root / SLUG / "v1"
    names = {p.name for p in path.iterdir()}
    assert names == {
        "resume.json", "resume.md", original.filename, "letter.md",
        "experience_map.md", "experience_map.json", "check.json", "feedback.txt",
    }
    assert (path / "feedback.txt").read_text(encoding="utf-8") == f"make it shorter, {FEEDBACK_WORD}"
    assert (path / "letter.md").read_text(encoding="utf-8") == original.letter_text
    assert "<!-- MPS-1 -->" in (path / "resume.md").read_text(encoding="utf-8")
    row = await db.get_package(SLUG, v)
    assert row["status"] == "ready"
    assert row["path"] == str(path)
    assert await db.building_package(SLUG) is None

    loaded = await store.load(SLUG, v)
    assert loaded == original  # включая байты PDF, removed.detail и warnings


async def test_no_feedback_no_file(store):
    v = await store.start(SLUG)
    path = await store.save(SLUG, v, built(), None)
    assert not (path / "feedback.txt").exists()


async def test_files_private(store):
    v = await store.start(SLUG)
    path = await store.save(SLUG, v, built(), "правка")
    for d in (store.root, store.root / SLUG, path):
        assert d.stat().st_mode & 0o077 == 0
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    files = list(path.iterdir())
    assert files
    for f in files:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600


async def test_restart_fails_stale_building(db, store):
    v = await store.start(SLUG)
    assert await db.fail_stale_packages() == 1
    assert (await db.get_package(SLUG, v))["status"] == "failed"
    assert await db.building_package(SLUG) is None
    assert await db.fail_stale_packages() == 0


async def test_events_have_no_text(db, store):
    v = await store.start(SLUG)
    await store.save(SLUG, v, built(), f"правка {FEEDBACK_WORD}")
    v2 = await store.start(SLUG)
    await store.fail(SLUG, v2, "api_error")

    async with db.conn.execute("SELECT type, slug, data FROM events WHERE type LIKE 'package_%' ORDER BY id") as cur:
        rows = [(r["type"], r["slug"], json.loads(r["data"])) for r in await cur.fetchall()]
    assert rows == [
        ("package_ready", SLUG, {
            "version": 1,
            "removed": [{"path": "jobs[0].bullets[1]", "code": "unknown_fact"}],
            "warnings": 1,
            "cut_steps": ["drop:AIC"],
        }),
        ("package_failed", SLUG, {"version": 2, "code": "api_error"}),
    ]
    dump = json.dumps(rows, ensure_ascii=False)
    for word in (RESUME_WORD, LETTER_WORD, DETAIL_WORD, WARNING_WORD, FEEDBACK_WORD):
        assert word not in dump
    assert (await db.get_package(SLUG, v2))["status"] == "failed"
    # подробности для разбора лежат только на диске
    check = json.loads((store.root / SLUG / "v1" / "check.json").read_text(encoding="utf-8"))
    assert DETAIL_WORD in json.dumps(check)


async def test_stale_restart_logs_code_only(db, store):
    await store.start(SLUG)
    await db.fail_stale_packages()
    async with db.conn.execute("SELECT data FROM events WHERE type = 'package_failed'") as cur:
        assert [json.loads(r["data"]) for r in await cur.fetchall()] == [{"version": 1, "code": "restart"}]


async def test_set_package_rejects_unknown_field(db, store):
    v = await store.start(SLUG)
    with pytest.raises(ValueError):
        await db.set_package(SLUG, v, created_at="x")
    await db.set_package(SLUG, v, tg_message_id=42)
    assert (await db.get_package(SLUG, v))["tg_message_id"] == 42
    assert await db.get_package(SLUG, 99) is None


async def test_slug_cannot_escape_root(store):
    with pytest.raises(ValueError):
        await store.start("../evil")
