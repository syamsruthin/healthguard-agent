"""
Per-turn LLM usage accounting.

A `UsageCollector` is placed in a contextvar for the duration of a chat turn
(or eval case). Every LLM call appends an `LLMCallRecord`; the chat service
persists them to `app.llm_calls` and rolls totals onto the assistant message
(tokens, prompt-cache tokens, USD cost).
"""

from __future__ import annotations

import contextvars
from dataclasses import asdict, dataclass, field
from typing import Any

from backend.core.telemetry import LLM_COST, LLM_LATENCY, LLM_TOKENS


@dataclass
class LLMCallRecord:
    role: str
    name: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    exact_cache_hit: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UsageCollector:
    calls: list[LLMCallRecord] = field(default_factory=list)

    def add(self, record: LLMCallRecord) -> None:
        self.calls.append(record)
        LLM_LATENCY.labels(role=record.role).observe(record.latency_ms / 1000)
        LLM_TOKENS.labels(role=record.role, model=record.model, kind="prompt").inc(record.prompt_tokens)
        LLM_TOKENS.labels(role=record.role, model=record.model, kind="completion").inc(
            record.completion_tokens
        )
        LLM_TOKENS.labels(role=record.role, model=record.model, kind="cached").inc(record.cached_tokens)
        if record.cost_usd:
            LLM_COST.labels(role=record.role, model=record.model).inc(record.cost_usd)

    @property
    def totals(self) -> dict[str, float]:
        return {
            "prompt_tokens": sum(c.prompt_tokens for c in self.calls),
            "completion_tokens": sum(c.completion_tokens for c in self.calls),
            "cached_tokens": sum(c.cached_tokens for c in self.calls),
            "cache_write_tokens": sum(c.cache_write_tokens for c in self.calls),
            "cost_usd": round(sum(c.cost_usd for c in self.calls), 6),
            "calls": len(self.calls),
            "exact_cache_hits": sum(1 for c in self.calls if c.exact_cache_hit),
        }


_current: contextvars.ContextVar[UsageCollector | None] = contextvars.ContextVar("hg_usage", default=None)


def start_collection() -> tuple[UsageCollector, contextvars.Token[UsageCollector | None]]:
    collector = UsageCollector()
    return collector, _current.set(collector)


def stop_collection(token: contextvars.Token[UsageCollector | None]) -> None:
    _current.reset(token)


def record(rec: LLMCallRecord) -> None:
    collector = _current.get()
    if collector is not None:
        collector.add(rec)


def parse_usage(usage: Any) -> dict[str, Any]:
    """Normalise OpenAI/OpenRouter usage objects (incl. prompt-cache details and cost)."""
    if usage is None:
        return {}
    data = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    details = data.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": int(data.get("prompt_tokens") or 0),
        "completion_tokens": int(data.get("completion_tokens") or 0),
        "cached_tokens": int(details.get("cached_tokens") or 0),
        "cache_write_tokens": int(
            details.get("cache_write_tokens") or data.get("cache_creation_input_tokens") or 0
        ),
        "cost_usd": float(data.get("cost") or 0.0),
    }
