"""Служебные команды: настройка вебхука в Hirify, пробная оценка вакансии, проверка промптов.

  uv run python -m jobagent.cli feeds
  uv run python -m jobagent.cli setup-webhook https://<ваш-домен>/hirify/webhook
  uv run python -m jobagent.cli test-webhook <webhook_id>
  uv run python -m jobagent.cli dry-run <vacancy_id> [--file vacancy.json]
  uv run python -m jobagent.cli eval prefilter|triage [--compare было.json]
  uv run python -m jobagent.cli eval scoring --limit 10 [--compare было.json]
  uv run python -m jobagent.cli eval prefilter --compare было.json стало.json
  uv run python -m jobagent.cli package <vacancy_id> [--file vacancy.json] [--out папка]
  uv run python -m jobagent.cli package-eval [--fixtures папка]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import replace
from html import unescape
from pathlib import Path
from typing import Any

import anthropic

from . import evals, package_eval, rules
from .cards import render_full
from .config import AGENT_DIR, Settings
from .hirify import HirifyClient
from .main import make_package_builder
from .package_bot import render_summary
from .package_store import _private_dir, _write, _write_text, map_to_markdown
from .pdf import LOCATION_LINE, to_markdown
from .pricing import usage_cost
from .research import LinkedInResearch, skip_reason
from .scoring import Scorer


async def cmd_feeds(s: Settings) -> None:
    hirify = HirifyClient(s.hirify_api_key)
    try:
        for f in await hirify.list_feeds():
            mark = "●" if f.get("is_active") else "○"
            print(f"{mark} {f['id']:>6}  {f.get('name')}")
    finally:
        await hirify.aclose()


def find_secret(data: Any) -> str | None:
    """Имя поля с секретом в OpenAPI не описано: ищем строковое поле, в имени которого есть secret."""
    if isinstance(data, dict):
        for key, value in data.items():
            if "secret" in key.lower() and isinstance(value, str) and value:
                return value
        for value in data.values():
            if found := find_secret(value):
                return found
    return None


def write_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [ln for ln in lines if not ln.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


async def cmd_setup_webhook(s: Settings, url: str, feed_ids: list[int], write_env: Path | None) -> None:
    hirify = HirifyClient(s.hirify_api_key)
    try:
        existing = next((w for w in await hirify.list_webhooks() if w.get("url") == url), None)
        if existing:
            webhook_id = existing["id"]
            hook = await hirify.rotate_webhook_secret(webhook_id)
            print(f"Вебхук уже был: id={webhook_id}, выпущен новый секрет")
        else:
            hook = await hirify.create_webhook("jobagent", url)
            webhook_id = hook.get("id")
            print(f"Вебхук создан: id={webhook_id}")
        secret = find_secret(hook)
        if write_env:
            if not secret:
                raise SystemExit(f"В ответе Hirify нет секрета. Поля ответа: {sorted(hook)}")
            write_env_value(write_env, "HIRIFY_WEBHOOK_SECRET", secret)
            print(f"Секрет подписи записан в {write_env}")
        elif secret:
            print("Секрет подписи показывается один раз. Впишите его в agent/.env:")
            print(f"HIRIFY_WEBHOOK_SECRET={secret}")
        else:
            print(json.dumps(hook, ensure_ascii=False, indent=1))
        feeds = await hirify.list_feeds()
        targets = [f for f in feeds if (f["id"] in feed_ids if feed_ids else f.get("is_active"))]
        for f in targets:
            await hirify.set_feed_webhook(f["id"], webhook_id)
            print(f"  фильтр {f['id']} «{f.get('name')}» → вебхук {webhook_id}")
    finally:
        await hirify.aclose()


async def cmd_test_webhook(s: Settings, webhook_id: int) -> None:
    hirify = HirifyClient(s.hirify_api_key)
    try:
        print(json.dumps(await hirify.test_webhook(webhook_id), ensure_ascii=False, indent=1))
        await asyncio.sleep(10)
        print(json.dumps(await hirify.webhook_deliveries(webhook_id), ensure_ascii=False, indent=1)[:3000])
    finally:
        await hirify.aclose()


async def read_detail(s: Settings, vacancy_id: int, file: Path | None) -> dict[str, Any]:
    """Полный текст вакансии: из JSON-файла (как отдаёт vacancies.read) или из Hirify."""
    if file:
        detail = json.loads(file.read_text(encoding="utf-8"))
        return detail.get("data", detail)
    hirify = HirifyClient(s.hirify_api_key)
    try:
        return await hirify.read_vacancy(vacancy_id)
    finally:
        await hirify.aclose()


async def cmd_dry_run(s: Settings, vacancy_id: int, file: Path | None) -> None:
    """Прогоняет одну вакансию через отсев и печатает карточку. Ничего не пишет в базу и бот."""
    detail = await read_detail(s, vacancy_id, file)
    if reason := rules.check_card(detail):
        print(f"Правила отсеяли: {reason}")
        return
    scorer = Scorer(s.knowledge_dir, s.triage_model, s.scoring_model, s.scoring_effort)
    triage, usage = await scorer.triage(detail)
    print(f"Haiku: relevant={triage.relevant} ({triage.reason}); токены {usage}")
    if stale := rules.check_detail(detail, s.max_age_days):
        print(f"Правила по полному тексту: {stale[1]} (в dry-run продолжаю оценку)")
    if s.prefilter:
        pre, usage = await scorer.prefilter(detail, [{"id": 0, "name": "dry-run"}])
        verdict = "" if pre.relevant else " (в dry-run продолжаю оценку)"
        print(f"Haiku по тексту: relevant={pre.relevant} ({pre.reason}); токены {usage}{verdict}")
    if s.scrapecreators_api_key and (skip := skip_reason(detail)):
        print(f"LinkedIn: не нужен, {skip}")
    elif s.scrapecreators_api_key:
        research = LinkedInResearch(s.scrapecreators_api_key)
        try:
            found = await research.research(detail.get("company"), detail.get("description") or "")
        finally:
            await research.aclose()
        print(f"LinkedIn: {'найдено' if found else 'нет данных'}, кредитов {research.credits_used}")
        if found:
            detail["linkedin_research"] = found
    assessment, usage = await scorer.assess(detail, [{"id": 0, "name": "dry-run"}])
    print(f"Sonnet: токены {usage}\n")
    html = render_full(detail.get("title", ""), assessment.model_dump(), [{"id": 0, "name": "dry-run"}], detail, expanded=True)
    print(unescape(re.sub(r"</?[bi]>", "", html)))
    print("\n" + json.dumps(assessment.model_dump(), ensure_ascii=False, indent=1))


PACKAGES_OUT = AGENT_DIR / "data" / "packages-cli"


async def cmd_package(
    s: Settings, vacancy_id: int, file: Path | None, out: Path | None, client: anthropic.AsyncAnthropic | None = None
) -> Path:
    """Собирает пакет одной вакансии без базы и бота: файлы в out, сводка проверки в консоль."""
    builder = make_package_builder(s, client)
    if builder is None:
        raise SystemExit("Для пакета нужны CANDIDATE_EMAIL и CANDIDATE_LINKEDIN в agent/.env")
    v = package_eval.vacancy_from_detail(await read_detail(s, vacancy_id, file))
    built, usages = await builder.build(v)
    out = out or PACKAGES_OUT / v.slug
    _private_dir(out)
    contacts = replace(builder.contacts, location_line=LOCATION_LINE[built.lang])
    _write(out / built.filename, built.pdf)
    _write_text(out / "resume.md", to_markdown(built.resume, builder.fb, contacts, built.lang))
    _write_text(out / "letter.md", built.letter_text)
    _write_text(out / "experience_map.md", map_to_markdown(built.req_map))
    costs = [usage_cost(u) for _, u in usages]
    print(f"Пакет {v.slug}: язык {built.lang}, тип {built.role_type}, страниц {package_eval.pdf_pages(built.pdf)}")
    print(render_summary(built) or "Проверка: замечаний нет")
    print(f"Вызовов моделей: {len(usages)}, стоимость ${sum(c for c in costs if c is not None):.3f}")
    print(f"Файлы: {out}")
    return out


async def cmd_package_eval(s: Settings, fixtures: Path) -> None:
    """Сборка пакетов по фикстурам настоящими моделями (стоит денег), отчёт рядом с отчётами eval."""
    # SDK сам повторяет запросы при 429, 5xx и обрывах связи.
    builder = make_package_builder(s, anthropic.AsyncAnthropic(max_retries=6))
    if builder is None:
        raise SystemExit("Для пакета нужны CANDIDATE_EMAIL и CANDIDATE_LINKEDIN в agent/.env")
    run = await package_eval.run(fixtures, builder, out_dir=evals.EVALS_DIR)
    print(package_eval.render_report(run))


async def cmd_eval(s: Settings, args: argparse.Namespace) -> None:
    """Прогон шага по снимку базы или сравнение двух сохранённых прогонов. В рабочую базу не пишет."""
    compare = [evals.load_run(p) for p in args.compare or []]
    # До запросов к API: сравнение с прогоном другого шага бессмысленно, а scoring стоит денег.
    for path, other in zip(args.compare or [], compare):
        if other["step"] != args.step:
            raise SystemExit(f"{path} — прогон шага {other['step']}, а сравнивается {args.step}")
    if len(compare) == 2:
        print(evals.render_compare(*compare))
        return
    if not args.db.exists():
        raise SystemExit(f"Нет снимка базы {args.db}. Как его снять — agent/README.md, «Проверка промптов».")
    # SDK сам повторяет запросы при 429, 5xx и обрывах связи; на прогон из сотни вакансий берём запас.
    client = anthropic.AsyncAnthropic(max_retries=6)
    scorer = Scorer(s.knowledge_dir, s.triage_model, s.scoring_model, s.scoring_effort, client=client)
    only = {i["slug"] for i in compare[0]["items"]} if compare and args.step == "scoring" else None
    run = await evals.run_eval(
        args.step, args.db, scorer, evals.EVALS_DIR, args.limit, args.concurrency, s.min_score, only
    )
    print(evals.render_report(run))
    if compare:
        print("\n" + evals.render_compare(compare[0], run))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="jobagent.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("feeds", help="список фильтров Hirify")
    sw = sub.add_parser("setup-webhook", help="создать вебхук и подключить к фильтрам")
    sw.add_argument("url")
    sw.add_argument("--feeds", default="", help="id фильтров через запятую (по умолчанию все активные)")
    sw.add_argument("--write-env", type=Path, help="записать секрет в этот .env, не печатая его")
    tw = sub.add_parser("test-webhook", help="отправить тестовое событие")
    tw.add_argument("webhook_id", type=int)
    dr = sub.add_parser("dry-run", help="оценить одну вакансию без записи в базу")
    dr.add_argument("vacancy_id", type=int)
    dr.add_argument("--file", type=Path, help="JSON полной вакансии вместо запроса к Hirify")
    ev = sub.add_parser("eval", help="проверить промпт шага на снимке базы")
    ev.add_argument("step", choices=evals.STEPS)
    ev.add_argument("--limit", type=int, help=f"сколько вакансий (для scoring обязательно, до {evals.MAX_SCORING_LIMIT})")
    ev.add_argument(
        "--compare", type=Path, nargs="+", metavar="FILE",
        help="сравнить с прошлым прогоном; с двумя файлами — только сравнить, без прогона",
    )
    ev.add_argument("--db", type=Path, default=evals.SNAPSHOT_PATH, help="снимок базы")
    ev.add_argument("--concurrency", type=int, default=evals.CONCURRENCY, help="запросов одновременно")
    pk = sub.add_parser("package", help="собрать пакет отклика одной вакансии без базы и бота")
    pk.add_argument("vacancy_id", type=int)
    pk.add_argument("--file", type=Path, help="JSON полной вакансии вместо запроса к Hirify")
    pk.add_argument("--out", type=Path, help=f"папка для файлов (по умолчанию {PACKAGES_OUT}/<slug>)")
    pe = sub.add_parser("package-eval", help="собрать пакеты по фикстурам и проверить итог (Opus, около доллара)")
    pe.add_argument("--fixtures", type=Path, default=package_eval.FIXTURES_DIR, help="папка с JSON вакансий")
    args = p.parse_args(argv)
    if args.cmd == "eval":
        if args.compare and len(args.compare) > 2:
            ev.error("--compare принимает один или два файла")
        if args.concurrency < 1:
            ev.error("--concurrency должен быть не меньше 1")
        runs_model = not (args.compare and len(args.compare) == 2)
        if args.step == "scoring" and runs_model and not args.limit:
            ev.error(f"для scoring нужен --limit от 1 до {evals.MAX_SCORING_LIMIT}: около 2 центов за вакансию")
        if args.step == "scoring" and args.limit and not 1 <= args.limit <= evals.MAX_SCORING_LIMIT:
            ev.error(f"--limit для scoring: от 1 до {evals.MAX_SCORING_LIMIT}")

    s = Settings.from_env()
    if args.cmd == "feeds":
        s.require("hirify_api_key")
        asyncio.run(cmd_feeds(s))
    elif args.cmd == "setup-webhook":
        s.require("hirify_api_key")
        ids = [int(x) for x in args.feeds.split(",") if x.strip()]
        asyncio.run(cmd_setup_webhook(s, args.url, ids, args.write_env))
    elif args.cmd == "test-webhook":
        s.require("hirify_api_key")
        asyncio.run(cmd_test_webhook(s, args.webhook_id))
    elif args.cmd == "dry-run":
        if not args.file:
            s.require("hirify_api_key")
        asyncio.run(cmd_dry_run(s, args.vacancy_id, args.file))
    elif args.cmd == "eval":
        asyncio.run(cmd_eval(s, args))
    elif args.cmd == "package":
        if not args.file:
            s.require("hirify_api_key")
        asyncio.run(cmd_package(s, args.vacancy_id, args.file, args.out))
    elif args.cmd == "package-eval":
        asyncio.run(cmd_package_eval(s, args.fixtures))


if __name__ == "__main__":
    main()
