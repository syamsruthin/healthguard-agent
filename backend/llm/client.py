"""
Provider-agnostic LLM client (OpenRouter by default, any OpenAI-compatible
endpoint via LLM_BASE_URL).

Features
--------
* Role-based model selection with OpenRouter `models` fallback routing.
* Provider prompt caching: messages flagged `cache=True` become
  `cache_control: ephemeral` breakpoints for providers that need explicit
  markers (Anthropic, Gemini); OpenAI-family models cache automatically.
* Usage accounting incl. cached / cache-write tokens and USD cost.
* Redis exact-match cache for deterministic calls.
* Structured output (JSON schema → Pydantic) with one repair attempt.
* Streaming with final usage.
* SDK-level retries with exponential backoff on 429/5xx and timeouts.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from backend.core.config import settings
from backend.core.errors import UpstreamError
from backend.core.logging import get_logger
from backend.core.telemetry import LLM_ERRORS, end_llm_span, start_span
from backend.llm import cache as llm_cache
from backend.llm.prompts import PROMPT_VERSION
from backend.llm.registry import ModelSpec, Role, is_router, spec_for, supports_cache_control
from backend.llm.usage import LLMCallRecord, parse_usage, record

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)
Msg = dict[str, Any]  # {"role": str, "content": str, "cache": bool?}

_MAX_BREAKPOINTS = 4  # Anthropic allows up to 4 cache_control breakpoints


class LLMError(UpstreamError):
    code = "llm_error"


@dataclass
class LLMResult:
    text: str
    model: str
    usage: dict[str, Any]
    cached: bool = False
    cache_key: str | None = None  # exact-cache entry holding this text; `llm_cache.forget` it if unusable


class LLMClient(Protocol):
    async def complete(
        self,
        role: Role,
        messages: list[Msg],
        *,
        name: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        use_cache: bool = True,
    ) -> LLMResult: ...

    async def structured(
        self,
        role: Role,
        messages: list[Msg],
        schema: type[T],
        *,
        name: str,
        model: str | None = None,
        max_tokens: int | None = None,
        use_cache: bool = True,
    ) -> T: ...

    def stream(
        self,
        role: Role,
        messages: list[Msg],
        *,
        name: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> AsyncIterator[str]: ...


# ── Helpers ─────────────────────────────────────────────────────────────
def prepare_messages(messages: list[Msg], model: str) -> list[dict[str, Any]]:
    """Strip internal flags and add `cache_control` breakpoints where supported."""
    flagged = [i for i, m in enumerate(messages) if m.get("cache")]
    use_markers = settings.prompt_cache_enabled and supports_cache_control(model)
    keep = set(flagged[-_MAX_BREAKPOINTS:]) if use_markers else set()
    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        content: Any = m["content"]
        if i in keep:
            content = [{"type": "text", "text": m["content"], "cache_control": {"type": "ephemeral"}}]
        out.append({"role": m["role"], "content": content})
    return out


_FENCE = re.compile(r"^```(?:json|sql|python)?\s*|\s*```$", re.I | re.M)


def strip_fences(text: str) -> str:
    return _FENCE.sub("", text.strip()).strip()


def extract_json(text: str) -> str:
    text = strip_fences(text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in model output")
    return text[start : end + 1]


def _is_openrouter() -> bool:
    return "openrouter.ai" in settings.llm_base_url


class OpenRouterClient:
    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.openrouter_api_key or "not-configured",
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            default_headers={
                "HTTP-Referer": settings.openrouter_app_url,
                "X-Title": settings.openrouter_app_title,
            },
        )

    @property
    def raw(self) -> AsyncOpenAI:
        return self._client

    def _check_configured(self) -> None:
        if _is_openrouter() and not settings.openrouter_api_key:
            raise LLMError("OPENROUTER_API_KEY is not configured", code="llm_not_configured")

    def _extra_body(self, spec: ModelSpec) -> dict[str, Any] | None:
        if not _is_openrouter():
            return None
        body: dict[str, Any] = {"usage": {"include": True}}
        if spec.fallbacks:
            body["models"] = [spec.model, *[m for m in spec.fallbacks if m != spec.model]]
        return body

    async def complete(
        self,
        role: Role,
        messages: list[Msg],
        *,
        name: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        use_cache: bool = True,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResult:
        spec = spec_for(role, model)
        temp = spec.temperature if temperature is None else temperature
        mtok = max_tokens or spec.max_tokens
        cacheable = use_cache and temp == 0
        key = None
        if cacheable:
            key = llm_cache.cache_key(
                {
                    "model": spec.model,
                    "fallbacks": spec.fallbacks,
                    "messages": messages,
                    "max_tokens": mtok,
                    "response_format": response_format,
                    "v": PROMPT_VERSION,
                }
            )
            hit = await llm_cache.get(key)
            if hit:
                record(LLMCallRecord(role=role.value, name=name, model=hit["model"], exact_cache_hit=True))
                return LLMResult(text=hit["text"], model=hit["model"], usage={}, cached=True, cache_key=key)

        self._check_configured()
        kwargs: dict[str, Any] = {
            "model": spec.model,
            "messages": prepare_messages(messages, spec.model),
            "temperature": temp,
            "max_tokens": mtok,
        }
        if response_format:
            kwargs["response_format"] = response_format
        extra = self._extra_body(spec)
        if extra:
            kwargs["extra_body"] = extra
        t0 = time.monotonic()
        llm_span = start_span(
            "llm.complete", llm_role=role.value, llm_call=name, llm_requested_model=spec.model
        )
        try:
            try:
                resp = await self._client.chat.completions.create(**kwargs)
            except openai.BadRequestError:
                if not response_format:
                    raise
                kwargs.pop("response_format")  # provider lacks structured outputs → prompt-only JSON
                resp = await self._client.chat.completions.create(**kwargs)
        except openai.OpenAIError as exc:
            LLM_ERRORS.labels(role=role.value).inc()
            record(
                LLMCallRecord(
                    role=role.value,
                    name=name,
                    model=spec.model,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    error=str(exc)[:500],
                )
            )
            log.warning("llm_call_failed", role=role.value, call=name, model=spec.model, error=str(exc)[:300])
            end_llm_span(llm_span, model=spec.model, usage={}, error=str(exc))
            raise LLMError(f"LLM call '{name}' failed: {exc.__class__.__name__}") from exc

        latency = int((time.monotonic() - t0) * 1000)
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        served = getattr(resp, "model", None) or spec.model
        usage = parse_usage(getattr(resp, "usage", None))
        record(LLMCallRecord(role=role.value, name=name, model=served, latency_ms=latency, **usage))
        end_llm_span(llm_span, model=served, usage=usage)
        if key and text:
            await llm_cache.put(key, {"text": text, "model": served})
        return LLMResult(text=text, model=served, usage=usage, cache_key=key if text else None)

    async def structured(
        self,
        role: Role,
        messages: list[Msg],
        schema: type[T],
        *,
        name: str,
        model: str | None = None,
        max_tokens: int | None = None,
        use_cache: bool = True,
    ) -> T:
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": False},
        }
        result = await self.complete(
            role,
            messages,
            name=name,
            model=model,
            max_tokens=max_tokens,
            use_cache=use_cache,
            response_format=response_format,
        )
        try:
            return schema.model_validate_json(extract_json(result.text))
        except (ValueError, ValidationError) as exc:
            log.info("structured_output_repair", call=name, error=str(exc)[:200])
            await llm_cache.forget(result.cache_key)  # never replay output that failed the schema
            repair = [
                *messages,
                {"role": "assistant", "content": result.text[:4000]},
                {
                    "role": "user",
                    "content": (
                        f"That output was not valid JSON for the required schema ({str(exc)[:300]}). "
                        "Reply with ONLY the corrected JSON object."
                    ),
                },
            ]
            # A router may keep choosing a model that ignores the schema; repair with a fixed fallback model.
            spec = spec_for(role, model)
            repair_model = spec.fallbacks[0] if is_router(spec.model) and spec.fallbacks else model
            fixed = await self.complete(
                role,
                repair,
                name=f"{name}.repair",
                model=repair_model,
                max_tokens=max_tokens,
                use_cache=False,
                response_format=response_format,
            )
            try:
                parsed = schema.model_validate_json(extract_json(fixed.text))
            except (ValueError, ValidationError) as exc2:
                raise LLMError(f"LLM call '{name}' returned invalid structured output") from exc2
            if result.cache_key:  # the original request now replays the repaired, valid output
                await llm_cache.put(result.cache_key, {"text": fixed.text, "model": fixed.model})
            return parsed

    async def stream(
        self,
        role: Role,
        messages: list[Msg],
        *,
        name: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> AsyncIterator[str]:
        self._check_configured()
        spec = spec_for(role, model)
        kwargs: dict[str, Any] = {
            "model": spec.model,
            "messages": prepare_messages(messages, spec.model),
            "temperature": spec.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or spec.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        extra = self._extra_body(spec)
        if extra:
            kwargs["extra_body"] = extra
        t0 = time.monotonic()
        served = spec.model
        usage: dict[str, Any] = {}
        error: str | None = None
        resp: Any = None
        completed = False
        llm_span = start_span(
            "llm.stream", llm_role=role.value, llm_call=name, llm_requested_model=spec.model
        )
        try:
            resp = await self._client.chat.completions.create(**kwargs)
            async for chunk in resp:
                served = getattr(chunk, "model", None) or served
                if chunk.choices:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        yield delta
                if getattr(chunk, "usage", None):
                    usage = parse_usage(chunk.usage)
            completed = True
        except openai.OpenAIError as exc:
            error = str(exc)
            LLM_ERRORS.labels(role=role.value).inc()
            raise LLMError(f"LLM stream '{name}' failed: {exc.__class__.__name__}") from exc
        finally:
            # Also runs when the consumer stops early (client gone, turn deadline): close the HTTP
            # stream and still account for the call.
            if not completed and error is None:
                error = "stream closed before completion"
                if resp is not None:
                    try:
                        await resp.close()
                    except Exception as close_exc:
                        log.debug("llm_stream_close_failed", error=str(close_exc)[:200])
            end_llm_span(llm_span, model=served, usage=usage, error=error)
            record(
                LLMCallRecord(
                    role=role.value,
                    name=name,
                    model=served,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    error=error[:500] if error else None,
                    **usage,
                )
            )


# ── Accessor (dependency-injection seam for tests) ──────────────────────
_llm: Any = None


def get_llm() -> Any:
    global _llm
    if _llm is None:
        _llm = OpenRouterClient()
    return _llm


def set_llm(client: Any) -> None:
    global _llm
    _llm = client


def to_json(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)
