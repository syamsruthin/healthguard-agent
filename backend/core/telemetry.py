"""
Observability: Prometheus metrics + optional OpenTelemetry tracing.

Metrics are always on (`/metrics`). OTel tracing is enabled when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set (e.g. the Jaeger service in the
`observability` compose profile); otherwise spans are no-ops.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from prometheus_client import Counter, Histogram

from backend.core.config import settings
from backend.core.logging import get_logger

log = get_logger(__name__)

# ── Prometheus metrics ──────────────────────────────────────────────────
LLM_TOKENS = Counter("hg_llm_tokens_total", "LLM tokens by role/model/kind", ["role", "model", "kind"])
LLM_COST = Counter("hg_llm_cost_usd_total", "LLM cost (USD) reported by OpenRouter", ["role", "model"])
LLM_LATENCY = Histogram(
    "hg_llm_latency_seconds", "LLM call latency", ["role"], buckets=(0.25, 0.5, 1, 2, 4, 8, 16, 32, 64)
)
LLM_ERRORS = Counter("hg_llm_errors_total", "LLM call failures", ["role"])
CACHE_EVENTS = Counter("hg_cache_events_total", "Cache lookups by layer/outcome", ["layer", "outcome"])
TOOL_EVENTS = Counter("hg_tool_events_total", "Tool executions by tool/outcome", ["tool", "outcome"])
GUARDRAIL_BLOCKS = Counter("hg_guardrail_blocks_total", "Guardrail blocks by reason", ["reason"])
TURN_LATENCY = Histogram(
    "hg_turn_latency_seconds", "End-to-end chat turn latency", buckets=(0.5, 1, 2, 4, 8, 16, 32, 64)
)

_tracer = trace.get_tracer("healthguard")


def setup_tracing(app: Any | None = None) -> None:
    """Configure OTLP export + auto-instrumentation if an endpoint is configured."""
    if not settings.otel_exporter_otlp_endpoint:
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": settings.otel_service_name}))
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{settings.otel_exporter_otlp_endpoint}/v1/traces"))
        )
        trace.set_tracer_provider(provider)
        HTTPXClientInstrumentor().instrument()
        if app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
        log.info("otel_enabled", endpoint=settings.otel_exporter_otlp_endpoint)
    except Exception as exc:  # pragma: no cover - optional dependency path
        log.warning("otel_setup_failed", error=str(exc))


def instrument_sqlalchemy(engine: Any) -> None:
    if not settings.otel_exporter_otlp_endpoint:
        return
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    except Exception as exc:  # pragma: no cover
        log.warning("otel_sqlalchemy_failed", error=str(exc))


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(
                    key, value if isinstance(value, (str, int, float, bool)) else str(value)
                )
        yield current


def start_span(name: str, **attributes: Any) -> Any:
    """A span that is NOT made current — safe to hold across `yield` in async generators. Call `.end()`."""
    s = _tracer.start_span(name)
    for key, value in attributes.items():
        if value is not None:
            s.set_attribute(key, value if isinstance(value, (str, int, float, bool)) else str(value))
    return s


def end_llm_span(s: Any, *, model: str, usage: dict[str, Any], error: str | None = None) -> None:
    s.set_attribute("llm.model", model)
    for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens", "cost_usd"):
        if key in usage:
            s.set_attribute(f"llm.{key}", usage[key])
    if error:
        s.set_attribute("error", True)
        s.set_attribute("error.message", error[:300])
    s.end()
