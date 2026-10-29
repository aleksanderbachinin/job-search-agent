"""PDF резюме: одна страница A4, стили не сжимаются, лишнее содержание режется по CUT_ORDER."""

import re
from pathlib import Path

import pytest

from jobagent.facts import parse_facts
from jobagent.package_models import Claim, Job, ResumeDraft
from jobagent.pdf import STYLES, Contacts, LOCATION_LINE, PackageError, fit_one_page, render_pdf, to_markdown

EXPERIENCE = Path(__file__).resolve().parents[2] / "experience" / "Bachinin_Alexander_Experience.md"

CONTACTS = Contacts(
    email="alexander@example.com",
    linkedin="https://www.linkedin.com/in/example/",
    telegram="https://t.me/example",
    location_line=LOCATION_LINE["en"],
)
CONTACTS_RU = Contacts(
    email=CONTACTS.email,
    linkedin=CONTACTS.linkedin,
    telegram=CONTACTS.telegram,
    location_line=LOCATION_LINE["ru"],
)


@pytest.fixture(scope="module")
def fb():
    return parse_facts(EXPERIENCE)


def pdf_pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf))


def c(text: str, *facts: str) -> Claim:
    return Claim(text=text, facts=list(facts))


# Пункты реальной длины (130-180 знаков, как в резюме, отправленных вручную). Номера фактов настоящие.
B = {
    "VIBE": [
        c("Designed the agent architecture of an internal AI platform: step order, agent roles, what passes between agents and where a human confirms.", "VIBE-2"),
        c("Worked AI-first: wrote PRDs with AI, built platform parts with Claude Code and created agents that test the interface before release.", "VIBE-6"),
        c("Interviewed internal users and turned the findings into roadmap priorities; reworked the UX and added voice input to the platform.", "VIBE-7", "VIBE-9"),
        c("Specified the part of the platform through which generated tools call the model via the corporate Claude Team subscription.", "VIBE-5"),
    ],
    "GPTS": [
        c("Found the opportunity through search trends, TikTok signals and competitor analysis; set the positioning and the pricing of the product.", "GPTS-1"),
        c("Ran landing and onboarding experiments: 75% site-to-registration conversion and 81% activation, the same on mobile.", "GPTS-2"),
        c("Built the revenue analytics myself in Paddle, ProfitWell, GA4 and Metabase: average LTV $14, lifetime about 2.5 months.", "GPTS-6", "GPTS-8"),
        c("Chose and benchmarked text LLMs, comparing output quality with competitors through GPTZero and ZeroGPT checks.", "GPTS-5"),
    ],
    "MPS": [
        c("Interviewed sellers, found an unmet need and launched a new analytics tool that reached 1,000+ MAU.", "MPS-1"),
        c("Sign-up experiments removed friction points and raised registration conversion by 25% (GA4, SQL).", "MPS-2", "MPS-6"),
        c("Grew paid users of the SEO module by 25% in 5 months, working with data science on funnel metrics and data quality.", "MPS-3", "MPS-5"),
        c("Introduced AI tools for processing user requests and wrote the prompts the team kept using: processing time halved.", "MPS-4"),
    ],
    "SKK": [
        c("Led a cross-functional team of UI/UX, frontend, backend, QA and DevOps on 9+ projects, including CPA integrations.", "SKK-1"),
        c("Set up Scrum: in-sprint task completion grew from 25% to 40%; customer-reported bugs fell by 15%.", "SKK-2", "SKK-4"),
    ],
    "STR": [
        c("Ran 45 customer interviews (JTBD), launched the MVP in 1 month and signed 17 paying clients from scratch in 3 months.", "STR-1", "STR-2"),
        c("Managed the launch marketing budget and Meta and TikTok campaigns; led a team of 3.", "STR-3", "STR-4"),
    ],
    "AEZ": [
        c("Built the product from zero to $18K MRR with 6.6% average monthly growth; raised paid conversion from 1.5% to 2.5%.", "AEZ-1", "AEZ-2"),
        c("Localized into English, Chinese and Portuguese: +30% paying users from these markets in the first month.", "AEZ-3"),
    ],
    "RLY": [
        c("Built content production end to end: trend search, hooks from winning ads, AI creator personas, video generation and publishing.", "RLY-1"),
        c("The first content for a new client ships in 48-72 hours at about $6 per creative variation.", "RLY-2"),
        c("Built a distributed publishing network with geotargeting on the US and other Tier-1 markets; hired and managed contractors.", "RLY-4", "RLY-5"),
    ],
    "VERDA": [
        c("Chose a non-standard acquisition channel before launch (viral AI videos on Facebook and Instagram) and built the launch around it.", "VERDA-1"),
    ],
    "AIC": [
        c("Produced UGC videos for SaaS, mobile apps, skincare and wellness for TikTok, Reels, Shorts and Facebook.", "AIC-1"),
    ],
}

