# HealthGuard: agentic RAG for health-insurance members

HealthGuard is an AI assistant for members of a (synthetic) US health insurer. It answers questions about plans, benefits, claims, prescriptions, prior authorizations, in-network care and policy rules. For each question it:

1. **plans** which data sources it needs,
2. **queries them safely**: SQL over the member's own records, pandas over the provider directory, and hybrid search over policy documents,
3. **writes an answer** in which every fact cites its source, and
4. **verifies** that answer before the member sees it.

Every turn streams live to the browser. It is stored with a full trace and its exact cost, and it can never expose another member's data. Repeated questions are answered from a verified cache without calling a model, and spend guards keep the bill predictable.

```text
Member (Maria): Why was my knee MRI in April denied, and how can I appeal it?

HealthGuard:   Your April 6, 2026 knee MRI at Austin Imaging Center (claim CLM-2026-0000005) was
               denied with code D002 because prior authorization was not obtained [S1]. The center
               is in network, so you owe $0.00 for this claim [S1][D3].
               Options: ask the provider for a retrospective authorization review within 30 days of
               the denial [D1], or file a Level 1 appeal within 180 calendar days of the date on
               your EOB [D2] — in the member portal, by mail, or by phone at 1-800-442-7732 [D2] …
               ───────────────────────────────────────────────────────────────────────────────
               Plan: s1 sql (denied knee MRI claim + denial code) ∥ s2 policy_search (appeals, D002)
               Verified: grounded ✓ complete ✓  ·  $0.009–0.017 per turn on the default Jev Router
```

> All member data in this project is synthetic and generated deterministically. The demo personas are fictional.

