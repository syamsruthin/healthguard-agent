# Architecture decision records

Every significant architecture decision in HealthGuard, with **the context that forced it, what was decided, why, the alternatives that were rejected, and the consequences** (including the honest downsides). Each record ends with a two-to-four-sentence "interview soundbite".

- New to the project? Read the [architecture overview](../architecture.md) (diagrams) first, then the records in the order below.
- Preparing to present the project? Use the [interview guide](../interview/INTERVIEW_GUIDE.md).
- Status, measurements and open items: [IMPLEMENTATION_STATUS.md](../IMPLEMENTATION_STATUS.md).

## Format

Each record follows the same template: **Status** (with the commit that introduced it) · **Context** · **Decision** · **Why** · **Alternatives considered** · **Consequences** · **Interview soundbite**. Records are written after the fact from the code, the commit history and live measurements; numbers quoted are measured unless marked as estimates.

## Decision log

### Foundations

| # | Decision | The one-line why |
|---|---|---|
| [0001](0001-rebuild-prototype-as-production-system.md) | Rebuild the Groq + SQLite + ChromaDB prototype as a production-style system | Isolation had to move from the prompt into the database, and cost and quality had to become measurable |
| [0002](0002-openrouter-openai-compatible-gateway.md) | OpenRouter behind one OpenAI-compatible client | One choke point for fallbacks, caching, cost, tracing and repair; models change by config |
| [0003](0003-role-based-model-registry.md) | Route by role (FAST / CODE / SYNTH / JUDGE), each with cross-provider fallbacks | Right-size each job; name every call so cost is attributable |
| [0004](0004-cost-optimising-router-default.md) | A cost-optimising router as the default; judges on fixed models | Halved cost per turn; a routed jury would stop being independent |
| [0005](0005-postgres-pgvector-single-system-of-record.md) | One Postgres 17 + pgvector with three schemas (`app`, `insurance`, `rag`) | RLS, roles, hybrid search and single-transaction audit live in one engine |
| [0006](0006-redis-shared-fast-state.md) | Redis 8 for caches, locks, rate limits and the queue | Shared hot state across replicas; every use fails safe |

### Agent

| # | Decision | The one-line why |
|---|---|---|
| [0007](0007-langgraph-explicit-state-machine.md) | LangGraph state machine with deterministic routing (not an open ReAct loop) | Bounded cost and latency; every answer passes the same checkpoints; testable |
| [0008](0008-single-understand-call.md) | One structured call for scope, safety, rewrite and plan | The four decisions share context; one call instead of three on the critical path |
| [0009](0009-plans-are-untrusted-sanitize-dag.md) | Sanitize the model's plan into a DAG | "Depend only on earlier steps" makes cycles impossible in one pass |
| [0010](0010-parallel-execution-inside-one-node.md) | Run dependency levels with `asyncio.gather` inside one node | Parallelism without reducers or cross-turn state leaks |
| [0011](0011-code-generating-tools-self-correction.md) | Text-to-SQL and text-to-pandas with one shared self-correction loop | Open-ended questions; errors fed back; outages stop at once |
| [0012](0012-verify-groundedness-completeness.md) | Verify every answer; one rewrite, one replan | A narrow second check beats self-policing; loops stay bounded |
| [0013](0013-error-coded-fallbacks.md) | Error codes and fixed fallback messages | "Model down" ≠ "no data" ≠ "couldn't verify"; never lose a good answer to a late failure |
| [0014](0014-tagged-sources-and-citations.md) | `[S#]` data and `[D#]` document citations | Writer, verifier and member all check against the same evidence |
| [0015](0015-per-turn-state-reset-checkpointer.md) | Reset state every turn; keep the Postgres checkpointer as an audit copy | No cross-turn leaks; history is queryable in `app.messages` |

### Security and privacy

| # | Decision | The one-line why |
|---|---|---|
| [0016](0016-defence-in-depth-for-generated-sql.md) | Four layers for generated SQL: AST guard → read-only role → read-only transaction → RLS | Any one layer failing still leaves three |
| [0017](0017-row-level-security-isolation-boundary.md) | Row-Level Security keyed to a server-set member id | Isolation by construction, even for SQL without a `WHERE` |
| [0018](0018-process-sandbox-for-pandas.md) | AST checks + restricted builtins + spawned process pool + timeouts for pandas | Contain CPU, memory and crashes; never `exec` in the API process |
| [0019](0019-layered-input-guardrails.md) | Regex prefilter, then LLM classification | Free blocks for literal attacks; nuance with context; not the security boundary |
| [0020](0020-jwt-and-rotating-refresh-tokens.md) | 15-min JWTs with no PII; rotating, hashed refresh-token families | Revocable sessions and stolen-token detection |
| [0021](0021-data-minimisation-and-pii.md) | Minimise PII per surface (tokens, logs, traces, memory, cache) | Every copy of PII is a liability |

### Retrieval and data

