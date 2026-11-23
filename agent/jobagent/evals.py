"""Проверка промптов на снимке рабочей базы: triage, prefilter и scoring против сохранённых меток.

Метки: оценка Sonnet из базы и решение Александра (откликнулся, пропустил с причиной).
Главная метрика отсева — ложные отсевы: вакансия с оценкой 4–5 или с откликом, которую шаг отсеял.
Каждый прогон сохраняется в JSON с хешем system prompt и git SHA, чтобы сравнить до и после правки.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
from anthropic import transform_schema

from . import rules
from .config import AGENT_DIR
from .db import Vacancy
from .scoring import Assessment, PrefilterAnswer, Triage

STEPS = ("triage", "prefilter", "scoring")
FILTER_STEPS = ("triage", "prefilter")
MAX_SCORING_LIMIT = 30
CONCURRENCY = 4
# Для правил по тексту: возраст вакансии в снимке не важен.
NO_AGE_LIMIT = 100_000

SNAPSHOT_PATH = AGENT_DIR / "data" / "eval.sqlite3"
EVALS_DIR = AGENT_DIR / "data" / "evals"

BUCKETS = {
    "applied": "откликнулся",
    "4-5": "оценка 4–5",
    "3": "оценка 3",
    "1-2": "оценка 1–2",
    "none": "без метки",
}
# Отсев вакансий из этих корзин — ложный.
GOOD_BUCKETS = ("applied", "4-5")


def label_of(v: Vacancy) -> dict[str, Any]:
    decision = v.status if v.status in ("applied", "skipped") else None
    return {
        "score": v.score,
        "decision": decision,
        "skip_reason": v.reason if decision == "skipped" else None,
    }


def bucket_of(label: dict[str, Any]) -> str:
    """Отклик важнее оценки: вакансия с откликом всегда в своей корзине."""
    if label["decision"] == "applied":
        return "applied"
    score = label["score"]
    if score is None:
        return "none"
    if score >= 4:
        return "4-5"
    return "3" if score == 3 else "1-2"


async def load_vacancies(db_path: Path) -> list[Vacancy]:
    # Только чтение: Database.connect перевёл бы файл в WAL и прогнал схему. Снимок не меняется,
    # поэтому immutable: SQLite не создаёт рядом -wal и -shm.
    conn = await aiosqlite.connect(f"file:{db_path.resolve()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = aiosqlite.Row
    try:
        async with conn.execute("SELECT * FROM vacancies ORDER BY created_at, slug") as cur:
            return [Vacancy.from_row(row) async for row in cur]
    finally:
        await conn.close()


def select(step: str, vacancies: list[Vacancy], limit: int | None = None) -> list[Vacancy]:
    """Какие вакансии видит шаг.

    triage — карточки, которые в работе дошли бы до Haiku: не baseline и прошли правила.
    prefilter — вакансии с сохранённым полным текстом, которые прошли правила по карточке и тексту.
    Свежесть не проверяем: в снимке почти всё уже старое, а проверяем мы промпт.
    С limit отсев берёт по одной вакансии из каждой корзины по кругу (отклики, 4–5, 3, 1–2,
    без метки): хорошие корзины, по которым считаются ложные отсевы, попадают первыми.
    scoring — вакансии с полным текстом и старой оценкой, по одной на оценку сверху вниз
    (отклики первыми), пока не наберётся limit.
    """
    if step in FILTER_STEPS:
        if step == "triage":
            pool = [v for v in vacancies if v.status != "baseline" and not card_reject(v)]
        else:
            pool = [v for v in vacancies if v.detail and not rules_reject(v)]
        if limit is None:
            return pool
        groups = {b: [v for v in pool if bucket_of(label_of(v)) == b] for b in BUCKETS}
        return round_robin(list(groups.values()), limit)
    groups_by_score: dict[int, list[Vacancy]] = {}
    for v in vacancies:
        if v.detail and v.score is not None:
            groups_by_score.setdefault(v.score, []).append(v)
    queues = [sorted(g, key=lambda v: v.status != "applied") for _, g in sorted(groups_by_score.items(), reverse=True)]
    return round_robin(queues, limit)


def round_robin(queues: list[list[Vacancy]], limit: int | None) -> list[Vacancy]:
    queues = [list(q) for q in queues]
    picked: list[Vacancy] = []
    while any(queues) and (limit is None or len(picked) < limit):
        for q in queues:
            if q and (limit is None or len(picked) < limit):
                picked.append(q.pop(0))
    return picked


def card_reject(v: Vacancy) -> bool:
    return rules.check_card(v.card, relocation=rules.format_exempt(v.feeds)) is not None


def rules_reject(v: Vacancy) -> bool:
    if card_reject(v):
        return True
    rejection = rules.check_detail(v.detail or {}, max_age_days=NO_AGE_LIMIT)
    return rejection is not None and rejection[0] != "stale"


async def evaluate(step: str, v: Vacancy, scorer: Any) -> tuple[dict[str, Any] | None, Any]:
    if step == "triage":
        verdict, usage = await scorer.triage(v.card)
        return {"relevant": verdict.relevant, "reason": verdict.reason}, usage
    if step == "prefilter":
        # В работе prefilter идёт до LinkedIn-проверки: блока, который снимок уже хранит, он не видит.
        detail = {k: x for k, x in v.detail.items() if k != "linkedin_research"}
        verdict, usage = await scorer.prefilter(detail, v.feeds)
        return {"relevant": verdict.relevant, "reason": verdict.reason}, usage
    a, usage = await scorer.assess(v.detail, v.feeds)
    return a.model_dump(), usage


async def run_items(step: str, vacancies: list[Vacancy], scorer: Any, concurrency: int) -> tuple[list, dict]:
    """Запросы идут параллельно, не больше concurrency сразу. Повторы при 429/5xx делает SDK."""
    sem = asyncio.Semaphore(concurrency)
    tokens = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}

    async def one(v: Vacancy) -> dict[str, Any]:
        label = label_of(v)
        item = {
            "slug": v.slug,
            "title": v.title,
            "company": v.company,
            "label": label,
            "bucket": bucket_of(label),
            "verdict": None,
            "error": None,
        }
        async with sem:
            try:
                item["verdict"], usage = await evaluate(step, v, scorer)
            except Exception as e:
                item["error"] = f"{type(e).__name__}: {e}"[:500]
                return item
        for key in tokens:
            tokens[key] += getattr(usage, key, 0) or 0
        return item

    return list(await asyncio.gather(*(one(v) for v in vacancies))), tokens


def rejected(item: dict[str, Any]) -> bool:
    return item["verdict"] is not None and not item["verdict"]["relevant"]


def false_rejections(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in items if rejected(i) and i["bucket"] in GOOD_BUCKETS]


def summarize(step: str, items: list[dict[str, Any]], min_score: int = 4) -> dict[str, Any]:
    errors = sum(1 for i in items if i["error"])
    if step in FILTER_STEPS:
        buckets = {b: {"total": 0, "rejected": 0} for b in BUCKETS}
        for i in items:
            if i["error"]:
                continue
            buckets[i["bucket"]]["total"] += 1
            buckets[i["bucket"]]["rejected"] += rejected(i)
        return {"buckets": buckets, "false_rejections": len(false_rejections(items)), "errors": errors}

    matrix: dict[str, dict[str, int]] = {}
    diffs = []
    crossed = 0
    for i in items:
        if i["error"]:
            continue
        old, new = i["label"]["score"], i["verdict"]["score"]
        row = matrix.setdefault(str(old), {})
        row[str(new)] = row.get(str(new), 0) + 1
        diffs.append(new - old)
        crossed += (old >= min_score) != (new >= min_score)
    return {
        "matrix": {k: dict(sorted(v.items())) for k, v in sorted(matrix.items())},
        "mean_diff": sum(diffs) / len(diffs) if diffs else 0.0,
        "same": diffs.count(0),
        "crossed_threshold": crossed,
        "min_score": min_score,
        "errors": errors,
    }


# Схема ответа входит в хэш промпта: порядок полей меняет ответы модели так же, как текст.
OUTPUT_SCHEMAS = {"triage": Triage, "prefilter": PrefilterAnswer, "scoring": Assessment}


def prompt_text(scorer: Any, step: str) -> str:
    if step == "triage":
        return scorer.triage_system
    blocks = scorer.prefilter_system if step == "prefilter" else scorer.scoring_system
    return "\n\n".join(b["text"] for b in blocks)


def git_sha() -> str:
    """Коммит кода и «-dirty», если есть незакоммиченные правки: промпт мог измениться до коммита."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=AGENT_DIR, capture_output=True, text=True).stdout.strip()

    sha = git("rev-parse", "--short", "HEAD") or "unknown"
    return sha + ("-dirty" if git("status", "--porcelain") else "")


