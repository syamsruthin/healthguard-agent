# ADR-0001: Rebuild the prototype as a production-style system

- **Status:** Accepted (Milestone 10, commits `d88c015` … `3acb639`)
- **Area:** Foundations

## Context

HealthGuard started as a demo (Milestones 1–9 in [specs/RoadMap.md](../../specs/RoadMap.md)):

| Concern | Prototype | Problem it caused |
|---|---|---|
| LLM | Groq, one model | No fallbacks, no cost accounting, one model for every job |
| Structured data | SQLite (`insurance.db`) with 50 plan rows | No roles, no Row-Level Security, no concurrency |
| Documents | ChromaDB + BM25 in process | A second datastore to operate; index lived in the API process |
| Memory | SQLite `SqliteSaver` checkpointer | History, state and audit mixed in one opaque blob |
| Isolation | Prompt instructions only | Nothing stopped generated SQL from reading another member's rows |
| Quality | 5 manual QA checks | No measured quality, no regression gate |
| Ops | Logs to JSONL and Markdown files | No metrics, no traces, no per-turn cost |

The prototype proved the idea (route a question to SQL, a CSV and RAG, then synthesise). It could not be put in front of real members: one member could read another's data, a provider outage took the whole app down, and nobody knew what a turn cost or whether answers were correct.

## Decision

Rebuild on a production-style stack while keeping the product idea:

- **OpenRouter** (OpenAI-compatible) with per-role models and fallbacks ([ADR-0002](0002-openrouter-openai-compatible-gateway.md), [ADR-0003](0003-role-based-model-registry.md)).
- **Postgres 17 + pgvector** as the one system of record for identity, chat, insurance data and vectors ([ADR-0005](0005-postgres-pgvector-single-system-of-record.md)).
- **Redis 8** for caches, locks, rate limits and the job queue ([ADR-0006](0006-redis-shared-fast-state.md)).
- **LangGraph** for the agent, with plan → parallel execute → synthesize → verify ([ADR-0007](0007-langgraph-explicit-state-machine.md)).
- **Isolation enforced by the database** (read-only role + RLS), not by the prompt ([ADR-0016](0016-defence-in-depth-for-generated-sql.md), [ADR-0017](0017-row-level-security-isolation-boundary.md)).
- **Measured quality** (golden set, judge jury, CI gate) and **measured cost** (every LLM call recorded) ([ADR-0034](0034-evaluation-strategy.md), [ADR-0035](0035-observability.md)).

The work was delivered in phases, one commit per phase, each with tests (see [IMPLEMENTATION_STATUS §1](../IMPLEMENTATION_STATUS.md#1-phase-history)).

## Why

- **Security had to move out of the prompt.** A prompt is a request, not a control. A model that is jailbroken or simply wrong must still be unable to read another member's claims. Only the database can guarantee that.
- **Fewer moving parts is safer to operate.** Postgres can hold relational data, vectors (pgvector) and full-text indexes. Dropping ChromaDB removed a datastore, a backup story and a consistency problem.
- **Cost and quality must be visible to be managed.** An agent makes 3–8 model calls per turn. Without per-call accounting and evaluation you cannot tell whether a change made things cheaper, better, or both.
- **Provider independence.** Model prices and quality change monthly. The app should change models through configuration, not code.

## Alternatives considered

| Option | Why not |
|---|---|
| Harden the prototype in place | SQLite has no roles or RLS, ChromaDB in process does not scale past one API instance, and the custom orchestrator had no clean place for parallelism or verification |
| A managed agent platform | Hides the parts this project exists to demonstrate (planning, isolation, caching, evaluation) and locks in a vendor |
| A separate vector database (Pinecone, Weaviate, Qdrant) | One more system to secure and keep in sync; the corpus (≈434 chunks) is far below the scale where pgvector struggles |

## Consequences

- **Positive:** isolation by construction, provider outages degrade instead of failing, every turn is auditable and cost-accounted, quality is measured.
- **Negative:** more infrastructure for a developer to run (Docker for Postgres and Redis), and more code (≈16k lines of backend).
- **Mitigations:** `make infra data` and `docker compose up` bring everything up; unit tests need neither Postgres nor Redis ([ADR-0036](0036-testing-strategy.md)).

## Interview soundbite

> "The prototype answered questions; it couldn't be trusted with them. The rebuild moved isolation from the prompt into Postgres, made every model call cost-accounted, and added an evaluation gate, so I can prove the system is safe, know what it costs, and measure whether a change made it better."
