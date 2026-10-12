"""
Model registry: maps each *role* in the system to an OpenRouter model,
its fallbacks and default generation parameters ("balanced tiers").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from backend.core.config import settings


class Role(StrEnum):
    FAST = "fast"  # understand (guardrail + rewrite + plan), verify, summaries, memory extraction
    CODE = "code"  # SQL + pandas generation / self-correction
    SYNTH = "synth"  # final answer synthesis (streamed)
    JUDGE = "judge"  # evaluation judges (model chosen per call)


@dataclass(frozen=True)
class ModelSpec:
    role: Role
    model: str
    fallbacks: list[str] = field(default_factory=list)
    temperature: float = 0.0
    max_tokens: int = 1024


# Routers choose the serving model per request; it may be Anthropic or Gemini, which only cache prompts
# with explicit breakpoints, so routers get the markers too (providers that cache automatically ignore them).
ROUTER_MODELS = ("typesafe/jev-router", "openrouter/auto")
ROUTER_MAX_TOKENS = 4096


def is_router(model: str) -> bool:
    return model in ROUTER_MODELS


def spec_for(role: Role | str, model: str | None = None) -> ModelSpec:
    role = Role(role)  # a plain string must not silently fall through to the judge spec
    if role == Role.FAST:
        spec = ModelSpec(role, model or settings.model_fast, settings.model_fast_fallbacks, 0.0, 1200)
    elif role == Role.CODE:
        spec = ModelSpec(role, model or settings.model_code, settings.model_code_fallbacks, 0.0, 1200)
    elif role == Role.SYNTH:
        spec = ModelSpec(role, model or settings.model_synth, settings.model_synth_fallbacks, 0.2, 1600)
    else:
        spec = ModelSpec(role, model or settings.eval_judges[0], [], 0.0, 2048)
    if is_router(spec.model) and spec.max_tokens < ROUTER_MAX_TOKENS:
        # A router may pick a reasoning mode whose thinking counts against max_tokens; a tight cap cut
        # verifier JSON off mid-object. Only generated tokens are billed, so the higher cap is free when unused.
        spec = ModelSpec(spec.role, spec.model, spec.fallbacks, spec.temperature, ROUTER_MAX_TOKENS)
    return spec


def all_configured_models() -> list[str]:
    models = {
        settings.model_fast,
        settings.model_code,
        settings.model_synth,
        *settings.model_fast_fallbacks,
        *settings.model_code_fallbacks,
        *settings.model_synth_fallbacks,
        *settings.eval_judges,
        *settings.online_eval_judges,
    }
    return sorted(models)


def supports_cache_control(model: str) -> bool:
    """Models that should receive explicit `cache_control` breakpoints."""
    return model.startswith(("anthropic/", "google/")) or is_router(model)
