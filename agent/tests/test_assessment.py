"""Нормализация Assessment в самой модели: счёт, длина списков, пустые строки, лимиты слов."""

import json
import logging

import anthropic
import httpx2
import pytest
from anthropic import transform_schema

from jobagent.config import AGENT_DIR
from jobagent.scoring import Assessment, PrefilterAnswer, Scorer, Triage
from test_service import assessment, detail


def make(**overrides) -> Assessment:
    return Assessment.model_validate({**assessment().model_dump(), **overrides})


def words(n: int) -> str:
    return " ".join(f"слово{i}" for i in range(n))


@pytest.mark.parametrize("raw, expected", [(9, 5), (5, 5), (3, 3), (1, 1), (0, 1), (-2, 1)])
def test_score_is_clamped_to_1_5(raw, expected):
    assert make(score=raw).score == expected


def test_pros_keep_first_two():
    assert make(pros=["a", "b", "c", "d"]).pros == ["a", "b"]


def test_pros_are_stripped_and_empty_dropped_before_cut():
    # Пустая строка не должна занимать одно из двух мест.
    assert make(pros=["", "  a  ", "   ", "b\n", "c"]).pros == ["a", "b"]


def test_pros_may_be_shorter_than_two():
    assert make(pros=["a"]).pros == ["a"]
    assert make(pros=[" ", ""]).pros == []


def test_questions_keep_first_three_without_empty():
    assert make(questions=["вилка", "", " команда ", "стек", "релокация"]).questions == ["вилка", "команда", "стек"]


@pytest.mark.parametrize("field", ["must_haves", "missing_must_haves", "red_flags"])
def test_lists_drop_empty_and_exact_duplicates_keeping_order(field):
    got = getattr(make(**{field: ["B", "", "A", "B", "  ", "A ", "C"]}), field)
    assert got == ["B", "A", "C"]


def test_exact_duplicates_only_not_similar_strings():
    assert make(red_flags=["Нет вилки", "нет вилки"]).red_flags == ["Нет вилки", "нет вилки"]


def test_must_haves_are_not_capped():
    items = [f"требование {i}" for i in range(12)]
    assert make(must_haves=items).must_haves == items


def test_empty_lists_stay_empty():
    a = make(must_haves=[], missing_must_haves=[], red_flags=[], pros=[], questions=[])
    assert a.must_haves == a.missing_must_haves == a.red_flags == a.pros == a.questions == []


def test_long_text_is_kept_whole_and_logged(caplog):
    long = {
        "verdict": words(16),
        "con": words(21),
        "ru_summary": words(9),
        "ru_linkedin": words(21),
    }
    with caplog.at_level(logging.WARNING, logger="jobagent.scoring"):
        a = make(**long, pros=[words(16), words(15)])
    for field, text in long.items():
        assert getattr(a, field) == text  # не обрезаем
    assert a.pros[0] == words(16)
    messages = [r.getMessage() for r in caplog.records if r.name == "jobagent.scoring"]
    assert any("verdict" in m and "16" in m and "15" in m for m in messages)
    assert any("con" in m and "21" in m and "20" in m for m in messages)
    assert any("ru_summary" in m and "9" in m and "8" in m for m in messages)
    assert any("ru_linkedin" in m and "21" in m and "20" in m for m in messages)
    assert any("pros[0]" in m and "16" in m for m in messages)
    assert not any("pros[1]" in m for m in messages)
    assert len(messages) == 5


def test_text_within_limits_is_not_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="jobagent.scoring"):
        make(verdict=words(15), con=words(20), ru_summary=words(8), ru_linkedin=words(20), pros=[words(15), "a"])
    assert not caplog.records


def test_only_pros_that_survive_are_checked(caplog):
    # Третий пункт отброшен, его длина уже не важна.
    with caplog.at_level(logging.WARNING, logger="jobagent.scoring"):
        make(pros=["a", "b", words(40)])
    assert not caplog.records


def test_api_schema_is_not_changed_by_validators():
    """Валидаторы не должны попасть в схему для API: ни ограничений, ни пометок в description."""
    schema = transform_schema(Assessment)
    props = schema["properties"]
    assert set(props) == set(Assessment.model_fields) and set(schema["required"]) == set(props)
    assert props["score"]["type"] == "integer"
    for name in ("pros", "questions", "must_haves", "missing_must_haves", "red_flags"):
        assert props[name]["type"] == "array" and props[name]["items"] == {"type": "string"}
    forbidden = {"minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength", "uniqueItems", "pattern"}

    def walk(node):
        if isinstance(node, dict):
            assert not forbidden & set(node)
            # SDK переносит неподдерживаемые ограничения в description как «{minimum: 1}».
            assert "{" not in node.get("description", "")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)


def client_returning(payload: dict) -> anthropic.AsyncAnthropic:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    return anthropic.AsyncAnthropic(
        api_key="test", http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    )


async def test_assess_returns_normalized_assessment_from_raw_model_json():
    raw = {
        **assessment().model_dump(),
        "score": 9,
        "pros": ["a", "", "b", "c"],
        "questions": ["q1", "q2", "q3", "q4"],
        "red_flags": ["x", "x", ""],
    }
    scorer = Scorer(AGENT_DIR.parent, "claude-haiku-5-5", "claude-sonnet-5-5", client=client_returning(raw))
    result, _ = await scorer.assess(detail(), [])
    assert result.score == 5
    assert result.pros == ["a", "b"] and result.questions == ["q1", "q2", "q3"] and result.red_flags == ["x"]
    # В базу и в карточку уходит уже нормализованное.
    stored = result.model_dump()
    assert stored["score"] == 5 and stored["pros"] == ["a", "b"]


def test_cards_still_cut_legacy_rows_from_db():
    """Карточки строятся из сохранённого dict, а не через модель: старые строки с тремя pros
    модель не нормализовала, поэтому срез в cards.py остаётся."""
    from jobagent.cards import render_card

    a = assessment().model_dump()
    a["pros"] = ["раз", "два", "три"]
    a["questions"] = ["q1", "q2", "q3", "q4"]
    text = render_card("t", a, [], detail())
    assert "➕ раз" in text and "➕ два" in text and "три" not in text
    assert "q1, q2, q3" in text and "q4" not in text


def test_haiku_writes_reason_before_verdict():
    # Модель пишет поля по порядку схемы. Вердикт до причины давал «relevant=false, пропускаю дальше».
    assert list(transform_schema(Triage)["properties"]) == ["reason", "relevant"]


@pytest.mark.parametrize(
    ("condition", "relevant", "reason"),
    [
        (0, True, "формат не указан, Senior PM"),
        (1, False, "гибрид в Москве, релокации нет (условие 1)"),
        (4, False, "Backend-разработчик (условие 4)"),
    ],
)
async def test_prefilter_decision_follows_condition_number(condition, relevant, reason):
    # Haiku называет выполненное условие, а «хотя бы одно из четырёх» считает код.
    raw = {"reason": reason.split(" (условие")[0], "condition": condition}
    scorer = Scorer(AGENT_DIR.parent, "claude-haiku-5-5", "claude-sonnet-5-5", client=client_returning(raw))
    result, _ = await scorer.prefilter(detail(), [])
    assert result.relevant is relevant
    assert result.reason == reason


def test_prefilter_answer_lists_facts_before_condition():
    assert list(transform_schema(PrefilterAnswer)["properties"]) == ["reason", "condition"]
