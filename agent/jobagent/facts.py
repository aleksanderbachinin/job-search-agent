"""Файл опыта как база фактов: номера пунктов, места работы, числа и названия для проверки текстов."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Номер факта в конце строки: «… 1 000+ MAU. [MPS-1]». Пробел перед номером в текст не входит.
_MARKER_AT_END = re.compile(r"\s\[([A-Z]+)-(\d+)\]\s*$")
_MARKER_FOR_STRIP = re.compile(r" \[[A-Z]+-\d+\](?=[ \t]*$)", re.M)

SECTION_PROFILE = "Профиль"
SECTION_EXPERIENCE = "Опыт по времени"
SECTION_SKILLS = "Навыки и инструменты"
SECTION_NOT_COUNTED = "Не засчитывать"
HEADLINES_PREFIX = "Позиционирования, под которые уже делал резюме:"

# Названия и сокращения, которые не требуют совпадения с фактом.
GENERIC_TERMS = frozenset({
    "AI", "SaaS", "B2B", "B2C", "LLM", "UX", "UI", "PM", "MVP", "API", "iOS", "Android", "MAU", "MRR",
    "LTV", "ARPU", "NPS", "ROI", "GTM", "JTBD", "CustDev", "PRD", "QA", "SEO", "A/B",
    "KPI", "OKR", "CRM", "ARR", "CAC", "PMM", "UGC", "DTC", "HR", "CEO", "CTO", "FAQ", "ID",
})
_GENERIC_LOWER = frozenset(t.lower() for t in GENERIC_TERMS)


class FactFileError(ValueError):
    """Ошибка разметки файла опыта."""


@dataclass(frozen=True)
class Fact:
    id: str
    employer: str
    text: str
    numbers: frozenset[float]
    terms: frozenset[str]


@dataclass(frozen=True)
class Employer:
    code: str
    heading: str
    titles: tuple[str, ...]
    context_fact: str | None  # номер описания места работы, CODE-0


@dataclass(frozen=True)
class FactBase:
    facts: dict[str, Fact]
    employers: dict[str, Employer]
    headlines: tuple[str, ...]
    vocabulary: frozenset[str]
    not_counted: tuple[str, ...]


def strip_markers(text: str) -> str:
    """Текст файла опыта без номеров фактов (вместе с пробелом перед номером)."""
    return _MARKER_FOR_STRIP.sub("", text)


# ---------- числа и названия ----------

_THOUSANDS = r"\d{1,3}(?:[,   ]\d{3})+(?!\d)"
_NUMBER = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<num>{_THOUSANDS}|\d+(?:[.,]\d+)?)(?P<suffix>[KkКкM×xXхХ]?)(?![A-Za-z0-9])"
)
_NUMBER_WORDS = {
    w: i for i, w in enumerate(
        "one two three four five six seven eight nine ten eleven twelve".split(), start=1
    )
}
_NUMBER_WORD = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", re.I)
_YEARS = range(1990, 2031)


def extract_numbers(text: str) -> frozenset[float]:
    """Числа из текста. Годы 1990–2030 не считаются: даты проверяются по месту работы."""
    found: set[float] = set()
    for m in _NUMBER.finditer(text):
        raw, suffix = m.group("num"), m.group("suffix")
        if re.fullmatch(r"\d{4}", raw) and int(raw) in _YEARS and not suffix:
            continue
        if re.fullmatch(_THOUSANDS, raw):
            value = float(re.sub(r"[,   ]", "", raw))
        else:
            value = float(raw.replace(",", "."))
        if suffix in ("K", "k", "К", "к"):
            value *= 1_000
        elif suffix == "M":
            value *= 1_000_000
        found.add(round(value, 6))
    for m in _NUMBER_WORD.finditer(text):
        found.add(float(_NUMBER_WORDS[m.group(1).lower()]))
    return frozenset(found)


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _latin_words(text: str) -> list[re.Match[str]]:
    return [m for m in _WORD.finditer(text) if len(m.group()) >= 2]


def build_vocabulary(text: str) -> frozenset[str]:
    """Латинские слова из текста, начинающиеся с заглавной или с цифрой внутри, в нижнем регистре."""
    words = set()
    for m in _latin_words(strip_markers(text)):
        word = m.group()
        if word[0].isupper() or any(c.isdigit() for c in word):
            words.add(word.lower())
    return frozenset(words - _GENERIC_LOWER)


def extract_terms(text: str, vocabulary: frozenset[str]) -> frozenset[str]:
    """Названия из словаря, найденные в тексте (без учёта регистра, в нижнем регистре)."""
    return frozenset(m.group().lower() for m in _latin_words(text) if m.group().lower() in vocabulary)


def _starts_sentence(text: str, pos: int) -> bool:
    i = pos - 1
    while i >= 0 and text[i] in " \t\"'«“( ":
        i -= 1
    return i < 0 or text[i] in ".!?\n"


def unknown_terms(text: str, vocabulary: frozenset[str]) -> frozenset[str]:
    """Латинские слова с заглавной не в начале предложения, которых нет ни в словаре, ни в GENERIC_TERMS."""
    out = set()
    for m in _latin_words(text):
        word = m.group()
        low = word.lower()
        if not word[0].isupper() or low in vocabulary or low in _GENERIC_LOWER:
            continue
        if _starts_sentence(text, m.start()):
            continue
        out.add(word)
    return frozenset(out)


# ---------- должности ----------

_TITLES = re.compile(r"Должност(?:ь|и)(?: в резюме)?:\s*([^.]*)")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_CYRILLIC_PARENS = re.compile(r"\s*\([^()]*[А-Яа-яЁё][^()]*\)")


def _split_top_level(text: str) -> list[str]:
    """Делит по «, » вне скобок."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == "," and depth == 0 and text[i + 1 : i + 2] == " ":
            parts.append(text[start:i])
            start = i + 2
            i += 1
        i += 1
    parts.append(text[start:])
    return parts