SUMMARY = [
    c(
        "Product manager who owns problems end to end and builds hands-on with AI. Launched an analytics tool for marketplace sellers "
        "(1,000+ MAU), grew a subscription SaaS to $18K MRR and run an LLM product where I compute the revenue metrics myself.",
        "MPS-1", "AEZ-1", "GPTS-6",
    )
]
SKILLS = [
    c("**AI building:** Claude Code, agentic coding, prompt design, agent workflows, human-in-the-loop, LLM benchmarking", "SKILL-2"),
    c("**Data & product:** SQL, GA4, Metabase, Paddle, ProfitWell; cohorts, LTV, churn; discovery, JTBD, A/B tests", "SKILL-1", "SKILL-3"),
]
TITLES = {
    "VIBE": "Product Manager, UX & AI Development",
    "GPTS": "Co-founder & Head of Product",
    "MPS": "Product Manager",
    "SKK": "Project Manager",
    "STR": "Product Manager",
    "AEZ": "Product Manager & Co-founder",
    "RLY": "Head of Content Production",
    "VERDA": "Co-founder, Growth",
    "AIC": "AI Content Producer",
}


def draft(codes_counts: list[tuple[str, int]], headline="AI Product Builder | Hands-on with Claude Code & AI Agents") -> ResumeDraft:
    return ResumeDraft(
        headline=headline,
        summary=SUMMARY,
        jobs=[Job(employer=code, title=TITLES[code], bullets=B[code][:n]) for code, n in codes_counts],
        skills=SKILLS,
    )


SHORT_DRAFT = draft([("VIBE", 3), ("GPTS", 3), ("MPS", 3), ("SKK", 1), ("STR", 1), ("AEZ", 1)])

SHORT_DRAFT_RU = ResumeDraft(
    headline="AI Product Builder | Claude Code и AI-агенты | Growth и выручка",
    summary=[c("Продакт-менеджер, который отвечает за задачу от начала до конца и строит продукты вместе с AI. Запустил аналитический "
               "инструмент для продавцов маркетплейсов (1 000+ MAU) и вывел подписочный SaaS на $18K MRR.", "MPS-1", "AEZ-1")],
    jobs=[
        Job(employer="VIBE", title="Product Manager, UX & AI Development", bullets=[
            c("Спроектировал агентную архитектуру внутренней AI-платформы: порядок шагов, роли агентов и точки, где человек подтверждает результат.", "VIBE-2"),
            c("Работал AI-first: PRD писал с AI, части платформы собирал через Claude Code, создал агентов, которые тестируют интерфейс.", "VIBE-6"),
            c("Интервью с внутренними пользователями превращал в приоритеты роадмапа, переработал UX и добавил голосовой ввод.", "VIBE-7", "VIBE-9"),
        ]),
        Job(employer="MPS", title="Product Manager", bullets=[
            c("Провёл интервью, нашёл незакрытую потребность и запустил новый аналитический инструмент: 1 000+ MAU.", "MPS-1"),
        ]),
        Job(employer="AEZ", title="Product Manager & Co-founder", bullets=[
            c("С нуля до $18K MRR при стабильном росте 6,6% в месяц; платная конверсия выросла с 1,5% до 2,5%.", "AEZ-1", "AEZ-2"),
        ]),
    ],
    skills=[c("**AI:** Claude Code, агенты, human-in-the-loop, оценка моделей", "SKILL-2")],
)

