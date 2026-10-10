# HealthGuard — agentic RAG for health-insurance members

HealthGuard answers members' questions about their plan, claims, prescriptions, prior authorizations and in-network care. It plans which data sources to use, queries them safely, writes a cited answer, and checks that answer before anyone sees it.

- **Agent:** a LangGraph plan → execute → synthesize → verify loop. Independent tool steps run in parallel, and incomplete answers trigger one replan.
- **Tools:**
  - SQL over the member's own records, using a guarded read-only role with Postgres Row-Level Security.
  - pandas over the provider directory, in a process sandbox.
  - Hybrid policy search: pgvector, full-text search and reciprocal rank fusion.
- **Models:** OpenRouter with per-role models (fast planner and verifier, stronger code and synthesis models) and fallback routing.
- **Caching:**
  - Provider prompt caching (54–60 % of planner and SQL prompt tokens served from cache).
  - A user-scoped semantic answer cache.
  - An exact LLM cache and an embedding cache.
- **Memory:** a token-budgeted conversation window, a rolling summary, and privacy-filtered long-term preferences.
- **Product:**
  - JWT auth with refresh rotation; SSE streaming chat; feedback; coverage card.
  - A Streamlit UI with admin dashboards.
  - An arq worker for titles, summaries, memories and online LLM-judge scoring.
- **Quality:**
  - A golden set built from the database.
  - `hg-eval`, combining deterministic checks with a RAGAS jury of GPT, Gemini and Claude judges and a CI gate.
  - 200+ tests.

See [docs/architecture.md](docs/architecture.md) for diagrams and [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md) for status and measured results.

## Quick start (local)

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/) ≥ 0.8 and an [OpenRouter](https://openrouter.ai) API key.

```bash
cp .env.example .env        # set OPENROUTER_API_KEY and JWT_SECRET
uv sync --all-groups
make infra                  # Postgres 17 + pgvector, Redis 8
make data                   # migrate → seed synthetic data → embed the policy corpus
```

If port 5432 is already taken on your machine, set `POSTGRES_HOST_PORT=5433` and point `DATABASE_URL` at `localhost:5433`.

Run the three processes in separate terminals:

```bash
make api                    # FastAPI on :8000  (docs at /docs)
make worker                 # arq background worker
make ui                     # Streamlit on :8501
```

Or start the whole stack in containers:

```bash
docker compose up -d --build
```

Sign in at http://localhost:8501 with a demo persona, for example `maria.garcia@demo.healthguard.dev`, using the password in `DEMO_PASSWORD`. Admin pages appear for `admin@demo.healthguard.dev`. [DEMO_QUESTIONS.md](DEMO_QUESTIONS.md) has a scripted demo per persona.

Try the agent without the API or UI:

```bash
uv run hg-chat --as maria.garcia@demo.healthguard.dev "Why was my knee MRI in April denied?"
```

## Tests and evaluation

| Command | What it does | Cost |
|---|---|---|
| `make test` | Unit tests (scripted fake LLM, fakeredis) | free |
| `make test-integration` | Postgres + Redis: RLS, API, SSE, memory, worker, eval runner | free |
| `make lint typecheck` | ruff + mypy | free |
| `make test-live` | One real turn per persona plus refusal checks | ≈ $0.20 |
| `make eval-estimate LIMIT=10` | Golden-suite plan and cost estimate | free |
| `make eval LIMIT=10` | Golden suite with the RAGAS jury; writes reports to `backend/evals/reports/` | ≈ $0.3–0.5 |
| `make models-check` | Checks that the configured model slugs exist on OpenRouter | free |

CI (`.github/workflows/ci.yml`) runs lint, format, mypy, unit tests, migrate + small seed + ingest, integration tests with a combined coverage gate, and a Docker build. `eval.yml` runs the golden suite on demand only.

## Configuration

Every setting is an environment variable; see [.env.example](.env.example) and `backend/core/config.py`.

| Variable | Default | Purpose |
|---|---|---|
| `MODEL_FAST` | `anthropic/claude-haiku-5.5` | Planner, verifier, memory, titles |
| `MODEL_CODE` | `anthropic/claude-sonnet-5.5` | SQL and pandas generation |
| `MODEL_SYNTH` | `anthropic/claude-sonnet-5.5` | Answer synthesis |
| `EVAL_JUDGES` / `ONLINE_EVAL_JUDGES` | GPT-5.4-mini, Gemini 3.8 Flash, Claude Haiku 5.5 | Judge juries |
| `EMBEDDING_PROVIDER` | `openrouter` | `openrouter`, `local` or `hash` (tests) |
| `SEMANTIC_CACHE_ENABLED` | `true` | Scoped semantic answer cache (`REDIS_URL` must use db 0) |
| `VERIFY_REPLACE_UNGROUNDED` | `true` | Replace answers that fail verification after one rewrite |

## Repository layout

```
backend/
  agents/      graph, nodes, planning, tools (SQL guard, sandbox, policy search), guardrails
  api/         FastAPI routers, dependencies, ASGI middleware
  services/    auth, chat pipeline, conversations, coverage, metrics, rate limits
  memory/      context window, rolling summary, long-term memories
  cache/       semantic answer cache
  llm/         OpenRouter client, prompts, usage, embeddings, fake LLM for tests
  workers/     arq tasks and settings
  evals/       golden set, runner, checks, RAGAS jury, reports, online eval
  db/          models, Alembic migrations (RLS, read-only agent role)
  data_gen/    deterministic synthetic insurance data and policy corpus
frontend/      Streamlit app (pages, API client, SSE parser, charts)
tests/         unit · integration · live
docs/          architecture and implementation status
```