def parse_titles(description: str) -> tuple[str, ...]:
    """Должности из описания места работы: текст после «Должность:» до первой точки."""
    m = _TITLES.search(description)
    if not m:
        return ()
    titles = []
    for part in _split_top_level(m.group(1).strip()):
        title = _CYRILLIC_PARENS.sub("", part).strip()
        # Кусок без латиницы («неполная занятость или контракт») названием должности не бывает.
        if title and not _CYRILLIC.search(title):
            titles.append(title)
    return tuple(titles)


# ---------- разбор файла ----------


@dataclass
class _Item:
    lines: list[str]
    bullet: bool


def _items(lines: list[str]) -> list[_Item]:
    """Пункты списка и абзацы раздела. Строка без пустой строки перед ней продолжает предыдущий пункт."""
    items: list[_Item] = []
    current: _Item | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped:
            current = None
        elif line.startswith("- "):
            current = _Item([stripped[2:]], True)
            items.append(current)
        elif current is None:
            current = _Item([stripped], False)
            items.append(current)
        else:
            current.lines.append(stripped)
    return items


def _split_sections(text: str, level: str) -> list[tuple[str, list[str]]]:
    """Заголовки уровня `level` ('## ' или '### ') и строки под ними до следующего заголовка того же или старшего уровня."""
    out: list[tuple[str, list[str]]] = []
    stop = ("# ", "## ") if level == "## " else ("# ", "## ", "### ")
    for line in text.splitlines():
        if line.startswith(level):
            out.append((line[len(level):].strip(), []))
        elif line.startswith(stop):
            out.append(("", []))  # раздел другого уровня закрывает текущий
        elif out:
            out[-1][1].append(line)
    return [(h, lines) for h, lines in out if h]


def _marked_section(heading: str, lines: list[str], facts: dict[str, Fact]) -> str | None:
    """Собирает факты раздела и возвращает его код. Раздел без пунктов и номеров («Перерыв») пропускается (None)."""
    items = _items(lines)
    texts = [" ".join(item.lines) for item in items]
    if not any(item.bullet for item in items) and not any(_MARKER_AT_END.search(t) for t in texts):
        return None
    code: str | None = None
    for text in texts:
        m = _MARKER_AT_END.search(text)
        if not m:
            raise FactFileError(f"Нет номера [КОД-N] в конце: «{text[:60]}» (раздел «{heading}»)")
        if code is not None and m.group(1) != code:
            raise FactFileError(f"В разделе «{heading}» два кода: {code} и {m.group(1)} ([{m.group(1)}-{m.group(2)}])")
        code = m.group(1)
        fact_id = f"{code}-{m.group(2)}"
        if fact_id in facts:
            raise FactFileError(f"Номер {fact_id} повторяется (раздел «{heading}»)")
        facts[fact_id] = Fact(fact_id, code, text[: m.start()].strip(), frozenset(), frozenset())
    return code


def parse_facts(path: Path) -> FactBase:
    text = path.read_text(encoding="utf-8")
    raw: dict[str, Fact] = {}
    employers: dict[str, Employer] = {}
    not_counted: tuple[str, ...] = ()

    for heading, lines in _split_sections(text, "## "):
        if heading == SECTION_NOT_COUNTED:
            not_counted = tuple(" ".join(i.lines) for i in _items(lines) if i.bullet)
        elif heading in (SECTION_PROFILE, SECTION_SKILLS):
            if _marked_section(heading, lines, raw) is None:
                raise FactFileError(f"В разделе «{heading}» нет ни одного номера")
        elif heading == SECTION_EXPERIENCE:
            for place, place_lines in _split_sections("\n".join(lines), "### "):
                code = _marked_section(place, place_lines, raw)
                if code is None:
                    continue
                context = f"{code}-0" if f"{code}-0" in raw else None
                if code in employers:
                    raise FactFileError(f"Код {code} у двух мест работы: «{employers[code].heading}» и «{place}»")
                employers[code] = Employer(code, place, parse_titles(raw[context].text if context else ""), context)

    headlines = _headlines(raw, not_counted)
    # Словарь только из текста пронумерованных фактов: вступление, «Перерыв» и «Не засчитывать»
    # в него не входят, иначе выдуманное название прошло бы проверку. Пункт с позиционированиями
    # даёт только разрешённые заголовки (CPO из него в словарь не попадает).
    vocabulary = build_vocabulary(
        "\n".join(", ".join(headlines) if f.text.startswith(HEADLINES_PREFIX) else f.text for f in raw.values())
    )
    facts = {
        fid: Fact(fid, f.employer, f.text, extract_numbers(f.text), extract_terms(f.text, vocabulary))
        for fid, f in raw.items()
    }
    return FactBase(
        facts=facts,
        employers=employers,
        headlines=headlines,
        vocabulary=vocabulary,
        not_counted=not_counted,
    )


def _headlines(facts: dict[str, Fact], not_counted: tuple[str, ...]) -> tuple[str, ...]:
    line = next((f.text for f in facts.values() if f.text.startswith(HEADLINES_PREFIX)), None)
    if line is None:
        raise FactFileError(f"В «Профиль» нет пункта «{HEADLINES_PREFIX} …»")
    names = [n.strip() for n in line[len(HEADLINES_PREFIX):].strip().rstrip(".").split(", ")]
    banned = "\n".join(not_counted)
    return tuple(
        n for n in names if n and not re.search(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])", banned)
    )
