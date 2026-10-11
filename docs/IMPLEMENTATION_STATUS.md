# HealthGuard — implementation status

**Last updated:** 2026-10-11  
**Branch:** `claude/vigilant-wozniak-f6rbb3`  
**Scope:** the HealthGuard demo (Groq + SQLite + ChromaDB + Streamlit) rebuilt as a production-style agentic RAG application on OpenRouter, LangGraph, Postgres/pgvector and Redis.

> **Current state.** Every planned phase is built, tested and pushed. The full Docker stack runs, GitHub CI passes, and all personas were verified against live models. Open items: the RAGAS quality gate is not yet met, and the evaluation baseline was measured on the fixed models before the Jev Router became the default (see [§7](#7-open-items)).

Related documents: [README](../README.md) (setup, usage, configuration), [architecture](architecture.md) (7 diagrams), [technical specification](../specs/TechnicalSpecifications.md), [demo script](../DEMO_QUESTIONS.md).

---

## 1. Phase history

| # | Phase | Commit(s) |
|---|---|---|
| 1 | Foundation: settings, structured logging, telemetry, Docker, CI | `d88c015` |
| 2 | Postgres schemas, pgvector, Alembic, read-only agent role, Row-Level Security | `0dd227d` |
| 3 | Synthetic insurance data, claims engine, policy corpus, personas | `b66687d` |
| 4 | OpenRouter LLM layer: per-role models, prompt caching, exact cache, usage accounting | `80fdde7` |
| 5–6 | Hybrid retrieval (pgvector + full text + RRF); hardened SQL and pandas tools | `a718a96` |
| P0 | Toolchain baseline, test scaffold, tests for phases 1–6 | `de78c35` |
| 7 | LangGraph plan → execute → verify agent, semantic answer cache, `hg-chat` | `5fd5769`, `9c8bc6d` (live-run fixes) |
| 9 | Auth, conversations, SSE chat, feedback, coverage card, admin API | `ea1eaeb` |
| 8 | Memory: history window, rolling summary, long-term memories | `92680bd` |
| 10 | arq worker: titles, summaries, memories, online evaluation | `06ba0bc` |
| L1 | Live persona suite through the HTTP API | `2822d57` |
| 12 | Multipage Streamlit UI with streaming chat and admin dashboards | `fffd676` |
| 14 | CI hardening: combined coverage gate, data smoke tests | `2467c38` |
| 11 | Golden set, eval runner, RAGAS judge jury, quality gate | `cd52d39`, `e3d06dc`, `edce9e7` |
| 13 | Architecture diagrams, LLM tracing spans, Docker stack verified, README | `f99ce81`, `1e2d363` |
| — | Jev Router as the default for the fast, code and synthesis roles | `4d8604d` |
| — | Engineering review: reliability, cost, privacy, refactors, docs (§5) | `9a1eb6d`, `f2bc4e2`, `f2c1515`, `651bdbd`, this commit |

---

## 2. Decisions

| Area | Decision |
|---|---|
| LLM gateway | OpenRouter (any OpenAI-compatible endpoint via `LLM_BASE_URL`) |
| Models | Per-role models, each overridable by env var, with OpenRouter fallback routing. Default: the TypeSafe Jev Router for every chat role |
| Judging | A jury of judges from different model families, on fixed models |
| Storage | Postgres 17 + pgvector, Redis 8, Alembic, Docker Compose |
| Auth | Built-in JWT: argon2id, 15-minute access tokens, rotating refresh-token families with reuse detection |
| Isolation | Generated SQL runs on a read-only role under Postgres Row-Level Security, behind a static SQL guard |
| Caching | Provider prompt caching, a scoped semantic answer cache, an exact LLM cache, an embedding cache |
| Evaluations | RAGAS offline (golden set + gate), plus sampled online judging of live answers |
| Transparency | Members see each answer's plan, steps, verdict and citations; the full trace and generated code are admin-only |
| Spend | Live model calls only when necessary: offline tests use a scripted `FakeLLM`; paid suites are manual |

### Models (defaults in `backend/core/config.py`)

| Role (env var) | Used for | Default | Fallbacks |
|---|---|---|---|
| `MODEL_FAST` | planning, verification, summaries, memory extraction, titles | `typesafe/jev-router` | `anthropic/claude-haiku-5.5`, `google/gemini-3.8-flash` |
| `MODEL_CODE` | SQL and pandas generation | `typesafe/jev-router` | `anthropic/claude-sonnet-5.5`, `openai/gpt-5.4-mini` |
| `MODEL_SYNTH` | the streamed answer | `typesafe/jev-router` | `anthropic/claude-sonnet-5.5`, `google/gemini-3.8-flash` |
| `EVAL_JUDGES` | offline RAGAS jury | `openai/gpt-5.4-mini`, `google/gemini-3.8-flash`, `anthropic/claude-haiku-5.5` | — |
| `ONLINE_EVAL_JUDGES` | live answer scoring (5 % sample + every thumbs-down) | `openai/gpt-5.4-mini`, `google/gemini-3.8-flash` | — |
| `EMBEDDING_MODEL` | retrieval, semantic cache, long-term memory | `openai/text-embedding-3-small` (1536-d) | `EMBEDDING_PROVIDER=local` or `hash` |

The router picks the model and reasoning effort per request and bills at the serving model's price. Router calls get a 4,096-token output budget (reasoning counts against `max_tokens`), and a structured-output repair goes to the first fixed fallback. Judges stay on fixed models so a router cannot send the whole jury to one model.

---

## 3. Live measurements

### L0 — first live run (2026-10-10, `hg-chat`, fixed Claude models, exact and semantic caches off)

| Persona / scenario | Plan | Outcome | Cost |
|---|---|---|---|
| Maria — April knee MRI denial + appeal | sql ∥ policy_search | grounded, complete; D002, $0 owed, appeal steps cited | $0.023–0.039 |
| James — Ozempic coverage + dermatology denial | sql, sql ∥ policy_search | PA + step therapy; D010 referral; retro-referral window | $0.039–0.040 |
| Priya — delivery cost + HSA balance | sql ∥ policy_search | answered | $0.035–0.042 |
| Robert — out-of-network ER (No Surprises Act) | sql ∥ policy_search | answered | $0.022–0.039 |
| Aisha — external review process | policy_search | answered | $0.011 |
| David — family urgent-care denial | sql ∥ policy_search | answered (D009) | $0.014–0.021 |
| David — in-network pediatrician with telehealth | providers (sandbox) | answered | $0.026 |
| Admin — top 5 denial reasons | sql | answered | $0.010 |
| Maria — 4-turn conversation | — | small talk; deductible $2,700 of $3,400; "how much more before I hit it?" → $700; "capital of France" blocked for $0.0002 | — |

Provider prompt caching: repeating Maria's turn served 12,051 of 19,201 prompt tokens from cache and cut the turn from $0.038 to $0.023 (−40 %).

### L1 — through the HTTP API (`HG_LIVE=1 pytest -m live`)
7 persona turns plus refusal checks passed: grounded, cited, no other member's IDs; out-of-scope, injection and cross-member probes blocked. The prompt cache served 60 % of planner and 56 % of SQL-generation prompt tokens. Average turn 9.9 s (p95 26.5 s), about $0.01.

### L2 — evaluation baseline (fixed models, before the router)
10 stratified cases (one per category), 3-judge jury.
- **Run `df6da3ff`:** answer rate, refusal accuracy, routing and isolation 1.0; required content 0.86; context recall 1.0; faithfulness 0.74; answer relevancy 0.52 (agent cost $0.15, p50 13.9 s).
- Eval defects found and fixed: judges truncated by RAGAS's default output cap, faithfulness judged without the profile and reference facts, one judge cache shared by all judges, follow-ups judged on their raw wording.
- **Rescore `50e47e0d`** (same answers, fixed jury, no agent calls): faithfulness 0.80, answer relevancy 0.68, context recall 1.0.
- The remaining gap is real answer quality: answers volunteered unasked detail. The synthesis-prompt fix (`PROMPT_VERSION 2026.10.4`) has not been measured yet.

### Router adoption (2026-10-11)
Maria's MRI-denial turn on `typesafe/jev-router`: **$0.0086–0.017 per turn vs $0.023–0.039** on fixed Claude models, grounded and complete. The router chose DeepSeek V4.1 Flash for planning, SQL and verification, and once Gemini 3.8 Flash for synthesis (18.5 s). Found and fixed: the verifier's reasoning consumed a 1,200-token cap and truncated its JSON.

### Where the money goes (recorded `app.llm_calls`, fixed models)
SQL generation was the costliest call (~$0.010 each, ~5.9k prompt tokens, ~4.2k of them cached), then synthesis (~$0.004), an online-eval judge (~$0.002 each), verification (~$0.0005) and planning (~$0.0002). Embeddings are negligible.

---

## 4. Design notes

- **One `execute` node per plan.** Dependency levels run with `asyncio.gather` inside a single node; `AgentState` has no reducers, so parallel LangGraph branches would conflict. Each step has a wall-clock limit; a step whose dependency failed is skipped with `dependency_failed`.
- **Untrusted plans are repaired** (`planning.sanitize_plan`): unknown tools, duplicate ids, forward/self/unknown dependencies and the step budget. Replan rounds prefix ids with `r{n}-`.
- **Tool error codes** let the graph tell a model outage (`llm_unavailable` fallback) from missing data (`no_data`).
- **Verification:** material claims only, shared `REFERENCE_FACTS` (contacts, as-of date) in synthesis and verify prompts, `minor_issues`, one rewrite of an ungrounded draft before it is replaced (`MAX_REVISIONS`), one replan for an incomplete answer (`MAX_REPLANS`). `VERIFY_REPLACE_UNGROUNDED=false` turns replacement into a log line.
- **Semantic cache keys:** the planner's rewrite differs run to run (cosine distance 0.18–0.30 between rewrites of one question vs a 0.08 threshold), so an opening question is keyed on its normalised text and checked before any model call; follow-ups are keyed on the rewrite. Members read their own scope, then public. An answer mentioning the user's identity or plan is stored privately. Only verified, fully successful turns are stored, tagged with the data and prompt versions.
- **Exact LLM cache:** only outputs that passed validation stay cached (bad JSON, rejected SQL or pandas are forgotten; a repaired structured output replaces the bad one).
- **Turn bounds:** `TURN_TIMEOUT_SECONDS` caps a turn; the per-conversation lock is owned by the turn id and outlives the deadline, and is released with compare-and-delete.
- **Sandbox warm-up:** spawning a worker (pandas import + CSV load) takes ~6 s and never counts against the user-code timeout.
- **Postgres checkpointer:** kept to showcase LangGraph persistence. State is reset every turn (history comes from `app.messages`), so it is effectively an audit copy of each turn's final state (~50 KB per turn).
- **Bugs caught by live runs:** an invalid `DATE :asof` bind parameter broke every member login; the verifier flagged the Member Services number and the as-of date; the semantic cache never hit when keyed on the rewrite; the router's reasoning truncated verifier JSON at 1,200 tokens.

---

## 5. Engineering review (2026-10-11)

A whole-codebase review as senior engineer / architect. Findings and what changed:

| Area | Finding | Change |
|---|---|---|
| Reliability | The exact cache stored outputs before callers validated them, so bad JSON or rejected SQL was replayed for 24 h, each time paying a repair or retry | Results carry their cache key; failed outputs are forgotten, repaired ones stored (`9a1eb6d`) |
| Reliability | No turn deadline; the 180 s lock could expire mid-turn, and release deleted unconditionally | `TURN_TIMEOUT_SECONDS=150` with a `timeout` fallback; owned lock, compare-and-delete release (`9a1eb6d`) |
| Privacy | Log masking missed member IDs and anything nested in lists or dicts | Member IDs masked; nested values masked (`9a1eb6d`) |
| Observability | An abandoned stream left its span open and its call unrecorded | Stream cleanup in `finally` (`9a1eb6d`) |
| Cost | Semantic-cache hits still paid for planning | `cache_first` node: opening questions hit the cache before any model call (`f2bc4e2`) |
| Cost | Memory extraction ran a model call after every answered turn; the summary job was queued every turn | Extraction only when the message states a preference; summary only when history overflows (`f2bc4e2`) |
| Cost | Online judging sampled 25 % of turns and could re-judge an answer; feedback was not rate-limited | 5 % sample, each answer judged once, feedback rate limit (`f2bc4e2`) |
| Cost | Open registration with only a per-minute limit; overlapping paid eval runs possible | `DAILY_TURN_LIMIT` per member; one eval run at a time (`f2bc4e2`) |
| Cost | Judge spend was invisible | `SpendMeter` sums OpenRouter's per-call cost into `judge_cost_usd` (`f2bc4e2`) |
| Latency | Memory search embedded every message, even for users without memories | Skipped when there are none; shares the cache key's embedding (`f2bc4e2`) |
| Privacy | Members received the full trace and generated SQL in every answer | Admin-only; members get plan, steps, verdict, citations (`f2c1515`) |
| Privacy | Access tokens carried email and member ID | Claims reduced to `sub`, `role` (`f2c1515`) |
| Maintainability | Duplicated retry loop (SQL / pandas), LLM-call rows, trace-source lookup | `codegen.py`, `llm_call_rows`, `synthesis_sources` (`651bdbd`) |
| Docs | This document still described phases 7–14 as pending | Rewritten |

Considered and left as is: the write-only checkpointer (showcase value, small cost); `UserContext` living in `agents/tools/base.py` (wide churn for little gain); the eval runner toggling a process-wide setting (CLI and worker only); the deprecated `usage.include` request flag (harmless; removing it cannot be verified without a paid call).

---

## 6. Data and personas

Deterministic (seed 42), loaded by `hg-seed` in about 3 s: 40 plans, 1,600 benefit rows, 117 formulary drugs, 1,504 members in families, 14.3k adjudicated medical claims, 9.6k pharmacy claims, 195 prior authorizations, 5,016 providers, 442 facilities, 433 pharmacies, 60 policy documents (20 hand-written + 40 plan SBCs → ~433 chunks). One source of truth (`backend/data_gen/reference.py`) keeps SQL answers and policy text consistent. Persona stories are in the [README](../README.md#demo-accounts) and the [demo script](../DEMO_QUESTIONS.md).

---

## 7. Open items

| Item | Impact | Next step |
|---|---|---|
| RAGAS gate not met on the baseline (faithfulness 0.80 < 0.85, relevancy 0.68 < 0.80) | Quality claim unproven | Re-run `hg-eval --per-category 1` (~$0.30–0.40) on the router with prompt `2026.10.4` |
| Baseline measured on fixed models | Router quality unmeasured | Same run as above |
| Router cost and latency vary per request; prompt-cache reuse drops when the serving model changes | Less predictable p95 | Watch `llm_calls` by served model on the Observability page |
| Calls record the served model only | Can't separate "router chose X" from "fell back to X" | Optional: a `requested_model` column |
| `/metrics` is unauthenticated | Exposes counters (models, cost) | Keep it on an internal network or behind the proxy |
| Streamlit session is lost on page reload | Re-login after reload | Inherent to Streamlit session state |
| Sandbox memory limit is Linux-only | macOS relies on the timeout | Run the API in Docker for the limit |
| $35 insulin cap stated in the corpus but not enforced by the claims engine; cardiac-rehab sessions counted toward the therapy limit | Cosmetic (no persona hits either) | Data-generator follow-up |
| A spouse who is not the subscriber sees only their own records (RLS) | Intended; documented | — |
