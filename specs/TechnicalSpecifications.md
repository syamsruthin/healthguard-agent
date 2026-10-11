# Technical specification

Diagrams for every part of this specification are in [docs/architecture.md](../docs/architecture.md).

## 1. Stack

| Layer | Choice |
|---|---|
| LLM gateway | OpenRouter (OpenAI-compatible); per-role models with fallback routing |
| Agent | LangGraph 1.2 `StateGraph`; `AsyncPostgresSaver` checkpointer |
| API | FastAPI with pure-ASGI middleware; SSE via sse-starlette |
| Data | Postgres 17 + pgvector (HNSW), Alembic migrations, async SQLAlchemy, asyncpg |
| Cache / queue | Redis 8: Query Engine for vector search, RedisVL, arq |
| UI | Streamlit 1.65 multipage app (`st.navigation`) with Plotly charts |
| Evaluation | RAGAS 0.4.3 collections metrics with a judge jury; an online LLM-judge jury |
| Observability | structlog with PII masking, Prometheus, OpenTelemetry (OTLP) |
| Tooling | uv, ruff, mypy, pytest (unit / integration / live), GitHub Actions, Docker Compose |

## 2. Agent

**State** (`backend/agents/state.py`) is reset for every turn:
- Inputs: user context, message, recent turns, summary, memories, data version.
- Planning: understanding, standalone query, plan, replans, feedback.
- Revision: revisions, revise flag, notes.
- Caching: whether the cache was already checked, the scope, the lookup key.
- Execution: results, sources, citations.
- Output: answer, verification, outcome, fallback, cache hit, scope and key, trace.

**Nodes:**

| Node | Model | Responsibility |
|---|---|---|
| `cache_first` | embeddings | Opening questions only (no history or summary): scoped semantic-cache lookup on the normalised message before any model call; a hit ends the turn |
| `understand` | FAST, structured `Understanding` | Deterministic prefilter, then scope, safety, coreference rewrite and a dependency-aware plan (≤ 4 steps) in one call; `sanitize_plan` repairs the plan |
| `cache_lookup` | embeddings | Follow-ups only: scoped semantic-cache lookup on the standalone rewrite |
| `execute` | CODE (inside tools) | Dependency levels run in parallel with `asyncio.gather`; per-step timeout; dependency-failure skipping; error codes |
| `synthesize` | SYNTH, streamed | Grounded answer with `[S#]` / `[D#]` citations; revision mode after an ungrounded verdict |
| `verify` | FAST, structured `Verification` | Checks the answer is grounded (material claims only) and complete |
| `replan` | — | Feeds back what was missing for one extra planning round |
| `finalize` | — | Sets the outcome: answered, smalltalk, blocked, cache_hit, insufficient or error |

**Tools:**

| Tool | Data | Safety |
|---|---|---|
| `sql` | `insurance` schema | sqlglot guard (one SELECT, table and function allow-lists); `hg_agent_ro` role; READ ONLY transaction; statement timeout; RLS via `SET LOCAL app.member_id / app.role`; self-correcting retries |
| `providers` | CSV lake (pandas) | AST validation, restricted builtins, spawned worker pool, wall-clock limit (memory limit on Linux), warm-up outside the timeout |
| `policy_search` | `rag.chunks` | pgvector cosine + `ts_rank_cd` full text, fused with RRF. Plan SBCs are searched only for the member's own plan or a plan named in the question |

## 3. API (`/api/v1`)

