from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.core.config import settings
from backend.llm import cache as llm_cache
from backend.llm.client import extract_json, prepare_messages
from backend.llm.usage import LLMCallRecord, parse_usage, record, start_collection, stop_collection


def _msgs(n_cached: int) -> list[dict]:
    msgs = [{"role": "system", "content": f"block {i}", "cache": True} for i in range(n_cached)]
    return [*msgs, {"role": "user", "content": "question"}]


def test_cache_markers_for_anthropic_models() -> None:
    out = prepare_messages(_msgs(2), "anthropic/claude-haiku-5.5")
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert out[-1]["content"] == "question"
    assert all("cache" not in m for m in out)


def test_no_markers_for_openai_models() -> None:
    out = prepare_messages(_msgs(2), "openai/gpt-5.4-mini")
    assert all(isinstance(m["content"], str) for m in out)


def test_at_most_four_breakpoints_keep_the_last() -> None:
    out = prepare_messages(_msgs(6), "anthropic/claude-sonnet-5.5")
    marked = [i for i, m in enumerate(out) if isinstance(m["content"], list)]
    assert marked == [2, 3, 4, 5]


def test_extract_json_strips_fences_and_prose() -> None:
    assert extract_json('Here you go:\n```json\n{"a": 1}\n```') == '{"a": 1}'
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_parse_usage_reads_cache_and_cost() -> None:
    usage = SimpleNamespace(
        model_dump=lambda: {
            "prompt_tokens": 3000,
            "completion_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 2600, "cache_write_tokens": 0},
            "cost": 0.0042,
        }
    )
    assert parse_usage(usage) == {
        "prompt_tokens": 3000,
        "completion_tokens": 120,
        "cached_tokens": 2600,
        "cache_write_tokens": 0,
        "cost_usd": 0.0042,
    }
    assert parse_usage(None) == {}


def test_usage_collector_scoped_to_context() -> None:
    record(LLMCallRecord(role="fast", name="outside", model="m"))  # no collector: ignored
    collector, token = start_collection()
    try:
        record(
            LLMCallRecord(role="fast", name="a", model="m", prompt_tokens=10, cached_tokens=8, cost_usd=0.1)
        )
        record(LLMCallRecord(role="code", name="b", model="m", prompt_tokens=5, completion_tokens=3))
    finally:
        stop_collection(token)
    assert [c.name for c in collector.calls] == ["a", "b"]
    assert collector.totals["prompt_tokens"] == 15
    assert collector.totals["cached_tokens"] == 8
    assert collector.totals["calls"] == 2


async def test_exact_cache_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_exact_cache_enabled", True)
    key = llm_cache.cache_key({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert key == llm_cache.cache_key({"messages": [{"role": "user", "content": "hi"}], "model": "m"})
    assert await llm_cache.get(key) is None
    await llm_cache.put(key, {"text": "hello"})
    assert await llm_cache.get(key) == {"text": "hello"}


async def test_exact_cache_disabled_is_noop() -> None:
    key = llm_cache.cache_key({"x": 1})
    await llm_cache.put(key, {"text": "hello"})
    assert await llm_cache.get(key) is None
