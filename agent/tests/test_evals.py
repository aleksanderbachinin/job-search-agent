"""Проверка промптов (jobagent.evals): без сети, на фейковом Scorer и базе в tmp_path."""

import asyncio
import json
import sqlite3

import pytest

from jobagent import cli, evals
from jobagent.db import Database
from jobagent.scoring import Triage, Usage
from test_service import CARD, assessment, detail

FEEDS = [{"id": 1, "name": "PM remote"}]
USAGE = Usage(model="fake", input_tokens=10, output_tokens=2, cache_read_tokens=0)


class FakeScorer:
    """Отвечает по slug: reject — {slug: причина} для отсева, scores — {slug: оценка}, fail — падает."""

    triage_model = "fake-haiku"
    scoring_model = "fake-sonnet"

    def __init__(self, reject=None, scores=None, fail=(), prompt="PREFILTER v1"):
        self.reject = reject or {}
        self.scores = scores or {}
        self.fail = set(fail)
        self.prefilter_system = [{"type": "text", "text": prompt}]
        self.scoring_system = [{"type": "text", "text": "SCORING v1"}]
        self.triage_system = "TRIAGE v1"
        self.calls: list[tuple[str, str]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def _answer(self, step, slug):
        self.calls.append((step, slug))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        if slug in self.fail:
            raise RuntimeError("API упал")

    async def triage(self, card):
        await self._answer("triage", card["slug"])
        reason = self.reject.get(card["slug"])
        return Triage(relevant=reason is None, reason=reason or "подходит"), USAGE

    async def prefilter(self, detail, feeds):
        assert feeds == FEEDS
        await self._answer("prefilter", detail["slug"])
        reason = self.reject.get(detail["slug"])
        return Triage(relevant=reason is None, reason=reason or "подходит"), USAGE

    async def assess(self, detail, feeds):
        await self._answer("scoring", detail["slug"])
        return assessment(score=self.scores[detail["slug"]]), USAGE


# slug, статус, оценка Sonnet, причина пропуска, есть ли полный текст
ROWS = [
    ("1-applied-low", "applied", 4, None, True),
    ("2-top", "rejected", 5, None, True),
    ("3-four", "shown", 4, None, True),
    ("4-skipped", "skipped", 4, "ru", True),
    ("5-mid", "rejected", 3, None, True),
    ("6-low", "rejected", 2, None, True),
    ("7-one", "rejected", 1, None, True),
    ("8-stale", "rejected", None, None, True),
    ("9-triage", "rejected", None, None, False),
    ("10-baseline", "baseline", None, None, False),
]


async def make_db(path, rows=ROWS):
    db = Database(path)
    await db.connect()
    for slug, status, score, reason, with_detail in rows:
        card = dict(CARD, slug=slug, vacancy_id=None, title=f"Product Manager {slug}")
        await db.upsert_card(card, FEEDS, "webhook", status="baseline" if status == "baseline" else "queued")
        fields = {"status": status, "score": score, "reason": reason}
        if with_detail:
            fields["detail"] = detail(slug=slug, title=card["title"])
        await db.update(slug, **fields)
    await db.close()
    return path


async def run(tmp_path, step, scorer, **kw):
    db_path = tmp_path / "eval.sqlite3"
    if not db_path.exists():
        await make_db(db_path)
    return await evals.run_eval(step, db_path, scorer, tmp_path / "evals", **kw)


async def test_prefilter_counts_rejections_per_bucket(tmp_path):
    scorer = FakeScorer(reject={"1-applied-low": "офис", "5-mid": "офис", "6-low": "junior", "7-one": "офис",
                                "8-stale": "офис"})
    result = await run(tmp_path, "prefilter", scorer)
    # Только вакансии с полным текстом; отклик идёт в свою корзину, даже если оценка 4.
    assert sorted(s for _, s in scorer.calls) == sorted(
        ["1-applied-low", "2-top", "3-four", "4-skipped", "5-mid", "6-low", "7-one", "8-stale"]
    )
    assert result["summary"]["buckets"] == {
        "applied": {"total": 1, "rejected": 1},
        "4-5": {"total": 3, "rejected": 0},
        "3": {"total": 1, "rejected": 1},
        "1-2": {"total": 2, "rejected": 2},
        "none": {"total": 1, "rejected": 1},
    }
    assert result["summary"]["false_rejections"] == 1


async def test_triage_skips_baseline_and_rule_rejected_cards(tmp_path):
    rows = ROWS + [("11-junior", "rejected", None, None, False)]
    db_path = await make_db(tmp_path / "eval.sqlite3", rows)
    db = Database(db_path)
    await db.connect()
    junior = await db.get("11-junior")
    await db.update("11-junior", card=json.dumps(dict(junior.card, grades=["junior"])))
    await db.close()
    scorer = FakeScorer(reject={"9-triage": "дизайнер"})
    result = await evals.run_eval("triage", db_path, scorer, tmp_path / "evals")
    called = {s for _, s in scorer.calls}
    assert "10-baseline" not in called and "11-junior" not in called
    assert "9-triage" in called and "1-applied-low" in called
    assert result["summary"]["buckets"]["none"] == {"total": 2, "rejected": 1}


async def test_false_rejections_list_applied_and_high_scores_with_model_reason(tmp_path):
    scorer = FakeScorer(reject={"1-applied-low": "офис без релокации", "2-top": "junior", "4-skipped": "гибрид",
                                "5-mid": "офис", "8-stale": "офис"})
    result = await run(tmp_path, "prefilter", scorer)
    false = {f["slug"]: f for f in evals.false_rejections(result["items"])}
    assert set(false) == {"1-applied-low", "2-top", "4-skipped"}
    assert false["1-applied-low"]["verdict"]["reason"] == "офис без релокации"
    assert false["4-skipped"]["label"] == {"score": 4, "decision": "skipped", "skip_reason": "ru"}
    report = evals.render_report(result)
    assert "1-applied-low" in report and "офис без релокации" in report
    assert "5-mid" not in report


async def test_api_error_is_reported_not_counted_as_rejection(tmp_path):
    scorer = FakeScorer(fail={"2-top"})
    result = await run(tmp_path, "prefilter", scorer)
    item = next(i for i in result["items"] if i["slug"] == "2-top")
    assert item["error"] == "RuntimeError: API упал" and item["verdict"] is None
    # Упавшая вакансия не входит ни в «всего», ни в «отсеяно»: о ней говорит счётчик ошибок.
    assert result["summary"]["buckets"]["4-5"] == {"total": 2, "rejected": 0}
    assert result["summary"]["errors"] == 1


async def test_requests_run_with_limited_concurrency(tmp_path):
    scorer = FakeScorer()
    await run(tmp_path, "prefilter", scorer, concurrency=3)
    assert scorer.max_in_flight == 3


async def test_run_is_saved_with_verdicts_prompt_hash_and_git_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(evals, "git_sha", lambda: "abc1234")
    result = await run(tmp_path, "prefilter", FakeScorer(reject={"6-low": "junior"}))
    saved = json.loads(result["path"].read_text(encoding="utf-8"))
    assert result["path"].parent == tmp_path / "evals" and result["path"].name.endswith("-prefilter.json")
    assert saved["git_sha"] == "abc1234"
    assert len(saved["prompt_hash"]) == 12
    low = next(i for i in saved["items"] if i["slug"] == "6-low")
    assert low["verdict"] == {"relevant": False, "reason": "junior"} and low["bucket"] == "1-2"


async def test_prompt_hash_changes_with_prompt_text_and_output_schema(tmp_path, monkeypatch):
    # Порядок полей схемы меняет ответы модели так же, как текст промпта: --compare должен это видеть.
    base = await run(tmp_path, "prefilter", FakeScorer())
    text = await run(tmp_path, "prefilter", FakeScorer(prompt="PREFILTER v2"))
    monkeypatch.setitem(evals.OUTPUT_SCHEMAS, "prefilter", Triage)
    schema = await run(tmp_path, "prefilter", FakeScorer())
    assert len({base["prompt_hash"], text["prompt_hash"], schema["prompt_hash"]}) == 3


async def test_compare_shows_changed_vacancies_and_before_after(tmp_path):
    before = await run(tmp_path, "prefilter", FakeScorer(reject={"2-top": "офис", "6-low": "junior"}))
    # У 6-low другая формулировка причины при том же вердикте: это не изменение.
    after = await run(tmp_path, "prefilter", FakeScorer(reject={"5-mid": "гибрид", "6-low": "стажёр"}, prompt="v2"))
    changes = evals.diff_runs(before, after)
    assert [(c["slug"], c["before"], c["after"]) for c in changes] == [
        ("2-top", "отсеяна: офис", "пропущена"),
        ("5-mid", "пропущена", "отсеяна: гибрид"),
    ]
    text = evals.render_compare(before, after)
    assert "ложных отсевов: 1 → 0" in text
    assert "6-low" not in text


async def test_compare_refuses_runs_of_different_steps(tmp_path):
    before = await run(tmp_path, "prefilter", FakeScorer())
    after = await run(tmp_path, "triage", FakeScorer())
    with pytest.raises(SystemExit, match="prefilter.*triage"):
        evals.render_compare(before, after)


async def test_scoring_matrix_and_mean_diff(tmp_path):
    scores = {"1-applied-low": 5, "2-top": 3, "3-four": 4, "4-skipped": 4, "5-mid": 4, "6-low": 2, "7-one": 1}
    result = await run(tmp_path, "scoring", FakeScorer(scores=scores), limit=30)
    summary = result["summary"]
    # Старые: 4→5, 5→3, 4→4, 4→4, 3→4, 2→2, 1→1. Без оценки (8-stale) не берём.
    assert summary["matrix"] == {"1": {"1": 1}, "2": {"2": 1}, "3": {"4": 1}, "4": {"4": 2, "5": 1}, "5": {"3": 1}}
    assert summary["mean_diff"] == pytest.approx((1 - 2 + 0 + 0 + 1 + 0 + 0) / 7)
    assert summary["same"] == 4
    # Через порог 4: 5→3 и 3→4.
    assert summary["crossed_threshold"] == 2


async def test_scoring_limit_spreads_over_old_scores(tmp_path):
    scorer = FakeScorer(scores={slug: 3 for slug, *_ in ROWS})
    await run(tmp_path, "scoring", scorer, limit=3)
    # По одной на оценку сверху вниз; среди четвёрок первым идёт отклик.
    assert sorted(s for _, s in scorer.calls) == ["1-applied-low", "2-top", "5-mid"]


@pytest.mark.parametrize("args", [["eval", "scoring"], ["eval", "scoring", "--limit", "31"]])
def test_scoring_refused_without_limit_or_above_30(args, monkeypatch, capsys):
    def no_scorer(*a, **kw):
        raise AssertionError("Scorer не должен создаваться")

    monkeypatch.setattr(cli, "Scorer", no_scorer)
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code != 0
    assert "--limit" in capsys.readouterr().err


async def test_cli_compares_two_saved_runs_without_calling_the_model(tmp_path, monkeypatch, capsys):
    before = await run(tmp_path, "prefilter", FakeScorer(reject={"3-four": "офис"}))
    after = await run(tmp_path, "prefilter", FakeScorer())
    monkeypatch.setattr(cli, "Scorer", lambda *a, **kw: pytest.fail("Scorer не должен создаваться"))
    await asyncio.to_thread(cli.main, ["eval", "prefilter", "--compare", str(before["path"]), str(after["path"])])
    out = capsys.readouterr().out
    assert "3-four" in out and "отсеяна: офис → пропущена" in out
    assert "ложных отсевов: 1 → 0" in out


async def test_scoring_rerun_limited_to_vacancies_of_previous_run(tmp_path):
    scorer = FakeScorer(scores={slug: 3 for slug, *_ in ROWS})
    await run(tmp_path, "scoring", scorer, limit=30, only={"6-low", "7-one", "9-triage"})
    # 9-triage без полного текста и оценки — его не берём даже по списку.
    assert sorted(s for _, s in scorer.calls) == ["6-low", "7-one"]


async def test_prefilter_skips_vacancies_rules_reject_but_not_old_ones(tmp_path):
    db_path = await make_db(tmp_path / "eval.sqlite3")
    db = Database(db_path)
    await db.connect()
    ua = await db.get("2-top")
    await db.update("2-top", detail=dict(ua.detail, vacancy_language="ua"))
    old = await db.get("3-four")
    await db.update("3-four", detail=dict(old.detail, created_at="2026-01-01T00:00:00Z"))
    await db.close()
    scorer = FakeScorer()
    await evals.run_eval("prefilter", db_path, scorer, tmp_path / "evals")
    called = {s for _, s in scorer.calls}
    # Украинскую в работе отсекают правила до Haiku; старую оцениваем: проверяем промпт, а не свежесть.
    assert "2-top" not in called and "3-four" in called


async def test_prefilter_sees_vacancy_without_linkedin_research_scoring_with_it(tmp_path):
    # В работе prefilter идёт до LinkedIn-проверки, а Sonnet после неё. В снимке блок уже лежит в detail.
    db_path = await make_db(tmp_path / "eval.sqlite3")
    db = Database(db_path)
    await db.connect()
    v = await db.get("3-four")
    await db.update("3-four", detail=dict(v.detail, linkedin_research={"people": [{"title": "CEO"}]}))
    await db.close()

    seen = {}

    class Recording(FakeScorer):
        async def prefilter(self, detail, feeds):
            seen["prefilter"] = detail
            return await super().prefilter(detail, feeds)

        async def assess(self, detail, feeds):
            seen["scoring"] = detail
            return await super().assess(detail, feeds)

    await evals.run_eval("prefilter", db_path, Recording(), tmp_path / "evals", only={"3-four"})
    await evals.run_eval("scoring", db_path, Recording(scores={"3-four": 4}), tmp_path / "evals", limit=1, only={"3-four"})
    assert "linkedin_research" not in seen["prefilter"]
    assert seen["scoring"]["linkedin_research"] == {"people": [{"title": "CEO"}]}


async def test_cli_refuses_compare_with_run_of_another_step_before_calling_model(tmp_path, monkeypatch, capsys):
    triage_run = await run(tmp_path, "triage", FakeScorer())
    monkeypatch.setattr(cli, "Scorer", lambda *a, **kw: pytest.fail("Scorer не должен создаваться"))
    for argv in (
        ["eval", "prefilter", "--db", str(tmp_path / "eval.sqlite3"), "--compare", str(triage_run["path"])],
        ["eval", "prefilter", "--compare", str(triage_run["path"]), str(triage_run["path"])],
    ):
        with pytest.raises(SystemExit) as exc:
            await asyncio.to_thread(cli.main, argv)
        assert exc.value.code != 0
        assert "triage" in str(exc.value) + capsys.readouterr().err


def test_cli_refuses_zero_concurrency(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["eval", "prefilter", "--concurrency", "0"])
    assert exc.value.code != 0
    assert "--concurrency" in capsys.readouterr().err


async def test_snapshot_is_opened_read_only(tmp_path):
    db_path = await make_db(tmp_path / "eval.sqlite3")
    with sqlite3.connect(db_path) as con:
        con.execute("PRAGMA journal_mode=DELETE")
    before = db_path.read_bytes()
    vacancies = await evals.load_vacancies(db_path)
    assert len(vacancies) == len(ROWS)
    # Database.connect перевёл бы снимок в WAL и прогнал схему: файл бы изменился.
    assert db_path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["eval.sqlite3"]


@pytest.mark.parametrize(
    "extra,backend,client_kind",
    [([], "claude-code", "claude-code"), (["--backend", "api"], "api", "api")],
)
async def test_cli_eval_backend_defaults_to_claude_code(tmp_path, monkeypatch, capsys, extra, backend, client_kind):
    # Локальные прогоны по умолчанию идут через `claude -p` по подписке; API — только явно.
    db_path = await make_db(tmp_path / "eval.sqlite3")
    monkeypatch.setattr(evals, "EVALS_DIR", tmp_path / "evals")
    made = {}

    class FakeClaudeCode:
        stopped = None

        def __init__(self, *a, **kw):
            made["client"] = "claude-code"

    class FakeAnthropic:
        def __init__(self, *a, **kw):
            made["client"] = "api"

    monkeypatch.setattr(cli, "ClaudeCodeClient", FakeClaudeCode)
    monkeypatch.setattr(cli.anthropic, "AsyncAnthropic", FakeAnthropic)
    monkeypatch.setattr(cli, "Scorer", lambda *a, client=None, **kw: FakeScorer())
    await asyncio.to_thread(cli.main, ["eval", "prefilter", "--db", str(db_path), *extra])
    assert made["client"] == client_kind
    [saved] = list((tmp_path / "evals").glob("*.json"))
    assert json.loads(saved.read_text(encoding="utf-8"))["backend"] == backend
    assert f"бэкенд {backend}" in capsys.readouterr().out


async def test_cli_eval_warns_when_subscription_limit_stopped_the_run(tmp_path, monkeypatch, capsys):
    db_path = await make_db(tmp_path / "eval.sqlite3")
    monkeypatch.setattr(evals, "EVALS_DIR", tmp_path / "evals")

    class StoppedClaudeCode:
        stopped = "You've hit your limit"

        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr(cli, "ClaudeCodeClient", StoppedClaudeCode)
    monkeypatch.setattr(cli, "Scorer", lambda *a, client=None, **kw: FakeScorer())
    await asyncio.to_thread(cli.main, ["eval", "prefilter", "--db", str(db_path)])
    out = capsys.readouterr().out
    assert "Прогон неполный" in out and "hit your limit" in out
