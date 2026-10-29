"""Проверка фактов пакета: структуры ответов, правила состава и проверка утверждений по фактам."""

import re

import pytest
from pydantic import ValidationError

from jobagent.config import AGENT_DIR
from jobagent.factcheck import Violation, check_claim, check_letter, check_resume
from jobagent.facts import parse_facts
from jobagent.package_models import (
    Claim,
    Job,
    LetterDraft,
    LetterSentence,
    Requirement,
    RequirementMap,
    ResumeDraft,
)
from jobagent.package_rules import (
    COMPOSITION,
    CUT_ORDER,
    EMPLOYER_NAMES,
    FORBIDDEN,
    NEVER,
    REQUIRED,
    STOP_PHRASES,
    allowed_fact_employers,
    bullet_limits,
)

KNOWLEDGE_DIR = AGENT_DIR.parent
EXPERIENCE = KNOWLEDGE_DIR / "experience" / "Bachinin_Alexander_Experience.md"


# ---------- структуры ответов ----------

def test_requirement_map_roundtrip():
    data = {
        "role_type": "growth",
        "requirements": [{"text": "Опыт A/B-тестов", "facts": ["RLY-7"], "status": "closed"}],
    }
    parsed = RequirementMap.model_validate(data)
    assert parsed.requirements[0].status == "closed"
    assert RequirementMap.model_validate(parsed.model_dump()) == parsed


def test_requirement_rejects_unknown_status_and_role_type():
    with pytest.raises(ValidationError):
        Requirement(text="x", facts=[], status="maybe")
    with pytest.raises(ValidationError):
        RequirementMap(role_type="cto", requirements=[])


def test_claim_requirement_is_optional():
    assert Claim(text="t", facts=["MPS-1"]).requirement is None


def test_resume_and_letter_drafts_validate():
    resume = ResumeDraft(
        headline="Growth PM",
        summary=[Claim(text="s", facts=["PROF-1"])],
        jobs=[Job(employer="MPS", title="PM", bullets=[Claim(text="b", facts=["MPS-1"], requirement="r")])],
        skills=[Claim(text="SQL", facts=["SKILL-7"])],
    )
    assert resume.jobs[0].bullets[0].requirement == "r"
    letter = LetterDraft(
        subject=None,
        salutation="Hello",
        sentences=[LetterSentence(text="Hook", facts=[], paragraph=1)],
        closing="Best regards",
    )
    assert letter.sentences[0].paragraph == 1 and letter.sentences[0].facts == []
    with pytest.raises(ValidationError):
        LetterSentence(text="no paragraph", facts=[])


# ---------- состав ----------

def test_composition_rules():
    assert "CK" not in COMPOSITION["builder"] and "VIBE" in REQUIRED["builder"]
    assert all("AEZ" in REQUIRED[t] for t in COMPOSITION)
    assert not NEVER & set().union(*COMPOSITION.values())
    assert allowed_fact_employers("VIBE", "growth") == {"VIBE", "CK"}
    assert allowed_fact_employers("VIBE", "builder") == {"VIBE"}
    assert bullet_limits(0) == (3, 4) and bullet_limits(3) == (1, 2)


def test_composition_exact_order_and_required():
    assert COMPOSITION["builder"] == ("VIBE", "GPTS", "MPS", "SKK", "STR", "AEZ")
    assert COMPOSITION["product"] == ("VIBE", "GPTS", "MPS", "SKK", "STR", "AEZ")
    assert COMPOSITION["growth"] == ("RLY", "VIBE", "GPTS", "VERDA", "AIC", "MPS", "SKK", "STR", "AEZ")
    assert REQUIRED["builder"] == {"VIBE", "AEZ"}
    assert REQUIRED["growth"] == REQUIRED["product"] == {"AEZ"}
    assert NEVER == {"CLOWE", "MDG", "MIR"}


def test_allowed_fact_employers_default_is_own_code_only():
    assert allowed_fact_employers("MPS", "growth") == {"MPS"}
    assert allowed_fact_employers("VIBE", "product") == {"VIBE"}