# Все места по максимуму: 4 пункта у первых трёх, 2 у остальных. Не помещается без подрезки Sidekick.
LONG_DRAFT = draft([("VIBE", 4), ("GPTS", 4), ("MPS", 4), ("SKK", 2), ("STR", 2), ("AEZ", 2)])

# Growth: 9 мест, минимум пунктов (3 у первых трёх, 1 у остальных), пункты реальной длины.
GROWTH_DRAFT = draft(
    [("RLY", 3), ("VIBE", 3), ("GPTS", 3), ("VERDA", 1), ("AIC", 1), ("MPS", 1), ("SKK", 1), ("STR", 1), ("AEZ", 1)],
    headline="Growth Product Manager | AI Video Creatives | Revenue Analytics",
)


def test_leading_invariant():
    assert STYLES
    assert all(s.leading >= 1.25 * s.fontSize for s in STYLES.values())
    assert all(s.fontName.startswith("DejaVuSans") for s in STYLES.values())


def test_short_resume_one_page(fb):
    pdf, pages = render_pdf(SHORT_DRAFT, fb, CONTACTS, "en")
    assert pages == 1 and pdf_pages(pdf) == 1


def test_long_resume_is_cut_not_squeezed(fb):
    leadings = {k: s.leading for k, s in STYLES.items()}
    draft_, pdf, steps = fit_one_page(LONG_DRAFT, fb, CONTACTS, "en", "product")
    assert pdf_pages(pdf) == 1 and steps[0] == "trim:SKK"
    assert "AEZ" in [j.employer for j in draft_.jobs]
    assert {k: s.leading for k, s in STYLES.items()} == leadings


def test_fit_keeps_draft_that_already_fits(fb):
    draft_, pdf, steps = fit_one_page(SHORT_DRAFT, fb, CONTACTS, "en", "builder")
    assert steps == [] and draft_ == SHORT_DRAFT and pdf_pages(pdf) == 1


def test_growth_resume_is_fitted_by_dropping_ai_content(fb):
    # 9 мест с минимумом пунктов не помещаются на страницу: по решению Александра убирается AI-контент.
    _, pages = render_pdf(GROWTH_DRAFT, fb, CONTACTS, "en")
    assert pages == 2
    draft_, pdf, steps = fit_one_page(GROWTH_DRAFT, fb, CONTACTS, "en", "growth")
    assert pdf_pages(pdf) == 1 and steps == ["drop:AIC"]
    jobs = {j.employer: j for j in draft_.jobs}
    assert "AIC" not in jobs and "VERDA" in jobs and "AEZ" in jobs
    assert [len(jobs[code].bullets) for code in ("RLY", "VIBE", "GPTS")] == [3, 3, 3]


def test_non_growth_draft_never_drops_ai_content_or_verda(fb):
    bloated = SHORT_DRAFT.model_copy(update={"jobs": [
        j.model_copy(update={"bullets": [c("Word " * 200, "SKK-1")]}) if j.employer == "SKK" else j
        for j in SHORT_DRAFT.jobs
    ]})
    _, _, steps = fit_one_page(bloated, fb, CONTACTS, "en", "builder")
    assert steps == ["drop:SKK"]
    with pytest.raises(PackageError):
        fit_one_page(LONG_DRAFT.model_copy(update={"summary": [c("Word " * 700, "MPS-1")]}), fb, CONTACTS, "en", "product")


def test_english_name_of_ai_content_is_english(fb):
    pdf, _ = render_pdf(GROWTH_DRAFT, fb, CONTACTS, "en")
    assert "AI content and own experiments" in to_markdown(GROWTH_DRAFT, fb, CONTACTS, "en")
    assert "AI-контент" in to_markdown(GROWTH_DRAFT, fb, CONTACTS_RU, "ru")