| # | Decision | The one-line why |
|---|---|---|
| [0022](0022-hybrid-retrieval-rrf.md) | pgvector + Postgres full-text fused with RRF; SBCs filtered to the member's plan | Semantics and exact codes; no score calibration |
| [0023](0023-structure-aware-chunking-versioned-ingest.md) | Heading-aware chunks with section paths; table headers repeated; hash-based idempotent ingest; data version | Self-describing chunks; cheap re-ingest; no stale cached answers |
| [0024](0024-deterministic-synthetic-data-single-source.md) | Deterministic synthetic data from one reference module and a real claims engine | SQL and documents never contradict; stable evaluation ground truth |

### Caching and cost

| # | Decision | The one-line why |
|---|---|---|
| [0025](0025-four-cache-layers.md) | Four cache layers with their own keys and safety rules | Different granularities catch different repetition, without leaks |
| [0026](0026-scoped-semantic-answer-cache.md) | Scoped semantic answer cache, looked up before any model call for opening questions | Rewrites drift; normalised text doesn't; only verified answers are replayed |
| [0027](0027-exact-llm-cache-validated-outputs.md) | Exact cache that forgets invalid outputs | A cache must hold validated outputs, not just outputs |
| [0028](0028-provider-prompt-caching.md) | Static-first prompts with cache breakpoints | 54–60 % of planner/SQL input tokens from cache; −40 % on repeats |
| [0029](0029-spend-guards-gated-background-work.md) | Daily caps, gated background calls, sampled judging, one eval at a time | Spend scales with useful work; every open door has a ceiling |

### Runtime

| # | Decision | The one-line why |
|---|---|---|
| [0030](0030-fastapi-sse-decoupled-turn.md) | FastAPI + SSE; the turn runs in a background task feeding a queue | A closed tab never loses the answer, trace or cost |
| [0031](0031-turn-lock-and-deadline.md) | One turn per conversation: owned lock, compare-and-delete, 150 s deadline | A slow turn can't release someone else's lock; every turn ends |
| [0032](0032-background-worker-enqueue-seam.md) | arq worker behind an enqueue interface | The member waits only for the answer; chat works without the worker |
| [0033](0033-three-tier-memory.md) | Token-budgeted window, rolling summary, privacy-filtered preferences | Bounded context cost; memory shapes tone, never facts |

### Quality and operations

| # | Decision | The one-line why |
|---|---|---|
| [0034](0034-evaluation-strategy.md) | Golden set from the DB, deterministic checks, three-family RAGAS jury, online sampling | Isolation is proven; quality is a gated number |
| [0035](0035-observability.md) | Per-turn traces, per-call cost ledger, Prometheus, OpenTelemetry | Every answer is explainable and priced |
| [0036](0036-testing-strategy.md) | Scripted FakeLLM and fakes at every seam; paid suites opt-in | ~250 free tests per push, including outage paths |
| [0037](0037-packaging-and-deployment.md) | One non-root image, one process per container, production config guards | Reproducible, race-free start-up; insecure configs can't ship |
| [0038](0038-streamlit-thin-client.md) | Streamlit strictly over the public API | Proportionate UI effort; replaceable without backend changes |

## Cross-cutting principles

These show up in many records; they are the real "architecture":

1. **Models propose, code disposes.** Models plan, write code and judge; deterministic code controls flow, limits, identity and safety ([0007](0007-langgraph-explicit-state-machine.md), [0009](0009-plans-are-untrusted-sanitize-dag.md), [0016](0016-defence-in-depth-for-generated-sql.md)).
2. **Every model output is untrusted input.** Plans are sanitized, SQL is guarded, pandas is sandboxed, answers are verified, memories are filtered ([0009](0009-plans-are-untrusted-sanitize-dag.md), [0016](0016-defence-in-depth-for-generated-sql.md), [0018](0018-process-sandbox-for-pandas.md), [0012](0012-verify-groundedness-completeness.md), [0033](0033-three-tier-memory.md)).
3. **Security lives below the model.** Guardrails reduce noise; the database role, RLS and the process sandbox are the boundary ([0017](0017-row-level-security-isolation-boundary.md), [0019](0019-layered-input-guardrails.md)).
4. **Cheap deterministic checks before expensive model calls.** Prefilter before planning, cache before planning, preference cue before memory extraction ([0019](0019-layered-input-guardrails.md), [0026](0026-scoped-semantic-answer-cache.md), [0029](0029-spend-guards-gated-background-work.md)).
5. **Bound everything.** Plan steps, retries, rewrites, replans, step and turn deadlines, rows, rate limits, daily turns ([0007](0007-langgraph-explicit-state-machine.md), [0031](0031-turn-lock-and-deadline.md)).
6. **Degrade, don't fail.** Fallback models, error-coded fallbacks, optional caches, fail-open verifier for the user and fail-closed for the cache ([0013](0013-error-coded-fallbacks.md)).
7. **Measure before claiming.** Every call is priced, every turn is traced, quality is gated, and open gaps are written down ([0034](0034-evaluation-strategy.md), [0035](0035-observability.md)).

## Adding a record

Copy any record, take the next number, fill in every section (an empty "Alternatives considered" usually means the decision wasn't really a decision), link it from this table, and reference the commit that implements it.
