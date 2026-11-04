"""Бэкенд eval через `claude -p`: вместо настоящего CLI — скрипт, который пишет, что получил."""

import json
import stat
import sys

import pytest

from jobagent.claude_code import ClaudeCodeClient, ClaudeCodeError, ClaudeCodeLimitError, parse_result
from jobagent.scoring import Scorer, Triage
from test_scoring_wire import CRITERIA_MARKER, RESUME_MARKER, SCORING_MODEL, TRIAGE_MODEL, knowledge  # noqa: F401
from test_service import CARD, assessment, detail

FAKE = """import json, os, sys
log = {{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "env_key": os.environ.get("ANTHROPIC_API_KEY")}}
with open({log!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps(log, ensure_ascii=False) + "\\n")
sys.stdout.write(open({reply!r}, encoding="utf-8").read())
"""


def result(structured=None, **over):
    data = {
        "type": "result",
        "is_error": False,
        "stop_reason": "end_turn",
        "structured_output": structured,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 7,
        },
        "modelUsage": {"claude-real-model": {}},
    }
    data.update(over)
    return data


@pytest.fixture
def fake_claude(tmp_path):
    """Возвращает (client, calls, set_reply): calls() — список вызовов CLI с argv, stdin и ключом из env."""
    log, reply = tmp_path / "calls.jsonl", tmp_path / "reply.json"
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE.format(log=str(log), reply=str(reply)), encoding="utf-8")
    # Обёртка на sh: в пути к Python есть пробелы («Agent Job Search»), shebang их не переносит.
    binary = tmp_path / "claude"
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)

    def set_reply(data):
        reply.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def calls():
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    return ClaudeCodeClient(binary=str(binary)), calls, set_reply


def flag(argv, name):
    return argv[argv.index(name) + 1]


async def test_triage_goes_through_cli_without_api_key(knowledge, fake_claude, monkeypatch):  # noqa: F811
    # Ключ API из agent/.env не должен дойти до `claude -p`: иначе прогон спишет кредиты API.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    client, calls, set_reply = fake_claude
    set_reply(result({"reason": "дизайнер", "relevant": False}))
    scorer = Scorer(knowledge, TRIAGE_MODEL, SCORING_MODEL, client=client)
    triage, usage = await scorer.triage(CARD)
    assert triage == Triage(reason="дизайнер", relevant=False)
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (10, 3, 7)
    [call] = calls()
    assert call["env_key"] is None
    argv = call["argv"]
    assert argv[0] == "-p" and flag(argv, "--model") == TRIAGE_MODEL
    assert flag(argv, "--system-prompt") == scorer.triage_system
    assert flag(argv, "--effort") == "low" and flag(argv, "--output-format") == "json"
    assert flag(argv, "--tools") == "" and flag(argv, "--setting-sources") == ""
    assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv
    assert "relevant" in json.loads(flag(argv, "--json-schema"))["properties"]
    assert "<card>" in call["stdin"]


async def test_assess_passes_full_system_and_vacancy(knowledge, fake_claude):  # noqa: F811
    client, calls, set_reply = fake_claude
    set_reply(result(assessment(score=9).model_dump(mode="json")))
    scorer = Scorer(knowledge, TRIAGE_MODEL, SCORING_MODEL, "high", client=client)
    a, usage = await scorer.assess(detail(), [{"id": 1, "name": "PM remote"}])
    assert a.score == 5  # те же валидаторы Assessment, что и через API
    assert usage.model == "claude-real-model"
    [call] = calls()
    system = flag(call["argv"], "--system-prompt")
    assert CRITERIA_MARKER in system and RESUME_MARKER in system
    assert flag(call["argv"], "--effort") == "high"
    assert "ru_label" in json.loads(flag(call["argv"], "--json-schema"))["properties"]
    assert "Remoteria" in call["stdin"]


async def test_limit_error_stops_the_rest_of_the_run(fake_claude):
    client, calls, set_reply = fake_claude
    set_reply(result(is_error=True, result="Failed to authenticate: OAuth session expired"))
    with pytest.raises(ClaudeCodeLimitError, match="claude auth login"):
        await client.messages.parse(
            model="m", system="s", messages=[{"role": "user", "content": "x"}], output_format=Triage
        )
    with pytest.raises(ClaudeCodeLimitError):
        await client.messages.parse(
            model="m", system="s", messages=[{"role": "user", "content": "x"}], output_format=Triage
        )
    assert len(calls()) == 1  # второй запрос CLI уже не запускал


def test_parse_result_errors():
    with pytest.raises(ClaudeCodeError, match="не JSON"):
        parse_result("oops", "m", Triage)
    with pytest.raises(ClaudeCodeLimitError):
        parse_result(json.dumps(result(is_error=True, result="You've hit your limit · resets 5pm")), "m", Triage)
    err = None
    try:
        parse_result(json.dumps(result(is_error=True, result="Overloaded")), "m", Triage)
    except ClaudeCodeError as e:
        err = e
    assert err is not None and not isinstance(err, ClaudeCodeLimitError)


def test_parse_result_without_structured_output_is_not_parsed():
    resp = parse_result(json.dumps(result(None, stop_reason="max_tokens")), "m", Triage)
    assert resp.parsed_output is None and resp.stop_reason == "max_tokens"


def test_missing_binary(monkeypatch):
    monkeypatch.setattr("jobagent.claude_code.shutil.which", lambda _: None)
    with pytest.raises(ClaudeCodeError, match="Не найден"):
        ClaudeCodeClient()
