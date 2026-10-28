"""Механические проверки текста: тире, число слов, стоп-фразы, язык и имя PDF-файла."""

import re
import unicodedata

from jobagent.package_models import Lang
from jobagent.package_rules import STOP_PHRASES

_DASHES = "—–"  # U+2014 и U+2013
_DASH_RE = re.compile(f"[{_DASHES}]")
_LINE_START_DASH_RE = re.compile(f"^[ \\t]*[{_DASHES}][ \\t]*", re.MULTILINE)
_NUMBER_RANGE_RE = re.compile(f"(?<=\\d)[ \\t]*[{_DASHES}][ \\t]*(?=\\d)")
_LINE_END_DASH_RE = re.compile(f"[ \\t]*[{_DASHES}][ \\t]*$", re.MULTILINE)
_BETWEEN_WORDS_RE = re.compile(f"[ \\t]*[{_DASHES}][ \\t]*")
_WORD_RE = re.compile(r"\w+(?:[-'’]\w+)*")

# Доля кириллических букв среди всех букв, с которой текст считается русским.
_RU_SHARE = 0.3

_TRANSLIT: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}

_FILENAME_PREFIX = "Bachinin_Alexander"


def find_dashes(text: str) -> list[int]:
    """Позиции длинных и средних тире (— и –) в тексте."""
    return [m.start() for m in _DASH_RE.finditer(text)]


def replace_dashes(text: str) -> str:
    """Убирает тире: в начале строки удаляет, между числами заменяет на дефис, иначе на запятую.

    Дефис «-» не трогает. После замены find_dashes возвращает пустой список.
    """
    text = _LINE_START_DASH_RE.sub("", text)
    text = _NUMBER_RANGE_RE.sub("-", text)
    text = _LINE_END_DASH_RE.sub("", text)
    return _BETWEEN_WORDS_RE.sub(", ", text)


def word_count(text: str) -> int:
    """Число слов: последовательности букв и цифр, составные через «-» и апостроф считаются одним."""
    return len(_WORD_RE.findall(text))


def _normalize(text: str) -> str:
    """Нижний регистр и один вид апострофа, чтобы сравнивать без учёта регистра."""
    return text.lower().replace("’", "'")


def find_stop_phrases(text: str, lang: Lang) -> list[str]:
    """Фразы из таблицы STOP_PHRASES для языка lang, найденные в тексте (в порядке таблицы)."""
    haystack = _normalize(text)
    return [p for p in STOP_PHRASES[lang] if _normalize(p) in haystack]


def detect_language(text: str) -> Lang:
    """«ru», если доля кириллических букв среди всех букв не меньше 0,3, иначе «en»."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "en"
    cyrillic = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
    return "ru" if cyrillic / len(letters) >= _RU_SHARE else "en"


def vacancy_language(detail: dict) -> Lang:
    """Язык резюме и письма: указанный в вакансии (ru → ru, любой другой → en), иначе по описанию."""
    declared = detail.get("vacancy_language")
    if isinstance(declared, str) and declared.strip():
        return "ru" if declared.strip().lower() == "ru" else "en"
    return detect_language(detail.get("description") or "")


def _transliterate(text: str) -> str:
    """Кириллица в латиницу простой таблицей; регистр первой буквы сохраняется."""
    out: list[str] = []
    for ch in text:
        mapped = _TRANSLIT.get(ch.lower())
        if mapped is None:
            out.append(ch)
        else:
            out.append(mapped.capitalize() if ch.isupper() else mapped)
    return "".join(out)


def _clean_part(text: str) -> str:
    """Оставляет только [A-Za-z0-9], прочее (включая пробелы) сворачивается в одно «_»."""
    text = _transliterate(text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")


def pdf_filename(company: str | None, role: str) -> str:
    """Имя PDF: Bachinin_Alexander_<Company>_<Role>.pdf; компания скрыта или пуста — без неё."""
    parts = [_FILENAME_PREFIX]
    company_part = _clean_part(company) if company else ""
    if company_part:
        parts.append(company_part)
    parts.append(_clean_part(role) or "Role")
    return "_".join(parts) + ".pdf"