| Method and path | Purpose |
|---|---|
| `POST /auth/register · /auth/login · /auth/refresh · /auth/logout`, `GET /auth/me` | argon2id passwords; 15-minute JWT access tokens; rotating refresh-token families with reuse detection |
| `GET/POST /conversations`, `PATCH/DELETE /conversations/{id}` | Owner-only (others get 404); soft delete also drops checkpoints |
| `GET /conversations/{id}/messages` | History with citations, tools, usage and metadata |
| `POST /conversations/{id}/messages` | Non-streaming turn |
| `POST /conversations/{id}/messages/stream` | SSE turn: `meta, status, plan, tool, token, answer_reset, answer_replaced, citations, error, final`. Members get the plan, steps, verdict and citations; the full trace and generated code are admin-only |
| `POST /messages/{id}/feedback` | Thumbs up or down (rate-limited); a thumbs-down queues an online evaluation (each answer is judged at most once) |
| `GET /me/coverage`, `GET/DELETE /me/memories[/{id}]`, `PATCH /me/settings` | Member card, memory controls |
| `GET /admin/metrics/summary`, `GET /admin/traces/{id}`, `GET /admin/evals/runs`, `POST /admin/evals/run` | Admin only; one evaluation run at a time (409 otherwise) |
| `GET /health/live`, `GET /health/ready`, `GET /metrics` | Operations |

**Turn pipeline:** rate limit (per minute, plus `DAILY_TURN_LIMIT` per member per UTC day) → input checks → ownership → Redis turn lock owned by the turn id → persist the user message. A background task then streams the graph's events (bounded by `TURN_TIMEOUT_SECONDS`), persists the assistant message, trace and LLM calls in one transaction, emits `final`, stores the answer in the semantic cache if it is eligible, queues the follow-up jobs that have work, and releases its own lock (compare-and-delete). A client disconnect never loses the answer.

## 4. Memory

- **Window:** the most recent turns after `summary_upto`, within `HISTORY_TOKEN_BUDGET`; assistant turns are truncated.
- **Rolling summary:** a worker job, queued only when a turn pushes enough history out of the window. When turns outside the window exceed `SUMMARY_TRIGGER_TOKENS`, they are folded into `conversations.summary`.
- **Long-term memory:**
  - The FAST model extracts non-clinical preferences, only after turns in which the user states one (a keyword gate skips the call otherwise).
  - A deterministic filter rejects IDs, amounts, dates, contacts and clinical terms.
  - A memory is skipped as a duplicate above cosine 0.9; each user keeps at most 50.
  - Retrieval uses pgvector top-k above a minimum similarity; users with no memories skip it.
  - Users can list, delete, clear or disable their memories.

## 5. Caching

1. **Provider prompt caching:** stable system prefixes with `cache_control` breakpoints for Anthropic and Gemini; OpenAI-family models cache automatically.
2. **Semantic answer cache:** RedisVL `AsyncSearchIndex`.
   - **When:** opening questions before any model call (keyed on the normalised message); follow-ups after planning (keyed on the rewrite).
   - **Scopes:** `public`, `member:{id}` and `role:admin`.
   - **Tags:** data version and prompt version.
   - **Storage:** an identity check routes personalised answers to member scope, and only verified, fully successful answers are stored.
3. **Exact LLM cache:** temperature-0 calls; outputs that fail validation are forgotten.
4. **Query-embedding cache.**

## 6. Evaluation

- **Golden set:** `hg-build-golden` builds 59 cases. Ground truth comes from SQL and pandas queries on the seeded data plus quotes from the corpus.
- **`hg-eval`** runs each case through the real chat pipeline as its persona. It applies deterministic checks (answer rate, refusal accuracy, routing, isolation, required content) and the RAGAS jury (faithfulness, answer relevancy, context recall; context precision and factual correctness optional), then gates on `thresholds.yaml`. It writes reports and DB rows. `--dry-run` prints a cost estimate.
- **Online evaluation:** the worker scores a 5 % sample of answers (`ONLINE_EVAL_SAMPLE_RATE`) and every thumbs-down with `ONLINE_EVAL_JUDGES`, each answer at most once.
- **Cost reporting:** every offline run reports agent cost and judge cost (from OpenRouter's per-call `usage.cost`).

## 7. Security

See §7 of the architecture document: JWT and refresh rotation (tokens carry only user id and role), rate limits and the daily turn cap, ownership checks, the prefilter, LLM safety classification, the SQL guard, the read-only role with RLS, the pandas sandbox, verification, admin-only traces and generated code, PII-masked logs and the production configuration guards. `ENV=production` refuses to start with the default JWT secret or without an API key.
