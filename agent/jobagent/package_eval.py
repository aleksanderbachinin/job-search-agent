"""Проверка сборщика пакетов на зафиксированных вакансиях: tests/fixtures/packages/*.json.

Фикстура — полный текст вакансии из Hirify (`vacancies.read`, поле data) без контактов, или
ловушка, написанная вручную в той же форме. Каждая вакансия собирается заново тем сборщиком,
который передан (в CLI — настоящие модели, это стоит денег).

В отчёт идут только числа и коды: сколько утверждений убрано по каким кодам, предупреждения,
шаги подгонки под страницу, страницы, слова письма, стоимость. Ни текста вакансии, ни резюме,
ни письма. Итог, который должен быть нулевым: утверждений без источника в финале, тире в письме,
PDF больше одной страницы.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic

from . import evals
from .config import AGENT_DIR
from .db import Vacancy
from .factcheck import check_letter, check_resume
from .package import ON_VIOLATION, BuiltPackage, PackageBuilder, _company, _role
from .pdf import PackageError
from .pricing import usage_cost
from .scoring import Usage
from .textchecks import find_dashes, word_count

FIXTURES_DIR = AGENT_DIR / "tests" / "fixtures" / "packages"

# Страницы считаются по самому PDF, а не по тому, что вернул движок вёрстки.
_PAGE_RE = re.compile(rb"/Type\s*/Page[^s]")


def load_fixture(path: Path) -> Vacancy:
    detail = json.loads(path.read_text(encoding="utf-8"))
    return vacancy_from_detail(detail.get("data", detail))


def vacancy_from_detail(detail: dict[str, Any]) -> Vacancy:
    """Вакансия из полного текста, как её видит сборщик: показана, без оценки Sonnet."""
    company = None if detail.get("company_masked") else detail.get("company")
    return Vacancy(
        slug=detail["slug"],
        vacancy_id=detail.get("vacancy_id"),
        title=detail.get("title") or "",
        company=company,
        feeds=[],
        card=detail,
        detail=detail,
        assessment=None,
        score=None,
        status="shown",
        stage=None,
        reason=None,
        source="eval",
        attempts=0,
        tg_message_id=None,
        tracker_id=None,
    )


def pdf_pages(pdf: bytes) -> int:
    return len(_PAGE_RE.findall(pdf))


def unsourced(v: Vacancy, built: BuiltPackage, builder: PackageBuilder) -> int:
    """Утверждения итогового пакета, которые проверка кодом по-прежнему удалила бы."""
    violations = check_resume(built.resume, built.req_map, builder.fb, built.lang)
    violations += check_letter(built.letter, built.req_map, builder.fb, built.lang, _company(v), _role(v))
    return len({x.path for x in violations if ON_VIOLATION[x.code] == "remove"})


def _cost(usages: list[tuple[str, Usage]]) -> tuple[float, list[str]]:
    total, unknown = 0.0, set()
    for _, u in usages:
        cost = usage_cost(u)
        if cost is None:
            unknown.add(u.model)
        else:
            total += cost
    return total, sorted(unknown)


def _item(name: str, v: Vacancy) -> dict[str, Any]:
    return {
        "fixture": name,
        "vacancy_id": v.vacancy_id,
        "error": None,
        "lang": None,
        "role_type": None,
        "calls": {},
        "retried": False,
        "removed": {},
        "warnings": 0,
        "cut_steps": [],
        "pages": None,
        "letter_words": None,
        "dashes": None,
        "unsourced": None,
        "cost_usd": 0.0,
        "unpriced_models": [],
    }


async def evaluate(name: str, v: Vacancy, builder: PackageBuilder) -> dict[str, Any]:
    item = _item(name, v)
    try:
        built, usages = await builder.build(v)
    except PackageError as err:
        item["error"], usages = err.code, err.usages
        built = None
    except anthropic.APIError as err:
        item["error"], usages = f"api_error:{type(err).__name__}", []
        built = None
    calls = Counter(step for step, _ in usages)
    item["calls"] = dict(calls)
    item["retried"] = calls["package_resume"] > 1 or calls["package_letter"] > 1
    item["cost_usd"], item["unpriced_models"] = _cost(usages)
    if built is None:
        return item
    item.update(
        lang=built.lang,
        role_type=built.role_type,
        removed=dict(sorted(Counter(x.code for x in built.removed).items())),
        warnings=len(built.warnings),
        cut_steps=built.cut_steps,
        pages=pdf_pages(built.pdf),
        letter_words=word_count(built.letter_text),
        dashes=len(find_dashes(built.letter_text)),
        unsourced=unsourced(v, built, builder),
    )
    return item


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    done = [i for i in items if i["error"] is None]
    removed: Counter[str] = Counter()
    for i in done:
        removed.update(i["removed"])
    return {
        "packages": len(done),
        "errors": len(items) - len(done),
        "unsourced_final": sum(i["unsourced"] for i in done),
        "dashes": sum(i["dashes"] for i in done),
        "pages_over_1": sum(1 for i in done if i["pages"] != 1),
        "removed": dict(sorted(removed.items())),
        "retried": sum(1 for i in items if i["retried"]),
        "cost_usd": sum(i["cost_usd"] for i in items),
        "unpriced_models": sorted({m for i in items for m in i["unpriced_models"]}),
    }


async def run(fixtures: Path, builder: PackageBuilder, out_dir: Path | None = None) -> dict[str, Any]:
    """Собирает пакет по каждой фикстуре по очереди; с out_dir сохраняет отчёт туда (ключ path)."""
    items = []
    for path in sorted(fixtures.glob("*.json")):
        items.append(await evaluate(path.stem, load_fixture(path), builder))
    report: dict[str, Any] = {
        "step": "package",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "git_sha": evals.git_sha(),
        "models": {"map": builder.map_model, "gen": builder.gen_model, "check": builder.check_model},
        "fixtures": str(fixtures),
        "summary": summarize(items),
        "items": items,
    }
    if out_dir is not None:
        report["path"] = evals.save_run(report, out_dir)
    return report


def render_report(run: dict[str, Any]) -> str:
    s = run["summary"]
    m = run["models"]
    lines = [
        f"eval package: {len(run['items'])} вакансий, модели {m['map']} / {m['gen']} / {m['check']}, "
        f"коммит {run['git_sha']}",
        "",
    ]
    for i in run["items"]:
        if i["error"]:
            lines.append(f"{i['fixture']}: ошибка {i['error']}, ${i['cost_usd']:.3f}")
            continue
        removed = ", ".join(f"{code} {n}" for code, n in i["removed"].items()) or "нет"
        lines.append(
            f"{i['fixture']}: {i['lang']}/{i['role_type']}, убрано: {removed}; предупреждений {i['warnings']}; "
            f"пересборка {'да' if i['retried'] else 'нет'}; подгонка {', '.join(i['cut_steps']) or 'нет'}; "
            f"страниц {i['pages']}; слов в письме {i['letter_words']}; ${i['cost_usd']:.3f}"
        )
    removed = ", ".join(f"{code} {n}" for code, n in s["removed"].items()) or "нет"
    lines += [
        "",
        f"Собрано: {s['packages']}, ошибок: {s['errors']}, с пересборкой: {s['retried']}",
        f"Убрано по кодам: {removed}",
        f"утверждений без источника в финале: {s['unsourced_final']}",
        f"тире: {s['dashes']}",
        f"страниц > 1: {s['pages_over_1']}",
        f"Стоимость: ${s['cost_usd']:.2f}"
        + (f" (без цены: {', '.join(s['unpriced_models'])})" if s["unpriced_models"] else ""),
    ]
    if run.get("path"):
        lines.append(f"Файл: {run['path']}")
    return "\n".join(lines)
