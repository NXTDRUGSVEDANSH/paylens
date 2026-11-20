"""Parser tests for the LLM transport layer. No network calls."""
import pytest

from backend.llm.provider import CallBudget, LLMUnavailable, extract_json, extract_sql, strip_reasoning


def test_extract_json_from_fence():
    assert extract_json('noise ```json\n{"a": {"b": [1, 2]}, "c": 3}\n``` tail') == {
        "a": {"b": [1, 2]}, "c": 3}


def test_extract_json_nested_unfenced():
    """Fractal AI's regex r'\\{[^{}]*\\}' cannot match this. Brace matching can."""
    assert extract_json('bare {"plan": {"nested": {"deep": true}}, "x": 1} end') == {
        "plan": {"nested": {"deep": True}}, "x": 1}


def test_extract_json_skips_malformed_then_finds_valid():
    assert extract_json('{not json} then {"ok": 1}') == {"ok": 1}


def test_extract_json_handles_braces_inside_strings():
    assert extract_json('{"sql": "select \'{\' as x", "n": 2}') == {"sql": "select '{' as x", "n": 2}


def test_extract_json_raises_when_absent():
    with pytest.raises(ValueError):
        extract_json("no object here")


def test_extract_sql_from_fence():
    assert extract_sql("sure:\n```sql\nSELECT 1 FROM t;\n```") == "SELECT 1 FROM t"


def test_extract_sql_bare_with_cte():
    assert extract_sql("here you go WITH a AS (SELECT 1) SELECT * FROM a").startswith("WITH")


def test_strip_reasoning_closed_block():
    thinking, cleaned = strip_reasoning("<think>hmm</think>ANSWER")
    assert thinking == "hmm" and cleaned == "ANSWER"


def test_strip_reasoning_truncated_block_yields_empty_content():
    """
    A never-closed <think> means the model ran out of tokens mid-thought. Returning
    empty content makes the caller treat it as a failure instead of parsing half a
    thought as SQL.
    """
    _, cleaned = strip_reasoning("<think>never closed")
    assert cleaned == ""


def test_call_budget_enforced():
    b = CallBudget()
    b.spend("plan")
    with pytest.raises(LLMUnavailable, match="budget exhausted"):
        b.spend("plan")
    assert b.total == 1


def test_call_budget_allows_configured_heal_retries():
    b = CallBudget()
    b.spend("heal")
    b.spend("heal")
    assert b.used["heal"] == 2