def test_composition_codes_exist_in_experience_file():
    codes = set(parse_facts(EXPERIENCE).employers)
    used = set().union(*COMPOSITION.values())
    assert used <= codes, used - codes
    assert set(EMPLOYER_NAMES) >= used
    assert EMPLOYER_NAMES["VIBE"] == "i-free"
    assert EMPLOYER_NAMES["GPTS"] == "GPT Scrambler"
    assert EMPLOYER_NAMES["STR"] == "Streasy (Proofix)"


def test_bullet_limits_boundaries():
    assert [bullet_limits(i) for i in range(5)] == [(3, 4), (3, 4), (3, 4), (1, 2), (1, 2)]


def test_cut_order():
    assert CUT_ORDER == ("trim:SKK", "trim:STR", "trim_long", "drop:AIC", "drop:VERDA", "drop:SKK")


# ---------- запрещённые формулировки ----------

def test_forbidden_has_pattern_for_every_not_counted_item():
    assert len(FORBIDDEN) >= len(parse_facts(EXPERIENCE).not_counted)


def test_forbidden_entries_are_bound_pairs():
    for code, pattern in FORBIDDEN:
        assert code is None or code in {"VIBE", "MPS", "SKK", "STR", "GPTS"}
        assert isinstance(pattern, re.Pattern)


@pytest.mark.parametrize("text", [
    "Ran AI hackathons on VibeCamp",
    "Проводил AI-хакатоны на VibeCamp",
    "Cut rework by -30% on the platform",
    "Сократил путь от идеи до инструмента на −50%",
    "ML-driven behavioral segmentation",
    "ML-кластеры поведения пользователей",
    "Grew ARPU through credit analytics",
    "Персонализированный онбординг для пользователей",
    "Reduced time-to-market by 20%",
    "Increased brand awareness by 40%",
    "Рост узнаваемости бренда на 40%",
    "Grew MRR to $2K at GPT Scrambler",
    "MRR 5K USD within a year",
    "Выручка 30 000 ₽ MRR",
    "Hired Reddit contractors for outreach",
    "Нанимал подрядчиков на Reddit",
    "Chief Product Officer (CPO)",
    "Worked as Head of Data",
    "KYB onboarding for fintech",
    "Owned compliance processes",
    "Strong Python for data processing",
    "Led the growth team at Proofix",
    "Руководил отделом роста в Proofix",
    "Led a growth department",
    "Managed growth team of five",
    "Headed the growth org at Proofix",
    "Hired 3 sales reps",
    "Нанял 3 продажников",
    "A/B test lifted conversion ×3,1",
    "Рост ×4,9 за месяц",
    "Рост ×7,6 по сравнению с прошлым",
    "Grew sales 3.1x",
    "4.9x more orders",
    "Рост в 7,6х за месяц",
])
def test_forbidden_catches(text):
    assert any(p.search(text) for _, p in FORBIDDEN)


@pytest.mark.parametrize("text", [
    "MRR stable within 3 months",
    "Traffic grew 4x in 3 months, MRR stayed stable",
    "Python read and reviewed, written with Claude Code",
    "Hired a dev team (Python/Node.js, JS/React, C++)",
    "Conversion grew from 0.3% to 2.5%",
    "Product Manager (UX & AI Development)",
    "Built a compliant onboarding flow",
])
def test_forbidden_does_not_catch_allowed(text):
    assert not any(p.search(text) for _, p in FORBIDDEN)


def test_forbidden_bound_patterns_name_their_employer():
    bound = {code: [p for c, p in FORBIDDEN if c == code] for code in ("VIBE", "MPS", "SKK", "STR", "GPTS")}
    assert any(p.search("hackathon") for p in bound["VIBE"])
    assert any(p.search("ARPU") for p in bound["MPS"])
    assert any(p.search("time-to-market") for p in bound["SKK"])
    assert any(p.search("brand awareness") for p in bound["STR"])
    assert any(p.search("MRR $2K") for p in bound["GPTS"])


