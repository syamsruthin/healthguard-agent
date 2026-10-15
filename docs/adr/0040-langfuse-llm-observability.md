# ADR-0040: Langfuse for LLM observability, keyed by the app's own trace id

- **Status:** Accepted (branch `feature/langfuse-observability`)
- **Area:** Operations
- **Extends:** [ADR-0035](0035-observability.md), which listed Langfuse as "easy to add as an OTel exporter later"

## Context

ADR-0035 gave us a per-turn trace in Postgres, a cost ledger, Prometheus and OpenTelemetry. Those answer "why" and "how much" for one turn at a time, through the admin UI. Debugging the agent at scale needs more:

- browsing and filtering turns by outcome, user, conversation, model or cost;
- seeing a turn as a tree of model calls with exact prompts, outputs, served model, prompt-cache reads and cost per call;
- joining quality signals to that tree: the verifier's verdict, user thumbs, the online jury and offline RAGAS scores;
- seeing the background jobs (titles, summaries, memory extraction, online evals) a turn caused.

Jaeger shows timings, not prompts or scores. Building those views in Streamlit would mean writing an LLM observability product ourselves.

## Decision

Add **Langfuse** (Python SDK v5, OpenTelemetry-based), enabled when `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set, with Langfuse Cloud as the default target. Everything lives in [core/observability.py](../../backend/core/observability.py).

| Concern | Choice |
|---|---|
| Trace identity | A turn's Langfuse trace id **is** its `trace_id` UUID (hex), the same id as `app.traces.id` and `messages.trace_id`. Feedback, the online jury and offline evals attach scores with no lookup table |
| Trace attributes | `propagate_attributes`: user = user UUID (never email), session = conversation id, tags = `chat` / `member\|admin` / env (+ `eval`, `eval-run:<id>`, `job`), version = app version + prompt version |
| Tree | `chat-turn` (agent) → `load-context` (→ `memory.search`) → one observation per graph node (`cache_first`/`cache_lookup` retriever, `understand` agent, `execute` chain, `verify` evaluator, …) → tool steps (tool) → codegen attempts → `sql.guard` (guardrail) / `sql.execute` / `sandbox.run`; `hybrid_search` → `vector_search` / `keyword_search` / `rrf_fusion`; `guardrail.prefilter`, `guardrail.llm`; `persist`, `semantic_cache.store`, `followups.enqueued` |
| Model calls | One **generation** per call (`llm.<call name>`): messages, output, requested vs served model, parameters, usage split into `input` / `input_cache_read` / `input_cache_creation` / `output`, and **OpenRouter's reported cost** (Langfuse's price table can't price routers). Exact-cache hits, schema repairs, prompt-only JSON fallbacks and time to first token (streams) are recorded |
| Node parity | The `@traced` decorator copies each node's `step()` (input, output, reasoning, metadata) onto its observation, so Langfuse and `app.traces` can't disagree |
| Scores | Per turn: `outcome`, `fallback` (categorical), `cache_hit`, `grounded`, `complete` (boolean), `replans`, `revisions`. Plus `user_feedback` (thumbs), `online_faithfulness` / `online_relevancy` (jury mean, per-judge values in metadata), and offline `ragas_<metric>` / `check_<name>` |
| Background and evals | Each worker job is its own trace (`job:<name>`) in the conversation's session. Offline eval judging is a second root (`eval:<case>`) inside the judged turn's trace; RAGAS judge calls are recorded from the existing `SpendMeter` response hook |
| Privacy | Inputs, outputs and metadata go through the log PII masker ([ADR-0021](0021-data-minimisation-and-pii.md)) and are cut to `LANGFUSE_MAX_FIELD_CHARS`; `mask_otel_spans` re-masks at export. `LANGFUSE_CAPTURE_IO=false` sends metadata, usage and scores only |
| Isolation | Own `TracerProvider`, exporting Langfuse spans only. The observation tree is tracked in a contextvar and children start on their parent explicitly, so the OTel context and Jaeger are untouched and neither orphans the other |
| Failure | Every SDK call is guarded; a Langfuse outage loses telemetry, never a turn. No keys = every helper is a no-op |

## Why

- **One id across systems.** Using our UUID as the Langfuse trace id makes the database trace, the admin view, the Langfuse UI and every late-arriving score point at the same turn.
- **Instrument our client, not the SDK.** `langfuse.openai` patches the OpenAI SDK process-wide, which would double-trace our calls (and RAGAS's), and it can't see OpenRouter's cost, the requested-vs-served model or our exact-cache hits. Our client already has all of that.
- **Explicit parents beat the ambient OTel context.** With Jaeger on, the current OTel span is often an `agent.*` or FastAPI span that Langfuse never receives; parenting on it would orphan Langfuse observations. An explicit parent pointer is also safe across `asyncio.gather` (contexts are copied per task) and across `yield` in the streaming generator.
- **Masked by default.** This is health-insurance data: claims, member ids and dates. The same masker as the logs keeps one policy, and the metadata-only mode exists for production sign-off.

## Alternatives considered

| Option | Why not |
|---|---|
| `@observe` decorators everywhere | Captures raw function arguments (state dicts with the user profile); harder to mask and to keep payloads small |
| `langfuse.openai` drop-in | Global monkey-patch; double tracing; no OpenRouter cost / served model / cache hits |
| LangChain/LangGraph callback handler | We don't use LangChain runnables; the graph's own nodes are the useful unit, and the handler would trace LangGraph internals |
| Self-hosted Langfuse in Compose | Six more services (web, worker, ClickHouse, MinIO, Redis, Postgres). Cloud first; self-hosting needs only `LANGFUSE_BASE_URL` |
| Export the existing OTel spans to Langfuse | They carry no prompts, outputs or scores; the tree would be timings only |

## Consequences

- **Positive:** every turn, job and eval case can be browsed, filtered and compared in Langfuse with prompts, costs and quality scores side by side; admins get an "Open in Langfuse" link per answer.
- **Negative / open:**
  - turn content leaves the deployment (masked) when using Langfuse Cloud; use `LANGFUSE_CAPTURE_IO=false` or self-host where that isn't acceptable;
  - masking is pattern-based: it catches ids, emails, phones, SSNs and ISO dates, not free-text clinical details (the same limit as the logs);
  - masking ISO dates also hides claim service dates in traced SQL results;
  - the admin trace link needs the project id, fetched once per process with a blocking call (run in a thread).

## Interview soundbite

> "Every turn is a Langfuse trace whose id is our own trace UUID, so the database trace, the admin view and every score (thumbs, the verifier, the online jury and offline RAGAS) all point at the same turn. Generations carry OpenRouter's real cost and prompt-cache reads, PII is masked before export, and we track parents ourselves so Jaeger and Langfuse never interfere."
