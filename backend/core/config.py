"""
Centralized, environment-driven configuration (pydantic-settings).

Every tunable lives here: infrastructure URLs, auth, the per-role OpenRouter
model registry, caching, memory budgets, guardrail limits and eval settings.
Values come from environment variables (or `.env`), so the same image runs
in dev, CI and production.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent  # backend/
PROJECT_DIR = BACKEND_DIR.parent

_INSECURE_DEFAULT_SECRET = "change-me-in-production-please-32+chars"

CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Application ──────────────────────────────────────────────────────
    env: Literal["development", "test", "production"] = "development"
    app_name: str = "HealthGuard"
    app_version: str = "1.0.0"
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"
    log_json: bool = False
    cors_origins: CsvList = Field(default_factory=lambda: ["http://localhost:8501"])
    max_input_chars: int = 2000

    # ── Infrastructure ───────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://healthguard:healthguard@localhost:5432/healthguard"
    # Read-only, RLS-restricted role used exclusively for LLM-generated SQL.
    agent_db_user: str = "hg_agent_ro"
    agent_db_password: str = "hg_agent_ro_pw"
    redis_url: str = "redis://localhost:6379/0"
    db_pool_size: int = 10
    db_max_overflow: int = 10

    # ── Auth ─────────────────────────────────────────────────────────────
    jwt_secret: str = _INSECURE_DEFAULT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "healthguard"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    password_min_length: int = 10
    demo_password: str = "HealthGuard!2026"
    admin_email: str = "admin@demo.healthguard.dev"

    # ── Rate limits (per minute) ────────────────────────────────────────
    rate_limit_chat_per_minute: int = 20
    rate_limit_auth_per_minute: int = 10
    rate_limit_feedback_per_minute: int = 30
    # Turns per member account per UTC day (0 = unlimited); admins are exempt. Caps spend from open registration.
    daily_turn_limit: int = 150

    # ── LLM gateway (OpenRouter, OpenAI-compatible) ─────────────────────
    openrouter_api_key: str = ""
    llm_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_app_url: str = "https://github.com/syamsruthin/healthguard-agent"
    openrouter_app_title: str = "HealthGuard Agentic RAG"
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 3

    # Per-role model registry. Every role defaults to the TypeSafe Jev Router, which picks the model and
    # reasoning effort per request and bills at the serving model's price (the cost-efficient option).
    # Fallbacks use OpenRouter's `models` routing, so a router or provider outage degrades to fixed models.
    model_fast: str = "typesafe/jev-router"
    model_fast_fallbacks: CsvList = Field(
        default_factory=lambda: ["anthropic/claude-haiku-5.5", "google/gemini-3.8-flash"]
    )
    model_code: str = "typesafe/jev-router"
    model_code_fallbacks: CsvList = Field(
        default_factory=lambda: ["anthropic/claude-sonnet-5.5", "openai/gpt-5.4-mini"]
    )
    model_synth: str = "typesafe/jev-router"
    model_synth_fallbacks: CsvList = Field(
        default_factory=lambda: ["anthropic/claude-sonnet-5.5", "google/gemini-3.8-flash"]
    )

    # Judge ensembles ("jury") — different model families reduce self-preference bias. These stay fixed
    # models on purpose: a router could send every judge to the same model and the jury would lose its point.
    eval_judges: CsvList = Field(
        default_factory=lambda: [
            "openai/gpt-5.4-mini",
            "google/gemini-3.8-flash",
            "anthropic/claude-haiku-5.5",
        ]
    )
    online_eval_judges: CsvList = Field(
        default_factory=lambda: ["openai/gpt-5.4-mini", "google/gemini-3.8-flash"]
    )
    online_eval_sample_rate: float = 0.05  # share of answered turns judged live (every thumbs-down is judged)
    judge_disagreement_threshold: float = 0.3

    # ── Embeddings ───────────────────────────────────────────────────────
    embedding_provider: Literal["openrouter", "local", "hash"] = "openrouter"
    embedding_model: str = "openai/text-embedding-3-small"
    embedding_dim: int = 1536
    embedding_batch_size: int = 64

    # ── Caching ──────────────────────────────────────────────────────────
    prompt_cache_enabled: bool = True  # provider-side prompt caching markers
    llm_exact_cache_enabled: bool = True  # Redis exact-match cache for temperature-0 calls
    llm_exact_cache_ttl_seconds: int = 86_400
    semantic_cache_enabled: bool = True
    semantic_cache_distance_threshold: float = 0.08  # cosine distance
    semantic_cache_ttl_public_seconds: int = 86_400
    semantic_cache_ttl_member_seconds: int = 3_600

    # ── Agent behaviour ─────────────────────────────────────────────────
    max_tool_retries: int = 2
    max_replans: int = 1
    max_revisions: int = 1  # rewrites without unsupported claims before an answer is replaced
    max_plan_steps: int = 4
    sql_statement_timeout_ms: int = 5_000
    sql_max_rows: int = 200
    sandbox_timeout_seconds: float = 10.0
    sandbox_memory_mb: int = 1024
    sandbox_max_rows: int = 100
    sandbox_workers: int = 2
    rag_top_k: int = 6
    rag_candidate_k: int = 20
    rag_rerank: bool = False
    tool_step_timeout_seconds: float = 90.0  # wall clock per plan step (includes self-correction retries)
    # Wall clock for a whole agent turn; the conversation's turn lock lives 30 s longer.
    turn_timeout_seconds: float = 150.0
    # Replace answers the verifier judges ungrounded with the insufficient-context fallback
    # (false = keep the answer and only log the verdict).
    verify_replace_ungrounded: bool = True

    # ── Memory ───────────────────────────────────────────────────────────
    history_token_budget: int = 2_500
    summary_trigger_tokens: int = 3_000
    long_term_memory_top_k: int = 5
    long_term_memory_min_similarity: float = 0.35

    # ── Observability ────────────────────────────────────────────────────
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "healthguard-api"
    # Langfuse LLM tracing: on when both keys are set (LANGFUSE_ENABLED=false forces it off).
    langfuse_enabled: bool = True
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_sample_rate: float = 1.0  # share of traces exported
    langfuse_capture_io: bool = True  # false = metadata, usage and scores only (no prompts / answers / rows)
    langfuse_max_field_chars: int = 6_000  # per input / output / metadata value, after PII masking
    langfuse_debug: bool = False

    # ── Paths ────────────────────────────────────────────────────────────
    data_dir: Path = BACKEND_DIR / "data"

    @field_validator(
        "cors_origins",
        "model_fast_fallbacks",
        "model_code_fallbacks",
        "model_synth_fallbacks",
        "eval_judges",
        "online_eval_judges",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.env == "production":
            if self.jwt_secret == _INSECURE_DEFAULT_SECRET or len(self.jwt_secret) < 32:
                raise ValueError("JWT_SECRET must be set to a strong (32+ char) value in production")
            if not self.openrouter_api_key and self.embedding_provider == "openrouter":
                raise ValueError("OPENROUTER_API_KEY is required in production")
        return self

    # ── Derived values ───────────────────────────────────────────────────
    @property
    def lake_dir(self) -> Path:
        """CSV 'data lake' (providers, facilities, pharmacies, zip centroids)."""
        return self.data_dir / "lake"

    @property
    def corpus_dir(self) -> Path:
        """Rendered policy corpus (markdown) consumed by the ingest pipeline."""
        return self.data_dir / "corpus"

    @property
    def sync_database_url(self) -> str:
        """psycopg-style URL (used by Alembic and the LangGraph checkpointer)."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")

    @property
    def agent_database_dsn(self) -> str:
        """Plain asyncpg DSN for the read-only agent role."""
        base = self.sync_database_url
        scheme, rest = base.split("://", 1)
        hostpart = rest.split("@", 1)[1] if "@" in rest else rest
        return f"{scheme}://{self.agent_db_user}:{self.agent_db_password}@{hostpart}"

    @property
    def langfuse_configured(self) -> bool:
        return self.langfuse_enabled and bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openrouter_api_key) or "openrouter.ai" not in self.llm_base_url


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