def test_real_facts_do_not_trip_forbidden():
    """Подтверждённые факты не должны попадать под запреты своего места работы."""
    fb = parse_facts(EXPERIENCE)
    hits = []
    for fact in fb.facts.values():
        for code, pattern in FORBIDDEN:
            if code in (None, fact.employer) and pattern.search(fact.text):
                hits.append((fact.id, pattern.pattern))
    # Позиционирование «CPO» в профиле перечислено намеренно: заголовок резюме проверяется отдельно.
    hits = [h for h in hits if h[0] != "PROF-2"]
    assert hits == []


# ---------- стоп-фразы ----------

def test_stop_phrases_both_languages():
    assert "to whom it may concern" in STOP_PHRASES["en"]
    assert "кому это может касаться" in STOP_PHRASES["ru"]
    for lang, phrases in STOP_PHRASES.items():
        assert phrases and all(p == p.lower() for p in phrases), lang
    assert {"командный игрок", "я идеально подхожу", "я увлечён"} <= set(STOP_PHRASES["ru"])


# ---------- проверка утверждений по фактам ----------
# Номера фактов сверены с experience/Bachinin_Alexander_Experience.md: MPS-1 (1 000+ MAU),
# AEZ-1 ($18K MRR, 6,6%), AEZ-2 (1,5% → 2,5%), GPTS-6 (продление 43% → 57%, 70% → 76%),
# GPTS-2 (активация 81%), MPS-0 (описание MPSTATS), MPS-2 (регистрация +25%), VIBE-1 (UX платформы).


@pytest.fixture(scope="module")
def fb():
    return parse_facts(EXPERIENCE)


@pytest.fixture
def req_map():
    return RequirementMap(
        role_type="builder",
        requirements=[Requirement(text="Strong Python", facts=[], status="gap")],
    )


def codes(violations: list[Violation]) -> set[str]:
    return {v.code for v in violations}


def test_true_bullets_from_manual_resume_pass(fb, req_map):
    # Правдивые пункты ручного резюме AI Product Builder, сверенные с файлом опыта.
    ok = [
        ("MPS", "Interviewed sellers, found an unmet need and launched a new analytics tool that reached 1,000+ MAU.", ["MPS-1"]),
        ("AEZ", "Built the product from zero to $18K MRR (6.6% average monthly growth); raised paid conversion from 1.5% to 2.5%.", ["AEZ-1", "AEZ-2"]),
        ("GPTS", "Lifecycle emails lifted first renewal from 43% to 57% and long-term retention from 70% to 76%.", ["GPTS-6"]),
        ("AEZ", "Localized the product into English, Chinese and Portuguese: +30% paying users from these markets in the first month.", ["AEZ-3"]),
        ("VIBE", "Owned UX of VibeCamp, an internal AI platform at i-free, one of the first in the company.", ["VIBE-0", "VIBE-1"]),
        ("STR", "Ran 45 JTBD customer interviews and shipped the MVP in 1 month", ["STR-1"]),
    ]
    for code, text, facts in ok:
        assert check_claim(Claim(text=text, facts=facts), "b", frozenset({code}), fb, req_map) == [], text
    # CoinKeeper под i-free в резюме типа growth.
    ck = Claim(text="Launched an organic short-video channel: 150+ videos and 1M+ views in 3 months", facts=["CK-1"])
    assert check_claim(ck, "b", allowed_fact_employers("VIBE", "growth"), fb, req_map) == []


@pytest.mark.parametrize("text,facts,allowed,code", [
    ("Metabase dashboards for sellers", ["MPS-5"], {"MPS"}, "term"),
    ("launched paddle billing", ["MPS-5"], {"MPS"}, "term"),
    ("Ozon, SQL, GA4", ["SKILL-3"], {"SKILL", "PROF"}, "term"),
])
def test_names_at_sentence_start_and_lowercase_are_checked(fb, req_map, text, facts, allowed, code):
    v = check_claim(Claim(text=text, facts=facts), "b", frozenset(allowed), fb, req_map)
    assert code in codes(v), v


