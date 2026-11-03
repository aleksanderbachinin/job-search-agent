"""Отсев без LLM: по карточке и по полному тексту вакансии."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

JUNIOR_GRADES = {"trainee", "intern", "junior"}

# Стоп-фактор с 2026-10-27: вакансии на украинском не берём.
UKRAINIAN_CODES = {"uk", "ua", "ukr", "ukrainian"}
# Буквы, которых нет в русском. В украинском тексте одна «і» даёт около 5% букв.
UKRAINIAN_LETTERS = re.compile(r"[іїєґІЇЄҐ]")
CYRILLIC = re.compile(r"[а-яёА-ЯЁіїєґІЇЄҐ]")
UKRAINIAN_SHARE = 0.02

# Стоп-фактор с 2026-11-03: вилка в рублях, верх которой ниже 250 тыс. в месяц. Нет верха —
# сравниваем низ («от 150 000»). Другие валюты не трогаем: там месячные и годовые вилки вперемешку.
MIN_SALARY_RUB = 250_000

# Роли, которые точно не про Александра, даже если в названии есть «product».
HARD_REJECT = re.compile(
    r"product\s+design|продуктов\w*\s+дизайн|product\s+analyst|продуктов\w*\s+аналитик"
    r"|data\s+analyst|data\s+scien|ux\s*/?\s*ui|ui\s*/?\s*ux",
    re.IGNORECASE,
)

# Слова целевых ролей: если они есть, название не отсеиваем.
KEEP = re.compile(
    r"product|продукт|\bpo\b|\bpm\b|\bpmm\b|growth|рост|маркетолог|marketing|cpo|builder",
    re.IGNORECASE,
)

# Роли не по профилю. Срабатывают, только если в названии нет слов из KEEP.
STOP = re.compile(
    r"develop|разработчик|engineer|инженер|programmer|designer|дизайнер|analyst|аналитик"
    r"|\bqa\b|тестировщик|devops|recruit|рекрутер|hr\b|sales|продаж|account\s+manager"
    r"|support|поддержк|copywriter|копирайтер|smm|accountant|бухгалтер|lawyer|юрист",
    re.IGNORECASE,
)


def is_ukrainian(vacancy: dict[str, Any]) -> bool:
    """По разметке языка Hirify, а если она не украинская — по буквам названия и текста."""
    if str(vacancy.get("vacancy_language") or "").lower() in UKRAINIAN_CODES:
        return True
    text = f"{vacancy.get('title') or ''} {vacancy.get('description') or ''}"
    cyrillic = len(CYRILLIC.findall(text))
    return cyrillic >= 20 and len(UKRAINIAN_LETTERS.findall(text)) / cyrillic > UKRAINIAN_SHARE


def low_salary(vacancy: dict[str, Any]) -> int | None:
    """Верх рублёвой вилки, если он ниже MIN_SALARY_RUB, иначе None."""
    salary = vacancy.get("salary")
    if not isinstance(salary, dict) or salary.get("currency") != "RUB":
        return None
    top = salary.get("max") or salary.get("min")
    if not isinstance(top, (int, float)) or isinstance(top, bool):
        return None
    return int(top) if 0 < top < MIN_SALARY_RUB else None


def check_card(card: dict[str, Any]) -> str | None:
    """Причина отказа по карточке или None, если карточку стоит смотреть дальше."""
    if card.get("has_applied"):
        return "уже откликался через Hirify"
    if card.get("in_tracker"):
        return "уже в трекере откликов"
    grades = {g.lower() for g in card.get("grades") or []}
    if grades and grades <= JUNIOR_GRADES:
        return f"грейд: {', '.join(sorted(grades))}"
    if is_ukrainian(card):
        return "вакансия на украинском"
    if top := low_salary(card):
        return f"зарплата до {top:,} ₽, ниже {MIN_SALARY_RUB:,} ₽".replace(",", " ")
    title = card.get("title") or ""
    if HARD_REJECT.search(title):
        return f"роль не по профилю: {title}"
    if not KEEP.search(title) and STOP.search(title):
        return f"роль не по профилю: {title}"
    return None


def check_detail(detail: dict[str, Any], max_age_days: int) -> tuple[str, str] | None:
    """(stage, причина) отказа по полному тексту или None."""
    if detail.get("has_applied") or detail.get("in_tracker"):
        return "already", "уже откликался или вакансия в трекере"
    if is_ukrainian(detail):
        return "rules", "вакансия на украинском"
    created = detail.get("created_at")
    if created:
        created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if datetime.now(UTC) - created_at > timedelta(days=max_age_days):
            return "stale", f"создана {created_at.date().isoformat()}, старше {max_age_days} дн."
    return None