def test_impossible_resume_raises(fb):
    huge = ResumeDraft(
        headline="Product Manager",
        summary=SUMMARY,
        jobs=[
            Job(employer=code, title=TITLES[code], bullets=[c("Word " * 80, "MPS-1") for _ in range(4)])
            for code in ("VIBE", "GPTS", "MPS", "SKK", "STR", "AEZ")
        ],
        skills=SKILLS,
    )
    with pytest.raises(PackageError, match="не помещается на страницу") as err:
        fit_one_page(huge, fb, CONTACTS, "en", "builder")
    assert err.value.code == "does_not_fit"


def test_sidekick_is_dropped_last_and_required_employers_stay(fb):
    bloated = SHORT_DRAFT.model_copy(update={"jobs": [
        j.model_copy(update={"bullets": [c("Word " * 200, "SKK-1")]}) if j.employer == "SKK" else j
        for j in SHORT_DRAFT.jobs
    ]})
    draft_, pdf, steps = fit_one_page(bloated, fb, CONTACTS, "en", "builder")
    assert steps == ["drop:SKK"] and pdf_pages(pdf) == 1
    assert [j.employer for j in draft_.jobs] == ["VIBE", "GPTS", "MPS", "STR", "AEZ"]


def test_russian_resume_renders_cyrillic(fb):
    pdf, pages = render_pdf(SHORT_DRAFT_RU, fb, CONTACTS_RU, "ru")
    assert pages == 1 and b"DejaVuSans" in pdf


def test_markdown_keeps_fact_ids(fb):
    md = to_markdown(SHORT_DRAFT, fb, CONTACTS)
    assert "<!-- MPS-1 -->" in md
    assert "<!-- SKILL-2 -->" in md
    assert "Jan 2024 – Aug 2025" in md  # даты места работы из файла опыта
    assert "MPSTATS" in md and SHORT_DRAFT.headline in md


def test_telegram_is_optional_in_pdf_and_markdown(fb):
    no_tg = Contacts(email=CONTACTS.email, linkedin=CONTACTS.linkedin, telegram="", location_line=LOCATION_LINE["en"])
    with_tg, _ = render_pdf(SHORT_DRAFT, fb, CONTACTS, "en")
    without_tg, _ = render_pdf(SHORT_DRAFT, fb, no_tg, "en")
    # Ссылки в PDF — аннотации /URI: без Telegram остаётся только LinkedIn, пустой ссылки нет.
    assert b"t.me/example" in with_tg and b"linkedin.com/in/example" in with_tg
    assert b"linkedin.com/in/example" in without_tg
    assert without_tg.count(b"/URI") * 2 == with_tg.count(b"/URI") and b"/URI ()" not in without_tg
    md = to_markdown(SHORT_DRAFT, fb, no_tg, "en")
    assert "Telegram" not in md
    assert f"{CONTACTS.email} | [LinkedIn]({CONTACTS.linkedin})\n" in md
    assert "[Telegram](https://t.me/example)" in to_markdown(SHORT_DRAFT, fb, CONTACTS, "en")


def test_long_summary_and_skills_are_trimmed_after_employers(fb):
    # Как черновик Aiphoria: места работы уже ужаты до предела, а summary и навыки длинные.
    sentence = c("Product manager who owns problems end to end and builds hands-on with AI tools every day. " * 2, "MPS-1")
    skill = c("**AI building:** Claude Code, agentic coding, prompt design, agent workflows, LLM benchmarking", "SKILL-2")
    heavy = draft([("VIBE", 3), ("GPTS", 3), ("MPS", 3), ("STR", 1), ("AEZ", 2)]).model_copy(
        update={"summary": [sentence] * 6, "skills": [skill] * 10}
    )
    fitted, pdf, steps = fit_one_page(heavy, fb, CONTACTS, "en", "product")
    assert pdf_pages(pdf) == 1
    assert "trim_skills" in steps and all(s in ("trim_skills", "trim_summary") for s in steps)
    assert len(fitted.skills) >= 3 and len(fitted.summary) >= 2
    assert [len(j.bullets) for j in fitted.jobs] == [3, 3, 3, 1, 2]