def test_skills_first_item_is_checked_for_unknown_term(fb, req_map):
    skills = [Claim(text="Tableau, SQL", facts=["SKILL-3"]), Claim(text="Analytics: Looker, GA4", facts=["SKILL-3"])]
    v = check_resume(_resume(fb, VALID_BUILDER, skills=skills), req_map, fb, "en")
    unknown = {x.path: x.detail for x in v if x.code == "unknown_term"}
    assert unknown == {"skills[0]": "Tableau", "skills[1]": "Looker"}


def test_invented_number(fb, req_map):
    v = check_claim(Claim(text="Raised registration conversion by 35%", facts=["MPS-2"]), "b", frozenset({"MPS"}), fb, req_map)
    assert [x.code for x in v] == ["number"] and "35" in v[0].detail
    assert v[0].path == "b"


def test_number_from_other_product(fb, req_map):
    v = check_claim(Claim(text="81% activation", facts=["GPTS-2"]), "b", frozenset({"MPS"}), fb, req_map)
    assert "wrong_employer" in codes(v)
    assert "GPTS-2" in next(x.detail for x in v if x.code == "wrong_employer")


def test_invented_market_intelligence(fb, req_map):
    v = check_claim(
        Claim(text="Market intelligence for Wildberries and Ozon with low-code tools", facts=["MPS-0"]),
        "b", frozenset({"MPS"}), fb, req_map,
    )
    assert {"term", "unknown_term"} & codes(v)


def test_invented_unknown_tool(fb, req_map):
    v = check_claim(Claim(text="Built dashboards in Tableau and Looker", facts=["MPS-5"]), "b", frozenset({"MPS"}), fb, req_map)
    unknown = [x for x in v if x.code == "unknown_term"]
    assert unknown and "Tableau" in unknown[0].detail and "Looker" in unknown[0].detail


def test_masked_gap(fb, req_map):
    c = Claim(text="Hands-on with data workflows", facts=["VIBE-1"], requirement="Strong Python")
    v = check_claim(c, "b", frozenset({"VIBE"}), fb, req_map)
    assert "gap" in codes(v)


def test_forbidden_claim(fb, req_map):
    c = Claim(text="Ran AI hackathons for employees", facts=["VIBE-1"])
    assert "forbidden" in codes(check_claim(c, "b", frozenset({"VIBE"}), fb, req_map))


def test_forbidden_bound_pattern_applies_by_job_or_cited_fact(fb, req_map):
    hack = Claim(text="Ran AI hackathons for employees", facts=["VIBE-1"])
    # Summary без места работы: шаблон VIBE срабатывает по месту работы процитированного факта.
    assert "forbidden" in codes(check_claim(hack, "summary[0]", None, fb, req_map))
    # Шаблон MPS (ARPU) не применяется к пункту GPT Scrambler с фактами GPT Scrambler.
    arpu = Claim(text="Tracked LTV and ARPU of subscribers", facts=["GPTS-6"])
    assert "forbidden" not in codes(check_claim(arpu, "b", frozenset({"GPTS"}), fb, req_map))
    assert "forbidden" in codes(check_claim(arpu, "b", frozenset({"MPS"}), fb, req_map))
    # Шаблон без места работы применяется везде.
    cpo = Claim(text="Acted as CPO", facts=["AEZ-5"])
    assert "forbidden" in codes(check_claim(cpo, "b", frozenset({"AEZ"}), fb, req_map))


def test_unknown_fact_and_no_facts(fb, req_map):
    v = check_claim(Claim(text="Grew things", facts=["MPS-99"]), "b", frozenset({"MPS"}), fb, req_map)
    assert "unknown_fact" in codes(v) and "MPS-99" in v[0].detail
    assert codes(check_claim(Claim(text="Grew things", facts=[]), "b", frozenset({"MPS"}), fb, req_map)) == {"no_facts"}


def test_summary_may_cite_any_employer_except_never(fb, req_map):
    ok = Claim(text="Launched a new analytics tool that reached 1,000+ MAU.", facts=["MPS-1"])
    assert check_claim(ok, "summary[0]", None, fb, req_map) == []
    never = Claim(text="Stopped a project", facts=["CLOWE-2"])
    assert "wrong_employer" in codes(check_claim(never, "summary[0]", None, fb, req_map))


