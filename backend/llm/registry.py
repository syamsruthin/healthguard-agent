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


def spec_for(role: Role, model: str | None = None) -> ModelSpec:
    if role is Role.FAST:
        return ModelSpec(role, model or settings.model_fast, settings.model_fast_fallbacks, 0.0, 1200)
    if role is Role.CODE:
        return ModelSpec(role, model or settings.model_code, settings.model_code_fallbacks, 0.0, 1200)
    if role is Role.SYNTH:
        return ModelSpec(role, model or settings.model_synth, settings.model_synth_fallbacks, 0.2, 1600)
    return ModelSpec(role, model or settings.eval_judges[0], [], 0.0, 2048)


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
    """Providers that need explicit `cache_control` breakpoints (others cache automatically)."""
    return model.startswith(("anthropic/", "google/"))
