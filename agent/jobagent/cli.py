"""Служебные команды: настройка вебхука в Hirify и пробная оценка вакансии.

  uv run python -m jobagent.cli feeds
  uv run python -m jobagent.cli setup-webhook https://<ваш-домен>/hirify/webhook
  uv run python -m jobagent.cli test-webhook <webhook_id>
  uv run python -m jobagent.cli dry-run <vacancy_id> [--file vacancy.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from html import unescape
from pathlib import Path
from typing import Any

from . import rules
from .cards import render_full
from .config import Settings
from .hirify import HirifyClient
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


async def cmd_dry_run(s: Settings, vacancy_id: int, file: Path | None) -> None:
    """Прогоняет одну вакансию через отсев и печатает карточку. Ничего не пишет в базу и бот."""
    if file:
        detail = json.loads(file.read_text(encoding="utf-8"))
        detail = detail.get("data", detail)
    else:
        hirify = HirifyClient(s.hirify_api_key)
        try:
            detail = await hirify.read_vacancy(vacancy_id)
        finally:
            await hirify.aclose()

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


def main() -> None:
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
    args = p.parse_args()

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


if __name__ == "__main__":
    main()
