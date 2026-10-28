"""Проверка фактов пакета: структуры ответов и правила состава."""

import re

import pytest
from pydantic import ValidationError

from jobagent.config import AGENT_DIR
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
    assert CUT_ORDER == ("trim:SKK", "trim:STR", "trim_long", "drop:SKK")


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