def _bullets(code: str, fb, n: int) -> list[Claim]:
    ids = sorted((f for f in fb.facts if f.startswith(code + "-") and f != f"{code}-0"), key=lambda f: int(f.split("-")[1]))
    ids = ids or [f"{code}-1"]
    return [Claim(text="Owned the work", facts=[ids[i % len(ids)]]) for i in range(n)]


def _resume(fb, jobs: list[tuple[str, str, int]], headline: str = "AI PM / AI Product Builder", skills=None) -> ResumeDraft:
    return ResumeDraft(
        headline=headline,
        summary=[Claim(text="Launched a new analytics tool that reached 1,000+ MAU.", facts=["MPS-1"])],
        jobs=[Job(employer=c, title=t, bullets=_bullets(c, fb, n)) for c, t, n in jobs],
        skills=skills if skills is not None else [Claim(text="SQL, GA4, Metabase", facts=["SKILL-3"])],
    )


VALID_BUILDER = [
    ("VIBE", "Product Manager (UX & AI Development)", 4),
    ("GPTS", "Head of Product", 3),
    ("MPS", "Product Manager", 3),
    ("AEZ", "Product Manager & Co-founder", 2),
]
MECHANICS = {"composition", "bullets", "headline", "title", "language"}


def test_resume_valid_has_no_mechanical_violations(fb, req_map):
    v = check_resume(_resume(fb, VALID_BUILDER), req_map, fb, "en")
    assert not codes(v) & MECHANICS, v
    assert not [x for x in v if x.code == "wrong_employer"]


def test_resume_composition(fb, req_map):
    no_vibe = [j for j in VALID_BUILDER if j[0] != "VIBE"]
    assert "composition" in codes(check_resume(_resume(fb, no_vibe), req_map, fb, "en"))
    with_ck = VALID_BUILDER[:3] + [("CK", "Product Marketing Manager (Growth)", 1)] + VALID_BUILDER[3:]
    assert "composition" in codes(check_resume(_resume(fb, with_ck), req_map, fb, "en"))
    reordered = [VALID_BUILDER[1], VALID_BUILDER[0]] + VALID_BUILDER[2:]
    assert "composition" in codes(check_resume(_resume(fb, reordered), req_map, fb, "en"))
    assert "headline" in codes(check_resume(_resume(fb, VALID_BUILDER, headline="CPO"), req_map, fb, "en"))
    five = [("VIBE", "Product Manager (UX & AI Development)", 5)] + VALID_BUILDER[1:]
    bullets = [x for x in check_resume(_resume(fb, five), req_map, fb, "en") if x.code == "bullets"]
    assert [x.path for x in bullets] == ["jobs[0]"]


def test_resume_title_skills_and_unknown_employer(fb, req_map):
    bad_title = [("VIBE", "Chief Product Officer", 4)] + VALID_BUILDER[1:]
    v = check_resume(_resume(fb, bad_title), req_map, fb, "en")
    assert [x.path for x in v if x.code == "title"] == ["jobs[0].title"]
    skills = [Claim(text="SQL", facts=["MPS-6"])]
    v = check_resume(_resume(fb, VALID_BUILDER, skills=skills), req_map, fb, "en")
    assert any(x.code == "wrong_employer" and x.path == "skills[0]" for x in v)
    # Неизвестный код места работы: нарушения, а не KeyError.
    v = check_resume(_resume(fb, VALID_BUILDER[:3] + [("XYZ", "PM", 1)] + VALID_BUILDER[3:]), req_map, fb, "en")
    assert {"composition", "title"} <= codes(v)


def test_resume_paths_and_language(fb, req_map):
    draft = _resume(fb, VALID_BUILDER)
    draft.jobs[1].bullets[2] = Claim(text="Raised registration conversion by 35%", facts=["GPTS-2"])
    draft.summary[0] = Claim(text="Запустил новый аналитический инструмент: 1 000+ MAU.", facts=["MPS-1"])
    v = check_resume(draft, req_map, fb, "en")
    assert any(x.path == "jobs[1].bullets[2]" and x.code == "number" for x in v)
    assert any(x.path == "summary[0]" and x.code == "language" for x in v)


