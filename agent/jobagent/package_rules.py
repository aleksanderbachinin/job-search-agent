"""Правила состава пакета отклика: места работы, лимиты, запрещённые формулировки, стоп-фразы."""

import re

from jobagent.package_models import Lang, RoleType

# Места работы в порядке показа (коды из файла опыта).
COMPOSITION: dict[RoleType, tuple[str, ...]] = {
    "builder": ("VIBE", "GPTS", "MPS", "SKK", "STR", "AEZ"),
    "growth": ("RLY", "VIBE", "GPTS", "VERDA", "AIC", "MPS", "SKK", "STR", "AEZ"),
    "product": ("VIBE", "GPTS", "MPS", "SKK", "STR", "AEZ"),
}

REQUIRED: dict[RoleType, frozenset[str]] = {
    "builder": frozenset({"VIBE", "AEZ"}),
    "growth": frozenset({"AEZ"}),
    "product": frozenset({"AEZ"}),
}

# Эти места работы не берутся никогда: Clowe, Mediagram, Mir Mining.
NEVER: frozenset[str] = frozenset({"CLOWE", "MDG", "MIR"})

# Пункт места работы может ссылаться на факты вложенного места (под i-free идёт CoinKeeper),
# но только в резюме типа growth.
NESTING: dict[str, frozenset[str]] = {"VIBE": frozenset({"CK"})}
NESTING_ROLE_TYPES: frozenset[RoleType] = frozenset({"growth"})

# Как место работы называется в резюме.
EMPLOYER_NAMES: dict[str, str] = {
    "VIBE": "i-free",
    "GPTS": "GPT Scrambler",
    "STR": "Streasy (Proofix)",
    "RLY": "RLY",
    "VERDA": "Verda: Home Apothecary",
    "AIC": "AI-контент и собственные эксперименты",
    "MPS": "MPSTATS",
    "SKK": "Sidekick",
    "AEZ": "AEZAKMI Browser",
}


def allowed_fact_employers(job_code: str, role_type: RoleType) -> frozenset[str]:
    """Коды мест работы, на чьи факты может ссылаться пункт под job_code."""
    nested = NESTING.get(job_code, frozenset()) if role_type in NESTING_ROLE_TYPES else frozenset()
    return frozenset({job_code}) | nested


def bullet_limits(position: int) -> tuple[int, int]:
    """Минимум и максимум пунктов у места работы на позиции position (с нуля)."""
    return (3, 4) if position < 3 else (1, 2)


# Шаги подгонки резюме под одну страницу, по порядку.
CUT_ORDER: tuple[str, ...] = ("trim:SKK", "trim:STR", "trim_long", "drop:SKK")

# Формулировки из раздела «Не засчитывать» файла опыта, по шаблону (минимум) на пункт.
# Первый элемент пары — код места работы, к которому привязан шаблон (проверяется только в его
# пунктах), или None: шаблон проверяется везде. Шаблоны ловят сочетания, а не отдельные слова.
_F = re.IGNORECASE
FORBIDDEN: tuple[tuple[str | None, re.Pattern[str]], ...] = (
    # AI-хакатоны на VibeCamp
    ("VIBE", re.compile(r"hackathon|хакатон", _F)),
    # VibeCamp «−30% переделок» и «−50% от идеи до инструмента»
    ("VIBE", re.compile(r"(?<![\d.,])30\s?%")),
    ("VIBE", re.compile(r"(?<![\d.,])50\s?%")),
    # MPSTATS: ML-кластеры поведения, персонализированный онбординг, рост ARPU
    (None, re.compile(r"\bML\b.{0,20}(cluster|segment|кластер|сегмент)", _F)),
    ("MPS", re.compile(r"\bARPU\b", _F)),
    ("MPS", re.compile(r"персонализированн\w*\s+онбординг|personali[sz]ed\s+onboarding", _F)),
    # Sidekick «time-to-market −20%»
    ("SKK", re.compile(r"time[\s-]to[\s-]market", _F)),
    # Streasy «+40% узнаваемости бренда»
    ("STR", re.compile(r"brand\s+(awareness|recognition)|узнаваемост", _F)),
    # GPT Scrambler: размер MRR (денежная сумма рядом, до 15 знаков с любой стороны) и подрядчики на Reddit
    ("GPTS", re.compile(
        r"MRR.{0,15}(\$|USD|₽|руб|\d\s?[KkКк]\b)|(\$|USD|₽|руб|\d\s?[KkКк]\b).{0,15}MRR", _F)),
    ("GPTS", re.compile(
        r"(contractor|подрядчик).{0,30}Reddit|Reddit.{0,30}(contractor|подрядчик)", _F)),
    # Должность CPO, Head of Data и KYB/compliance-опыт
    (None, re.compile(r"\bCPO\b|Chief\s+Product\s+Officer")),
    (None, re.compile(r"Head\s+of\s+Data", _F)),
    (None, re.compile(r"\bKYB\b|compliance", _F)),
    # Python как рабочий навык: допустим только рядом с Claude Code (весь текст утверждения)
    # и как стек нанятой команды («Python/Node.js»)
    (None, re.compile(r"\A(?!.*Claude Code)(?!.*Python/Node).*\bPython\b", re.DOTALL)),
    # Proofix «руководил отделом роста, нанял 3 продажников»
    ("STR", re.compile(r"\b(руководил\w*|led|headed|managed|возглавил\w*).{0,30}(отдел|department|growth team|growth org|sales team)", _F)),
    ("STR", re.compile(r"(нанял\w*|hired).{0,20}(продажник|sales)", _F)),
    # Цифры ×3,1 / ×4,9 / ×7,6 по Cheng
    # префикс «×3,1» и суффикс «3.1x»
    (None, re.compile(
        r"(?<![A-Za-zА-Яа-я])[×xх]\s?(3[,.]1|4[,.]9|7[,.]6)(?!\d)"
        r"|(?<![\d.,])(3[,.]1|4[,.]9|7[,.]6)\s?[x×х](?!\w)", _F)),
)

# Фразы из таблицы «Avoid» cover_letter_guide.md и русские аналоги (нижний регистр, для сравнения без учёта регистра).
STOP_PHRASES: dict[Lang, tuple[str, ...]] = {
    "en": (
        "to whom it may concern",
        "i'm applying for the position",
        "i am applying for the position",
        "hard worker",
        "team player",
        "results-driven",
        "i would be perfect for this role",
        "i'm passionate about what you do",
        "i am passionate about what you do",
        "this position would help me grow",
        "i think i would be a great fit",
    ),
    "ru": (
        "кому это может касаться",
        "я подаю заявку на позицию",
        "трудолюбив",
        "командный игрок",
        "ориентирован на результат",
        "я идеально подхожу",
        "я увлечён",
        "эта позиция поможет мне вырасти",
        "я думаю, что подойду",
    ),
}
