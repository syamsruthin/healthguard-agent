"""The real OpenRouter client against a mocked HTTP transport (no network, no cost)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel

from backend.core.config import settings
from backend.llm.client import LLMError, OpenRouterClient
from backend.llm.registry import Role
from backend.llm.usage import start_collection, stop_collection

USAGE = {
    "prompt_tokens": 3000,
    "completion_tokens": 40,
    "prompt_tokens_details": {"cached_tokens": 2600},
    "cost": 0.0012,
}


def _completion(content: str, model: str = "anthropic/claude-haiku-5.5") -> dict[str, Any]:
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
        "usage": USAGE,
    }


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Callable[[httpx.Request], httpx.Response]], tuple[OpenRouterClient, list[dict[str, Any]]]]:
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")

    def make(
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> tuple[OpenRouterClient, list[dict[str, Any]]]:
        sent: list[dict[str, Any]] = []

        def recording(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return handler(request)

        c = OpenRouterClient()
        c._client = AsyncOpenAI(
            api_key="test-key",
            base_url=settings.llm_base_url,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(recording)),
        )
        return c, sent

    return make


MESSAGES = [{"role": "system", "content": "stable prefix", "cache": True}, {"role": "user", "content": "hi"}]


async def test_complete_sends_fallbacks_cache_markers_and_records_usage(client) -> None:
    c, sent = client(lambda r: httpx.Response(200, json=_completion("hello")))
    collector, token = start_collection()
    try:
        result = await c.complete(Role.FAST, MESSAGES, name="understand", use_cache=False)
    finally:
        stop_collection(token)
    assert result.text == "hello"
    body = sent[0]
    assert body["model"] == settings.model_fast
    assert body["models"][0] == settings.model_fast and len(body["models"]) > 1  # fallback routing
    assert body["usage"] == {"include": True}
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    call = collector.calls[0]
    assert (call.name, call.prompt_tokens, call.cached_tokens, call.cost_usd) == (
        "understand",
        3000,
        2600,
        0.0012,
    )


class _Out(BaseModel):
    answer: str


async def test_structured_repairs_invalid_json_once(client) -> None:
    replies = iter([_completion("not json at all"), _completion('{"answer": "ok"}')])
    c, sent = client(lambda r: httpx.Response(200, json=next(replies)))
    collector, token = start_collection()
    try:
        out = await c.structured(Role.FAST, MESSAGES, _Out, name="verify", use_cache=False)
    finally:
        stop_collection(token)
    assert out.answer == "ok"
    assert [x.name for x in collector.calls] == ["verify", "verify.repair"]
    assert sent[0]["response_format"]["type"] == "json_schema"


async def test_structured_falls_back_when_response_format_rejected(client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "response_format" in json.loads(request.content):
            return httpx.Response(400, json={"error": {"message": "response_format not supported"}})
        return httpx.Response(200, json=_completion('```json\n{"answer": "plain"}\n```'))

    c, sent = client(handler)
    out = await c.structured(Role.FAST, MESSAGES, _Out, name="understand", use_cache=False)
    assert out.answer == "plain" and len(sent) == 2 and "response_format" not in sent[1]


async def test_upstream_error_becomes_llm_error_and_is_recorded(client) -> None:
    c, _ = client(lambda r: httpx.Response(503, json={"error": {"message": "overloaded"}}))
    collector, token = start_collection()
    try:
        with pytest.raises(LLMError):
            await c.complete(Role.CODE, MESSAGES, name="sql", use_cache=False)
    finally:
        stop_collection(token)
    assert collector.calls[0].error


async def test_exact_cache_serves_repeat_deterministic_calls(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_exact_cache_enabled", True)
    c, sent = client(lambda r: httpx.Response(200, json=_completion("cached text")))
    first = await c.complete(Role.FAST, MESSAGES, name="title")
    second = await c.complete(Role.FAST, MESSAGES, name="title")
    assert first.text == second.text == "cached text"
    assert len(sent) == 1 and second.cached


async def test_stream_yields_text_and_records_final_usage(client) -> None:
    chunks = [
        {
            "id": "g",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "anthropic/claude-sonnet-5.5",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }
        for text in ("Hello ", "world")
    ] + [
        {
            "id": "g",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "anthropic/claude-sonnet-5.5",
            "choices": [],
            "usage": USAGE,
        }
    ]
    body = "".join(f"data: {json.dumps(ch)}\n\n" for ch in chunks) + "data: [DONE]\n\n"
    c, sent = client(lambda r: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
    collector, token = start_collection()
    try:
        text = "".join([part async for part in c.stream(Role.SYNTH, MESSAGES, name="synthesize")])
    finally:
        stop_collection(token)
    assert text == "Hello world"
    assert sent[0]["stream"] is True and sent[0]["stream_options"] == {"include_usage": True}
    assert collector.calls[0].cached_tokens == 2600