def _letter(sentences: list[str], subject: str | None = "Senior Product Manager at Acme") -> LetterDraft:
    return LetterDraft(
        subject=subject,
        salutation="Hello Acme team,",
        sentences=[LetterSentence(text=t, facts=[], paragraph=1) for t in sentences],
        closing="Best regards,",
    )


GOOD_LETTER = [
    "Acme is hiring a Senior Product Manager to grow its analytics product.",
    "I would like to talk about the role.",
]


def test_letter_valid(fb, req_map):
    draft = _letter(GOOD_LETTER)
    draft.sentences.append(LetterSentence(
        text="At MPSTATS I launched a new analytics tool that reached 1,000+ MAU.", facts=["MPS-1"], paragraph=2))
    assert check_letter(draft, req_map, fb, "en", "Acme", "Senior Product Manager") == []


def test_letter_mechanics(fb, req_map):
    def run(draft, lang="en", company="Acme"):
        return check_letter(draft, req_map, fb, lang, company, "Senior Product Manager")

    v = run(_letter(GOOD_LETTER, subject="Senior Product Manager — Acme"))
    assert [(x.path, x.code) for x in v] == [("letter.subject", "dash")]
    assert "length" in codes(run(_letter(GOOD_LETTER + ["word " * 320])))
    assert "stop_phrase" in codes(run(_letter(GOOD_LETTER + ["I am a team player."])))
    ru = _letter(["Acme ищет Senior Product Manager, и мне интересна эта роль в команде продукта."], subject=None)
    ru.salutation, ru.closing = "Здравствуйте, команда Acme!", "С уважением,"
    assert "language" in codes(run(ru))
    assert "language" not in codes(run(ru, lang="ru"))
    no_company = _letter(["We should talk about the Senior Product Manager role."], subject=None)
    no_company.salutation = "Hello,"
    assert any(x.code == "title" for x in run(no_company))
    # Компания скрыта: её названия в письме не требуется.
    assert not any(x.code == "title" for x in run(no_company, company=None))


def test_letter_claim_checks_only_sentences_with_facts(fb, req_map):
    draft = _letter(GOOD_LETTER + ["Your 3M users love the product."])
    assert check_letter(draft, req_map, fb, "en", "Acme", "Senior Product Manager") == []
    draft.sentences.append(LetterSentence(text="I raised registration conversion by 35%.", facts=["MPS-2"], paragraph=2))
    v = check_letter(draft, req_map, fb, "en", "Acme", "Senior Product Manager")
    assert [(x.path, x.code) for x in v] == [("letter[3]", "number")]
    gap = LetterSentence(text="I am strong in data work.", facts=[], paragraph=2, requirement="Strong Python")
    draft.sentences.append(gap)
    assert ("letter[4]", "gap") in {(x.path, x.code) for x in check_letter(draft, req_map, fb, "en", "Acme", "Senior Product Manager")}


def test_violation_detail_has_no_claim_text(fb, req_map):
    claims = [
        (Claim(text="Spearheaded marketplace intelligence for Wildberries sellers, boosting revenue 35%",
               facts=["MPS-0", "GPTS-2", "MPS-77"], requirement="Strong Python"), frozenset({"MPS"})),
        (Claim(text="Organized internal hackathons with Tableau dashboards", facts=["VIBE-1"]), frozenset({"VIBE"})),
        (Claim(text="Organized internal hackathons", facts=[]), frozenset({"VIBE"})),
    ]
    found = set()
    for claim, allowed in claims:
        for v in check_claim(claim, "b", allowed, fb, req_map):
            found.add(v.code)
            words = {w.lower() for w in re.findall(r"[A-Za-zА-Яа-я]{4,}", claim.text)}
            leaked = {w for w in re.findall(r"[A-Za-zА-Яа-я]{4,}", v.detail.lower()) if w in words}
            assert leaked <= {"wildberries", "tableau"}, (v.code, leaked)
    assert {"unknown_fact", "wrong_employer", "number", "term", "unknown_term", "forbidden", "gap", "no_facts"} <= found


