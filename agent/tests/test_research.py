"""RU-проверка по LinkedIn, несколько файлов резюме и новые правила грейда в промпте."""

import json

import httpx

from jobagent.config import AGENT_DIR
from jobagent.pipeline import Pipeline
from jobagent.research import LinkedInResearch, name_variants, skip_reason
from jobagent.scoring import CANDIDATE_BRIEF, MASTER_RESUME, load_knowledge, resume_files, vacancy_for_prompt
from test_service import CARD, FakeHirify, FakeScorer, FakeSink, db, detail  # noqa: F401 (db — фикстура)

PEOPLE = {
    "credits_charged": 1,
    "results": [
        {"title": "Dmitry Schetnikovich - CEO @ Paralect", "url": "https://www.linkedin.com/in/schetnikovich", "description": "CEO"},
        {"title": "Peter Shimanski - QA – Paralect", "url": "https://by.linkedin.com/in/peter", "description": "Minsk"},
    ],
}
PAGES = {"credits_charged": 1, "results": [{"title": "Paralect", "url": "https://www.linkedin.com/company/paralect?trk=x"}]}
COMPANY = {
    "credits_charged": 1,
    "name": "Paralect",
    "headquarters": "Middletown, Delaware",
    "logo": "https://img",
    "posts": [{"text": "пост"}],
    "employees": [{"name": "Shaleen Shah", "title": "", "image": "https://img"}],
    "similarPages": [{"link": "https://by.linkedin.com/company/vadarod", "name": "VADAROD", "image": "x"}],
}


def scrapecreators(captured: list, fail: bool = False) -> LinkedInResearch:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if fail:
            return httpx.Response(500, json={"success": False})
        if request.url.path == "/v1/linkedin/company":
            return httpx.Response(200, json=COMPANY)
        q = request.url.params["query"]
        return httpx.Response(200, json=PEOPLE if "linkedin.com/in" in q else PAGES)

    r = LinkedInResearch("key")
    r._http = httpx.AsyncClient(base_url="https://api.scrapecreators.com", headers={"x-api-key": "key"},
                                transport=httpx.MockTransport(handler))
    return r


async def test_research_collects_people_and_company_page():
    captured = []
    r = scrapecreators(captured)
    out = await r.research("Paralect")
    assert [p["url"] for p in out["people"]][1] == "https://by.linkedin.com/in/peter"
    assert out["company"]["headquarters"] == "Middletown, Delaware"
    assert out["company"]["similar_pages"] == [{"name": "VADAROD", "link": "https://by.linkedin.com/company/vadarod"}]
    assert "posts" not in out["company"] and "logo" not in out["company"]
    company_req = [q for q in captured if q.url.path == "/v1/linkedin/company"][0]
    assert company_req.url.params["url"] == "https://www.linkedin.com/company/paralect"
    assert all(q.headers["x-api-key"] == "key" for q in captured)
    assert r.credits_used == 3


async def test_research_failure_and_hidden_company_return_none():
    captured = []
    assert await scrapecreators(captured, fail=True).research("Paralect") is None
    assert await scrapecreators(captured).research(None) is None


def test_name_variants_find_latin_spelling():
    desc = "Компания DocsInBox ищет Product Owner. Работаем с ЕГАИС и B2B SaaS."
    assert name_variants("Доксинбокс", desc)[:2] == ["DocsInBox", "Doksinboks"]
    assert name_variants("Доксинбокс (DocsInBox)")[0] == "DocsInBox"
    assert name_variants("Марс", "FMCG, Mars Wrigley")[0] == "Mars"
    assert name_variants("Paralect") == ["Paralect"]
    assert name_variants("") == []


async def test_research_tries_next_spelling_after_empty_search():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        q = request.url.params.get("query", "")
        if "linkedin.com/in" in q and "docsinbox" not in q.lower():
            return httpx.Response(404, json={"success": False, "error": "not_found", "credits_charged": 0})
        if request.url.path == "/v1/linkedin/company":
            return httpx.Response(200, json=COMPANY)
        return httpx.Response(200, json=PEOPLE if "linkedin.com/in" in q else PAGES)

    r = LinkedInResearch("key")
    r._http = httpx.AsyncClient(base_url="https://api.scrapecreators.com", transport=httpx.MockTransport(handler))
    out = await r.research("Доксинбокс", "Мы — Docsinbox, B2B SaaS")
    assert out["query_company"] == "Docsinbox" and len(out["people"]) == 2
    assert r.credits_used == 3  # пустой поиск бесплатный


