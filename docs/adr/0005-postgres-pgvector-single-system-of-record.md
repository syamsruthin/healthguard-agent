# ADR-0005: Postgres 17 + pgvector as the single system of record

- **Status:** Accepted (`0dd227d`)
- **Area:** Data

## Context

The system stores four kinds of data:

1. **Identity and chat:** users, refresh tokens, conversations, messages, traces, LLM-call costs, feedback, memories, evaluation runs.
2. **Insurance records:** plans, benefits, formulary, members, claims, pharmacy claims, prior authorizations, accumulators. These must be isolated per member.
3. **Policy corpus:** documents, chunks, embeddings and full-text vectors.
4. **Agent checkpoints** (LangGraph).

The prototype spread these across SQLite files and ChromaDB.

## Decision

One Postgres 17 database with the `vector` extension and **three schemas** ([migration](../../backend/db/migrations/versions/0001_initial_schema.py)):

| Schema | Contents | Who can read it |
|---|---|---|
| `app` | users, conversations, messages, traces, llm_calls, feedback, user_memories, eval runs and results, online eval scores | The application role only |
| `insurance` | plans, plan benefits, formulary, codes, members, claims, pharmacy claims, prior auths, accumulators | The app role, and `hg_agent_ro` (SELECT only, RLS on member tables) |
| `rag` | documents, chunks (`vector(1536)` + generated `tsvector`), `data_version` | The application role |

- Async SQLAlchemy 2 + asyncpg for the app; **Alembic** for migrations.
- A **separate asyncpg pool** logged in as `hg_agent_ro` is the only connection generated SQL ever runs on ([db/session.py](../../backend/db/session.py)).
- Indexes: **HNSW** on `rag.chunks.embedding` (`m=16, ef_construction=64`, cosine ops), **GIN** on the generated `tsv` column, and composite indexes for hot paths (`conversations(user_id, updated_at)`, `messages(conversation_id, created_at)`, `llm_calls(created_at)`).
- The LangGraph `AsyncPostgresSaver` uses its own psycopg pool in the same database.

## Why

- **Security features we need are native.** Roles, `GRANT`, `READ ONLY` transactions, `statement_timeout` and **Row-Level Security** are the foundation of member isolation ([ADR-0017](0017-row-level-security-isolation-boundary.md)). No vector DB offers row-level policies tied to a session variable.
- **Hybrid search in one query engine.** pgvector (dense) and `tsvector`/`ts_rank_cd` (sparse) live next to each other, so hybrid retrieval needs no second store and no sync job ([ADR-0022](0022-hybrid-retrieval-rrf.md)).
- **Transactions across concerns.** The assistant message, its trace and its LLM-call rows are written in **one transaction**. A crash never leaves a message without its cost record.
- **Scale is far within limits.** ≈434 chunks and ≈25k claim rows. pgvector with HNSW handles millions of vectors.
- **Schemas are a permission boundary.** `hg_agent_ro` has `USAGE` on `insurance` only and `search_path = insurance`. It cannot see `app` (password hashes, tokens) or `rag` even if the SQL guard missed something.
- **One backup, one migration tool, one thing to monitor.**

## Alternatives considered

| Option | Why not |
|---|---|
| Postgres + a dedicated vector DB | Extra system, extra auth model, dual writes; no RLS on vectors |
| SQLite (prototype) | No roles, no RLS, single writer |
| One schema | The agent role's reach would be harder to reason about; schema-level grants make it obvious |
| IVFFlat instead of HNSW | IVFFlat needs training on existing data and tuning `lists`; HNSW gives better recall/latency out of the box at this size |

## Consequences

- **Positive:** isolation, hybrid search and audit all come from one well-understood database.
- **Negative:** vector search shares CPU with OLTP; at large scale you would move retrieval to a read replica.
- **Note:** the checkpointer uses psycopg while the app uses asyncpg, so there are two drivers in the process. That is intentional: `AsyncPostgresSaver` requires psycopg.

## Interview soundbite

> "I used one Postgres with three schemas instead of Postgres plus a vector database, because the thing I most needed, Row-Level Security tied to the caller, only exists in the relational engine. pgvector and full-text search give me hybrid retrieval in the same place, and I can write a message, its trace and its cost rows in one transaction."