---

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [How a question is answered](#how-a-question-is-answered)
- [Tech stack](#tech-stack)
- [Getting started](#getting-started)
- [Demo accounts](#demo-accounts)
- [Using HealthGuard](#using-healthguard): web UI, terminal CLI, HTTP API
- [Configuration](#configuration)
- [Synthetic data](#synthetic-data)
- [Security and privacy](#security-and-privacy)
- [Caching and cost](#caching-and-cost)
- [Memory](#memory)
- [Observability](#observability)
- [Testing](#testing)
- [Evaluation](#evaluation)
- [Project structure](#project-structure)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Further documentation](#further-documentation)

---

## Features

**Agent**
- LangGraph graph with these nodes: cache-first lookup → understand → (follow-up cache lookup) → execute → synthesize → verify, plus replan and rewrite.
- An opening question that was already answered (and verified) is served from the semantic cache **before any model call**.
- One fast call does four jobs: classifies scope and safety, resolves references ("that plan", "it"), rewrites the question so it stands alone, and produces a plan of dependent steps.
- Independent steps run in parallel. Dependent steps receive their prerequisites' output.
- Each tool retries itself: SQL and pandas errors are sent back to the model so it can fix the code.
- The answer is checked for **groundedness** and **completeness**:
  - An unsupported claim triggers one rewrite. If the rewrite also fails, the answer is replaced with an honest fallback.
  - An incomplete answer triggers one replan.
- Error-coded fallbacks keep the two failure cases distinct: "the model is unavailable" vs "no data found".

**Data tools**
- **`sql`:** Postgres over plans, benefits, the formulary, claims, pharmacy claims, prior authorizations and accumulators. Three layers keep it safe:
  - a static SQL guard,
  - a read-only database role,
  - Row-Level Security.
- **`providers`:** pandas over 5k providers, facilities, pharmacies and zip centroids. Generated code runs in a sandboxed process pool.
- **`policy_search`:** hybrid retrieval over 60 policy documents, combining pgvector cosine search and Postgres full-text search with reciprocal rank fusion.

**Product**
- JWT authentication with rotating refresh tokens (reuse revokes the whole token family) and member/admin roles.
- Conversations with history, titles and soft delete.
- Streaming answers over Server-Sent Events (SSE), with live plan and tool progress.
- Citations, feedback (👍/👎), a coverage card, and memory controls.
- "Show how answers are produced": members see the plan, steps and verification verdict; admins also see the full trace and the generated SQL / pandas code.
- A multipage Streamlit UI with admin dashboards for observability and evaluations.
- A background worker (arq) for titles, rolling summaries, long-term memories and online answer scoring.

**Cost and quality**
- Per-role models through OpenRouter, defaulting to the cost-optimising Jev Router with fixed-model fallbacks.
- Spend guards: a per-member daily turn cap, background model calls only when they have work to do, 5 % online-eval sampling, one paid evaluation run at a time, and judge spend reported per run.
- Four cache layers:
  - provider prompt caching,
  - a user-scoped semantic answer cache,
  - an exact LLM cache,
  - an embedding cache.
- Golden evaluation set built from the database.
- `hg-eval` combines deterministic checks with a **RAGAS judge jury** (GPT, Gemini and Claude), a quality gate and reports.
- About 250 automated tests, a live persona suite, and CI with a coverage gate.

---

## Architecture

```mermaid
flowchart LR
    U([Browser]) --> UI["Streamlit UI"]
    UI -- "REST + SSE (JWT)" --> API["FastAPI /api/v1"]
    API --> G["LangGraph agent"]
    API -- jobs --> Q[(Redis · arq)]
    Q --> W["arq worker"]
    G --> LLM["LLM layer"] --> OR([OpenRouter])
    G --> SQL["sql tool"] --> PG[("Postgres 17 + pgvector")]
    G --> RAG["policy_search"] --> PG
    G --> PD["providers tool"] --> CSV[("CSV data lake")]
    G <--> SC[("Redis 8 semantic cache")]
    API --> PG
    W --> PG
```

```mermaid
flowchart TD
    S((start)) -- "opening question" --> CF{"cache_first<br/>no model call"}
    S -- "follow-up" --> U
    CF -- hit --> F
    CF -- miss --> U["understand<br/>prefilter + scope/safety + rewrite + plan"]
    U -- "blocked" --> F
    U -- "small talk" --> SY
    U -- "plan (opening question)" --> X
    U -- "plan (follow-up)" --> C{"cache_lookup<br/>on the rewrite"}
    C -- hit --> F
    C -- miss --> X["execute<br/>parallel dependency levels"]
    X --> SY["synthesize<br/>streamed, cited"]
    SY --> V{"verify"}
    V -- "ungrounded (once)" --> SY
    V -- "incomplete" --> R["replan"] --> U
    V -- ok --> F["finalize"]
```

[docs/architecture.md](docs/architecture.md) has all seven diagrams: system, agent graph, streaming sequence, data model, caching layers, evaluation pipeline and security boundaries.

---

## How a question is answered

1. **API.** The request is authenticated (JWT) and rate-limited, and conversation ownership is checked.
   - A per-conversation lock blocks concurrent turns.
   - The user message is stored before anything else happens.
2. **Turn task.** A background task runs the turn and feeds an event queue that the SSE response streams. If the browser disconnects, the answer is still saved. A turn is bounded by `TURN_TIMEOUT_SECONDS`.
3. **Context.** The task loads the member profile and dependents, the recent conversation turns (within a token budget), the rolling summary, and relevant long-term preferences.
4. **`cache_first`** (opening questions only). The question is self-contained, so its normalised text is looked up in the semantic cache before any model call: the member's own answers first, then public ones. A hit ends the turn here.
5. **`understand`.**
   - A deterministic prefilter blocks prompt-injection patterns and other members' IDs without calling a model.
   - One structured call to the fast model decides scope and safety, rewrites the question, and plans up to four tool steps.
   - `planning.sanitize_plan` repairs whatever the model returns.
   - For a follow-up, **`cache_lookup`** then checks the cache on the standalone rewrite.
6. **`execute`.** Runs the plan level by level; steps within a level run concurrently.
7. **`synthesize`.** Streams the answer token by token, citing `[S#]` for data and `[D#]` for documents.
8. **`verify`.** Decides whether the answer is grounded and complete, then routes it to a rewrite, a replan or `finalize`.
9. **Persistence.** The assistant message, the trace (every node's input, output, reasoning and timing) and every LLM call (tokens, cached tokens, cost) are saved in one transaction. Members receive the plan, steps and verdict; admins also receive the full trace.
10. **Follow-ups.**
    - Eligible answers are stored in the semantic cache.
    - Worker jobs are queued only when they have work: a title on the first turn, a summary when history overflows the window, memory extraction when the user states a preference, and online evaluation for a 5 % sample.

---

## Tech stack

| Layer | Technology |
|---|---|
| Language and tooling | Python 3.12, [uv](https://docs.astral.sh/uv/), ruff, mypy, pytest |
| LLM gateway | [OpenRouter](https://openrouter.ai) (OpenAI-compatible); `openai` SDK |
| Agent | LangGraph 1.2; `AsyncPostgresSaver` checkpointer |
| API | FastAPI, sse-starlette, pydantic-settings, PyJWT, pwdlib (argon2id) |
| Database | Postgres 17 with pgvector (HNSW); SQLAlchemy 2 (async) and asyncpg; Alembic |
| SQL safety | sqlglot (AST guard); dedicated read-only role; Row-Level Security |
| Cache and queue | Redis 8 (Query Engine for vector search) via RedisVL; arq |
| Data | pandas, NumPy, Faker (synthetic generation) |
| UI | Streamlit 1.65 (`st.navigation`), Plotly |
| Evaluation | RAGAS 0.4.3 (collections metrics) |
| Observability | structlog (PII masking), Prometheus, OpenTelemetry (OTLP / Jaeger), Langfuse (SDK v5) |
| Delivery | Docker (multi-stage, non-root), Docker Compose, GitHub Actions |

---

## Getting started

### Prerequisites

- **Docker** (Docker Desktop on macOS or Windows), for Postgres and Redis.
- **uv ≥ 0.8.** Check with `uv --version`. Older versions (for example the one bundled with some Anaconda installs) rewrite `uv.lock`.
- **An OpenRouter API key** with a little credit. A typical question costs about $0.01–0.02 on the default Jev Router; a repeated opening question is free. Unit and integration tests make no model calls.

### Option A: local development

```bash
git clone https://github.com/syamsruthin/healthguard-agent.git
cd healthguard-agent
cp .env.example .env
```

Edit `.env` and set at least `OPENROUTER_API_KEY=sk-or-v1-...` and a random `JWT_SECRET`. To generate a secret, run the command below and paste its output into `.env`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

> If another Postgres already listens on port 5432, add `POSTGRES_HOST_PORT=5433` and change `DATABASE_URL` to `...@localhost:5433/healthguard`.

```bash
uv sync --all-groups        # install the locked dependencies (incl. dev tools)
make infra                  # start Postgres (pgvector) + Redis 8 in Docker
make data                   # alembic upgrade → hg-seed → hg-ingest
make models-check           # optional: verify the configured model IDs exist on OpenRouter
```

`make data` does three things:
- creates the `app`, `insurance` and `rag` schemas, the read-only agent role and the RLS policies;
- loads the synthetic dataset and demo users, which takes about 3 seconds;
- embeds the 60-document policy corpus, which takes about 2 minutes and costs less than $0.01 with OpenRouter embeddings.

Start the three processes, each in its own terminal:

```bash
make api       # FastAPI on http://localhost:8000  (interactive docs at /docs)
make worker    # arq background worker
make ui        # Streamlit on http://localhost:8501
```

### Option B: everything in Docker

```bash
cp .env.example .env        # set OPENROUTER_API_KEY and JWT_SECRET
docker compose up -d --build
```

Compose starts these services:

| Service | What it runs |
|---|---|
| `postgres` | pgvector/pg17 |
| `redis` | redis:8 |
| `migrate` | one-shot job: migrations → seed → ingest |
| `api` | FastAPI on :8000 |
| `worker` | arq worker |
| `ui` | Streamlit on :8501 |

Add `--profile observability` to also start Jaeger, with its UI at http://localhost:16686. Stop the stack with `docker compose down`.

### Try it without the API

```bash
uv run hg-chat --as maria.garcia@demo.healthguard.dev "Why was my knee MRI in April denied?"
```

This prints the live plan, tool results and streamed answer, followed by a per-call table of tokens, prompt-cache hits and cost.

---

## Demo accounts

The password for every account is the value of `DEMO_PASSWORD` (default `HealthGuard!2026`). For convenience, the UI's **Demo accounts** tab signs in with one click when `DEMO_PASSWORD` is set for the UI process.

| Login | Member | Plan | Story to explore |
|---|---|---|---|
| `maria.garcia@demo.healthguard.dev` | M100001, Austin TX | Choice PPO Silver Plus | April knee MRI **denied D002** (no prior auth, $0 owed); knee arthroscopy prior auth pending; $2,700 of $3,400 deductible met |
| `james.chen@demo.healthguard.dev` | M100002, Seattle WA | Core HMO Gold Standard | Type 2 diabetes; Ozempic needs prior auth and step therapy; dermatology visit **denied D010** (no referral) |
| `priya.patel@demo.healthguard.dev` | M100003, Brooklyn NY | Saver HDHP Bronze Plus | Pregnant (due Nov 2026); prenatal care $0; HSA balance $2,150 |
| `robert.johnson@demo.healthguard.dev` | M100004, Miami FL | Choice PPO Silver Standard | Out-of-network ER covered under the No Surprises Act; **out-of-pocket max reached** ($8,500); duplicate claim **D004** |
| `aisha.okafor@demo.healthguard.dev` | M100005, Chicago IL | Core HMO Catastrophic | 3 free primary care visits; telehealth therapy; a pending claim |
| `david.kim@demo.healthguard.dev` | M100006 + spouse + 2 children, Denver CO | Select EPO Gold Plus | Family plan; out-of-network urgent care **denied D009**; family accumulators |
| `admin@demo.healthguard.dev` | — | — | Portfolio analytics across all members; admin dashboards |

[DEMO_QUESTIONS.md](DEMO_QUESTIONS.md) has a scripted demo for each persona, including guardrail checks.

---

## Using HealthGuard

### Web UI (http://localhost:8501)

| Page | What you can do |
|---|---|
| **Chat** | Persona quick prompts and a conversation list. Answers stream with live status ("Looking up the information…"), the plan and each tool step. Each answer shows outcome, latency, cost and prompt-cache badges, an expandable list of sources, and a thumbs-up/down widget. **Show how answers are produced** reveals the plan, steps, verification verdict and trace. |
| **My coverage** | Plan summary, deductible and out-of-pocket progress bars (individual and family), what you pay per service, family members, and claims activity |
| **Memory** | Toggle long-term memory; review, delete or clear everything HealthGuard remembers about you |
| **Observability** (admin) | Turns, latency p50/p95, cost, prompt-cache ratio, semantic-cache hit rate, guardrail blocks, feedback, per-day charts, model calls, outcomes, tools, and online-eval jury scores |
| **Evaluations** (admin) | Queue a golden-suite run (10 cases by default) and review runs, metrics and the quality gate |

### Terminal CLI

| Command | Purpose |
|---|---|
| `hg-chat --as EMAIL "q1" "q2" …` | Run one or more turns in-process as a persona (`--repeat N` repeats the conversation to measure prompt caching; `-v` adds node status) |
| `hg-seed [--scale full\|small]` | Regenerate and load the synthetic dataset, corpus and demo users (deterministic) |
| `hg-ingest [--force]` | Chunk and embed the corpus. Only changed documents are re-embedded unless you pass `--force` |
| `hg-models-check` | Check that every configured model ID exists on OpenRouter |
| `hg-build-golden` | Rebuild the golden evaluation set from the seeded database |
| `hg-eval …` | Run evaluations (see [Evaluation](#evaluation)) |

### HTTP API

Interactive OpenAPI docs are at http://localhost:8000/docs (disabled when `ENV=production`). All routes below are under `/api/v1` except the health checks and metrics.

| Method and path | Purpose |
|---|---|
| `POST /auth/register` | Create an account (never linked to member records) and return tokens |
| `POST /auth/login` | Return `access_token` (15 min), a rotating `refresh_token` and the user |
| `POST /auth/refresh` | Rotate the refresh token. Replaying an old token revokes the whole family |
| `POST /auth/logout` · `GET /auth/me` | Revoke the token family · current user |
| `POST /conversations` · `GET /conversations` | Create · list your conversations |
| `PATCH /conversations/{id}` · `DELETE /conversations/{id}` | Rename · soft-delete (also deletes agent checkpoints) |
| `GET /conversations/{id}/messages` | History with citations, tools, usage and metadata |
| `POST /conversations/{id}/messages` | Ask a question and get the whole answer as JSON |
| `POST /conversations/{id}/messages/stream` | Ask a question and stream the answer (SSE) |
| `POST /messages/{id}/feedback` | `{"rating": 1 \| -1, "comment"?}`. A thumbs-down queues an online evaluation |
| `GET /me/coverage` | The member's coverage card (RLS-scoped) |
| `GET /me/memories` · `DELETE /me/memories[/{id}]` · `PATCH /me/settings` | Memory controls (`{"memory_enabled": bool}`) |
| `GET /admin/metrics/summary?days=7` | Admin: latency, cost, caching, outcomes, model calls, tools, feedback, online eval |
| `GET /admin/traces/{trace_id}` | Admin: full per-node trace of a turn |
| `GET /admin/evals/runs` · `POST /admin/evals/run` | Admin: list evaluation runs · queue one (`{"limit": 10}`) |
| `GET /health/live` · `GET /health/ready` · `GET /metrics` | Liveness · readiness (Postgres + Redis) · Prometheus |

Errors share one shape: `{"error": {"code", "message", "request_id"}}`. Every response carries `X-Request-ID`.

**Streaming events.** Each SSE `event:` carries a JSON `data:` payload.

| Event | Payload |
|---|---|
| `meta` | `turn_id`, `conversation_id`, `user_message_id`, `trace_id` |
| `status` | `node`, `message` (for example "Looking up the information") |
| `plan` | `round`, `standalone_query`, `steps[{id, tool, task, depends_on}]` |
| `tool` | `step_id`, `tool`, `status` (`running`, `ok` or `error`), `row_count`, `duration_ms`, `attempts`, `error_code` |
| `token` | `text`: a chunk of the answer |
| `answer_reset` | The answer is being regenerated (`reason`: `replan` or `revision`) |
| `answer_replaced` | `answer`, `reason`: the final text replaces what was streamed |
| `citations` | `citations[{tag, kind, title, …}]` |
| `error` | `code` (`turn_failed`, `turn_timeout`, …), `message`, `retryable` (always followed by `final`) |
| `final` | `message_id`, `answer`, `outcome`, `fallback`, `cache_hit`, `plan`, `tools`, `citations`, `verification`, `latency_ms`, `usage`, `trace_id`, `trace` (full trace and generated code for admins only; members get an empty `trace` and no code) |

```bash
B=http://localhost:8000/api/v1
TOKEN=$(curl -s -X POST $B/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"robert.johnson@demo.healthguard.dev","password":"'"$DEMO_PASSWORD"'"}' | jq -r .access_token)
CID=$(curl -s -X POST $B/conversations -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{}' | jq -r .id)
curl -N -X POST $B/conversations/$CID/messages/stream -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"message":"Have I hit my out-of-pocket maximum?"}'
```

---

## Configuration

Settings are read from environment variables (or `.env`) by `backend/core/config.py`. Everything has a development default. With `ENV=production`, the app refuses to start with the default `JWT_SECRET` or without `OPENROUTER_API_KEY`.

**Infrastructure and app**

| Variable | Default | Notes |
|---|---|---|
| `ENV` | `development` | `development`, `test` or `production` |
| `DATABASE_URL` | `postgresql+asyncpg://healthguard:healthguard@localhost:5432/healthguard` | |
| `POSTGRES_HOST_PORT` | `5432` | Host port of the Compose Postgres |
| `REDIS_URL` | `redis://localhost:6379/0` | **Must use db 0**: Redis search indexes only exist there |
| `AGENT_DB_USER` / `AGENT_DB_PASSWORD` | `hg_agent_ro` / … | Read-only role created by the migration |
| `CORS_ORIGINS` | `http://localhost:8501` | Comma-separated |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `false` | JSON logs in containers |
| `BACKEND_URL` | `http://localhost:8000` | Used by the UI |

**Auth**

| Variable | Default |
|---|---|
| `JWT_SECRET` | dev placeholder (must be changed in production) |
| `ACCESS_TOKEN_TTL_MINUTES` / `REFRESH_TOKEN_TTL_DAYS` | `15` / `7` |
| `DEMO_PASSWORD` | `HealthGuard!2026` (seeded demo personas only) |
| `RATE_LIMIT_CHAT_PER_MINUTE` / `RATE_LIMIT_AUTH_PER_MINUTE` / `RATE_LIMIT_FEEDBACK_PER_MINUTE` | `20` / `10` / `30` |
| `DAILY_TURN_LIMIT` | `150` turns per member per UTC day (`0` = unlimited; admins exempt) |

**Models (OpenRouter)**

| Variable | Default | Used for |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Required for real answers |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | Any OpenAI-compatible endpoint works |
| `MODEL_FAST` (+ `_FALLBACKS`) | `typesafe/jev-router` (fallbacks: Claude Haiku 5.5, Gemini 3.8 Flash) | Understanding and planning, verification, memory, titles, summaries |
| `MODEL_CODE` (+ `_FALLBACKS`) | `typesafe/jev-router` (fallbacks: Claude Sonnet 5.5, GPT-5.4 mini) | SQL and pandas generation |
| `MODEL_SYNTH` (+ `_FALLBACKS`) | `typesafe/jev-router` (fallbacks: Claude Sonnet 5.5, Gemini 3.8 Flash) | The streamed answer |
| `EVAL_JUDGES` | `openai/gpt-5.4-mini, google/gemini-3.8-flash, anthropic/claude-haiku-5.5` | Offline RAGAS jury |
| `ONLINE_EVAL_JUDGES` / `ONLINE_EVAL_SAMPLE_RATE` | GPT + Gemini / `0.05` | Live answer scoring (every thumbs-down is judged too; each answer at most once) |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` / `EMBEDDING_DIM` | `openrouter` / `openai/text-embedding-3-small` / `1536` | `local` uses sentence-transformers (`uv sync --extra local-embeddings`); `hash` is for tests only |

`typesafe/jev-router` is TypeSafe's Jev Router on OpenRouter. It picks the model and reasoning effort for each request and bills at the price of the model that serves it, which makes it the cost-efficient default. Router-served calls get a 4,096-token output budget, because the reasoning tokens it may spend count against `max_tokens`. A structured-output repair goes to the first fixed fallback model. The judge juries stay on fixed models from different families, since a router could send every judge to the same model.

**Agent behaviour, memory and caching**

| Variable | Default | Effect |
|---|---|---|
| `MAX_PLAN_STEPS` | `4` | Tool steps per plan |
| `MAX_TOOL_RETRIES` | `2` | Self-correction attempts for SQL and pandas |
| `MAX_REPLANS` / `MAX_REVISIONS` | `1` / `1` | Extra planning rounds / rewrites without unsupported claims |
| `VERIFY_REPLACE_UNGROUNDED` | `true` | Replace answers that still fail verification after a rewrite (`false` keeps them and only logs the verdict) |
| `TURN_TIMEOUT_SECONDS` | `150` | Wall clock for a whole turn; the conversation lock lives 30 s longer |
| `TOOL_STEP_TIMEOUT_SECONDS` / `SANDBOX_TIMEOUT_SECONDS` / `SQL_STATEMENT_TIMEOUT_MS` | `90` / `10` / `5000` | Limits |
| `SQL_MAX_ROWS` / `RAG_TOP_K` / `RAG_RERANK` | `200` / `6` / `false` | Result sizes; optional LLM re-ranking of passages |
| `MAX_INPUT_CHARS` | `2000` | Longest accepted question |
| `HISTORY_TOKEN_BUDGET` / `SUMMARY_TRIGGER_TOKENS` | `2500` / `3000` | Verbatim conversation window / when to fold older turns into the summary |
| `LONG_TERM_MEMORY_TOP_K` / `LONG_TERM_MEMORY_MIN_SIMILARITY` | `5` / `0.35` | Memories added to each turn |
| `PROMPT_CACHE_ENABLED` / `LLM_EXACT_CACHE_ENABLED` | `true` / `true` | Provider prompt caching / exact-match cache for temperature-0 calls |
| `SEMANTIC_CACHE_ENABLED` / `SEMANTIC_CACHE_DISTANCE_THRESHOLD` | `true` / `0.08` | Scoped answer cache and its cosine-distance threshold |
| `SEMANTIC_CACHE_TTL_PUBLIC_SECONDS` / `SEMANTIC_CACHE_TTL_MEMBER_SECONDS` | `86400` / `3600` | |
| `JUDGE_DISAGREEMENT_THRESHOLD` | `0.3` | Flags eval cases where judges disagree |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | Enables OpenTelemetry export (for example `http://localhost:4318`) |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` | — / — / `https://cloud.langfuse.com` | Langfuse tracing, on when both keys are set (`LANGFUSE_ENABLED=false` forces it off) |
| `LANGFUSE_CAPTURE_IO` / `LANGFUSE_MAX_FIELD_CHARS` / `LANGFUSE_SAMPLE_RATE` | `true` / `6000` / `1.0` | Send masked prompts and outputs (or metadata only) / per-value size limit / share of traces exported |

---

## Synthetic data

`hg-seed` builds a deterministic dataset (seed 42), so answers and evaluation ground truth are reproducible. Claims are adjudicated by a real cost-sharing engine (`backend/data_gen/claims_engine.py`) that applies:
- copays, deductibles and coinsurance;
- the out-of-pocket cap and family accumulators;
- HMO referrals and EPO/HMO out-of-network denials;
- No Surprises Act emergencies;
- duplicate claims, and pending and in-review statuses.

| Dataset | Rows (full scale) |
|---|---|
| Plans: PPO / HMO / EPO / HDHP × metal tier × Value/Standard/Plus | 40 |
| Plan benefits (40 service categories per plan) | 1,600 |
| Formulary (real drug names, tiers, prior auth, step therapy, quantity limits) | 117 |
| Members (families with spouses and children) / accumulators | 1,504 / 1,504 |
| Medical claims / pharmacy claims / prior authorizations | 14,303 / 9,631 / 195 |
| Providers / facilities / pharmacies / zip centroids (CSV lake) | 5,016 / 442 / 433 / 240 |
| Policy corpus: 20 hand-written documents + 40 plan Summaries of Benefits and Coverage | 60 docs → 434 chunks |

Reference data, the claims engine, plan benefits and the documents share one source of truth (`backend/data_gen/reference.py`), so SQL answers and policy text agree. `hg-seed --scale small` builds a faster dataset with the same personas, which CI uses.

---

## Security and privacy

Defence in depth, from the request down to the data:

1. **Authentication.** argon2id passwords. Short-lived JWT access tokens that carry no PII (user id and role only). Refresh tokens are stored only as SHA-256 hashes and rotate in families; reusing one revokes the family. Login takes the same time whether or not the account exists.
2. **Request controls.** Rate limits per IP (auth) and per user (chat, feedback), a daily turn cap per member, a 64 KB body cap, security headers and a CORS allow-list. Conversation ownership is checked, and other users get **404**, never 403. Each conversation runs one turn at a time under a lock owned by that turn.
3. **Input guardrails.** A deterministic prefilter catches injection patterns and IDs of members other than the user or their dependents. The planner then classifies scope and safety: out of scope, prompt injection, other members' data, medical advice, or self-harm (which gets crisis resources).
4. **SQL.** The **sqlglot guard** allows one SELECT/CTE only, over `insurance` tables only, with a function allow-list and no `INTO`, `COPY`, `SET`, DML or DDL. Queries run as the **`hg_agent_ro`** role inside a **READ ONLY** transaction with a statement timeout; `set_config` is revoked. **Row-Level Security** then limits rows to the member (a subscriber also sees dependents; a spouse who is not the subscriber sees only their own). Admins see all rows; accounts with no member record see no member rows.
5. **pandas sandbox.** AST validation (no imports, dunders, I/O or `eval`/`query`/`getattr`), restricted builtins, separate spawned processes with a wall-clock limit, and an address-space limit on Linux.
6. **Output guardrails.** Answers are verified for groundedness and completeness, and every factual sentence carries a citation.
7. **Data minimisation.**
   - Long-term memory stores only non-clinical preferences; a deterministic filter rejects IDs, amounts, dates, contact details and clinical terms.
   - Logs mask emails, phone numbers, SSNs, dates and member IDs, including values nested in lists and objects.
   - Members never receive the generated SQL / pandas code or the full trace; admins read them through `/admin/traces`.
   - Self-registered accounts are never linked to member records.
8. **Operations.** `/metrics` (Prometheus) is unauthenticated: keep it on an internal network or block it at the proxy.

---

## Caching and cost

| Layer | What it saves | Safety |
|---|---|---|
| **Provider prompt cache** | Large stable prompt prefixes: planner ≈ 4.9k tokens, SQL ≈ 5.8k, plus the synthesis and verifier prefixes | Per provider. Anthropic and Gemini get explicit `cache_control` breakpoints; OpenAI-family models cache automatically |
| **Semantic answer cache** (Redis vector index) | Whole verified answers to repeated questions. Opening questions are looked up before any model call; follow-ups after planning, on the rewrite | Scoped `public` / `member:{id}` / `role:admin`. A personalised answer is never stored as public. Entries are tagged with the data version and prompt version |
| **Exact LLM cache** | Byte-identical temperature-0 calls | Keyed on model, messages and prompt version. Outputs that fail validation (bad JSON, rejected SQL or pandas) are dropped, so they are never replayed |
| **Embedding cache** | Query embeddings (7 days); the cache key and memory search share one embedding | — |

Background work is gated too: memory extraction runs only when the user states a preference, the summary job only when history overflows the window, online judging for 5 % of answers (each at most once), and members are capped at `DAILY_TURN_LIMIT` turns a day.

Measured on the live demo. The first five rows used fixed Claude models; the last row is the Jev Router default:

| Measurement | Result |
|---|---|
| Prompt tokens served from the provider cache | 54–60 % for the planner and SQL calls |
| Cost of a repeated turn | 40 % lower ($0.038 → $0.023) |
| Typical turn | $0.01–0.04; about 10 s average, 27 s p95 |
| A second member asking a general question already in the semantic cache | $0.0002 instead of about $0.01 (measured when planning ran first; an opening question now hits before any model call) |
| An out-of-scope question | blocked for $0.0002 |
| The same MRI-denial turn on the Jev Router default | $0.009–0.017, vs $0.023–0.039 on fixed Claude models; the router picked DeepSeek V4.1 Flash and Gemini 3.8 Flash |

---

## Memory

| Kind | How it works |
|---|---|
| **Conversation window** | Recent turns that fit `HISTORY_TOKEN_BUDGET`. Long assistant replies are truncated, and the current message is never duplicated. |
| **Rolling summary** | When turns outside the window exceed `SUMMARY_TRIGGER_TOKENS`, the worker folds them into `conversations.summary`. `summary_upto` ensures the window and the summary never overlap. |
| **Long-term memories** | Durable preferences (language, location, provider preferences, plan interests, communication style). Extracted by the fast model after a turn in which the user states a preference (a cheap keyword gate skips the call otherwise), filtered deterministically, de-duplicated (cosine > 0.9) and capped at 50 per user. Retrieved by similarity; users with no memories skip the lookup. Users control them on the **Memory** page or via `/me/memories`. |

---

## Observability

- **Per-turn trace.** Each graph node's input, output, reasoning and timing, plus the generated SQL and pandas code and the sources used. Stored in `app.traces`, shown in the chat UI, and available from `GET /admin/traces/{id}`.
- **Usage accounting.** Every LLM call is recorded in `app.llm_calls` with role, call name, model, tokens, cached and cache-write tokens, cost and latency. Background jobs are recorded too.
- **Prometheus** (`GET /metrics`):
  - `hg_llm_tokens_total`, `hg_llm_cost_usd_total`, `hg_llm_latency_seconds`, `hg_llm_errors_total`
  - `hg_cache_events_total{layer,outcome}`, `hg_tool_events_total`, `hg_guardrail_blocks_total`, `hg_turn_latency_seconds`
  - HTTP metrics
  - Run one uvicorn worker per container, because counters are per process.
- **OpenTelemetry.** Set `OTEL_EXPORTER_OTLP_ENDPOINT` to export spans for FastAPI, SQLAlchemy, httpx, each graph node (`agent.*`) and each LLM call (`llm.complete` / `llm.stream`, with model, tokens and cost). `docker compose --profile observability up -d` adds Jaeger.
- **Langfuse** ([ADR-0040](docs/adr/0040-langfuse-llm-observability.md)). Set `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` (and `LANGFUSE_BASE_URL` for a non-EU or self-hosted instance). Each turn is one trace whose id is the turn's `trace_id`, with user = user id and session = conversation:
  - `chat-turn` → `load-context` / `memory.search` → one observation per graph node → tool steps → codegen attempts → `sql.guard` / `sql.execute` / `sandbox.run`, retrieval stages (`hybrid_search`, `vector_search`, `keyword_search`, `rrf_fusion`), guardrails, semantic-cache lookups and stores, `persist`, enqueued follow-ups;
  - a generation per model call with messages, output, requested vs served model, prompt-cache reads / writes and OpenRouter's reported cost (time to first token for the streamed answer);
  - scores: `outcome`, `fallback`, `cache_hit`, `grounded`, `complete`, `replans`, `revisions`, `user_feedback` (thumbs), `online_faithfulness` / `online_relevancy` (jury), and `ragas_*` / `check_*` from `hg-eval`;
  - worker jobs are `job:<name>` traces in the same session; eval turns are tagged `eval` and `eval-run:<id>`;
  - inputs, outputs and metadata are PII-masked and size-limited; `LANGFUSE_CAPTURE_IO=false` sends metadata, usage and scores only. Admins get an **Open in Langfuse** link under each answer.
- **Structured logs.** structlog with request, user, conversation and trace IDs bound to every line; JSON in containers.

---

## Testing

| Command | Scope | Needs | Cost |
|---|---|---|---|
| `make test` | Unit tests | nothing (scripted `FakeLLM`, fakeredis) | free |
| `make test-integration` | Postgres + Redis tests | `make infra data` | free |
| `make lint` · `make typecheck` · `make format` | ruff and mypy | — | free |
| `make test-live` | One real turn per persona, plus refusal checks | `OPENROUTER_API_KEY` | ≈ $0.20 |

What the suites cover:
- **Unit tests:**
  - SQL guard (safe queries and 15 attack patterns) and sandbox escapes;
  - chunking, rank fusion, prompt-cache markers and usage accounting;
  - JWT handling, guardrails, plan sanitising and semantic-cache scoping;
  - the real OpenRouter client over a mocked HTTP transport;
  - graph flows: routing, the cache-first path, real concurrency, dependencies, fallbacks, replan and rewrite, event order, and state isolation between turns;
  - exact-cache validation (bad outputs are not replayed), turn-lock ownership, log masking;
  - memory filters and gating, eval checks, judge spend metering, SSE parsing and Streamlit pages (`AppTest`).
- **Integration tests:**
  - the RLS matrix and database-level blocks;
  - the auth flow (rotation and reuse detection) and rate limits;
  - conversation ownership, the SSE contract, and RLS applied to generated SQL;
  - persistence, the turn lock and deadline, member vs admin visibility, daily limits, feedback, the coverage card and admin endpoints;
  - memory and summarisation, worker tasks, the eval runner, the Postgres checkpointer, the Redis 8 vector cache, and data smoke tests.

**CI** (`.github/workflows/ci.yml`) runs on pushes to `main` and `claude/**` branches and on pull requests:
1. lint, format check, mypy;
2. unit tests;
3. migrate, small seed, hash-embedding ingest;
4. integration tests with a **combined coverage gate (78 %)**;
5. a Docker image build.

---

## Evaluation

The golden set (`backend/evals/datasets/golden.jsonl`, 59 cases) is built by `hg-build-golden` from the seeded data. Numeric ground truth comes from SQL and pandas queries; policy references quote the corpus. It covers ten categories:

| Category | Cases |
|---|---|
| Plan questions | 8 |
| Provider search | 6 |
| Policy RAG | 12 |
| Member-scoped (per persona) | 14 |
| Multi-hop | 4 |
| Memory follow-ups | 2 |
| Out of scope | 3 |
| Prompt injection | 3 |
| Cross-member probes | 3 |
| Admin analytics | 4 |

`hg-eval` runs each case as its persona through the **same chat pipeline as the API**. Evaluation conversations are flagged `is_eval` and the semantic cache is off. Each case gets:
- **deterministic checks:** answer rate, refusal accuracy, tool routing, data isolation (no other member's IDs) and required content;
- **RAGAS metrics**, scored by each judge in `EVAL_JUDGES`: faithfulness, answer relevancy and context recall by default; context precision and factual correctness are optional. The case score is the mean across judges, and the spread flags disagreement.

Results are checked against `backend/evals/thresholds.yaml` (exit code 1 on failure), stored in `eval_runs` / `eval_results`, and written as Markdown and JSON reports to `backend/evals/reports/`. Each run reports its agent cost and its judge cost (summed from the cost OpenRouter returns on every judge call). The admin **Evaluations** page queues one run at a time.

```bash
make eval-estimate LIMIT=10                     # plan + cost estimate, no model calls
uv run hg-eval --per-category 1                 # one case per category (~$0.30 with the 3-judge jury)
uv run hg-eval --limit 10 --judges 1            # cheapest real run (~$0.21)
uv run hg-eval --no-ragas                       # deterministic checks only
uv run hg-eval --rescore <RUN_ID>               # re-judge stored answers without re-running the agent
uv run hg-eval --metrics all --categories policy_rag,multi_hop
```

Judge responses are cached on disk (`backend/evals/.cache/`, one cache per judge), so re-scoring unchanged answers costs nothing. The `eval.yml` workflow runs **only on demand** (10 cases by default) and never on a schedule.

**Current baseline:** 10 cases, one per category, 3-judge jury, measured on the fixed Claude models before the Jev Router became the default.

| Metric | Score |
|---|---|
| Answer rate, refusal accuracy, routing, isolation | 1.0 |
| Required content | 0.86 |
| Context recall | 1.0 |
| Faithfulness | 0.80 ❌ (gate 0.85) |
| Answer relevancy | 0.68 ❌ (gate 0.80) |

The gap comes mainly from answers adding detail nobody asked for. A synthesis prompt fix for that is in place but not yet re-measured, and the router default has not been evaluated yet. See [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md).

---

## Project structure

```text
backend/
├── agents/            # LangGraph agent
│   ├── graph.py       #   graph wiring, conditional routing, Postgres checkpointer
│   ├── nodes/         #   understand · cache_lookup · execute · synthesize · verify/replan/finalize
│   ├── planning.py    #   plan sanitising + dependency levels
│   ├── runner.py      #   stream a turn: custom events → sink, updates → final state
│   ├── guardrails.py  #   deterministic prefilter + fallback replies
│   ├── sources.py     #   [S#]/[D#] source block + citations
│   └── tools/         #   sql_guard · sql_tool · sandbox · dataframe_tool · rag_tool
├── api/               # FastAPI routers, dependencies, pure-ASGI middleware, schemas
├── services/          # auth, chat pipeline, conversations, coverage, metrics, rate limits
├── memory/            # context window, rolling summary, long-term memories
├── cache/             # semantic answer cache (RedisVL)
├── llm/               # OpenRouter client, per-role registry, prompts, usage, embeddings, FakeLLM
├── workers/           # arq settings, tasks, enqueue seam
├── evals/             # golden builder, runner, checks, RAGAS jury, reports, online eval
├── db/                # SQLAlchemy models, sessions, Alembic migrations (RLS, read-only role)
├── data_gen/          # deterministic synthetic data, claims engine, policy corpus
├── core/              # settings, logging, telemetry, errors, security, Redis
├── scripts/           # hg-chat, hg-models-check
└── main.py            # app factory + lifespan
frontend/
├── app.py             # navigation, session guard
├── lib/               # API client, SSE parser, UI helpers, charts, page registry
└── views/             # login · chat · coverage · memory · admin_observability · admin_evals
tests/                 # unit/ · integration/ · live/
docs/                  # architecture.md (diagrams) · IMPLEMENTATION_STATUS.md · render_diagrams.sh
deploy/Dockerfile      # multi-stage image for api / worker / ui / migrate
specs/                 # mission, technical specification, roadmap
```

---

## Development

```bash
make format            # ruff format + autofix
make lint typecheck    # ruff + mypy (strict enough to catch Optional/async mistakes)
make test              # fast unit suite
make test-integration  # needs `make infra data`
```

**Database changes.** Edit the models in `backend/db/models/`, generate a migration with `uv run alembic revision --autogenerate -m "..."`, review it, then run `make migrate`.

**Prompt changes.** Edit `backend/llm/prompts/` and bump `PROMPT_VERSION` in `backend/llm/prompts/__init__.py`. The version is part of the exact-cache key and the semantic-cache filter, so stale answers are never replayed.

**Adding a tool:**
1. Implement `async def run_x(step_id, task, user, prior="") -> ToolResult` in `backend/agents/tools/`.
2. Add it to `TOOLS` in `backend/agents/nodes/execute.py` and to `ToolName` in `backend/llm/prompts/schemas.py`.
3. Describe it in the `understand` prompt.
4. Add a label in `backend/agents/sources.py`.
5. Cover it with a graph-flow test using a stub tool.

**Re-embedding.** After changing corpus sources in `backend/data_gen/corpus/`, run `make seed ingest`. Only changed documents are re-embedded.

**Diagrams.** `make diagrams` exports the Mermaid diagrams to `docs/diagrams/*.svg`, using a local Chrome or Chromium through `@mermaid-js/mermaid-cli`.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Ports are not available: … 5432` when starting Postgres | Another Postgres is running. Set `POSTGRES_HOST_PORT=5433` and use port 5433 in `DATABASE_URL`. |
| `uv.lock` changes after `uv run` | An old uv (< 0.8) is first on `PATH`. Use a current uv (`uv self update`, or put `~/.local/bin` first). |
| Every answer is "I'm having trouble reaching the AI service…" | `OPENROUTER_API_KEY` is missing or has no credit. Run `make models-check`. |
| Semantic cache warnings, `Cannot create index on db != 0` | `REDIS_URL` must use database `0`. |
| Answers seem to ignore real embeddings, or retrieval looks lexical | The corpus was ingested with `EMBEDDING_PROVIDER=hash`. Set `openrouter` and run `uv run hg-ingest --force`. |
| Reloading the Streamlit page signs you out | Streamlit keeps the session in memory per browser tab. Navigate with the sidebar (see [Known limitations](#known-limitations)). |
| The first provider search after a restart is slow | Sandbox workers load pandas and the CSV lake (about 6 s). The API warms them at startup; this time never counts against the code timeout. |
| `409 turn_in_progress` | A reply is still being generated for that conversation. A turn stops after `TURN_TIMEOUT_SECONDS` (150 s); if a process dies, the lock clears itself 30 s after that. |
| `429 daily_limit` | The member used `DAILY_TURN_LIMIT` turns today. It resets at midnight UTC; raise the limit (or set `0`) for heavy demo sessions. |
| `409 eval_in_progress` on **Run evaluation** | Another run is queued or running. A run stuck in `running` for more than 3 hours (a dead worker) no longer blocks new ones. |
| `hg-chat` / API: `persona … not found` | Run `make data` (or `uv run hg-seed`). |

---

## Known limitations

- **Session persistence.** Streamlit sessions are not persisted across page reloads.
- **Sandbox memory on macOS.** The pandas sandbox's memory limit is enforced only on Linux. On macOS it relies on the wall-clock timeout.
- **Paraphrases miss the semantic cache.** It serves repeats, not paraphrases (measured distance between paraphrases ≥ 0.18 vs a 0.08 threshold).
- **Router variability.** The Jev Router chooses the serving model per request, so cost and latency vary from turn to turn, and provider prompt caching helps less when the serving model changes.
- **Judge cost is per run.** RAGAS judge spend appears in each evaluation report, not in `llm_calls` or the Observability dashboard.
- **Synthetic data only.** No real member data or real insurer policies are included.

---

## Further documentation

- [docs/architecture.md](docs/architecture.md): architecture with 7 diagrams
- [docs/deployment/AWS_DEPLOYMENT.md](docs/deployment/AWS_DEPLOYMENT.md): AWS deployment design (ECS on Fargate, Aurora PostgreSQL + pgvector, ElastiCache, ALB + WAF, CI/CD, security, scaling, DR, cost) and the changes it requires
- [docs/adr/](docs/adr/README.md): 39 architecture decision records, each with its context, rationale, rejected alternatives and consequences
- [docs/interview/INTERVIEW_GUIDE.md](docs/interview/INTERVIEW_GUIDE.md): how to present and defend the architecture (pitches, request walk-through, key numbers, follow-up questions, scaling, known weaknesses)
- [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md): what was built in each phase, design decisions, live measurements, the engineering review and open items
- [DEMO_QUESTIONS.md](DEMO_QUESTIONS.md): persona-driven demo script
- [specs/](specs/): mission, technical specification and roadmap