class FakeResearch:
    def __init__(self):
        self.credits_used = 0
        self.calls = []

    async def research(self, company, description=""):
        self.calls.append(company)
        self.credits_used += 3
        return {"query_company": company, "people": [{"title": "CEO"}]}


async def test_pipeline_adds_linkedin_research_before_scoring(db):
    seen = {}

    class Scorer(FakeScorer):
        async def assess(self, d, feeds):
            seen["research"] = d.get("linkedin_research")
            return await super().assess(d, feeds)

    await db.upsert_card(CARD, [{"id": 1, "name": "PM remote"}], source="webhook")
    research = FakeResearch()
    p = Pipeline(db, FakeHirify(detail(vacancy_language="en")), Scorer(), FakeSink(), 4, 7, research)
    await p.process_safely(await db.next_queued())
    assert research.calls == [detail()["company"]]
    assert seen["research"]["people"] == [{"title": "CEO"}]
    saved = await db.get(CARD["slug"])
    assert saved.detail["linkedin_research"]["query_company"] == detail()["company"]


async def test_pipeline_prefilter_reject_spends_no_research_credits(db):
    await db.upsert_card(CARD, [{"id": 1, "name": "PM remote"}], source="webhook")
    research = FakeResearch()
    scorer = FakeScorer(prefilter_relevant=False)
    p = Pipeline(db, FakeHirify(detail(vacancy_language="en")), scorer, FakeSink(), 4, 7, research)
    await p.process_safely(await db.next_queued())
    assert research.calls == [] and scorer.assessed == 0


def test_skip_reason_checks_only_english_vacancies_outside_russia():
    assert skip_reason(detail(vacancy_language="ru")) == "вакансия на русском"
    assert skip_reason(detail(vacancy_language="en", regions=[{"code": "russia"}])) == "компания в России"
    assert skip_reason(detail(vacancy_language="en", macroregion="russia")) == "компания в России"
    assert skip_reason(detail(vacancy_language="en", company_masked=True)) == "компания скрыта"
    assert skip_reason(detail(vacancy_language=None, description="We are hiring a PM")) is None
    assert skip_reason(detail(vacancy_language=None)) == "вакансия на русском"
    assert skip_reason(detail(vacancy_language="en", regions=[{"code": "serbia"}])) is None


async def test_pipeline_skips_research_for_russian_vacancy(db):
    await db.upsert_card(CARD, [{"id": 1, "name": "PM remote"}], source="webhook")
    research = FakeResearch()
    p = Pipeline(db, FakeHirify(detail()), FakeScorer(), FakeSink(), 4, 7, research)
    await p.process_safely(await db.next_queued())
    assert research.calls == []


async def test_pipeline_skips_research_for_masked_company(db):
    await db.upsert_card(CARD, [{"id": 1, "name": "PM remote"}], source="webhook")
    research = FakeResearch()
    p = Pipeline(db, FakeHirify({**detail(), "company_masked": True}), FakeScorer(), FakeSink(), 4, 7, research)
    await p.process_safely(await db.next_queued())
    assert research.calls == []


def test_vacancy_prompt_includes_linkedin_block():
    text = vacancy_for_prompt({**detail(), "linkedin_research": {"people": [{"title": "CEO"}]}}, [])
    assert "<linkedin_research>" in text and '"CEO"' in text


def test_resume_files_experience_first_then_other_notes(tmp_path):
    (tmp_path / "search_criteria.md").write_text("criteria", encoding="utf-8")
    (tmp_path / "experience").mkdir()
    for name in (MASTER_RESUME, "aaa.md", "README.md"):
        (tmp_path / "experience" / name).write_text(name, encoding="utf-8")
    (tmp_path / "Bachinin_Alexander_Resume_PMM.md").write_text("old copy", encoding="utf-8")
    names = [p.name for p in resume_files(tmp_path)]
    assert names == [MASTER_RESUME, "aaa.md"]
    resume = load_knowledge(tmp_path)["resume"]
    assert f'<file name="experience/{MASTER_RESUME}">' in resume
    assert "README" not in resume and "old copy" not in resume


def test_real_knowledge_dir_loads_experience_file():
    resume = load_knowledge(AGENT_DIR.parent)["resume"]
    assert resume.startswith(f'<file name="experience/{MASTER_RESUME}">')
    assert "Не засчитывать" in resume and "GPT Scrambler" in resume


def test_brief_accepts_middle_and_c_level():
    assert "middle" in CANDIDATE_BRIEF and "C-level" in CANDIDATE_BRIEF
