"""Парсер файла опыта: номера фактов, должности, позиционирования, числа и названия."""

import re
from pathlib import Path

import pytest

from jobagent.config import AGENT_DIR
from jobagent.facts import (
    GENERIC_TERMS,
    FactFileError,
    extract_numbers,
    extract_terms,
    parse_facts,
    strip_markers,
    unknown_terms,
)
from jobagent.scoring import Scorer, load_knowledge

KNOWLEDGE_DIR = AGENT_DIR.parent
EXPERIENCE = KNOWLEDGE_DIR / "experience" / "Bachinin_Alexander_Experience.md"

HEADLINES_LINE = "- Позиционирования, под которые уже делал резюме: Product Manager, CPO, Growth PM."


def write_experience(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "experience.md"
    path.write_text(body, encoding="utf-8")
    return path


def section(name: str, *lines: str) -> str:
    return f"## {name}\n\n" + "\n".join(lines) + "\n\n"


def test_every_bullet_in_profile_and_experience_has_unique_id():
    fb = parse_facts(EXPERIENCE)
    text = EXPERIENCE.read_text(encoding="utf-8")
    # Разделы «Профиль», «Опыт по времени», «Навыки и инструменты»: каждый пункт с номером в конце строки.
    in_scope = False
    bullets = 0
    for line in text.splitlines():
        if line.startswith("## "):
            in_scope = not line.startswith("## Не засчитывать")
        if in_scope and line.startswith("- "):
            bullets += 1
            assert re.search(r" \[[A-Z]+-\d+\]\s*$", line), line
    markers = re.findall(r" \[([A-Z]+-\d+)\]\s*$", text, flags=re.M)
    assert len(markers) == len(set(markers)) == len(fb.facts)
    assert bullets <= len(fb.facts)
    assert {f.employer for f in fb.facts.values() if f.id.startswith("PROF-")} == {"PROF"}
    assert {f.employer for f in fb.facts.values() if f.id.startswith("SKILL-")} == {"SKILL"}


def test_fact_belongs_to_employer():
    fb = parse_facts(EXPERIENCE)
    assert fb.facts["MPS-1"].employer == "MPS" and "1 000+" in fb.facts["MPS-1"].text
    assert 1000 in fb.facts["MPS-1"].numbers and "metabase" not in fb.facts["MPS-1"].terms
    assert fb.employers["VIBE"].titles == ("Product Manager (UX & AI Development)",)
    assert fb.employers["RLY"].titles == ("Head of Content Production", "Growth Product Manager")


def test_context_fact_is_description_paragraph():
    fb = parse_facts(EXPERIENCE)
    assert fb.employers["MPS"].context_fact == "MPS-0"
    assert "Должность: Product Manager" in fb.facts["MPS-0"].text
    assert all(e.context_fact in fb.facts for e in fb.employers.values())


def test_titles_for_all_employers():
    titles = {code: e.titles for code, e in parse_facts(EXPERIENCE).employers.items()}
    assert titles["GPTS"] == ("Co-founder & Head of Product", "Head of Product")
    assert titles["CK"] == ("Product Marketing Manager (Growth)",)
    assert titles["VERDA"] == ("Co-founder", "Growth")
    assert titles["AEZ"] == ("Product Manager & Co-founder", "Co-founder Product & Growth")
    assert titles["MDG"] == ("Co-founder", "Project Manager", "Business Development Manager")
    assert titles["AIC"] == ("AI Content Producer", "UGC Video Producer")
    assert titles["CLOWE"] == ()


def test_employer_codes_and_break_section():
    fb = parse_facts(EXPERIENCE)
    assert set(fb.employers) == {
        "VIBE", "CK", "RLY", "VERDA", "GPTS", "AIC", "CLOWE", "MPS", "SKK", "STR", "AEZ", "MDG", "MIR",
    }
    # «Перерыв» и «Не засчитывать» номеров не получают.
    assert not any("Перерыв" in e.heading for e in fb.employers.values())
    assert not any("Перерыв" in f.text or "хакатон" in f.text for f in fb.facts.values())


def test_headlines_exclude_cpo():
    fb = parse_facts(EXPERIENCE)
    assert "AI PM / AI Product Builder" in fb.headlines or "AI Product Builder" in fb.headlines
    assert "Product Manager" in fb.headlines and "Growth PM" in fb.headlines
    assert not any("CPO" in h for h in fb.headlines)


def test_not_counted_section_parsed():
    fb = parse_facts(EXPERIENCE)
    assert any("хакатон" in x for x in fb.not_counted)
    assert all(not x.startswith("- ") for x in fb.not_counted)


def test_vocabulary_has_tools_without_generic_terms():
    fb = parse_facts(EXPERIENCE)
    assert {"metabase", "ga4", "higgsfield", "chatfuel", "claude"} <= fb.vocabulary
    assert not {t.lower() for t in GENERIC_TERMS} & fb.vocabulary


def test_vocabulary_only_from_marked_facts():
    vocab = parse_facts(EXPERIENCE).vocabulary
    # ml, kyb, cpo встречаются только во вступлении или в «Не засчитывать»: опытом они не считаются.
    assert not {"ml", "kyb", "cpo"} & vocab
    assert {"metabase", "ga4"} <= vocab


@pytest.mark.parametrize("text,expected", [
    ("1,000+ MAU", {1000}), ("1 000+ MAU", {1000}), ("from 1.5% to 2.5%", {1.5, 2.5}),
    ("с 1,5% до 2,5%", {1.5, 2.5}), ("$18K MRR", {18000}), ("18K $", {18000}), ("$4,5K", {4500}),
    ("1M+ views", {1_000_000}), ("в 4 раза", {4}), ("4x traffic", {4}), ("three pipelines", {3}),
    ("Feb 2026 – Oct 2026", set()),
    ("×4 к прошлому", {4}), ("4 times", {4}), ("twelve and One", {12, 1}),
    ("48–72 часа, $6", {48, 72, 6}), ("GA4, B2C, web2app", set()), ("1,000,000 users", {1_000_000}),
    ("0,3% до 2,5%", {0.3, 2.5}), ("2024 – 2025, 150+ роликов", {150}), ("9+ лет", {9}),
])
def test_extract_numbers(text, expected):
    assert extract_numbers(text) == expected


def test_extract_terms_ignores_case():
    vocab = frozenset({"metabase", "ga4"})
    assert extract_terms("Built in METABASE and GA4, not Kubernetes", vocab) == {"metabase", "ga4"}


def test_unknown_terms_skip_sentence_start_and_generic():
    vocab = frozenset({"metabase", "ga4"})
    assert unknown_terms("Built SaaS analytics in Metabase and Kubernetes", vocab) == {"Kubernetes"}
    # Начало предложения и начало строки не считаются именами.
    assert unknown_terms("Used tools. Kubernetes helped.\nDocker too", vocab) == set()
    assert unknown_terms("Used tools. Then Kubernetes helped", vocab) == {"Kubernetes"}


def test_duplicate_id_raises(tmp_path):
    path = write_experience(tmp_path, section("Профиль", HEADLINES_LINE + " [PROF-1]", "- Второй [PROF-1]"))
    with pytest.raises(FactFileError, match="PROF-1"):
        parse_facts(path)


def test_bullet_without_marker_raises(tmp_path):
    path = write_experience(tmp_path, section("Профиль", HEADLINES_LINE + " [PROF-1]", "- Без номера"))
    with pytest.raises(FactFileError, match="Без номера"):
        parse_facts(path)


def test_mixed_codes_in_one_section_raise(tmp_path):
    body = section("Профиль", HEADLINES_LINE + " [PROF-1]") + "## Опыт по времени\n\n### Акме · 2020\n\nОписание. [ACME-0]\n\n- Пункт [OTHER-1]\n"
    with pytest.raises(FactFileError, match="OTHER"):
        parse_facts(write_experience(tmp_path, body))


def test_minimal_file_parses_and_skips_unmarked_break(tmp_path):
    body = (
        section("Профиль", HEADLINES_LINE + " [PROF-1]")
        + "## Опыт по времени\n\nВступление без номера.\n\n"
        + "### Акме · 2020\n\nДолжность: Lead PM (команда, из 3), Head (Latin, kept). Описание. [ACME-0]\n\n- Рост 2,5% [ACME-1]\n\n"
        + "### Перерыв · 2021\nВ резюме не показывается.\n\n"
        + "## Не засчитывать\n\n- Ложь: CPO в Акме.\n"
    )
    fb = parse_facts(write_experience(tmp_path, body))
    assert set(fb.employers) == {"ACME"} and set(fb.facts) == {"PROF-1", "ACME-0", "ACME-1"}
    assert fb.employers["ACME"].titles == ("Lead PM", "Head (Latin, kept)")
    assert fb.facts["ACME-1"].numbers == {2.5}
    assert fb.headlines == ("Product Manager", "Growth PM")


def test_multiline_description_takes_marker_on_last_line(tmp_path):
    body = (
        section("Профиль", HEADLINES_LINE + " [PROF-1]")
        + "## Опыт по времени\n\n### Акме · 2020\n\nПервая строка\nвторая строка. [ACME-0]\n\n- Пункт [ACME-1]\n"
    )
    fb = parse_facts(write_experience(tmp_path, body))
    assert fb.facts["ACME-0"].text == "Первая строка вторая строка."


def test_strip_markers_restores_unmarked_text():
    assert strip_markers("- Пункт 1. [MPS-1]\nссылка [i-free.com](https://i-free.com/)\n") == (
        "- Пункт 1.\nссылка [i-free.com](https://i-free.com/)\n"
    )
    assert strip_markers("Хвост с пробелом  [PROF-4]\n") == "Хвост с пробелом \n"
    assert strip_markers("Ссылка [PROF-4] в середине\n") == "Ссылка [PROF-4] в середине\n"


def test_scorer_prompt_has_no_fact_markers_and_matches_unmarked_file(tmp_path):
    knowledge = load_knowledge(KNOWLEDGE_DIR)
    assert "[MPS-" not in knowledge["resume"]
    # Файл без номеров: те же файлы, но из файла опыта номера убраны независимой регуляркой.
    copy = tmp_path / "knowledge"
    (copy / "experience").mkdir(parents=True)
    for name in ("search_criteria.md", "applied_examples.md"):
        src = KNOWLEDGE_DIR / name
        if src.exists():
            (copy / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    marked = EXPERIENCE.read_text(encoding="utf-8")
    unmarked = re.sub(r" \[(?:[A-Z]+)-\d+\]$", "", marked, flags=re.M)
    assert unmarked != marked
    (copy / "experience" / EXPERIENCE.name).write_text(unmarked, encoding="utf-8")
    models = ("claude-haiku-5-5", "claude-sonnet-5-5")
    real = Scorer(KNOWLEDGE_DIR, *models, client=object())
    plain = Scorer(copy, *models, client=object())
    assert real.scoring_system == plain.scoring_system
    assert real.prefilter_system == plain.prefilter_system
    assert "[MPS-" not in real.scoring_system[0]["text"]
