# HealthGuard: AI Architect interview guide

How to present, defend and extend HealthGuard in an AI Architect interview. The [decision records](../adr/README.md) hold the full reasoning for each decision; this guide is about **saying it well under questioning**.

**Contents**

1. [Pitches (30 s, 2 min)](#1-pitches)
2. [The whiteboard: system and request lifecycle](#2-the-whiteboard)
3. [Numbers to know cold](#3-numbers-to-know-cold)
4. [Deep dives with likely follow-ups](#4-deep-dives-with-likely-follow-ups)
5. [Scaling to production](#5-scaling-to-production)
6. [Honest weaknesses and what you'd do next](#6-honest-weaknesses-and-what-youd-do-next)
7. [Curveball questions](#7-curveball-questions)
8. [Story bank: problems you found and fixed](#8-story-bank)
9. [Glossary](#9-glossary)
10. [Self-check before the interview](#10-self-check)

---

## 1. Pitches

### 30 seconds

> "HealthGuard is an agentic RAG assistant for health-insurance members. It answers questions that cross three sources: the member's own claims in Postgres, a provider directory, and policy documents. A LangGraph agent plans which tools to use, generates SQL and pandas, runs independent steps in parallel, writes a cited answer and verifies it before the member sees it. The core design idea is that models propose and code disposes: isolation is enforced by Postgres Row-Level Security, not the prompt; every model output is treated as untrusted; and every turn is traced and priced. A typical turn costs about a cent, and a repeated question costs nothing."

### 2 minutes (problem → architecture → three hard parts → results)

1. **Problem.** "Why was my MRI denied and how do I appeal?" needs the member's claim (SQL), the denial-code meaning and the appeals policy (RAG), scoped to what *that* member may see, and the answer must be correct and traceable.
2. **Architecture.** Streamlit → FastAPI (JWT, rate limits, SSE) → a LangGraph state machine: *cache-first → understand → execute → synthesize → verify*, with one bounded rewrite and one bounded replan. Postgres 17 + pgvector is the system of record, Redis 8 holds caches, locks and the job queue, an arq worker does titles, summaries, memory and answer judging, and OpenRouter serves per-role models with fallbacks.
3. **Hard part 1: safety of generated code.** SQL passes an AST guard, runs as a read-only role in a read-only transaction with a timeout, under Row-Level Security keyed to a server-set member id. Pandas runs in a spawned, time-limited process pool.
4. **Hard part 2: correctness.** Every fact is cited (`[S#]` data, `[D#]` documents) and a separate verifier checks groundedness and completeness; an unsupported claim gets one rewrite, an incomplete answer one replan, otherwise an honest fallback.
5. **Hard part 3: cost.** Four cache layers (scoped semantic answer cache, exact cache of validated outputs, provider prompt caching, embedding cache), a cost-optimising router, and spend guards. Every model call is a priced row in a ledger.
6. **Results.** Isolation, refusal and routing at 100 % on the evaluation baseline; a repeated turn 40 % cheaper from prompt caching; the router halved cost per turn. And I'm upfront that faithfulness (0.80) and relevancy (0.68) are below my gate, with a diagnosed cause and a fix waiting to be measured.

> Tip: end the pitch on the honest gap. Interviewers for architect roles probe for judgement; volunteering the weak spot and its plan reads as maturity.

---

## 2. The whiteboard

### Draw this first (five boxes)

```
 Browser ──► Streamlit UI ──REST+SSE (JWT)──► FastAPI ──► LangGraph agent ──► OpenRouter (per-role models)
                                                 │              │
                                                 │              ├─► sql tool ──► Postgres (hg_agent_ro + RLS)  ─┐
                                                 │              ├─► providers ─► pandas sandbox (CSV lake)      │ Postgres 17 + pgvector
                                                 │              └─► policy_search ► pgvector + FTS + RRF ───────┘ app · insurance · rag
                                                 │
                                                 ├─► Redis 8: semantic cache · exact cache · rate limits · turn lock · arq queue
                                                 └─► arq worker: titles · summaries · memories · online eval
```

### Then walk one request (the MRI question), naming the *why* at each step

| # | Step | What happens | Why (one line) |
|---|---|---|---|
| 1 | Auth + limits | JWT checked, per-minute and daily limits, conversation ownership (404 if not yours) | Cost ceilings and no existence oracle |
| 2 | Turn lock | `SET NX` with the turn id; 409 if a turn is running | One turn per conversation; owned lock |
| 3 | Persist + stream | User message saved; 200 `text/event-stream`; the turn runs in a background task | A closed tab never loses the answer |
| 4 | Context | Member profile + dependents, token-budgeted window, rolling summary, relevant preferences | Bounded context cost |
| 5 | `cache_first` | Opening question → normalised text looked up in the member's scope, then public | A repeat costs zero model calls |
| 6 | `understand` | Regex prefilter, then one FAST structured call: scope, safety, rewrite, plan (`sql` ∥ `policy_search`) | Shared context; one call on the critical path |
| 7 | Sanitize plan | Unknown tools dropped, ids fixed, deps only on earlier steps | Plans are untrusted input |
| 8 | `execute` | Both steps in parallel; SQL guard → read-only role → RLS; hybrid search | Parallel I/O; isolation in the DB |
| 9 | `synthesize` | Streamed answer citing `[S1]` (the claim) and `[D2]` (appeals policy) | Provenance per sentence |
| 10 | `verify` | Grounded? Complete? → finalize / rewrite once / replan once | Second check with a narrow job |
| 11 | Persist | Message + trace + every LLM call, one transaction; `final` event | Audit never partially written |
| 12 | Follow-ups | Semantic-cache store if verified; queue title/summary/memory/eval only if there's work | Spend scales with useful work |

---

## 3. Numbers to know cold

| What | Number | Source |
|---|---|---|
| Cost per turn, router default | **$0.009–0.017** (vs $0.023–0.039 on fixed Claude models) | live, MRI turn |
| Typical turn (fixed models, via API) | avg **9.9 s**, p95 **26.5 s**, ~$0.01 | live persona suite |
| Prompt-cache share of planner / SQL input tokens | **54–60 %** | live |
| Repeated turn with prompt cache | **−40 %** ($0.038 → $0.023) | live |
| Semantic-cache hit for a known general question | **$0.0002** vs ~$0.01 (when planning ran first); opening questions now **zero model calls** | live |
| Out-of-scope block | **$0.0002** (prefilter blocks: $0) | live |
| Most expensive call | SQL generation, ~$0.010 (~5.9k prompt tokens, ~4.2k cached) | `llm_calls` |
| Prompt prefixes | planner ≈ **4.9k** tokens, SQL ≈ **5.8k** | measured |
| Semantic-cache threshold vs rewrite drift | **0.08** vs **0.18–0.30** cosine distance | measured |
| Limits | 4 plan steps · 2 tool retries · 1 rewrite · 1 replan · 90 s per step · **150 s per turn** · 5 s SQL · 10 s sandbox · 200 SQL rows | config |
| Eval baseline (10 cases, 3 judges) | answer rate, refusal, routing, isolation **1.0**; recall **1.0**; content 0.86; faithfulness **0.80** (gate 0.85); relevancy **0.68** (gate 0.80) | rescore `50e47e0d` |
| Data | 40 plans · 1,504 members · 14.3k claims · 9.6k pharmacy claims · 5,016 providers · 60 docs → ~434 chunks | seed 42 |
| Golden set | **71 cases**, 12 categories | `golden.jsonl` |
| Tests | ~**250** automated, coverage gate **78 %** | CI |

---

## 4. Deep dives with likely follow-ups

For each topic: what to say, then the follow-ups you should expect, with answers.

### 4.1 Agent design

**Say:** "It's a plan-and-execute agent built as an explicit LangGraph state machine. Models fill in each node; routing is plain Python over state. That bounds cost and guarantees every answer passes the same checkpoints." ([ADR-0007](../adr/0007-langgraph-explicit-state-machine.md))

| Follow-up | Answer |
|---|---|
| Why not a ReAct loop or native tool calling? | ReAct has no ceiling on calls and lets the model skip steps like verification. I want a known worst case and mandatory checkpoints. The plan still gives flexibility: it's a dependency graph the model writes. |
| Why LangGraph rather than your own orchestrator? | The prototype *was* a hand-written orchestrator, about 900 lines. LangGraph gives typed state, conditional edges, custom-event streaming and a Postgres checkpointer, so my code is only the nodes and routing functions. |
| Why one `understand` call doing four jobs? | Scope, safety, rewrite and plan all need the same history and profile. One call instead of three cuts critical-path latency, and the static prefix is prompt-cached. Each output has a deterministic backstop. ([ADR-0008](../adr/0008-single-understand-call.md)) |
| What if the plan is wrong? | It's sanitized: unknown tools dropped, ids renamed, dependencies only on earlier steps, so it's acyclic by construction. A wrong-but-valid plan shows up as an incomplete answer, which triggers one targeted replan. ([ADR-0009](../adr/0009-plans-are-untrusted-sanitize-dag.md)) |
| How is parallelism done? | Dependency levels run with `asyncio.gather` inside one node. LangGraph fan-out would need reducers on state that's reset every turn, which is how cross-turn leaks happen. ([ADR-0010](../adr/0010-parallel-execution-inside-one-node.md)) |
| Worst-case calls per turn? | Roughly: 2 understand (initial + replan), up to 4 steps × 3 code attempts per round, 2–3 syntheses, 2–3 verifications, plus embeddings. Each step has a 90 s cap and the turn has 150 s. In practice a turn is 4–6 calls. |
| Why multi-agent wasn't used? | A supervisor plus specialist agents adds hand-offs, calls and latency. One planner with three tools solves this domain; I'd only split agents if tools needed very different context or permissions. |
| Human in the loop? | Not needed for read-only Q&A. The Postgres checkpointer is the hook: for an action like filing an appeal, I'd add a LangGraph interrupt before the action and resume after the member confirms. |

### 4.2 Text-to-SQL safety

**Say:** "Four independent layers: AST guard, read-only role, read-only transaction with a timeout, and Row-Level Security keyed to a server-set member id. I assume the model will eventually write anything, and design for the blast radius." ([ADR-0016](../adr/0016-defence-in-depth-for-generated-sql.md), [ADR-0017](../adr/0017-row-level-security-isolation-boundary.md))

| Follow-up | Answer |
|---|---|
| A member says "show me claims for M100002". What happens? | The regex prefilter sees an id that isn't theirs or a dependent's and blocks with no model call. If it slipped through, the planner classifies `other_member_data`. If *that* failed and SQL was generated, RLS returns only the caller's rows. Three independent stops. |
| Can generated SQL change its own RLS context? | No. `SET` is a forbidden node in the guard, unknown functions like `set_config` aren't on the allow-list, I revoked `EXECUTE` on `set_config` from `PUBLIC`, and the prefilter regex catches `set_config(`. |
| Why an AST guard and not a regex? | Regexes are bypassed by comments, casing and nesting. Walking the sqlglot tree catches a `DELETE` inside a CTE. Tables and unknown functions are allow-listed, so new dangerous functions are blocked by default. |
| Why RLS instead of appending `WHERE member_id = ?` | Rewriting arbitrary SQL with subqueries, CTEs and unions is fragile. RLS applies to every table reference automatically, and a missing context returns no rows, not all rows. |
| How is the identity set if `SET` doesn't take bind parameters? | `SET LOCAL` with interpolated values, but only after validation: role is from a fixed set, member id must match `^M\d{6}$`. Neither comes from the model or the message. `SET LOCAL` scopes it to the transaction, so a pooled connection never carries it over. |
| What about expensive queries? | 5 s statement timeout (role default and per transaction), a 200-row cap by wrapping the query, and 30 s idle-in-transaction timeout. |
| Family plans? | The policy also matches `subscriber_id`, so a subscriber sees dependents; a non-subscriber spouse sees only their own rows. That's intended and documented. |

### 4.3 Retrieval (RAG)

**Say:** "Hybrid retrieval: pgvector HNSW for meaning, Postgres full-text for exact tokens like denial codes and plan ids, fused with Reciprocal Rank Fusion." ([ADR-0022](../adr/0022-hybrid-retrieval-rrf.md), [ADR-0023](../adr/0023-structure-aware-chunking-versioned-ingest.md))

| Follow-up | Answer |
|---|---|
| Why RRF instead of weighted scores? | Cosine similarity and `ts_rank_cd` are on different scales. RRF uses ranks only (`1/(60+rank)`), so there's nothing to calibrate per corpus. |
| Why not a vector DB? | The corpus is ~434 chunks; pgvector handles millions. Keeping it in Postgres means no second store, no sync job, and full-text in the same query engine. |
| Chunking? | Heading-aware with a section path on every chunk; ~380 tokens with one-paragraph overlap; long tables split by rows with the header repeated, so an SBC row like "$40" keeps its meaning. |
| A retrieval failure you fixed? | Forty near-identical plan SBCs crowded out policy documents and "my plan" answers cited the wrong plan's SBC. I filter SBCs to the member's plan or a plan named in the query. |
| Re-ranking? | Optional LLM listwise rerank behind a flag, off by default: it adds a call per search and recall was already 1.0 on the baseline. |
| How do you know retrieval is good? | Context recall (RAGAS) on the golden set, 1.0 on the baseline. I also never judge retrieval with the hash embedder used in CI. |
| How do you keep cached answers fresh after documents change? | Ingest is idempotent by content hash and bumps `rag.data_version`; semantic-cache entries are filtered by data version and prompt version. |

### 4.4 Caching

**Say:** "Four layers at different granularities, each with a key designed so a hit can only return something the caller could already see." ([ADR-0025](../adr/0025-four-cache-layers.md))

| Follow-up | Answer |
|---|---|
| How do you stop the semantic cache leaking between members? | Scopes: `public`, `member:{id}`, `role:admin`. A lookup can only match the caller's scope or public. On store, a "public" answer that mentions the member's name, id, dependents or plan is moved to their private scope. |
| What do you refuse to cache? | Anything not answered, not verified grounded *and* complete, where the verifier was skipped, where any tool failed, or that was itself a hit. Evaluation runs disable the cache. |
| Why look up before planning? | My first version keyed on the planner's rewrite and never hit: rewrites of the same question were 0.18–0.30 apart against a 0.08 threshold, and every hit still paid for planning. An opening question is self-contained, so its normalised text is a stable key and the lookup needs no model call. Follow-ups still use the rewrite. ([ADR-0026](../adr/0026-scoped-semantic-answer-cache.md)) |
| Why such a tight threshold? | Precision over recall. "Deductible" and "out-of-pocket max" questions are close in embedding space; a wrong cached answer is worse than a miss. The cost is that paraphrases miss. |
| What went wrong with the exact cache? | It stored outputs before callers validated them, so bad JSON or rejected SQL replayed for 24 h, each time paying for a repair. Now results carry their cache key and invalid outputs are evicted; repaired outputs replace them. ([ADR-0027](../adr/0027-exact-llm-cache-validated-outputs.md)) |
| Prompt caching details? | Static-first prompts with explicit `cache_control` breakpoints for Anthropic, Gemini and routers (max 4); per-request data last. 54–60 % of planner/SQL input tokens served from cache. ([ADR-0028](../adr/0028-provider-prompt-caching.md)) |

### 4.5 Cost

**Say:** "Cost is measured per call, not estimated. I attack it at four points: model choice per role, caching, avoiding unnecessary calls, and ceilings." ([ADR-0004](../adr/0004-cost-optimising-router-default.md), [ADR-0029](../adr/0029-spend-guards-gated-background-work.md))

| Follow-up | Answer |
|---|---|
| Where does the money go? | SQL generation (~$0.010), then synthesis (~$0.004), online-eval judges (~$0.002 each), verification (~$0.0005), planning (~$0.0002), on fixed models. Embeddings are negligible. |
| How did you cut it? | Router default (about half), prompt caching (−40 % on repeats), cache-first lookup (repeats free), gating background jobs (memory only when a preference is stated, summary only on overflow, 5 % judging, each answer once). |
| What stops a runaway bill? | Per-minute and per-day turn limits per member, feedback rate limits, one evaluation at a time, dry-run estimates, and paid CI that only runs on demand. |
| Router downsides? | Variable latency and cost per request, less prompt-cache reuse when the serving model changes, and I can't yet tell "router chose X" from "fell back to X". And its quality is not yet measured on the golden suite. |

### 4.6 Correctness and verification

**Say:** "A separate verifier checks material claims against the same evidence block the writer had, plus completeness. Repairs are bounded: one rewrite, one replan, then an honest fallback." ([ADR-0012](../adr/0012-verify-groundedness-completeness.md))

| Follow-up | Answer |
|---|---|
| Isn't an LLM verifying an LLM circular? | It's a different, narrower task: classification against provided text, not generation. It's not perfect, which is why offline faithfulness is measured independently by a three-family jury. |
| What if the verifier is down? | Fail open for the member (the answer is shown, marked skipped) and fail closed for the cache (unverified answers are never stored). |
| False positives? | The first live run flagged the Member Services phone number and the as-of date. I added shared reference facts to both prompts and a `minor_issues` field for harmless imprecision. |
| What if the model invents a citation tag? | Only citations whose tag exists in the sources are rendered, and the verifier checks claims against the actual source text. A deterministic "every tag must exist" check would be a cheap next addition. |

### 4.7 Evaluation

**Say:** "A 71-case golden set built from the database, so references are correct by construction; each case runs as its persona through the real chat pipeline; deterministic checks for binary properties, a three-family RAGAS jury for quality, and a CI gate." ([ADR-0034](../adr/0034-evaluation-strategy.md))

| Follow-up | Answer |
|---|---|
| Why a jury? | One judge has self-preference bias and noise. Three families (GPT, Gemini, Claude) give a mean and a spread; high spread flags cases for review. Judges are fixed models, never routed. |
| Why deterministic checks too? | Isolation and refusal must be exactly 100 %. A judge is the wrong tool for a binary safety property; a regex for forbidden member ids isn't. |
| What did evaluation teach you? | The first baseline found bugs in the *evaluation*: judges truncated by an output cap, faithfulness judged without the profile, one cache shared by all judges, follow-ups judged on raw wording. Fixing those moved faithfulness 0.74 → 0.80 and relevancy 0.52 → 0.68 on the same answers. |
| Why below the gate? | Answers volunteer detail nobody asked for, which hurts relevancy and adds claims to verify. The synthesis prompt now says "answer what was asked"; that version hasn't been re-measured yet. |
| Online evaluation? | A 5 % sample plus every thumbs-down is judged by two judges in the worker, each answer once, and shown on the admin dashboard. |
| How would you gate prompt changes? | Run a stratified per-category subset (~$0.30) on any PR touching prompts, with `--rescore` to separate judge changes from agent changes. |

### 4.8 Memory

**Say:** "Three tiers: a token-budgeted verbatim window, a rolling summary updated by a worker only on overflow, and long-term memories restricted to non-clinical preferences with a deterministic privacy filter." ([ADR-0033](../adr/0033-three-tier-memory.md))

| Follow-up | Answer |
|---|---|
| Why not store facts like the deductible? | They go stale and would bypass the tools and verification. Facts come from tools every turn; memory shapes tone and defaults only, and the prompt says so explicitly. |
| How do you keep clinical data out? | The extraction prompt forbids it, and a regex filter rejects ids, amounts, dates, contacts and clinical terms anyway. Users can list, delete, clear and disable memories. |
| Why a summary instead of retrieving old turns? | Retrieval can miss the referent of "the second one"; a summary keeps the narrative. `summary_upto` guarantees no overlap or gap with the window. |

### 4.9 Security and privacy beyond SQL

| Question | Answer |
|---|---|
| Auth design? | 15-minute JWTs with only `sub` and `role`; opaque refresh tokens stored as SHA-256, rotating in families; replaying a used token revokes the family. Constant-time-ish login via a dummy hash. 404, never 403, for others' resources. ([ADR-0020](../adr/0020-jwt-and-rotating-refresh-tokens.md)) |
| Running model-written Python? | AST allow/deny lists, restricted builtins, a spawned process pool, 10 s wall clock with pool kill, address-space limit on Linux. Acceptable because the directory data is public; for sensitive data I'd use gVisor or Firecracker. ([ADR-0018](../adr/0018-process-sandbox-for-pandas.md)) |
| Prompt injection? | Direct: regex prefilter + LLM classification. Indirect (through retrieved text): the corpus is internal, and the only components that read retrieved text, the synthesizer and verifier, have no tools, so injected text can't trigger actions. Generated SQL can't escape RLS regardless. |
| PII in logs? | A structlog processor masks SSNs, emails, phones, dates and member ids, including nested values. Names in free text are a known gap. ([ADR-0021](../adr/0021-data-minimisation-and-pii.md)) |
| Would this be HIPAA-ready? | Not as is: the data is synthetic. For real PHI I'd need BAAs with every model provider (or BAA-covered endpoints such as a cloud provider's hosted models via `LLM_BASE_URL`), zero data retention on model calls, encryption at rest for Postgres, Redis and backups, TLS everywhere, audit logs of admin trace access, retention and hard-delete policies for messages and traces, and a review of what the semantic cache stores. The architecture supports this: the gateway is one URL, PII is minimised per surface, and access is role-based. |

### 4.10 Reliability and runtime

| Question | Answer |
|---|---|
| Provider outage? | OpenRouter `models` fallbacks across providers per role; SDK retries with backoff; error-coded fallbacks if everything fails ("the AI service is unavailable" vs "no data"). |
| Client disconnects mid-stream? | The turn runs in a background task feeding a queue; the response only drains it. The answer, trace and cost are saved regardless. ([ADR-0030](../adr/0030-fastapi-sse-decoupled-turn.md)) |
| Double-submit? | Owned Redis lock per conversation (value = turn id), compare-and-delete release, TTL = deadline + 30 s. ([ADR-0031](../adr/0031-turn-lock-and-deadline.md)) |
| Redis down? | Caches miss, rate limits fail open, enqueue is best effort, but the turn lock can't be taken, so chat returns an error. That's a trade-off worth naming. |
| Graceful shutdown? | Lifespan drains in-flight turns (30 s), stops the sandbox pool, closes pools in reverse order. |

---

## 5. Scaling to production

Expect: "How would this handle a million members / 100 requests per second?" Walk the bottlenecks in order.

| Layer | Today | At scale |
|---|---|---|
| **API** | One uvicorn worker per container; in-flight turns in process | Horizontal replicas behind a load balancer with long idle timeouts for SSE; scale on concurrent streams, not CPU. Turns are I/O-bound, so one process handles many concurrently. |
| **Model gateway** | OpenRouter with fallbacks | Per-provider concurrency limits and a global semaphore; per-tenant budgets on top of the cost ledger; more aggressive caching; consider a self-hosted small model for `understand`/`verify` if volume justifies it. |
| **Postgres (agent reads)** | One pool for `hg_agent_ro` | Point the agent pool at **read replicas** (RLS and `SET LOCAL` work in read-only transactions); PgBouncer in **transaction** mode is compatible because the RLS context is `SET LOCAL`. |
| **Postgres (writes)** | Message + trace + calls per turn; checkpoint per turn | Partition `messages`, `traces`, `llm_calls` by month; retention for traces; drop or sample the checkpointer if unused. |
| **Vectors** | ~434 chunks, HNSW | Fine to millions; partition by category or plan; tune `ef_search`; move retrieval to a replica. |
| **Redis** | Single node, vector index in DB 0 | Managed Redis with search support, or move the semantic cache into pgvector if Redis search clustering is a constraint; separate instances for queue vs cache. |
| **Sandbox** | Process pool inside the API container | A separate, autoscaled code-execution service with gVisor/Firecracker isolation. |
| **Worker** | One arq worker | Horizontal workers; separate queues so long evaluations don't starve follow-ups. |
| **Observability** | Prometheus per process; trace table | Prometheus scraping per replica (already one process per container); ship spans to a managed backend; sampled traces. |

**Cloud deployment:** the AWS design (service by service, with the reasons, gotchas, cost and an interview Q&A) is in [docs/deployment/AWS_DEPLOYMENT.md](../deployment/AWS_DEPLOYMENT.md#23-interview-questions-on-the-deployment).

**Back-of-envelope cost (illustrative assumptions, say them out loud):** 1M members × 2 % daily active × 3 questions = 60k turns/day. At ~$0.012 per uncached turn that's ≈ $720/day (≈ $22k/month) before caching; every point of semantic-cache hit rate and prompt-cache share comes straight off that. The per-call ledger is what lets you verify these numbers in production rather than guess.

---

## 6. Honest weaknesses and what you'd do next

Raise these yourself; each comes with a plan.

| Weakness | Why it exists | Next step |
|---|---|---|
| RAGAS gate not met (faithfulness 0.80, relevancy 0.68) | Answers add unasked detail | Re-run `hg-eval --per-category 1` (~$0.30–0.40) on prompt `2026.10.4` and the router |
| Router quality unmeasured | Router adopted after the baseline | Same run; compare per category |
| Can't tell routed vs fallback model | Calls store the served model only | Add `requested_model` to `llm_calls` |
| Paraphrases miss the semantic cache | Tight threshold for precision | Canonicalise questions (e.g. an intent + entity key), or a second-stage equivalence check for near misses |
| Sandbox is process-level | Demo scope | gVisor/Firecracker service |
| `/metrics` unauthenticated | Internal-network assumption | Auth or network policy |
| Rate limits fail open; fixed windows | Availability over strictness | Sliding window or token bucket; alert on Redis errors |
| Streamlit session lost on reload | Streamlit design | React + backend-for-frontend with httpOnly refresh cookie |
| HS256 shared secret | Single service | RS256/EdDSA + JWKS or an IdP |
| Names not masked in logs | Regex masking | NER-based masking or log only ids |
| Checkpointer is write-only | Kept for audit and future interrupts | Use it for human-in-the-loop actions or remove it |

---

## 7. Curveball questions

**"Why not fine-tune a model for SQL?"**
Prompts + a schema catalog + self-correction already work, prompt caching makes long prompts cheap, and prompt changes are evaluated in minutes. Fine-tuning ties you to one provider and needs retraining when the schema changes. Once the golden set is larger, a fine-tuned small SQL model is a reasonable cost optimisation, and the golden set is the training/eval data for it.

**"Why not just use function calling?"**
Function calling picks tools; I also need dependencies between steps, a standalone rewrite and a safety verdict, in one structured object, across many models (some behind the router don't support tools well). And my tools take natural-language tasks because they generate code.

**"Where does MCP fit?"**
MCP standardises exposing tools to other agents and clients. Internally it adds nothing; externally, wrapping `sql` (with RLS), `providers` and `policy_search` as an MCP server would let other assistants use them safely, because the isolation lives in the database, not in my agent.

**"What would you change if latency were the top priority?"**
Start synthesis while slower steps finish (stream partial answers), use the cache-first path more (canonical keys), a faster fixed model for `understand`/`verify` instead of the router, skip verification for low-risk categories (with evaluation to prove it's safe), and pre-warm prompt caches.

**"How do you roll out a prompt change safely?"**
Bump `PROMPT_VERSION` (invalidates exact and semantic caches), run a stratified eval subset, compare to the last run per category, check judge spread, then watch online-eval scores and the 👎 rate after release.

**"How would you add a new data source, say, EOB PDFs?"**
Convert to Markdown, ingest through the same chunker (idempotent, versioned), add a category filter, describe it in the data-source catalog (which feeds the planner prompt), add golden cases. If it's member-specific, it must be RLS-scoped like the other member tables, or filtered by member in the retriever.

**"What's the single most important decision?"**
Putting member isolation in Postgres (read-only role + RLS) instead of in the prompt. Everything else improves quality or cost; that one is what makes the system safe to put in front of members.

**"What would you do differently from the start?"**
Build the golden set and the cost ledger on day one. Several early decisions (keying the cache on the rewrite, caching before validating) looked right and were only proven wrong by measurement.

---

## 8. Story bank

Use the STAR shape: situation, task, action, result. Each is real and in the commit history.

1. **The semantic cache that never hit.** *S:* Cache keyed on the planner's rewrite. *T:* Make repeats cheap. *A:* Measured rewrite drift (0.18–0.30 vs 0.08 threshold); moved opening questions to a normalised-text key looked up before any model call; kept the rewrite for follow-ups. *R:* Repeated opening questions now cost zero model calls; a known general question went from ~$0.01 to $0.0002 even before that change.
2. **The cache that replayed failures.** *S:* Exact cache stored outputs before validation. *A:* Results carry their cache key; schema, guard, database or sandbox failures evict; repairs replace. *R:* A one-off bad output can no longer become a 24-hour failure.
3. **The lock that released someone else's turn.** *S:* 180 s lock, no turn deadline, unconditional delete. *A:* 150 s turn deadline, lock owned by turn id with TTL = deadline + 30 s, compare-and-delete. *R:* No cross-turn lock theft; every turn ends.
4. **Reasoning tokens truncated the verifier.** *S:* Router picked a reasoning mode; 1,200-token cap cut the JSON. *A:* 4,096-token budget for router calls (only generated tokens are billed); structured repair on a fixed model. *R:* Valid verdicts on the router at half the cost.
5. **The evaluation was wrong before the agent was.** *S:* First baseline faithfulness 0.74, relevancy 0.52. *A:* Found truncated judges, missing grounding context, a shared judge cache, follow-ups judged on raw wording; added `--rescore`. *R:* Same answers rescored 0.80 / 0.68, and the remaining gap was correctly attributed to the answers themselves.
6. **Live runs catch what fakes can't.** The first live OpenRouter run found that an invalid `DATE :asof` bind parameter broke every member profile load, that the verifier rejected correct answers because it flagged the Member Services number and the as-of date, and that the semantic cache never hit. Each led to a fix and a regression test (`9c8bc6d`).
7. **Cold starts are part of the timeout budget.** While building the test scaffold, the sandbox's ~6 s cold start (pandas import + CSV load) could push the first provider query past its 10 s limit. The pool is now warmed at startup with its own budget, so start-up never counts against user code (`de78c35`).

---

## 9. Glossary

| Term | Meaning here |
|---|---|
| **Agentic RAG** | Retrieval-augmented generation where an agent plans which retrievals and tools to run, rather than a single fixed retrieve-then-generate step |
| **Plan-and-execute** | One planning step produces a list of steps; an executor runs them; contrast with ReAct's think-act-observe loop |
| **RLS** | Postgres Row-Level Security: per-table policies that filter rows for a role based on session settings |
| **`SET LOCAL`** | A Postgres setting that lasts only until the end of the current transaction |
| **sqlglot** | A SQL parser/transpiler used here to walk the AST of generated SQL |
| **HNSW** | Hierarchical Navigable Small World: an approximate nearest-neighbour index (pgvector supports it) |
| **`tsvector` / `ts_rank_cd`** | Postgres full-text search vector and cover-density ranking |
| **RRF** | Reciprocal Rank Fusion: combine ranked lists by summing `1/(k + rank)` |
| **SBC** | Summary of Benefits and Coverage: a standard plan summary document |
| **Prompt caching** | Provider-side discount for re-sent identical prompt prefixes; Anthropic/Gemini need `cache_control` markers |
| **Semantic cache** | Cache keyed by embedding similarity rather than exact text |
| **SSE** | Server-Sent Events: a one-way HTTP stream of `event:`/`data:` messages |
| **RAGAS faithfulness / answer relevancy / context recall** | Share of answer claims supported by context / how directly the answer addresses the question / share of the reference covered by retrieved context |
| **Judge jury** | Several LLM judges from different model families scoring the same output; mean and spread reported |
| **Refresh-token family** | All refresh tokens descended from one login; reuse of a revoked member revokes the family |
| **No Surprises Act** | US law limiting surprise out-of-network bills (e.g. emergencies); modelled in the claims engine |

---

## 10. Self-check

Before the interview, make sure you can, without notes:

- [ ] Draw the five-box system and the agent graph, including the cache-first path and both loops.
- [ ] Walk the MRI question through all 12 steps, giving the "why" for each.
- [ ] Name the four SQL layers and explain why `set_config` is revoked.
- [ ] Explain the RLS policy, including the subscriber/spouse behaviour and the fail-closed default.
- [ ] Explain why the semantic cache keys opening questions on normalised text, with the drift numbers.
- [ ] List the four cache layers and each one's safety rule.
- [ ] Give the cost per turn on the router vs fixed models, and the prompt-cache numbers.
- [ ] Explain verify → rewrite → replan → fallback, and the fail-open/fail-closed split.
- [ ] Give the eval baseline, the gate, why it's not met, and the plan.
- [ ] Name three weaknesses and how you'd fix them.
- [ ] Sketch the scaling plan (replicas, read replicas + PgBouncer transaction mode, sandbox service, budgets).
- [ ] Tell two stories from the story bank in under 90 seconds each.
