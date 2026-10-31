# HealthGuard architecture

HealthGuard is an agentic RAG assistant for health-insurance members. A LangGraph agent plans which tools to use (SQL over the member's records, pandas over the provider directory, hybrid search over policy documents), runs them in parallel, writes a cited answer, and verifies it before the member sees it. Every turn streams over Server-Sent Events, is persisted with a full trace, and is cost-accounted.

Diagrams render on GitHub. `make diagrams` also exports them as SVG to `docs/diagrams/`.

The reasoning behind each design choice (context, alternatives, trade-offs) is in the [architecture decision records](adr/README.md).

## 1. System view

Six containers (`ui`, `api`, `worker`, `migrate`, `postgres`, `redis`) plus external services. The LangGraph agent, its tools and the LLM layer all run inside the API process. Solid arrows are the request path; dotted arrows are telemetry.

```mermaid
flowchart LR
    USER(["Member / admin<br/>browser"]) --> UI

    subgraph C1["ui :8501"]
        UI["Streamlit UI<br/>chat · coverage · memory<br/>admin dashboards"]
    end

    subgraph C2["api :8000 · FastAPI process"]
        direction TB
        API["Routers + middleware<br/>JWT · rate limits · /metrics"]
        TURN["Turn task (chat_service)<br/>turn lock · deadline<br/>memory context · SSE · persist"]
        AGENT["LangGraph agent<br/>cache_first → understand<br/>→ execute → synthesize<br/>→ verify → finalize<br/>(rewrite + replan loops)"]
        subgraph TOOLS["Tools"]
            direction TB
            SQLT["sql<br/>sqlglot guard · RLS"]
            PDT["providers<br/>pandas sandbox"]
            RAGT["policy_search<br/>pgvector + FTS · RRF"]
        end
        LLM["LLM layer<br/>roles FAST · CODE · SYNTH<br/>exact cache · usage"]
        API --> TURN --> AGENT --> TOOLS
        AGENT --> LLM
        TOOLS -- "codegen" --> LLM
    end

    subgraph C3["worker · arq"]
        WK["titles · summaries<br/>memories · online / offline eval"]
    end

    subgraph STORES["Data stores"]
        direction TB
        PG[("Postgres 17 + pgvector<br/>app · insurance (RLS) · rag<br/>graph checkpoints")]
        RD[("Redis 8<br/>semantic + LLM + embedding caches<br/>locks · rate limits · arq queue")]
        CSV[("CSV lake<br/>providers · facilities<br/>pharmacies")]
    end

    subgraph EXT["External"]
        direction TB
        OR(["OpenRouter<br/>chat + embeddings"])
        LF(["Langfuse"])
        OBS(["Prometheus · Jaeger"])
    end

    UI -- "REST + SSE · JWT" --> API
    SQLT --> PG
    RAGT --> PG
    PDT --> CSV
    TURN --> PG
    TURN -- "jobs" --> RD
    AGENT <-- "semantic cache" --> RD
    LLM --> OR
    LLM <--> RD
    RD --> WK
    WK --> PG
    WK --> OR
    C2 -.-> LF
    C2 -.-> OBS
```

- **Turn task** (`backend/services/chat_service.py`): takes the per-conversation Redis lock, loads the memory context, runs the graph under the turn deadline, persists the result and enqueues follow-up jobs.
- **Tools:** `sql` and `providers` generate code with the CODE model and self-correct on errors; `policy_search` is hybrid retrieval. A dependent step receives its prerequisites' output.
- **LLM layer:** maps roles to OpenRouter models with fallbacks, checks the exact LLM cache in Redis, marks stable prefixes for provider prompt caching, and records tokens and cost per call.
- **Redis** holds every piece of fast shared state: the semantic answer cache, the exact LLM and embedding caches, turn locks, rate limits and the arq queue.

## 2. Agent graph

```mermaid
flowchart TD
    S((start)) -- "opening question" --> CF{"cache_first<br/>semantic cache, no model call"}
    S -- "follow-up" --> U
    CF -- hit --> F
    CF -- miss --> U["understand<br/>prefilter + one FAST call:<br/>scope · safety · rewrite · plan"]
    U -- "blocked / model down" --> F
    U -- "empty plan (small talk)" --> SY
    U -- "replan round" --> X
    U -- "plan, opening question<br/>(cache already checked)" --> X
    U -- "plan, follow-up" --> C{"cache_lookup<br/>on the standalone rewrite"}
    C -- hit --> F
    C -- miss --> X["execute<br/>dependency levels run in parallel<br/>sql · providers · policy_search"]
    X -- "every step failed" --> F
    X --> SY["synthesize<br/>streamed, cited answer"]
    SY -- "small talk / model down" --> F
    SY --> V{"verify<br/>grounded? complete?"}
    V -- "ungrounded (first time)" --> SY
    V -- "ungrounded again" --> F
    V -- "incomplete, replans left" --> R["replan<br/>feedback = what is missing"]
    R --> U
    V -- ok --> F["finalize"]
    F --> E((end))
```

- An opening question (no history or summary) is self-contained, so `cache_first` looks it up by its normalised text before any model call: the member's own scope first, then public answers. Messages the deterministic prefilter would block skip the lookup. A follow-up needs the planner's rewrite, so its lookup (`cache_lookup`) runs after `understand`.
- The state is reset on every turn (`initial_state`), so a checkpointed thread never carries one turn's results into the next. The Postgres checkpointer stores the final state per conversation (`thread_id` = conversation id).
- A whole turn is bounded by `TURN_TIMEOUT_SECONDS` (the chat service cancels the graph and answers with the `timeout` fallback).
- `planning.sanitize_plan` repairs the model's plan: it drops unknown tools and empty tasks, renames duplicate IDs, removes forward, self and unknown dependencies, and enforces the step budget.
- An ungrounded draft is rewritten once without the flagged claims. Only if the rewrite also fails is it replaced with the insufficient-context fallback.

## 3. A streamed chat turn

A member asks a question that needs two independent tool steps and one dependent step. Events the agent emits go through the turn task's queue to the SSE response; the diagram draws them straight to the UI for brevity. The login/refresh flow, admin endpoints and the offline eval runner are left out.

```mermaid
sequenceDiagram
    autonumber
    actor M as Member
    participant UI as Streamlit UI
    participant API as FastAPI
    participant RD as Redis
    participant DB as Postgres
    participant T as Turn task
    participant G as LangGraph agent
    participant TL as Tools
    participant L as LLM layer
    participant OR as OpenRouter
    participant W as arq worker

    rect rgba(31,122,90,0.08)
    Note over M,DB: Admission
    M->>UI: types a question
    UI->>API: POST /conversations/{id}/messages/stream (Bearer JWT)
    API->>API: verify JWT, input checks
    API->>RD: per-user rate limit + daily turn cap
    API->>DB: load conversation (not owner → 404)
    API->>RD: SET hg:turn:{id} NX EX (turn lock)
    alt lock already held
        API-->>UI: 409 turn already running
    end
    API->>DB: insert user message, commit
    API->>T: spawn background task (event queue)
    API-->>UI: 200 text/event-stream
    end

    rect rgba(31,122,90,0.04)
    Note over T,DB: Context
    T-->>UI: meta
    T->>DB: user + member profile + dependents
    T->>DB: recent turns, rolling summary, long-term memories, data version
    T->>G: run_graph(initial_state), bounded by TURN_TIMEOUT_SECONDS
    end

    rect rgba(31,122,90,0.08)
    Note over G,OR: Cache-first (opening question only)
    G->>L: embed normalised question
    L->>RD: embedding cache (miss → OpenRouter)
    G->>RD: semantic cache search (member scope, then public)
    alt verified answer cached
        RD-->>G: cached answer + citations
        G-->>T: finalize (no chat-model call)
    end
    end

    rect rgba(31,122,90,0.04)
    Note over G,OR: Understand
    G->>G: prefilter (injection patterns, other member IDs)
    G->>L: FAST structured call (scope, safety, rewrite, plan)
    L->>RD: exact LLM cache lookup
    L->>OR: chat completion with prompt-cached prefix
    OR-->>L: Understanding JSON
    L-->>G: validated Understanding
    G->>G: sanitize_plan → plan_levels (DAG)
    G-->>UI: status, plan
    opt follow-up question
        G->>RD: cache_lookup on the standalone rewrite
    end
    end

    rect rgba(31,122,90,0.08)
    Note over G,OR: Execute, level 0 (independent steps in parallel)
    par s1 sql
        G->>TL: run_sql_tool(task)
        loop until valid, at most MAX_TOOL_RETRIES
            TL->>L: CODE: generate SQL (with last error)
            L->>OR: completion
            TL->>TL: sqlglot guard (one SELECT, allowed tables)
            TL->>DB: SET LOCAL app.role, app.member_id, timeout → SELECT as hg_agent_ro under RLS
        end
        TL-->>G: rows formatted as S sources
    and s2 policy_search
        G->>TL: run_policy_search(task)
        TL->>L: embed query
        TL->>DB: pgvector cosine + full-text ts_rank
        TL->>TL: reciprocal rank fusion
        TL-->>G: chunks formatted as D sources
    end
    G-->>UI: tool running / ok per step
    end

    rect rgba(31,122,90,0.04)
    Note over G,OR: Execute, level 1 (s3 depends_on s1)
    G->>G: prior_context(s1 output)
    G->>TL: run_dataframe_tool(task, prior)
    TL->>L: CODE: generate pandas
    L->>OR: completion
    TL->>TL: AST allow-list → run in process-pool sandbox over CSV lake
    TL-->>G: providers table
    G-->>UI: tool ok
    end

    rect rgba(31,122,90,0.08)
    Note over G,OR: Synthesize and verify
    G->>L: SYNTH stream (question + tagged sources)
    L->>OR: streaming completion
    OR-->>L: tokens
    G-->>UI: token … token, citations
    G->>L: FAST verify (grounded, complete)
    L->>OR: completion
    alt ungrounded, first time
        G->>L: rewrite without the flagged claims
    else ungrounded again
        G-->>UI: answer_replaced (insufficient-context fallback)
    else incomplete and a replan is left
        G->>G: replan → understand → execute r1- steps → synthesize
    else grounded and complete
        G->>G: finalize
    end
    G-->>T: final state
    end

    rect rgba(31,122,90,0.04)
    Note over T,W: Persist and follow up
    T->>DB: assistant message + trace + llm_calls (one transaction)
    T-->>UI: final (answer, plan, steps, citations, usage)
    T->>RD: store in semantic cache (verified turns only, scoped)
    T->>RD: enqueue jobs that have work
    T->>RD: release turn lock (compare-and-delete)
    Note over T: Langfuse trace flushed, Prometheus counters updated
    RD-->>W: title / summarize / extract memories / online eval
    W->>L: FAST or JUDGE calls
    L->>OR: completion
    W->>DB: titles, summaries, memories, eval scores, llm_calls
    end
```

1. **Admission** is synchronous in the request: auth, rate limits, ownership and the turn lock. The user message is committed before any model call.
2. **Context.** The turn task loads the member profile and a token-budgeted memory context, then runs the graph under `TURN_TIMEOUT_SECONDS`. On timeout the graph is cancelled and the `timeout` fallback is saved.
3. **Cache-first.** Only an opening question is self-contained enough to look up before planning. A hit replays a verified answer with no chat-model call.
4. **Understand.** The deterministic prefilter runs first; one FAST call then classifies, rewrites and plans. The plan is untrusted and is repaired into a DAG.
5. **Execute.** Steps in the same dependency level run concurrently inside the one node. A dependent step gets up to 3,000 characters of each prerequisite's output; if a prerequisite failed, it is skipped.
6. **Synthesize and verify.** The answer streams token by token, then the FAST verifier checks it: one rewrite for ungrounded claims, one replan for missing parts.
7. **Persist and follow up.** Message, trace and per-call cost are saved in one transaction. Background jobs are queued only when they have work.

A client disconnect never cancels the turn task, so the answer is always persisted.

## 4. Data model (main tables)

```mermaid
erDiagram
    USERS ||--o{ CONVERSATIONS : owns
    USERS ||--o{ REFRESH_TOKENS : "rotating family"
    USERS ||--o{ USER_MEMORIES : remembers
    CONVERSATIONS ||--o{ MESSAGES : contains
    MESSAGES ||--o| TRACES : "trace_id"
    TRACES ||--o{ LLM_CALLS : "trace_id"
    MESSAGES ||--o{ FEEDBACK : rated
    MESSAGES ||--o{ ONLINE_EVAL_SCORES : judged
    EVAL_RUNS ||--o{ EVAL_RESULTS : has
    USERS }o--o| MEMBERS : "member_id (RLS key)"
    MEMBERS }o--|| PLANS : enrolled
    MEMBERS ||--o{ CLAIMS : has
    MEMBERS ||--o{ PHARMACY_CLAIMS : fills
    MEMBERS ||--o{ PRIOR_AUTHORIZATIONS : requests
    MEMBERS ||--|| MEMBER_ACCUMULATORS : "deductible / OOP"
    DOCUMENTS ||--o{ CHUNKS : "embedding + tsvector"
    USERS {
        uuid id
        string email
        string role "member | admin"
        string member_id
        bool memory_enabled
    }
    CONVERSATIONS {
        uuid id
        text summary "rolling summary"
        timestamptz summary_upto
        bool is_eval
        timestamptz deleted_at
    }
    MESSAGES {
        uuid id
        string role
        text content
        jsonb tools
        jsonb citations
        numeric cost_usd
        int cached_tokens
        bool cache_hit
    }
```

There are three schemas. `app` holds identity, chat, memory, traces, usage and evals. `insurance` holds plans, members, claims and authorizations, with RLS on the member-scoped tables. `rag` holds documents, chunks and the data version.

## 5. Caching layers

```mermaid
flowchart LR
    Q[New question] --> SC{"Semantic answer cache<br/>Redis vector index<br/>scope · data version · prompt version"}
    SC -- "hit (cosine distance ≤ 0.08)" --> A[Verified answer replayed<br/>opening question: no model call<br/>follow-up: one FAST call]
    SC -- miss --> P[Plan + tools + synthesis]
    P --> EX{"Exact LLM cache<br/>temperature-0 calls"}
    EX -- miss --> PC["Provider prompt cache<br/>stable system prefixes<br/>cache_control breakpoints"]
    PC --> OR([OpenRouter])
    P --> EC{"Embedding cache<br/>query embeddings, 7 days"}
```

| Layer | Key | Scope and safety | Measured effect |
|---|---|---|---|
| Semantic answer cache | Normalised raw message for an opening question (looked up before planning); the rewritten query when the turn depends on history (looked up after planning) | `public`, `member:{id}` or `role:admin`. Members read their own scope first. A turn whose answer or rewrite mentions the member's identity or plan is stored privately. Only verified, fully successful turns are stored. | A repeated general question cost $0.0002 instead of about $0.01 when planning ran first; an opening question now hits with no model call |
| Exact LLM cache | Hash of model, fallbacks, messages, params and prompt version | Byte-identical input only. Outputs that fail validation (bad JSON, SQL or pandas the guard, sandbox or database rejected) are dropped so they are never replayed | — |
| Provider prompt cache | Stable prompt prefixes (understand ≈ 4.9k tokens, SQL ≈ 5.8k) | Per provider | 54–60 % of planner and SQL prompt tokens served from cache; repeat turn −40 % cost |
| Embedding cache | Model + text hash | — | The cache key and the memory search embed the same normalised text, so one call serves both |

## 6. Evaluation

```mermaid
flowchart LR
    DB[(Seeded DB + CSV lake + corpus)] --> B["hg-build-golden<br/>71 cases, numeric ground truth"]
    B --> R["hg-eval runner<br/>each case as its persona<br/>through chat_service"]
    R --> C["Deterministic checks<br/>answer rate · refusals · routing<br/>isolation · required content"]
    R --> J["RAGAS jury<br/>GPT · Gemini · Claude judges<br/>mean + spread per metric"]
    C --> GATE{thresholds.yaml}
    J --> GATE
    GATE --> REP["eval_runs / eval_results<br/>Markdown + JSON reports<br/>exit code for CI"]
    LIVE["Live turns"] -- "5 % sample + every 👎<br/>(each answer once)" --> OE["Online jury (worker)<br/>faithfulness · relevancy"] --> DASH[Admin dashboards]
    REP --> DASH
```

`hg-eval --dry-run` prints a cost estimate before anything runs, and every report shows the agent cost and the judge cost (summed from OpenRouter's per-call cost). Judge responses are cached on disk (`backend/evals/.cache/`), so re-running unchanged cases costs nothing. `eval.yml` is manual-only, and the admin API queues one run at a time.

## 7. Security boundaries

```mermaid
flowchart TD
    REQ[Request] --> JWT{"JWT access token<br/>15 min · refresh rotation<br/>reuse revokes the family"}
    JWT --> RL{"Rate limits<br/>auth per IP · chat and feedback per user<br/>daily turn cap per member"}
    RL --> OWN{"Conversation ownership<br/>others get 404"}
    OWN --> PRE{"Prefilter<br/>injection patterns · other members' IDs"}
    PRE --> LLMG{"understand<br/>scope + safety classification"}
    LLMG --> SQLG{"SQL guard (sqlglot)<br/>one SELECT · insurance tables only<br/>function allow-list"}
    SQLG --> RO["hg_agent_ro role<br/>READ ONLY transaction · statement timeout<br/>set_config revoked"]
    RO --> RLS{"Row-Level Security<br/>app.member_id / app.role"}
    LLMG --> SBX{"pandas sandbox<br/>AST allow-list · restricted builtins<br/>separate processes · wall-clock limit"}
    RLS --> VER{"verify<br/>grounded + complete"}
    SBX --> VER
    VER --> OUT[Answer]
```

Notes:
- **RLS visibility.** A member sees their own rows. A subscriber also sees their dependents' rows. A spouse who is not the subscriber sees only their own. Admins see everything. An account with no linked member record sees no member rows.
- **Self-registration.** Self-registered accounts are never linked to member records, because linking requires identity verification. They can ask general plan and policy questions.
- **Sandbox memory limit.** The pandas sandbox enforces an address-space limit only on Linux, where it reads `/proc`. On macOS, development relies on the wall-clock timeout.
- **Long-term memory.** Memory stores only non-clinical preferences. A deterministic filter rejects IDs, amounts, dates, contact details and clinical terms, even when the extraction model proposes them.
- **Logs.** PII (emails, phone numbers, SSNs, dates, member IDs) is masked in structured logs, including values nested in lists and objects.
- **What members see.** Members get each answer's plan, steps, verification verdict and citations. The full trace and the generated SQL / pandas code go to admins only. Access tokens carry only the user id and role.

## Observability

- **Prometheus `/metrics`:** LLM tokens (prompt, completion, cached) and cost per role and model, LLM latency and errors, cache hits and misses per layer, tool outcomes, guardrail blocks, and turn latency. Run one uvicorn worker per container, because counters are per process.
- **OpenTelemetry** (set `OTEL_EXPORTER_OTLP_ENDPOINT`; `docker compose --profile observability` adds Jaeger): FastAPI, SQLAlchemy and httpx spans. There is also one span per graph node (`agent.*`) and one per LLM call (`llm.complete` / `llm.stream`), carrying the model, tokens, cached tokens and cost.
- **Per-turn trace:** each node's input, output, reasoning and timing are stored in `app.traces`, shown in the chat UI and in `GET /admin/traces/{id}`.
- **Admin dashboards:** latency percentiles, cost per turn, prompt-cache ratio, semantic-cache hit rate, outcomes, tools, feedback and online-eval scores.
