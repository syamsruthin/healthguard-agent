# ADR-0036: Testing strategy: a scripted FakeLLM, fakes at the seams, paid suites opt-in

- **Status:** Accepted (`de78c35`, `2822d57`, `2467c38`)
- **Area:** Quality / engineering

## Context

LLM applications are hard to test: real model calls are slow, cost money, and are non-deterministic. Yet the riskiest logic (routing, fallbacks, caching rules, isolation, locks) is ordinary code that must be tested exhaustively. The budget for live model calls is limited.

## Decision

A testing pyramid with **every external dependency behind a seam**:

| Seam | Production | Test double |
|---|---|---|
| LLM | `OpenRouterClient` via `get_llm()` | **`FakeLLM`** ([llm/fake.py](../../backend/llm/fake.py)): responses scripted by call name (`"understand"`, `"sql"`, `"verify"` …); values can be strings, models, dicts, lists consumed in order, callables, or **exceptions** to simulate outages; it records usage like the real client |
| Redis | `get_redis()` | `fakeredis` (autouse fixture) |
| Semantic cache | `RedisSemanticCache` | `InMemorySemanticCache` (same scoping, versioning and threshold logic) |
| Job queue | `ArqEnqueuer` | `RecordingEnqueuer` |
| Embeddings | OpenRouter | `HashEmbedder` (deterministic, lexical; never used to judge retrieval quality) |
| OpenRouter HTTP | real | the real client over a **mocked HTTP transport** (tests prompt-cache markers, usage parsing, fallbacks, repair) |

Suites:

| Suite | Needs | Covers | Cost |
|---|---|---|---|
| **Unit** (`make test`) | nothing | SQL guard (safe queries + 15 attack patterns), sandbox escapes, chunking, RRF, plan sanitising, guardrails, JWT, semantic-cache scoping, exact-cache validation, turn-lock ownership, log masking, memory filters and gating, eval checks, judge spend metering, SSE parsing, Streamlit pages (`AppTest`), and **graph flows** (routing, cache-first path, real concurrency, dependencies, fallbacks, replan and rewrite, event order, state isolation) | free |
| **Integration** (`make test-integration`) | Postgres + Redis | RLS matrix, auth rotation and reuse, rate limits, ownership, SSE contract, RLS on generated SQL, persistence, turn lock and deadline, member vs admin visibility, daily limits, coverage, admin endpoints, memory and summarisation, worker tasks, eval runner, Postgres checkpointer, Redis 8 vector cache, data smoke tests | free |
| **Live** (`make test-live`, `HG_LIVE=1`) | OpenRouter key | One real turn per persona through the HTTP API + refusal checks | ≈ $0.20 |
| **Evaluation** (`hg-eval`) | OpenRouter key | Quality ([ADR-0034](0034-evaluation-strategy.md)) | ≈ $0.21–0.40 per small run |

**CI** ([.github/workflows/ci.yml](../../.github/workflows/ci.yml)) on every push and PR: ruff lint + format check, mypy, unit tests, migrate + small seed + hash-embedding ingest, integration tests with a **combined coverage gate of 78 %**, and a Docker image build. Paid suites never run automatically.

## Why

- **The control flow is where the bugs are,** and it's testable without a model: "if `verify` says ungrounded twice, does the member get the fallback and is nothing cached?" is a deterministic question.
- **Scripting by call name** makes tests readable and resilient: a test lists what each step "says", including failures, and asserts on events, state and recorded calls.
- **Exceptions as scripted responses** make outage paths (planner down, synthesis down mid-replan, verifier down) first-class tests.
- **Real Postgres and Redis for isolation tests,** because RLS, roles and vector search can't be faked meaningfully.
- **Live suites catch what fakes can't** (the first live run found an invalid `DATE :asof` bind that broke member login, the verifier flagging phone numbers, the cache never hitting, router reasoning truncating JSON), so they exist, but they're opt-in to respect the budget.

## Consequences

- **Positive:** ~250 free tests run in CI on every push; outage and security paths are tested; paid calls are deliberate.
- **Negative:** fakes can drift from real behaviour (e.g. `HashEmbedder` is lexical). Live runs and the mocked-transport client tests bound that drift.

## Interview soundbite

> "Every external dependency sits behind a seam, so about 250 tests run free on every push. A scripted FakeLLM lets me test every graph path, including model outages, as ordinary code. Isolation is tested against real Postgres RLS. Live model suites exist and found real bugs, but they're opt-in because they cost money."
