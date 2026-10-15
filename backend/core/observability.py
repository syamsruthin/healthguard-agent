"""
Langfuse LLM observability.

Every chat turn becomes one Langfuse trace whose id is the turn's `trace_id`
(the same UUID as `app.traces.id` and `messages.trace_id`), so feedback,
online evals and offline evals can attach scores later without a lookup.
Inside it: one observation per graph node, per tool step and codegen attempt,
per guardrail, cache and retrieval stage, and a generation per model call with
the served model, token usage (incl. prompt-cache reads / writes) and the cost
OpenRouter reports. Background jobs are their own traces in the same session.

Design rules
------------
* **Off unless configured.** Without `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`
  every helper is a no-op returning `NOOP`, so tests and CI never export.
* **Never breaks a turn.** Every SDK call is guarded; a Langfuse outage only
  loses telemetry.
* **Own parent tracking.** The observation tree is kept in a contextvar and
  children are started on their parent explicitly, on an isolated
  TracerProvider. The OTel context (FastAPI / Jaeger spans) is never touched,
  so enabling both never orphans Langfuse observations.
* **PII stays masked.** Inputs, outputs and metadata go through the log PII
  masker (ADR-0021) and are truncated per value; `mask_otel_spans` re-applies
  the masker at export as a backstop. `LANGFUSE_CAPTURE_IO=false` sends
  metadata, usage and scores only. Users are identified by UUID, never email.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, is_dataclass
from typing import Any, Literal

from backend.core.config import settings
from backend.core.logging import get_logger, mask_pii

log = get_logger(__name__)

ObsType = Literal[
    "span", "generation", "embedding", "agent", "tool", "chain", "retriever", "evaluator", "guardrail"
]
ScoreType = Literal["NUMERIC", "CATEGORICAL", "BOOLEAN"]

# Metadata keys that carry conversation content; dropped when LANGFUSE_CAPTURE_IO=false.
_CONTENT_KEYS = frozenset(
    {
        "answer",
        "cached_query",
        "code",
        "formatted",
        "missing",
        "prior",
        "query",
        "reasoning",
        "sources",
        "sql",
        "task",
        "unsupported_claims",
    }
)
_MAX_ITEMS = 50  # list items kept per value
_MAX_DEPTH = 6
_IO_ATTRIBUTE_MARKERS = (".input", ".output", ".metadata")

_client: Any = None
_initialised = False
_current: contextvars.ContextVar[Observation | None] = contextvars.ContextVar("hg_langfuse_obs", default=None)


# ── Client ──────────────────────────────────────────────────────────────
def _build_client(**overrides: Any) -> Any:
    from langfuse import Langfuse
    from langfuse.span_filter import is_langfuse_span
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    kwargs: dict[str, Any] = {
        "public_key": settings.langfuse_public_key,
        "secret_key": settings.langfuse_secret_key,
        "base_url": settings.langfuse_base_url,
        "environment": settings.env,
        "release": settings.app_version,
        "sample_rate": settings.langfuse_sample_rate,
        "debug": settings.langfuse_debug,
        # Isolated provider: Langfuse never becomes (or exports from) the global Jaeger provider.
        "tracer_provider": TracerProvider(
            resource=Resource.create({"service.name": settings.otel_service_name})
        ),
        "should_export_span": is_langfuse_span,
        "mask_otel_spans": mask_otel_spans,
    }
    kwargs.update(overrides)
    return Langfuse(**kwargs)


def get_langfuse() -> Any | None:
    """The process-wide Langfuse client, or None when tracing is not configured."""
    global _client, _initialised
    if not _initialised:
        _initialised = True
        if settings.langfuse_configured:
            try:
                _client = _build_client()
                log.info("langfuse_enabled", base_url=settings.langfuse_base_url)
            except Exception as exc:
                log.warning("langfuse_setup_failed", error=str(exc)[:300])
                _client = None
    return _client


def set_langfuse(client: Any | None) -> None:
    """Install a client (tests) or None to disable tracing."""
    global _client, _initialised
    _client, _initialised = client, True


def enabled() -> bool:
    return get_langfuse() is not None


def flush() -> None:
    if (lf := get_langfuse()) is not None:
        try:
            lf.flush()
        except Exception as exc:
            log.debug("langfuse_flush_failed", error=str(exc)[:200])


def shutdown() -> None:
    """Flush and stop the exporter (process exit: API lifespan, worker, CLIs)."""
    global _client, _initialised
    if _client is not None:
        try:
            _client.shutdown()
        except Exception as exc:
            log.debug("langfuse_shutdown_failed", error=str(exc)[:200])
    _client, _initialised = None, False


def trace_id_for(value: uuid.UUID | str) -> str:
    """The Langfuse (OTel) trace id for an app trace UUID: its 32 lowercase hex digits."""
    return uuid.UUID(str(value)).hex


def trace_url(trace_id: uuid.UUID | str) -> str | None:
    lf = get_langfuse()
    if lf is None:
        return None
    try:
        return str(lf.get_trace_url(trace_id=trace_id_for(trace_id)))
    except Exception as exc:
        log.debug("langfuse_trace_url_failed", error=str(exc)[:200])
        return None


# ── Payload hygiene ─────────────────────────────────────────────────────
def clip(value: Any, limit: int | None = None, depth: int = 0) -> Any:
    """PII-masked, size-bounded, JSON-friendly copy of a value."""
    limit = limit or settings.langfuse_max_field_chars
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        text = mask_pii(value)
        return text if len(text) <= limit else f"{text[:limit]}… [+{len(text) - limit} chars]"
    if depth >= _MAX_DEPTH:
        return clip(str(value), limit, depth)
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    elif is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {str(k): clip(v, limit, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
        out = [clip(v, limit, depth + 1) for v in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            out.append(f"… [+{len(items) - _MAX_ITEMS} items]")
        return out
    return clip(str(value), limit, depth)


def io(value: Any) -> Any:
    """An observation input / output: None when I/O capture is off."""
    return clip(value) if settings.langfuse_capture_io and value is not None else None


def meta(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not metadata:
        return None
    kept = {
        k: v
        for k, v in metadata.items()
        if v not in (None, "", [], {}) and (settings.langfuse_capture_io or k not in _CONTENT_KEYS)
    }
    return clip(kept) if kept else None


def mask_otel_spans(*, params: Any) -> Any:
    """Export-stage backstop: mask PII in any input / output / metadata attribute (incl. third-party spans)."""
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches: dict[Any, Any] = {}
    for identifier, span in params.spans.items():
        changed = {
            key: masked
            for key, value in span.attributes.items()
            if isinstance(value, str)
            and any(marker in key for marker in _IO_ATTRIBUTE_MARKERS)
            and (masked := mask_pii(value)) != value
        }
        if changed:
            patches[identifier] = OtelSpanPatch(set_attributes=changed)
    return MaskOtelSpansResult(span_patches=patches)


# ── Observations ────────────────────────────────────────────────────────
_UNSET: Any = object()


class Observation:
    """Null-safe wrapper over a Langfuse observation; every method is a no-op when inactive."""

    __slots__ = ("raw",)

    def __init__(self, raw: Any = None):
        self.raw = raw

    @property
    def active(self) -> bool:
        return self.raw is not None

    @property
    def id(self) -> str | None:
        return getattr(self.raw, "id", None)

    @property
    def trace_id(self) -> str | None:
        return getattr(self.raw, "trace_id", None)

    def update(
        self,
        *,
        input: Any = _UNSET,
        output: Any = _UNSET,
        metadata: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> Observation:
        """Set input / output (masked, clipped), metadata and any generation field
        (model, usage_details, cost_details, completion_start_time, model_parameters, level, status_message)."""
        if self.raw is None:
            return self
        kwargs = {k: v for k, v in fields.items() if v is not None}
        if input is not _UNSET and (value := io(input)) is not None:
            kwargs["input"] = value
        if output is not _UNSET and (value := io(output)) is not None:
            kwargs["output"] = value
        if (m := meta(metadata)) is not None:
            kwargs["metadata"] = m
        if kwargs:
            try:
                self.raw.update(**kwargs)
            except Exception as exc:
                log.debug("langfuse_update_failed", error=str(exc)[:200])
        return self

    def fail(self, exc: BaseException | str, *, level: str = "ERROR") -> Observation:
        if isinstance(exc, BaseException):
            import asyncio

            if isinstance(exc, asyncio.CancelledError | GeneratorExit):
                return self.update(level="WARNING", status_message="cancelled")
            message = f"{exc.__class__.__name__}: {exc}"
        else:
            message = exc
        return self.update(level=level, status_message=mask_pii(message)[:500])

    def event(self, name: str, *, input: Any = None, output: Any = None, **metadata: Any) -> None:
        if self.raw is None:
            return
        try:
            self.raw.create_event(name=name, input=io(input), output=io(output), metadata=meta(metadata))
        except Exception as exc:
            log.debug("langfuse_event_failed", error=str(exc)[:200])

    def end(self) -> None:
        if self.raw is not None:
            try:
                self.raw.end()
            except Exception as exc:
                log.debug("langfuse_end_failed", error=str(exc)[:200])


NOOP = Observation()


def current() -> Observation:
    """The innermost open observation in this context (NOOP outside a trace)."""
    return _current.get() or NOOP


def start(
    name: str,
    *,
    as_type: ObsType = "span",
    input: Any = None,
    metadata: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
    parent: Observation | None = None,
    require_parent: bool = False,
    **fields: Any,
) -> Observation:
    """Start an observation WITHOUT making it current (safe across `yield`); the caller must `.end()` it.

    `trace_id` starts a new trace root with that id; otherwise the observation nests under `parent`
    (default: the current one) or, with no parent, starts its own trace unless `require_parent`.
    """
    lf = get_langfuse()
    if lf is None:
        return NOOP
    parent = parent if parent is not None else _current.get()
    if require_parent and not trace_id and (parent is None or not parent.active):
        return NOOP
    kwargs: dict[str, Any] = {
        "name": name,
        "as_type": as_type,
        **{k: v for k, v in fields.items() if v is not None},
    }
    if (value := io(input)) is not None:
        kwargs["input"] = value
    if (m := meta(metadata)) is not None:
        kwargs["metadata"] = m
    try:
        if trace_id:
            raw = lf.start_observation(trace_context={"trace_id": trace_id}, **kwargs)
        elif parent is not None and parent.active:
            raw = parent.raw.start_observation(**kwargs)
        else:
            raw = lf.start_observation(**kwargs)
    except Exception as exc:
        log.debug("langfuse_start_failed", observation=name, error=str(exc)[:200])
        return NOOP
    return Observation(raw)


@contextmanager
def observe(
    name: str,
    *,
    as_type: ObsType = "span",
    input: Any = None,
    metadata: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
    require_parent: bool = False,
    **fields: Any,
) -> Iterator[Observation]:
    """An observation that is current for the block: children nest under it; exceptions mark it failed.

    Do not hold one open across `yield` in an async generator — use `start()` there.
    """
    obs = start(
        name,
        as_type=as_type,
        input=input,
        metadata=metadata,
        trace_id=trace_id,
        require_parent=require_parent,
        **fields,
    )
    token = _current.set(obs) if obs.active else None
    try:
        yield obs
    except BaseException as exc:
        obs.fail(exc)
        raise
    finally:
        if token is not None:
            try:
                _current.reset(token)
            except ValueError:  # reset from another context; the parent is restored by that context's exit
                _current.set(None)
        obs.end()


def event(name: str, *, input: Any = None, output: Any = None, **metadata: Any) -> None:
    """A point-in-time event under the current observation (dropped outside a trace)."""
    current().event(name, input=input, output=output, **metadata)


@contextmanager
def propagate(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    trace_name: str | None = None,
    tags: list[str] | None = None,
    version: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Iterator[None]:
    """Trace-level attributes (user, session, tags, version, metadata) for every observation started inside.
    Nested blocks merge tags and metadata."""
    if get_langfuse() is None:
        yield
        return
    try:
        from langfuse import propagate_attributes

        cm: Any = propagate_attributes(
            user_id=user_id,
            session_id=session_id,
            trace_name=trace_name,
            tags=tags,
            version=version,
            # Propagated metadata must be short strings.
            metadata={k: mask_pii(str(v))[:200] for k, v in (metadata or {}).items() if v not in (None, "")}
            or None,
        )
    except Exception as exc:
        log.debug("langfuse_propagate_failed", error=str(exc)[:200])
        cm = nullcontext()
    with cm:
        yield


# ── Scores ──────────────────────────────────────────────────────────────
def score(
    trace_id: str | None,
    name: str,
    value: float | str | bool,
    *,
    data_type: ScoreType | None = None,
    comment: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    observation_id: str | None = None,
) -> None:
    """Attach a score to a trace (or one of its observations). Booleans are sent as BOOLEAN 1 / 0."""
    lf = get_langfuse()
    if lf is None or not trace_id:
        return
    if isinstance(value, bool):
        value, data_type = (1.0 if value else 0.0), "BOOLEAN"
    try:
        lf.create_score(
            trace_id=trace_id,
            observation_id=observation_id,
            name=name,
            value=value,
            data_type=data_type,
            comment=mask_pii(comment)[:1000] if comment else None,
            metadata=clip(dict(metadata)) if metadata else None,
        )
    except Exception as exc:
        log.debug("langfuse_score_failed", score=name, error=str(exc)[:200])


# ── LLM usage → Langfuse usage / cost ───────────────────────────────────
def usage_details(usage: Mapping[str, Any]) -> dict[str, int] | None:
    """OpenRouter usage (see `llm.usage.parse_usage`) as Langfuse usage keys; `input` excludes cache traffic."""
    if not usage:
        return None
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int(usage.get("cached_tokens") or 0)
    written = int(usage.get("cache_write_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    details = {
        "input": max(prompt - cached - written, 0),
        "input_cache_read": cached,
        "input_cache_creation": written,
        "output": completion,
        "total": prompt + completion,
    }
    return {k: v for k, v in details.items() if v or k in ("input", "output", "total")}


def cost_details(usage: Mapping[str, Any]) -> dict[str, float] | None:
    """The provider-reported cost: Langfuse's price table does not know routers such as Jev."""
    cost = usage.get("cost_usd") if usage else None
    return {"total": float(cost)} if cost is not None else None
