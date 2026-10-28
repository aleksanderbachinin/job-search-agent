"""Проверяет запросы Scorer к Claude API без сети: подменяем HTTP-транспорт SDK."""

import json

import anthropic
import httpx2

from jobagent.config import AGENT_DIR
from jobagent.scoring import Scorer
from test_service import CARD, assessment, detail


def make_client(captured: list, payload: dict, model: str):
    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 7,
                },
            },
        )

    return anthropic.AsyncAnthropic(
        api_key="test", http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    )


async def test_triage_request_and_parse():
    captured = []
    client = make_client(captured, {"relevant": False, "reason": "дизайнер"}, "claude-haiku-5-5")
    scorer = Scorer(AGENT_DIR.parent, "claude-haiku-5-5", "claude-sonnet-5-5", client=client)
    triage, usage = await scorer.triage(CARD)
    assert triage.relevant is False and usage.cache_read_tokens == 3 and usage.cache_write_tokens == 7
    body = json.loads(captured[0].content)
    assert body["model"] == "claude-haiku-5-5"
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "<card>" in body["messages"][0]["content"]


async def test_assess_request_uses_cache_fallbacks_and_knowledge():
    captured = []
    client = make_client(captured, assessment(score=9).model_dump(), "claude-sonnet-5-5")
    scorer = Scorer(AGENT_DIR.parent, "claude-haiku-5-5", "claude-sonnet-5-5", "medium", client=client)
    result, _ = await scorer.assess(detail(), [{"id": 1, "name": "PM remote"}])
    assert result.score == 5  # зажато в 1..5
    req = captured[0]
    body = json.loads(req.content)
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in req.headers["anthropic-beta"]
    assert body["output_config"]["effort"] == "medium"
    schema = body["output_config"]["format"]["schema"]
    assert "ru_label" in schema["properties"]
    system = body["system"][0]
    assert system["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "<search_criteria>" in system["text"] and "<resume>" in system["text"]
    assert "<applied_examples>" in system["text"]
    assert "Remoteria" in body["messages"][0]["content"]


async def test_prefilter_request_uses_haiku_criteria_and_long_cache():
    captured = []
    client = make_client(captured, {"relevant": False, "reason": "гибрид в Москве"}, "claude-haiku-5-5")
    scorer = Scorer(AGENT_DIR.parent, "claude-haiku-5-5", "claude-sonnet-5-5", client=client)
    result, usage = await scorer.prefilter(detail(), [{"id": 1, "name": "PM remote"}])
    assert result.relevant is False and usage.cache_write_tokens == 7
    body = json.loads(captured[0].content)
    assert body["model"] == "claude-haiku-5-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    system = body["system"][0]
    assert system["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # Стоп-факторы берутся из критериев; резюме Haiku не нужно.
    assert "<search_criteria>" in system["text"] and "Стоп-факторы" in system["text"]
    assert "<resume>" not in system["text"]
    content = body["messages"][0]["content"]
    assert content.startswith("<vacancy>") and "Remoteria" in content


async def test_prefilter_lets_vacancy_through_on_refusal():
    def handler(request):
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [],
                "stop_reason": "refusal",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        )

    client = anthropic.AsyncAnthropic(
        api_key="test", http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    )
    scorer = Scorer(AGENT_DIR.parent, "claude-haiku-5-5", "claude-sonnet-5-5", client=client)
    result, _ = await scorer.prefilter(detail(), [])
    assert result.relevant is True
