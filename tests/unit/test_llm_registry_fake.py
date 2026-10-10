from __future__ import annotations

import pytest
from pydantic import BaseModel

from backend.core.config import settings
from backend.llm.client import LLMError
from backend.llm.registry import Role, spec_for, supports_cache_control


def test_spec_for_accepts_plain_strings() -> None:
    assert spec_for("fast").model == settings.model_fast
    assert spec_for(Role.SYNTH).temperature == 0.2
    assert spec_for("judge", model="openai/x").model == "openai/x"
    with pytest.raises(ValueError):
        spec_for("nonsense")


def test_cache_control_support() -> None:
    assert supports_cache_control("anthropic/claude-haiku-5.5")
    assert supports_cache_control("google/gemini-3.8-flash")
    assert not supports_cache_control("openai/gpt-5.4-mini")


class _Out(BaseModel):
    answer: str


async def test_fake_llm_script_semantics(fake_llm) -> None:
    llm = fake_llm({"sql": ["first", "second"], "understand": {"answer": "ok"}, "boom": LLMError("down")})
    assert (await llm.complete(Role.CODE, [], name="sql")).text == "first"
    assert (await llm.complete(Role.CODE, [], name="sql.retry1")).text == "second"
    assert (await llm.complete(Role.CODE, [], name="sql")).text == "second"  # last element repeats
    assert (await llm.structured(Role.FAST, [], _Out, name="understand")).answer == "ok"
    with pytest.raises(LLMError):
        await llm.complete(Role.FAST, [], name="boom")
    with pytest.raises(KeyError):
        await llm.complete(Role.FAST, [], name="unscripted")
    assert llm.names()[:3] == ["sql", "sql.retry1", "sql"]


async def test_fake_llm_stream_chunks(fake_llm) -> None:
    llm = fake_llm({"synthesize": "A" * 30})
    chunks = [c async for c in llm.stream(Role.SYNTH, [], name="synthesize")]
    assert "".join(chunks) == "A" * 30 and len(chunks) == 3