# ---------- финальная волна правок: F1, F2, F4, F10 ----------


def test_company_name_from_vocabulary_still_needs_cited_fact(fb, req_map):
    # Прецедент Wildberries: название компании из вакансии не освобождает слово словаря от сверки
    # с процитированными фактами. «wildberries» в словаре (CLOWE-2), в MPS-0 его нет.
    draft = _letter(GOOD_LETTER, subject="Senior Product Manager at Wildberries")
    draft.sentences.append(LetterSentence(
        text="At MPSTATS I built market intelligence for Wildberries sellers.", facts=["MPS-0"], paragraph=2))
    v = check_letter(draft, req_map, fb, "en", "Wildberries", "Senior Product Manager")
    terms = [x for x in v if x.path == "letter[2]" and x.code == "term"]
    assert terms and "wildberries" in terms[0].detail, v


def test_company_name_outside_vocabulary_is_not_unknown_term(fb, req_map):
    # Название компании, которого нет в словаре, — не выдуманный инструмент.
    draft = _letter(["Zyxcorp is hiring a Senior Product Manager.", "I would like to talk about the role."],
                    subject="Senior Product Manager at Zyxcorp")
    draft.salutation = "Hello Zyxcorp team,"
    draft.sentences.append(LetterSentence(
        text="At MPSTATS I launched a new analytics tool for Zyxcorp style sellers that reached 1,000+ MAU.",
        facts=["MPS-1"], paragraph=2))
    assert check_letter(draft, req_map, fb, "en", "Zyxcorp", "Senior Product Manager") == []


@pytest.mark.parametrize("field,text,detail_part", [
    ("closing", "I grew ARR to $5M at Ozon and would love to talk.", "5000000"),
    ("closing", "Happy to show how MPSTATS sellers used my tool.", "mpstats"),
    ("subject", "Senior Product Manager, 9 years in AI", "9"),
    ("subject", "Senior Product Manager at Acme, ex Tableau", "Tableau"),
])
def test_letter_subject_and_closing_are_claims_without_facts(fb, req_map, field, text, detail_part):
    draft = _letter(GOOD_LETTER)
    setattr(draft, field, text)
    v = check_letter(draft, req_map, fb, "en", "Acme", "Senior Product Manager")
    found = [x for x in v if x.path == f"letter.{field}" and x.code == "no_facts"]
    assert found and detail_part in found[0].detail, v


def test_letter_subject_and_closing_may_name_company_and_role(fb, req_map):
    draft = _letter(GOOD_LETTER, subject="Senior Product Manager at Acme")
    draft.closing = "I would be glad to talk about the Senior Product Manager role at Acme."
    assert check_letter(draft, req_map, fb, "en", "Acme", "Senior Product Manager") == []


def test_gap_requirement_matched_ignoring_case_and_spaces(fb, req_map):
    c = Claim(text="Hands-on with data workflows", facts=["VIBE-1"], requirement="  strong   PYTHON ")
    assert "gap" in codes(check_claim(c, "b", frozenset({"VIBE"}), fb, req_map))


@pytest.mark.parametrize("text,facts,allowed", [
    ("Analytics stack: GA4, Yandex Metrica, SQL", ["MPS-6"], {"MPS"}),
    ("Analytics: GA4, Yandex Metrika, Metabase, SQL", ["SKILL-3"], {"SKILL", "PROF"}),
])
def test_cyrillic_product_names_in_english_text(fb, req_map, text, facts, allowed):
    # «Яндекс Метрика» в факте: английское написание — то же название, а не выдуманное.
    assert check_claim(Claim(text=text, facts=facts), "b", frozenset(allowed), fb, req_map) == []


def test_transliterated_name_still_needs_its_fact(fb, req_map):
    # Yandex становится словом словаря: без факта, где есть «Яндекс», это term.
    v = check_claim(Claim(text="Launched a new analytics tool on Yandex that reached 1,000+ MAU.", facts=["MPS-1"]),
                    "b", frozenset({"MPS"}), fb, req_map)
    assert [(x.code, x.detail) for x in v] == [("term", "yandex")]
