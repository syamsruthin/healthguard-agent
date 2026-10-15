# ADR-0035: Observability: per-turn traces, a per-call cost ledger, Prometheus and OpenTelemetry

- **Status:** Accepted (`d88c015`, `f99ce81`, `9a1eb6d`)
- **Area:** Operations

## Context

An agent turn is a small distributed system: up to ~10 model calls across providers, SQL on two pools, a process pool, Redis, and streaming. Questions operators and auditors ask:

- *Why* did the agent answer this way? (plan, sources, verdict)
- *What* did it cost, per turn, per call, per model, per role?
- *Where* did the time go?
- *How often* do guardrails block, caches hit, tools fail?

## Decision

Four complementary signals:

| Signal | What | Where |
|---|---|---|
| **Per-turn trace** (`app.traces`) | One step per node: input, output, reasoning, duration, metadata (plan warnings, generated code, sources, cache distance, verdict) | `@traced` decorator + `step()` in [agents/trace.py](../../backend/agents/trace.py); admin view `GET /admin/traces/{id}` |
| **Cost ledger** (`app.llm_calls`) | One row per LLM call: role, call name, served model, prompt / completion / cached / cache-write tokens, cost (from OpenRouter), latency, exact-cache hit, error; linked to the trace (background jobs have no trace id) | `UsageCollector` in a `contextvar` ([llm/usage.py](../../backend/llm/usage.py)); persisted with the message in one transaction |
| **Prometheus** (`/metrics`) | `hg_llm_tokens_total`, `hg_llm_cost_usd_total`, `hg_llm_latency_seconds`, `hg_llm_errors_total`, `hg_cache_events_total{layer,outcome}`, `hg_tool_events_total{tool,outcome}`, `hg_guardrail_blocks_total{reason}`, `hg_turn_latency_seconds`, HTTP metrics | [core/telemetry.py](../../backend/core/telemetry.py) |
| **OpenTelemetry** (OTLP → Jaeger) | FastAPI, SQLAlchemy and httpx auto-instrumentation; one span per graph node (`agent.*`) and per LLM call (`llm.complete` / `llm.stream`) with model, tokens, cached tokens, cost | enabled by `OTEL_EXPORTER_OTLP_ENDPOINT`; `docker compose --profile observability` |

Plus **structured logs** (structlog, JSON in containers) with `request_id`, `user_id`, `conversation_id` and `trace_id` bound via contextvars, and PII masking ([ADR-0021](0021-data-minimisation-and-pii.md)).

The **admin dashboards** (Streamlit) read aggregates over these tables: latency p50/p95, cost per turn, prompt-cache ratio, semantic-cache hit rate, outcomes, tools, feedback and online-eval scores.

**Visibility split:** members receive the plan, steps, verification verdict and citations; the full trace and generated code are admin-only.

## Why

- **The trace answers "why".** For an AI system, the reasoning path is the debugging artefact. Storing it per turn makes every answer auditable after the fact.
- **The cost ledger answers "how much" precisely.** Using the provider's reported cost avoids maintaining price tables, which change monthly. Per-call rows made it possible to see that SQL generation was the most expensive call, which drove prompt-cache work.
- **A `contextvar` collector** captures every call made during a turn (including inside tools and the retriever) without threading a collector argument through every function.
- **Prometheus for rates and alerts, OTel for latency breakdowns,** each the standard tool for its job. Tracing is off unless configured, so there's no cost in development.
- **One transaction for message + trace + calls** means audit data is never partially written.

## Alternatives considered

| Option | Why not (here) |
|---|---|
| LangSmith / Langfuse | Good products; a hosted dependency for core audit data; the in-database trace keeps everything next to the message and under the same access control. Langfuse was later added *alongside* these signals, not instead of them: see [ADR-0040](0040-langfuse-llm-observability.md) |
| Logs only | Hard to aggregate cost and latency; no structure for the UI |
| Compute cost from a local price table | Goes stale; routers bill at the serving model's price, which only the provider knows |

## Consequences

- **Positive:** every answer is explainable and priced; operators can see cache, guardrail and tool health at a glance.
- **Negative / open:**
  - Prometheus counters are **per process**, so the API runs **one uvicorn worker per container** and scales by replicas;
  - `/metrics` is **unauthenticated**: keep it on an internal network;
  - calls record the served model only (no `requested_model` yet);
  - the trace table grows per turn and needs a retention policy in production.

## Interview soundbite

> "Every turn stores a per-node trace, the 'why', and every model call is a row in a cost ledger with tokens, cached tokens, latency and the provider-reported cost, the 'how much'. Prometheus covers rates like cache hits and guardrail blocks, and OpenTelemetry gives a span per graph node and per LLM call. Members see the plan and verdict; the full trace and generated SQL are admin-only."
