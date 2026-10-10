# HealthGuard architecture

HealthGuard is an agentic RAG assistant for health-insurance members. A LangGraph agent plans which tools to use (SQL over the member's records, pandas over the provider directory, hybrid search over policy documents), runs them in parallel, writes a cited answer, and verifies it before the member sees it. Every turn streams over Server-Sent Events, is persisted with a full trace, and is cost-accounted.

Diagrams render on GitHub. `make diagrams` also exports them as SVG to `docs/diagrams/`.

## 1. System view

```mermaid
flowchart LR
    U([Member / admin browser]) --> UI["Streamlit UI<br/>login · chat · coverage · memory · admin"]
    UI -- "REST + SSE (JWT)" --> API["FastAPI /api/v1<br/>auth · rate limits · chat · admin"]
    API --> G["LangGraph agent<br/>understand → cache → execute → synthesize → verify"]
    API -- "follow-up jobs" --> Q[(Redis 8<br/>arq queue)]
    Q --> W["arq worker<br/>titles · summaries · memories · online eval · offline eval"]
    G --> LLM["LLM layer<br/>per-role models · prompt caching · usage"]
    LLM --> OR([OpenRouter])
    G --> SQL["SQL tool<br/>guard · read-only role · RLS"]
    G --> PD["Provider tool<br/>pandas sandbox"]
    G --> RAG["Policy search<br/>pgvector + full text + RRF"]
    SQL --> PG[("Postgres 17 + pgvector<br/>app · insurance · rag")]
    RAG --> PG
    PD --> CSV[("CSV lake<br/>providers · facilities · pharmacies")]
    API --> PG
    W --> PG
    G <--> SC[("Redis 8<br/>semantic answer cache")]
    LLM <--> EC[("Redis<br/>exact LLM + embedding cache")]
    API -. "/metrics" .-> PROM([Prometheus])
    API -. "OTLP spans" .-> J([Jaeger])
```

## 2. Agent graph

```mermaid
flowchart TD
    S((start)) --> U["understand<br/>prefilter + one FAST call:<br/>scope · safety · rewrite · plan"]
    U -- "blocked / model down" --> F
    U -- "empty plan (small talk)" --> SY
    U -- "replan round" --> X
    U -- "plan" --> C{"cache_lookup<br/>semantic cache"}
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

- The state is reset on every turn (`initial_state`), so a checkpointed thread never carries one turn's results into the next. The Postgres checkpointer stores the final state per conversation (`thread_id` = conversation id).
- `planning.sanitize_plan` repairs the model's plan: it drops unknown tools and empty tasks, renames duplicate IDs, removes forward, self and unknown dependencies, and enforces the step budget.
- An ungrounded draft is rewritten once without the flagged claims. Only if the rewrite also fails is it replaced with the insufficient-context fallback.

## 3. A streamed chat turn

```mermaid
sequenceDiagram
    autonumber
    participant UI as Streamlit
    participant API as FastAPI
    participant T as Turn task
    participant G as LangGraph
    participant OR as OpenRouter
    participant DB as Postgres
    UI->>API: POST /conversations/{id}/messages/stream (JWT)
    API->>API: rate limit · ownership · Redis turn lock
    API->>DB: insert user message
    API-->>UI: 200 text/event-stream
    API->>T: start background task (queue → SSE)
    T-->>UI: meta
    T->>DB: user context · memory context · data version
    T->>G: run graph
    G->>OR: understand (prompt-cached prefix)
    G-->>UI: status, plan
    par independent steps
        G->>DB: generated SQL as hg_agent_ro under RLS
    and
        G->>DB: hybrid policy search
    end
    G-->>UI: tool (running / ok)
    G->>OR: synthesize (stream)
    G-->>UI: token … token, citations
    G->>OR: verify
    T->>DB: assistant message · trace · llm_calls
    T-->>UI: final (answer, citations, usage, trace)
    T->>T: semantic-cache store · enqueue follow-ups · release lock
```

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
    SC -- "hit (cosine distance ≤ 0.08)" --> A[Verified answer replayed<br/>one FAST call, no tools]
    SC -- miss --> P[Plan + tools + synthesis]
    P --> EX{"Exact LLM cache<br/>temperature-0 calls"}
    EX -- miss --> PC["Provider prompt cache<br/>stable system prefixes<br/>cache_control breakpoints"]
    PC --> OR([OpenRouter])
    P --> EC{"Embedding cache<br/>query embeddings, 7 days"}
```

| Layer | Key | Scope and safety | Measured effect |
|---|---|---|---|
| Semantic answer cache | Normalised raw message for a first turn; the rewritten query when the turn depends on history | `public`, `member:{id}` or `role:admin`. Members read their own scope first. A turn whose answer or rewrite mentions the member's identity or plan is stored privately. Only verified, fully successful turns are stored. | A repeated general question costs $0.0002 instead of about $0.01 |
| Exact LLM cache | Hash of model, fallbacks, messages, params and prompt version | Byte-identical input only | — |
| Provider prompt cache | Stable prompt prefixes (understand ≈ 4.9k tokens, SQL ≈ 5.8k) | Per provider | 54–60 % of planner and SQL prompt tokens served from cache; repeat turn −40 % cost |
| Embedding cache | Model + text hash | — | — |

## 6. Evaluation

```mermaid
flowchart LR
    DB[(Seeded DB + CSV lake + corpus)] --> B["hg-build-golden<br/>59 cases, numeric ground truth"]
    B --> R["hg-eval runner<br/>each case as its persona<br/>through chat_service"]
    R --> C["Deterministic checks<br/>answer rate · refusals · routing<br/>isolation · required content"]
    R --> J["RAGAS jury<br/>GPT · Gemini · Claude judges<br/>mean + spread per metric"]
    C --> GATE{thresholds.yaml}
    J --> GATE
    GATE --> REP["eval_runs / eval_results<br/>Markdown + JSON reports<br/>exit code for CI"]
    LIVE["Live turns"] -- "sample + every 👎" --> OE["Online jury (worker)<br/>faithfulness · relevancy"] --> DASH[Admin dashboards]
    REP --> DASH
```

`hg-eval --dry-run` prints a cost estimate before anything runs. Judge responses are cached on disk (`backend/evals/.cache/`), so re-running unchanged cases costs nothing. `eval.yml` is manual-only.

## 7. Security boundaries

```mermaid
flowchart TD
    REQ[Request] --> JWT{"JWT access token<br/>15 min · refresh rotation<br/>reuse revokes the family"}
    JWT --> RL{"Rate limits<br/>auth per IP · chat per user"}
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
- **Logs.** PII (emails, phone numbers, SSNs, dates) is masked in structured logs.

## Observability

- **Prometheus `/metrics`:** LLM tokens (prompt, completion, cached) and cost per role and model, LLM latency and errors, cache hits and misses per layer, tool outcomes, guardrail blocks, and turn latency. Run one uvicorn worker per container, because counters are per process.
- **OpenTelemetry** (set `OTEL_EXPORTER_OTLP_ENDPOINT`; `docker compose --profile observability` adds Jaeger): FastAPI, SQLAlchemy and httpx spans. There is also one span per graph node (`agent.*`) and one per LLM call (`llm.complete` / `llm.stream`), carrying the model, tokens, cached tokens and cost.
- **Per-turn trace:** each node's input, output, reasoning and timing are stored in `app.traces`, shown in the chat UI and in `GET /admin/traces/{id}`.
- **Admin dashboards:** latency percentiles, cost per turn, prompt-cache ratio, semantic-cache hit rate, outcomes, tools, feedback and online-eval scores.