def save_run(run: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{run['step']}"
    path = out_dir / f"{stem}.json"
    n = 2
    while path.exists():
        path = out_dir / f"{stem}-{n}.json"
        n += 1
    path.write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_run(path: Path) -> dict[str, Any]:
    run = json.loads(path.read_text(encoding="utf-8"))
    run["path"] = path
    return run


async def run_eval(
    step: str,
    db_path: Path,
    scorer: Any,
    out_dir: Path,
    limit: int | None = None,
    concurrency: int = CONCURRENCY,
    min_score: int = 4,
    only: set[str] | None = None,
    backend: str = "api",
) -> dict[str, Any]:
    """Прогоняет шаг по снимку, сохраняет результат в out_dir и возвращает его (с ключом path).

    only — slug вакансий, которыми ограничить прогон (для scoring: те же, что в прошлом прогоне).
    """
    vacancies = await load_vacancies(db_path)
    if only is not None:
        vacancies = [v for v in vacancies if v.slug in only]
    vacancies = select(step, vacancies, limit)
    items, tokens = await run_items(step, vacancies, scorer, concurrency)
    prompt = prompt_text(scorer, step)
    run = {
        "step": step,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "git_sha": git_sha(),
        "prompt_hash": hashlib.sha256(
            (prompt + json.dumps(transform_schema(OUTPUT_SCHEMAS[step]), ensure_ascii=False)).encode()
        ).hexdigest()[:12],
        "model": scorer.scoring_model if step == "scoring" else scorer.triage_model,
        # api — Claude API по ключу, claude-code — `claude -p` по подписке. Сравнивать внутри одного.
        "backend": backend,
        # С limit прогон взял выборку: --compare с ним повторяет ровно её.
        "limit": limit,
        "db": str(db_path),
        "summary": summarize(step, items, min_score),
        "tokens": tokens,
        "items": items,
    }
    run["path"] = save_run(run, out_dir)
    return run


# --- отчёты ---


def describe(item: dict[str, Any]) -> str:
    label = item["label"]
    parts = []
    if label["score"] is not None:
        parts.append(f"оценка {label['score']}")
    if label["decision"] == "applied":
        parts.append("откликнулся")
    elif label["decision"] == "skipped":
        parts.append(f"пропустил: {label['skip_reason']}")
    company = f" ({item['company']})" if item.get("company") else ""
    return f"{item['slug']} «{item['title']}»{company} [{', '.join(parts) or 'без метки'}]"


def render_header(run: dict[str, Any]) -> str:
    return (
        f"eval {run['step']}: {len(run['items'])} вакансий, модель {run['model']}, "
        f"бэкенд {run.get('backend', 'api')}, промпт {run['prompt_hash']}, коммит {run['git_sha']}"
    )


def render_report(run: dict[str, Any]) -> str:
    s = run["summary"]
    lines = [render_header(run), ""]
    if run["step"] in FILTER_STEPS:
        lines.append(f"{'корзина':<14}{'всего':>7}{'отсеяно':>9}")
        for key, name in BUCKETS.items():
            b = s["buckets"][key]
            lines.append(f"{name:<14}{b['total']:>7}{b['rejected']:>9}")
        false = false_rejections(run["items"])
        lines += ["", f"Ложных отсевов: {len(false)}"]
        lines += [f"  - {describe(i)}: {i['verdict']['reason']}" for i in false]
    else:
        lines += render_matrix(s)
        lines += [
            "",
            f"Средняя разница (новая − старая): {s['mean_diff']:+.2f}",
            f"Совпало: {s['same']} из {sum(sum(r.values()) for r in s['matrix'].values())}, "
            f"пересекли порог {s['min_score']}: {s['crossed_threshold']}",
        ]
        changed = [i for i in run["items"] if i["verdict"] and i["verdict"]["score"] != i["label"]["score"]]
        if changed:
            lines += ["", "Изменились:"]
            lines += [
                f"  - {describe(i)}: {i['label']['score']} → {i['verdict']['score']}, {i['verdict']['verdict']}"
                for i in changed
            ]
    errors = [i for i in run["items"] if i["error"]]
    if errors:
        lines += ["", f"Ошибки модели: {len(errors)} (в подсчёт не вошли)"]
        lines += [f"  - {i['slug']}: {i['error']}" for i in errors]
    t = run["tokens"]
    lines += [
        "",
        f"Токены: ввод {t['input_tokens']}, вывод {t['output_tokens']}, "
        f"кеш чтение {t['cache_read_tokens']}, кеш запись {t['cache_write_tokens']}",
    ]
    if run.get("path"):
        lines.append(f"Файл: {run['path']}")
    return "\n".join(lines)


def render_matrix(s: dict[str, Any]) -> list[str]:
    lines = ["Старая оценка (строки) → новая (столбцы):", "     " + "".join(f"{n:>4}" for n in range(1, 6))]
    for old in range(1, 6):
        row = s["matrix"].get(str(old), {})
        lines.append(f"{old:>5}" + "".join(f"{row.get(str(n), 0) or '.':>4}" for n in range(1, 6)))
    return lines


def state(step: str, item: dict[str, Any]) -> str:
    v = item["verdict"]
    if v is None:
        return f"ошибка: {item['error']}"
    if step == "scoring":
        return str(v["score"])
    return "пропущена" if v["relevant"] else f"отсеяна: {v['reason']}"


def decision(step: str, item: dict[str, Any]) -> Any:
    """С чем сравниваем прогоны: оценка или отсеяна/пропущена. Формулировка причины меняется от прогона к прогону."""
    v = item["verdict"]
    if v is None:
        return "error"
    return v["score"] if step == "scoring" else v["relevant"]


def diff_runs(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Вакансии, у которых вердикт изменился («было → стало»), в порядке прогона «после»."""
    if before["step"] != after["step"]:
        raise SystemExit(f"Нельзя сравнить разные шаги: {before['step']} и {after['step']}")
    old = {i["slug"]: i for i in before["items"]}
    changes = []
    for item in after["items"]:
        if item["slug"] not in old:
            continue
        was = old[item["slug"]]
        if decision(before["step"], was) != decision(after["step"], item):
            changes.append(
                {"slug": item["slug"], "item": item, "before": state(before["step"], was), "after": state(after["step"], item)}
            )
    return changes


def render_compare(before: dict[str, Any], after: dict[str, Any]) -> str:
    changes = diff_runs(before, after)
    step = after["step"]
    common = {i["slug"] for i in before["items"]} & {i["slug"] for i in after["items"]}
    lines = [
        f"Сравнение {step}: было {before.get('path', '?')} (промпт {before['prompt_hash']}, "
        f"коммит {before['git_sha']})",
        f"            стало {after.get('path', '?')} (промпт {after['prompt_hash']}, коммит {after['git_sha']})",
        f"Общих вакансий: {len(common)}, только в «было»: {len(before['items']) - len(common)}, "
        f"только в «стало»: {len(after['items']) - len(common)}",
        "",
        f"Изменились: {len(changes)}",
    ]
    lines += [f"  - {describe(c['item'])}: {c['before']} → {c['after']}" for c in changes]
    b, a = before["summary"], after["summary"]
    lines += ["", "До → после:"]
    if step in FILTER_STEPS:
        for key, name in BUCKETS.items():
            bb, ab = b["buckets"][key], a["buckets"][key]
            lines.append(f"  {name}: отсеяно {bb['rejected']} из {bb['total']} → {ab['rejected']} из {ab['total']}")
        lines.append(f"  ложных отсевов: {b['false_rejections']} → {a['false_rejections']}")
    else:
        lines.append(f"  средняя разница: {b['mean_diff']:+.2f} → {a['mean_diff']:+.2f}")
        lines.append(f"  совпало со старой оценкой: {b['same']} → {a['same']}")
        lines.append(f"  пересекли порог: {b['crossed_threshold']} → {a['crossed_threshold']}")
    lines.append(f"  ошибки модели: {b['errors']} → {a['errors']}")
    return "\n".join(lines)
