"""Langfuse helpers: off-by-default no-ops, PII masking, payload limits, usage mapping and trace shape."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from backend.core import observability as obs
from backend.core.config import settings
from tests.langfuse_tree import collect


# ── Disabled (the default in tests and without keys) ────────────────────
def test_disabled_without_keys() -> None:
    assert settings.langfuse_configured is False
    obs.set_langfuse(None)
    assert obs.get_langfuse() is None and not obs.enabled()


def test_disabled_helpers_are_noops() -> None:
    obs.set_langfuse(None)
    with obs.observe("anything", input={"x": 1}) as o:
        assert o is obs.NOOP and not o.active
        o.update(output="y", metadata={"a": 1}, usage_details={"input": 1}).fail("boom")
        obs.event("e", output=1)
        assert obs.current() is obs.NOOP
    with obs.propagate(user_id="u", tags=["t"]):
        pass
    obs.score("0" * 32, "grounded", True)
    obs.flush()
    assert obs.trace_url(uuid.uuid4()) is None


def test_exceptions_still_propagate_when_disabled() -> None:
    obs.set_langfuse(None)
    with pytest.raises(ValueError), obs.observe("x"):
        raise ValueError("kept")


def test_trace_id_matches_app_uuid() -> None:
    u = uuid.uuid4()
    assert obs.trace_id_for(u) == u.hex == obs.trace_id_for(str(u))
    assert len(obs.trace_id_for(u)) == 32


# ── Payload hygiene ─────────────────────────────────────────────────────
def test_clip_masks_pii_everywhere() -> None:
    value = {
        "text": "Maria (M100001, maria@example.com, 512-555-0101) born 1980-04-02, SSN 123-45-6789",
        "rows": [{"member_id": "M100002"}],
        "nested": ("M100003",),
    }
    out = obs.clip(value)
    flat = str(out)
    for leaked in ("M100001", "M100002", "M100003", "maria@example.com", "512-555-0101", "123-45-6789"):
        assert leaked not in flat
    assert "<member_id>" in out["text"] and out["rows"][0]["member_id"] == "<member_id>"


def test_clip_bounds_size() -> None:
    out = obs.clip("x" * 100, limit=10)
    assert out.startswith("x" * 10) and "+90 chars" in out
    items = obs.clip(list(range(80)))
    assert len(items) == 51 and items[-1] == "… [+30 items]"
    assert obs.clip(SimpleNamespace(a=1)) == "namespace(a=1)"


def test_capture_io_off_keeps_only_metadata(monkeypatch) -> None:
    monkeypatch.setattr(settings, "langfuse_capture_io", False)
    assert obs.io("the answer") is None
    assert obs.meta({"sources": "[S1] …", "code": "SELECT 1", "rows": 3, "ok": True}) == {
        "rows": 3,
        "ok": True,
    }


def test_meta_drops_empty_values_but_keeps_falsy_scalars() -> None:
    assert obs.meta({"a": None, "b": "", "c": [], "d": 0, "e": False}) == {"d": 0, "e": False}
    assert obs.meta({}) is None


def test_usage_and_cost_details() -> None:
    usage = {
        "prompt_tokens": 1000,
        "cached_tokens": 600,
        "cache_write_tokens": 100,
        "completion_tokens": 50,
        "cost_usd": 0.0042,
    }
    assert obs.usage_details(usage) == {
        "input": 300,
        "input_cache_read": 600,
        "input_cache_creation": 100,
        "output": 50,
        "total": 1050,
    }
    assert obs.cost_details(usage) == {"total": 0.0042}
    assert obs.usage_details({}) is None and obs.cost_details({}) is None


def test_export_masking_backstop() -> None:
    from langfuse.types import MaskOtelSpansParams

    span = SimpleNamespace(
        attributes={
            "langfuse.observation.input": '{"q": "claims for M100004"}',
            "langfuse.observation.model.name": "M100004-not-io",
            "langfuse.observation.output": "nothing sensitive",
        }
    )
    params = MaskOtelSpansParams.__new__(MaskOtelSpansParams)
    object.__setattr__(params, "spans", {"id": span})
    patches = obs.mask_otel_spans(params=params).span_patches
    assert dict(patches["id"].set_attributes) == {
        "langfuse.observation.input": '{"q": "claims for <member_id>"}'
    }


# ── Real SDK, in-memory exporter ────────────────────────────────────────
async def test_tree_attributes_and_errors(langfuse_spans) -> None:
    trace_id = obs.trace_id_for(uuid.uuid4())

    async def tool(i: int) -> None:
        with obs.observe(f"tool {i}", as_type="tool", input={"task": f"look up M10000{i}"}):
            gen = obs.start("llm.sql", as_type="generation", model="m")
            gen.update(output="SELECT 1", usage_details={"input": 5, "output": 1, "total": 6})
            gen.end()

    with (
        obs.propagate(user_id="user-1", session_id="conv-1", tags=["chat"]),
        obs.observe("chat-turn", as_type="agent", trace_id=trace_id, input={"message": "hi"}) as root,
    ):
        await asyncio.gather(tool(1), tool(2))  # concurrent children keep the right parent
        with pytest.raises(RuntimeError), obs.observe("failing"):
            raise RuntimeError("bad M100001")
        obs.event("note", output="done")
        root.update(output="answer")

    [turn] = collect(langfuse_spans)
    assert turn.name == "chat-turn" and turn.type == "agent" and turn.trace_id == trace_id
    assert turn.attrs["user.id"] == "user-1" and turn.attrs["session.id"] == "conv-1"
    assert sorted(turn.child_names()) == ["failing", "note", "tool 1", "tool 2"]
    for i in (1, 2):
        t = turn.child(f"tool {i}")
        assert t.type == "tool" and t.input == {"task": "look up <member_id>"}
        [gen] = t.children
        assert gen.type == "generation" and gen.usage == {"input": 5, "output": 1, "total": 6}
        assert gen.attrs["session.id"] == "conv-1"  # trace attributes reach every observation
    failing = turn.child("failing")
    assert failing.level == "ERROR"
    assert failing.attrs["langfuse.observation.status_message"] == "RuntimeError: bad <member_id>"
    assert obs.current() is obs.NOOP  # the parent pointer is restored after the block


async def test_require_parent_skips_orphans(langfuse_spans) -> None:
    with obs.observe("orphan-embedding", as_type="embedding", require_parent=True) as o:
        assert o is obs.NOOP
    with obs.observe("root"), obs.observe("child", as_type="embedding", require_parent=True) as c:
        assert c.active
    assert [r.name for r in collect(langfuse_spans)] == ["root"]


async def test_scores_go_to_the_langfuse_client(langfuse_spans, monkeypatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(obs.get_langfuse(), "create_score", lambda **kw: sent.append(kw))
    obs.score("a" * 32, "grounded", False, comment="claim about M100001")
    obs.score("a" * 32, "outcome", "answered", data_type="CATEGORICAL")
    obs.score(None, "ignored", 1.0)
    assert sent[0]["value"] == 0.0 and sent[0]["data_type"] == "BOOLEAN"
    assert sent[0]["comment"] == "claim about <member_id>"
    assert sent[1]["value"] == "answered" and len(sent) == 2


async def test_sdk_failure_never_breaks_the_caller(langfuse_spans, monkeypatch) -> None:
    def broken(**_):
        raise RuntimeError("sdk down")

    monkeypatch.setattr(obs.get_langfuse(), "start_observation", broken)
    with obs.observe("x") as o:
        assert o is obs.NOOP
    monkeypatch.setattr(obs.get_langfuse(), "create_score", broken)
    obs.score("a" * 32, "s", 1.0)  # swallowed
