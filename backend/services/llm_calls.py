"""Persist per-call LLM usage (`app.llm_calls`) for chat turns and background jobs alike."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from decimal import Decimal

from backend.db.models import LLMCall
from backend.llm.usage import LLMCallRecord


def llm_call_rows(calls: Iterable[LLMCallRecord], trace_id: uuid.UUID | None) -> list[LLMCall]:
    """Rows for one turn's (or one job's) calls; background jobs have no trace."""
    return [
        LLMCall(
            trace_id=trace_id,
            role=c.role,
            name=c.name[:64],
            model=c.model[:128],
            prompt_tokens=c.prompt_tokens,
            completion_tokens=c.completion_tokens,
            cached_tokens=c.cached_tokens,
            cache_write_tokens=c.cache_write_tokens,
            cost_usd=Decimal(str(c.cost_usd)),
            latency_ms=c.latency_ms,
            exact_cache_hit=c.exact_cache_hit,
            error=c.error,
        )
        for c in calls
    ]
