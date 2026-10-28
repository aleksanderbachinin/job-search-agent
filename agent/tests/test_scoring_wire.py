"""Проверяет запросы Scorer к Claude API без сети: подменяем HTTP-транспорт SDK."""

import json

import anthropic
import httpx2
import pytest

from jobagent.scoring import MASTER_RESUME, Scorer
from test_service import CARD, assessment, detail

TRIAGE_MODEL = "test-triage-model"
SCORING_MODEL = "test-scoring-model"
CRITERIA_MARKER = "МАРКЕР-КРИТЕРИЕВ-7731"
RESUME_MARKER = "МАРКЕР-РЕЗЮМЕ-4402"
EXAMPLES_MARKER = "МАРКЕР-ПРИМЕРОВ-9158"
PROFILE_MARKER = "МАРКЕР-ПРОФИЛЯ-5520"
CRITERIA = f"""# Критерии

## Критерии кандидата

Кандидат {PROFILE_MARKER}

## Стоп-факторы

Стоп-факторы {CRITERIA_MARKER}
"""


@pytest.fixture
def knowledge(tmp_path):
    """Своя папка знаний с маркерами: по ним видно, какой файл в какой prompt попал."""
    (tmp_path / "search_criteria.md").write_text(CRITERIA, encoding="utf-8")
    (tmp_path / "applied_examples.md").write_text(f"примеры {EXAMPLES_MARKER}", encoding="utf-8")
    (tmp_path / "experience").mkdir()
    (tmp_path / "experience" / MASTER_RESUME).write_text(f"опыт {RESUME_MARKER}", encoding="utf-8")
    return tmp_path


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


async def test_triage_request_and_parse(knowledge):
    captured = []
    client = make_client(captured, {"relevant": False, "reason": "дизайнер"}, TRIAGE_MODEL)
    scorer = Scorer(knowledge, TRIAGE_MODEL, SCORING_MODEL, client=client)
    triage, usage = await scorer.triage(CARD)
    assert triage.relevant is False and usage.cache_read_tokens == 3 and usage.cache_write_tokens == 7
    body = json.loads(captured[0].content)
    assert body["model"] == TRIAGE_MODEL
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "<card>" in body["messages"][0]["content"]


async def test_every_step_sees_candidate_profile_once_from_criteria_file(knowledge):
    # Профиль кандидата правится в одном месте, search_criteria.md, и доходит до всех трёх шагов.
    captured = []
    client = make_client(captured, {"relevant": True, "reason": "PM"}, TRIAGE_MODEL)
    scorer = Scorer(knowledge, TRIAGE_MODEL, SCORING_MODEL, client=client)
    await scorer.triage(CARD)
    triage_system = json.dumps(json.loads(captured[0].content)["system"], ensure_ascii=False)
    # По карточке стоп-факторы не проверить: Haiku видит только профиль.
    assert triage_system.count(PROFILE_MARKER) == 1 and CRITERIA_MARKER not in triage_system
    for blocks in (scorer.prefilter_system, scorer.scoring_system):
        assert "".join(b["text"] for b in blocks).count(PROFILE_MARKER) == 1


def test_scorer_refuses_criteria_without_candidate_section(knowledge):
    (knowledge / "search_criteria.md").write_text("## Стоп-факторы\nофис", encoding="utf-8")
    with pytest.raises(ValueError, match="Критерии кандидата"):
        Scorer(knowledge, TRIAGE_MODEL, SCORING_MODEL, client=make_client([], {}, TRIAGE_MODEL))


async def test_assess_request_uses_cache_fallbacks_and_knowledge(knowledge):
    captured = []
    client = make_client(captured, assessment(score=9).model_dump(), SCORING_MODEL)
    scorer = Scorer(knowledge, TRIAGE_MODEL, SCORING_MODEL, "high", client=client)
    result, _ = await scorer.assess(detail(), [{"id": 1, "name": "PM remote"}])
    assert result.score == 5  # зажато в 1..5
    req = captured[0]
    body = json.loads(req.content)
    assert body["model"] == SCORING_MODEL
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in req.headers["anthropic-beta"]
    assert body["output_config"]["effort"] == "high"
    schema = body["output_config"]["format"]["schema"]
    assert "ru_label" in schema["properties"]
    system = body["system"][0]
    assert system["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # Sonnet видит критерии, опыт и примеры откликов.
    assert all(m in system["text"] for m in (CRITERIA_MARKER, RESUME_MARKER, EXAMPLES_MARKER))
    assert "Remoteria" in body["messages"][0]["content"]


async def test_prefilter_request_uses_triage_model_criteria_and_long_cache(knowledge):
    captured = []
    client = make_client(captured, {"reason": "гибрид в Москве", "condition": 1}, TRIAGE_MODEL)
    scorer = Scorer(knowledge, TRIAGE_MODEL, SCORING_MODEL, client=client)
    result, usage = await scorer.prefilter(detail(), [{"id": 1, "name": "PM remote"}])
    assert result.relevant is False and usage.cache_write_tokens == 7
    body = json.loads(captured[0].content)
    assert body["model"] == TRIAGE_MODEL
    assert body["output_config"]["format"]["type"] == "json_schema"
    system = body["system"][0]
    assert system["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # Стоп-факторы Haiku берёт из критериев; резюме и примеры ему не нужны (лишние токены в кеше).
    assert CRITERIA_MARKER in system["text"]
    assert RESUME_MARKER not in system["text"] and EXAMPLES_MARKER not in system["text"]
    content = body["messages"][0]["content"]
    assert content.startswith("<vacancy>") and "Remoteria" in content


async def test_prefilter_lets_vacancy_through_on_refusal(knowledge):
    def handler(request):
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": TRIAGE_MODEL,
                "content": [],
                "stop_reason": "refusal",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        )

    client = anthropic.AsyncAnthropic(
        api_key="test", http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    )
    scorer = Scorer(knowledge, TRIAGE_MODEL, SCORING_MODEL, client=client)
    result, _ = await scorer.prefilter(detail(), [])
    assert result.relevant is True
