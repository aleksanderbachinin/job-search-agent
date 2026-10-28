"""Проверка фактов кодом: утверждения резюме и письма сверяются с номерами фактов из файла опыта.

В `Violation.detail` попадают только номера фактов, числа, названия и коды мест работы,
никогда не текст утверждения: нарушения пишутся в логи и события.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from jobagent.facts import FactBase, extract_names, extract_numbers, unknown_terms
from jobagent.package_models import Claim, LetterDraft, Lang, RequirementMap, ResumeDraft
from jobagent.package_rules import (
    COMPOSITION,
    EMPLOYER_NAMES,
    FORBIDDEN,
    NEVER,
    REQUIRED,
    allowed_fact_employers,
    bullet_limits,
)
from jobagent.textchecks import detect_language, find_dashes, find_stop_phrases, word_count

ViolationCode = Literal[
    "unknown_fact", "wrong_employer", "no_facts", "number", "term", "unknown_term", "forbidden",
    "gap", "composition", "bullets", "headline", "title", "dash", "length", "stop_phrase",
    "language", "semantic",
]

# Пункт навыков ссылается только на разделы «Навыки и инструменты» и «Профиль».
SKILL_EMPLOYERS: frozenset[str] = frozenset({"SKILL", "PROF"})

MAX_LETTER_WORDS = 300

# Слова с заглавной, которые в английском пересказе русского факта появляются сами собой
# («январь–май» → «Jan–May», «английский» → «English»). Названиями продуктов они не бывают.
_COMMON_CAPITALIZED = frozenset(
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec "
    "monday tuesday wednesday thursday friday saturday sunday "
    "english russian chinese portuguese spanish german french".split()
)

_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")


@dataclass(frozen=True)
class Violation:
    path: str  # jobs[2].bullets[1], summary[0], skills[0], letter[4], letter.subject, resume
    code: ViolationCode
    detail: str  # номера фактов, числа, названия; без текста утверждения


def _fmt_number(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}"


def _words(text: str) -> set[str]:
    return {m.group().lower() for m in _LATIN_WORD.finditer(text)}


def _employer_names(codes: set[str], fb: FactBase) -> set[str]:
    """Слова из названий мест работы: назвать место, где был факт, — не выдумка."""
    out: set[str] = set()
    for code in codes:
        employer = fb.employers.get(code)
        if employer is not None:
            out |= _words(employer.heading)
        out |= _words(EMPLOYER_NAMES.get(code, ""))
    return out


def _gap_index(requirement: str | None, req_map: RequirementMap) -> int | None:
    if not requirement:
        return None
    key = _norm(requirement)
    for i, req in enumerate(req_map.requirements):
        if req.status == "gap" and _norm(req.text) == key:
            return i
    return None


def _gap(claim: Claim, path: str, req_map: RequirementMap) -> list[Violation]:
    i = _gap_index(claim.requirement, req_map)
    # Текст требования — это текст вакансии, в detail только его номер в карте.
    return [] if i is None else [Violation(path, "gap", f"requirements[{i}]")]


def _check_claim(
    claim: Claim,
    path: str,
    allowed_employers: frozenset[str] | None,
    fb: FactBase,
    req_map: RequirementMap,
    extra_names: frozenset[str] = frozenset(),
) -> list[Violation]:
    out: list[Violation] = []

    unknown = [f for f in claim.facts if f not in fb.facts]
    if unknown:
        out.append(Violation(path, "unknown_fact", ", ".join(unknown)))
    known = [fb.facts[f] for f in claim.facts if f in fb.facts]

    def allowed(employer: str) -> bool:
        if allowed_employers is None:
            return employer not in NEVER
        return employer in allowed_employers

    wrong = [f.id for f in known if not allowed(f.employer)]
    if wrong:
        out.append(Violation(path, "wrong_employer", ", ".join(wrong)))
    valid = [f for f in known if allowed(f.employer)]

    if not claim.facts:
        out.append(Violation(path, "no_facts", ""))
    else:
        # Числа и названия подтверждаются только фактами, на которые пункт имеет право ссылаться.
        numbers = frozenset().union(*(f.numbers for f in valid))
        extra_numbers = sorted(extract_numbers(claim.text) - numbers)
        if extra_numbers:
            out.append(Violation(path, "number", ", ".join(_fmt_number(n) for n in extra_numbers)))

        exempt = set(extra_names) | _COMMON_CAPITALIZED | _employer_names({f.employer for f in valid}, fb)
        terms = frozenset().union(*(f.terms for f in valid))
        extra_terms = sorted(extract_names(claim.text, fb.vocabulary) - terms - exempt)
        if extra_terms:
            out.append(Violation(path, "term", ", ".join(extra_terms)))
        unknown_names = sorted(t for t in unknown_terms(claim.text, fb.vocabulary) if t.lower() not in exempt)
        if unknown_names:
            out.append(Violation(path, "unknown_term", ", ".join(unknown_names)))

    # Шаблон применяется, если он общий, привязан к месту работы пункта или к месту работы
    # процитированного факта (у summary и письма места работы нет).
    codes = set(allowed_employers or ()) | {f.employer for f in known}
    hits = [
        f"FORBIDDEN[{i}]" + (f" ({code})" if code else "")
        for i, (code, pattern) in enumerate(FORBIDDEN)
        if (code is None or code in codes) and pattern.search(claim.text)
    ]
    if hits:
        out.append(Violation(path, "forbidden", ", ".join(hits)))

    out += _gap(claim, path, req_map)
    return out


def check_claim(
    claim: Claim,
    path: str,
    allowed_employers: frozenset[str] | None,
    fb: FactBase,
    req_map: RequirementMap,
) -> list[Violation]:
    """Сверяет утверждение с процитированными фактами.

    allowed_employers — коды мест работы, на чьи факты можно ссылаться; None (summary, письмо) —
    любые, кроме NEVER.
    """
    return _check_claim(claim, path, allowed_employers, fb, req_map)


def _language(text: str, path: str, lang: Lang) -> list[Violation]:
    found = detect_language(text)
    return [] if found == lang else [Violation(path, "language", f"{found}, нужен {lang}")]


def _is_subsequence(items: list[str], order: tuple[str, ...]) -> bool:
    it = iter(order)
    return all(code in it for code in items)


def check_resume(draft: ResumeDraft, req_map: RequirementMap, fb: FactBase, lang: Lang) -> list[Violation]:
    role_type = req_map.role_type
    composition = COMPOSITION[role_type]
    out: list[Violation] = []

    codes = [job.employer for job in draft.jobs]
    extra = [c for c in codes if c not in composition]
    if extra:
        out.append(Violation("resume", "composition", "лишние: " + ", ".join(extra)))
    missing = sorted(REQUIRED[role_type] - set(codes))
    if missing:
        out.append(Violation("resume", "composition", "нет обязательных: " + ", ".join(missing)))
    known = [c for c in codes if c in composition]
    if len(set(known)) != len(known) or not _is_subsequence(known, composition):
        out.append(Violation("resume", "composition", "порядок: " + ", ".join(codes)))

    if draft.headline not in fb.headlines:
        out.append(Violation("headline", "headline", draft.headline))

    for i, claim in enumerate(draft.summary):
        path = f"summary[{i}]"
        out += check_claim(claim, path, None, fb, req_map)
        out += _language(claim.text, path, lang)

    for i, job in enumerate(draft.jobs):
        lo, hi = bullet_limits(i)
        if not lo <= len(job.bullets) <= hi:
            out.append(Violation(f"jobs[{i}]", "bullets", f"{len(job.bullets)}, нужно от {lo} до {hi}"))
        employer = fb.employers.get(job.employer)
        if employer is None or job.title not in employer.titles:
            out.append(Violation(f"jobs[{i}].title", "title", f"{job.employer}: {job.title}"))
        allowed = allowed_fact_employers(job.employer, role_type)
        for j, claim in enumerate(job.bullets):
            path = f"jobs[{i}].bullets[{j}]"
            out += check_claim(claim, path, allowed, fb, req_map)
            out += _language(claim.text, path, lang)

    for i, claim in enumerate(draft.skills):
        path = f"skills[{i}]"
        out += check_claim(claim, path, SKILL_EMPLOYERS, fb, req_map)
        # Навыки — в основном названия инструментов на латинице: в русском резюме это норма.
        # Ловится только русский текст в английском резюме.
        if lang == "en":
            out += _language(claim.text, path, lang)
    return out


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _contains(haystack: str, needle: str) -> bool:
    """Подстрока без учёта регистра и разницы в пробелах."""
    return _norm(needle) in _norm(haystack)


def check_letter(
    draft: LetterDraft,
    req_map: RequirementMap,
    fb: FactBase,
    lang: Lang,
    company: str | None,
    role: str,
) -> list[Violation]:
    """Письмо без подписи. Утверждения без фактов (хук, призыв) проверяет только проверка смысла."""
    out: list[Violation] = []
    parts: list[tuple[str, str]] = []
    if draft.subject is not None:
        parts.append(("letter.subject", draft.subject))
    parts.append(("letter.salutation", draft.salutation))
    parts += [(f"letter[{i}]", s.text) for i, s in enumerate(draft.sentences)]
    parts.append(("letter.closing", draft.closing))

    for path, text in parts:
        dashes = find_dashes(text)
        if dashes:
            out.append(Violation(path, "dash", str(len(dashes))))
        phrases = find_stop_phrases(text, lang)
        if phrases:
            out.append(Violation(path, "stop_phrase", "; ".join(phrases)))
        # Тема — обычно название роли и компании, язык по ней не определить.
        if path != "letter.subject":
            out += _language(text, path, lang)

    # Подпись добавляет код, тема в объём письма не входит.
    body = "\n".join(text for path, text in parts if path != "letter.subject")
    words = word_count(body)
    if words > MAX_LETTER_WORDS:
        out.append(Violation("letter", "length", f"{words} > {MAX_LETTER_WORDS}"))

    full = "\n".join(text for _, text in parts)
    if company and not _contains(full, company):
        out.append(Violation("letter", "title", "нет названия компании"))
    if role and not _contains(full, role):
        out.append(Violation("letter", "title", "нет названия роли"))

    # Название компании и роли — не выдуманные инструменты.
    names = frozenset(_words(company or "") | _words(role))
    for i, sentence in enumerate(draft.sentences):
        path = f"letter[{i}]"
        if sentence.facts:
            out += _check_claim(sentence, path, None, fb, req_map, extra_names=names)
        else:
            out += _gap(sentence, path, req_map)
    return out
