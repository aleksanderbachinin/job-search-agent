import anthropic
import httpx
import pytest

from jobagent.llm import OPENROUTER_BASE_URL, canonical_model, make_client, openrouter_model, out_of_credits
from jobagent.pricing import usage_cost


@pytest.mark.parametrize(
    "name, expected",
    [
        ("claude-sonnet-5-5", "anthropic/claude-sonnet-5.5"),
        ("claude-haiku-5-5", "anthropic/claude-haiku-5.5"),
        ("claude-opus-5", "anthropic/claude-opus-5"),
        ("anthropic/claude-opus-5.5", "anthropic/claude-opus-5.5"),
        ("some-other-model", "some-other-model"),
    ],
)
def test_openrouter_model(name, expected):
    assert openrouter_model(name) == expected


def test_canonical_model_round_trip():
    assert canonical_model(openrouter_model("claude-sonnet-5-5")) == "claude-sonnet-5-5"


def test_usage_cost_prices_openrouter_model_names():
    u = {"model": "anthropic/claude-sonnet-5.5", "input_tokens": 1_000_000, "output_tokens": 0}
    assert usage_cost(u) == pytest.approx(2.0)


def test_make_client_openrouter_does_not_leak_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    client = make_client("sk-or-test")
    assert str(client.base_url).rstrip("/") == OPENROUTER_BASE_URL
    assert "sk-ant-secret" not in str(client.auth_headers)
    assert "sk-or-test" in str(client.auth_headers)


def test_make_client_without_openrouter_uses_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    client = make_client("")
    assert "openrouter" not in str(client.base_url)


def _status_error(code: int, msg: str) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/messages")
    return anthropic.APIStatusError(msg, response=httpx.Response(code, request=request), body=None)


def test_out_of_credits_openrouter_402():
    assert out_of_credits(_status_error(402, "Insufficient credits. Add more using https://openrouter.ai/settings/credits"))


def test_out_of_credits_ignores_other_errors():
    assert not out_of_credits(_status_error(400, "prompt is too long"))


def test_settings_map_models_for_openrouter(monkeypatch):
    from jobagent.config import Settings

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    for name in ("TRIAGE_MODEL", "SCORING_MODEL", "PACKAGE_MODEL", "PACKAGE_CHECK_MODEL"):
        monkeypatch.delenv(name, raising=False)
    s = Settings.from_env()
    assert s.openrouter_api_key == "sk-or-test"
    assert s.triage_model == "anthropic/claude-haiku-5.5"
    assert s.scoring_model == "anthropic/claude-sonnet-5.5"
    assert s.package_model == "anthropic/claude-opus-5.5"
    assert s.package_check_model == "anthropic/claude-haiku-5.5"


def test_settings_keep_anthropic_models_without_openrouter(monkeypatch):
    from jobagent.config import Settings

    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.delenv("SCORING_MODEL", raising=False)
    s = Settings.from_env()
    assert s.scoring_model == "claude-sonnet-5-5"


def test_usage_cost_free_openrouter_model_is_zero():
    u = {"model": "particle-ai/particle-1:free", "input_tokens": 50_000, "output_tokens": 3_000}
    assert usage_cost(u) == 0.0
