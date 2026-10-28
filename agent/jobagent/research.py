"""Проверка «русскоязычная компания» по LinkedIn через ScrapeCreators.

Вкладку People в LinkedIn без входа не получить, поэтому сотрудников ищем через Google:
site:linkedin.com/in "<компания>". Страницу компании (штаб-квартира, размер, похожие
компании) берём из ScrapeCreators по ссылке, найденной тем же поиском. Три кредита на вакансию.

У российских компаний на hh название часто кириллицей, а в LinkedIn — латиницей
(«Доксинбокс» и DocsInBox). Поэтому пробуем несколько написаний, пока не найдутся люди.
Пустой поиск ScrapeCreators отдаёт как 404 и кредит за него не списывает.

Проверка нужна только англоязычным вакансиям компаний не из России (решение 2026-10-27):
русский текст или российская локация уже говорят о русскоязычной компании.
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.scrapecreators.com"
MAX_PEOPLE = 10
COMPANY_URL = re.compile(r"https?://([a-z]{2,3}\.)?(www\.)?linkedin\.com/company/[^/?#\s]+", re.IGNORECASE)
MAX_NAME_TRIES = 3
LATIN = re.compile(r"[A-Za-z]")
LATIN_WORD = re.compile(r"\b[A-Za-z][A-Za-z0-9&.-]{2,}\b")
# Сходство транслита с латинским словом из текста: «доксинбокс» → doksinboks ≈ docsinbox (0,84).
MIN_SIMILARITY = 0.7
TRANSLIT = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        "a b v g d e e zh z i y k l m n o p r s t u f kh ts ch sh sch - y - e yu ya".split(),
        strict=True,
    )
)


CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def skip_reason(detail: dict[str, Any]) -> str | None:
    """Почему LinkedIn-проверка не нужна; None — нужна."""
    if detail.get("company_masked") or not detail.get("company"):
        return "компания скрыта"
    lang = detail.get("vacancy_language")
    if not lang:
        # Язык не размечен: смотрим, каких букв в тексте больше.
        text = detail.get("description") or ""
        lang = "ru" if len(CYRILLIC.findall(text)) > len(LATIN.findall(text)) else "en"
    if lang == "ru":
        return "вакансия на русском"
    regions = {r.get("code") if isinstance(r, dict) else r for r in detail.get("regions") or []}
    if "russia" in regions or detail.get("macroregion") == "russia":
        return "компания в России"
    return None


def translit(text: str) -> str:
    return "".join(TRANSLIT.get(ch, ch) if ch.lower() in TRANSLIT else ch for ch in text.lower()).replace("-", "")


def name_variants(company: str, description: str = "") -> list[str]:
    """Написания названия для поиска, от самого вероятного в LinkedIn к наименее вероятному."""
    parts = [p.strip(" «»\"'") for p in re.split(r"[()/]", company) if p.strip(" «»\"'")]
    out = [p for p in parts if LATIN.search(p)]
    for cyr in (p for p in parts if not LATIN.search(p)):
        target = translit(cyr).replace(" ", "")
        words = {w.strip(".-") for w in LATIN_WORD.findall(description)}
        best = max(words, key=lambda w: SequenceMatcher(None, target, w.lower()).ratio(), default="")
        if best and SequenceMatcher(None, target, best.lower()).ratio() >= MIN_SIMILARITY:
            out.append(best)
        out += [translit(cyr).title(), cyr]
    seen: list[str] = []
    for n in out:
        if n and n.lower() not in (s.lower() for s in seen):
            seen.append(n)
    return seen


class LinkedInResearch:
    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = 40.0):
        self._http = httpx.AsyncClient(
            base_url=base_url, headers={"x-api-key": api_key, "Accept": "application/json"}, timeout=timeout
        )
        self.credits_used = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        resp = await self._http.get(path, params=params)
        if resp.status_code == 404 and path == "/v1/google/search":
            return {"results": [], "credits_charged": 0}
        resp.raise_for_status()
        data = resp.json()
        charged = data.get("credits_charged")
        self.credits_used += 1 if charged is None else int(charged)
        return data

    async def _google(self, query: str) -> list[dict[str, Any]]:
        data = await self._get("/v1/google/search", query=query)
        return data.get("results") or []

    async def research(self, company: str | None, description: str = "") -> dict[str, Any] | None:
        """Сводка по компании для промпта Sonnet или None, если компания скрыта или поиск не удался.

        Сбой ScrapeCreators не должен останавливать оценку: Sonnet тогда судит по тексту.
        """
        variants = name_variants((company or "").strip(), description)
        if not variants:
            return None
        name = variants[0]
        try:
            people: list[dict[str, Any]] = []
            for name in variants[:MAX_NAME_TRIES]:
                if people := await self._google(f'site:linkedin.com/in "{name}"'):
                    break
            out: dict[str, Any] = {
                "query_company": name,
                "people": [
                    {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("description")}
                    for r in people[:MAX_PEOPLE]
                ],
            }
            pages = await self._google(f'site:linkedin.com/company "{name}"')
            url = next((m.group(0) for r in pages if (m := COMPANY_URL.match(r.get("url") or ""))), None)
            if url:
                out["company"] = company_summary(await self._get("/v1/linkedin/company", url=url))
            return out
        except (httpx.HTTPError, ValueError) as e:
            log.warning("LinkedIn-проверка «%s» не удалась: %s", name, e)
            return None


def company_summary(data: dict[str, Any]) -> dict[str, Any]:
    """Только поля, полезные для RU-проверки: без постов, логотипов и картинок."""
    keys = ("name", "url", "headquarters", "location", "size", "employeeCount", "founded", "website", "industry")
    out = {k: data.get(k) for k in keys if data.get(k) not in (None, "", [], {})}
    if data.get("employees"):
        out["featured_employees"] = [{"name": e.get("name"), "title": e.get("title")} for e in data["employees"]]
    if data.get("similarPages"):
        out["similar_pages"] = [{"name": p.get("name"), "link": p.get("link")} for p in data["similarPages"]]
    return out
