# ADR-0006: Redis 8 as the shared fast-state layer

- **Status:** Accepted (`d88c015`, `5fd5769`)
- **Area:** Infrastructure

## Context

Several pieces of state are short-lived, hot, and must be shared by every API and worker process:

- the **semantic answer cache** (vector similarity search over past questions),
- the **exact LLM cache** and the **query-embedding cache**,
- **rate-limit counters** (per minute and per day),
- the **per-conversation turn lock**,
- the **background job queue**.

Keeping any of this in process memory breaks as soon as there are two API replicas.

## Decision

Use **Redis 8** for all of it, with distinct key prefixes:

| Use | Key / structure | TTL |
|---|---|---|
| Semantic answer cache | RedisVL `AsyncSearchIndex` (hash + FLAT cosine vector field + tag fields) | 24 h public, 1 h member |
| Exact LLM cache | `hg:llm:<sha256>` | 24 h |
| Embedding cache | per model + text hash | 7 days |
| Rate limits | `hg:rl:<bucket>`, `hg:daily:<yyyymmdd>:<user>` | window / until UTC midnight |
| Turn lock | `hg:turn:<conversation_id>` = turn id | turn timeout + 30 s |
| Jobs | arq queue | `keep_result` 1 h |

Redis runs with `appendonly yes` and `maxmemory-policy allkeys-lru`. `REDIS_URL` must use **database 0**, because Redis search indexes exist only there.

## Why

- **Redis 8 includes the Query Engine** (vector search) in the core image. The semantic cache needs no separate vector service.
- **One fast store for many small jobs** keeps the stack small: Postgres for durable truth, Redis for things that may be lost.
- **Atomic primitives** fit the needs exactly: `SET NX EX` for the lock, `INCR` + `EXPIRE` for rate limits, `WATCH`/`MULTI` for compare-and-delete.
- **arq** is an asyncio job queue on Redis. It shares the event-loop model with the API and needs no broker beyond Redis ([ADR-0032](0032-background-worker-enqueue-seam.md)).
- **Every Redis use fails safe:**
  - caches: a Redis error is logged, counted and treated as a miss;
  - rate limits **fail open** (a Redis outage must not lock out every user);
  - enqueue failures are logged; follow-up work is best effort.
- **`allkeys-lru`** means a full cache evicts old entries instead of rejecting writes.

## Alternatives considered

| Option | Why not |
|---|---|
| In-process caches and locks | Wrong as soon as there are two replicas |
| Postgres for locks and queues (advisory locks, SKIP LOCKED) | Workable, but adds load to the primary database for hot, disposable state |
| Celery + RabbitMQ | A heavier stack and a sync-first model for an asyncio app |
| A separate vector store for the semantic cache | Unneeded; Redis 8 does vector search |

## Consequences

- **Positive:** horizontal scaling of API and worker processes; caches survive restarts (AOF).
- **Negative:** rate limits fail open, so a Redis outage removes the per-user limits until it returns. This is a deliberate availability-over-strictness choice. The daily cap is a cost guard, not a security control.
- **Constraint:** the vector index must live in DB 0, which rules out isolating caches by logical DB number. Key prefixes do that job instead.

## Interview soundbite

> "Postgres holds what must not be lost; Redis holds what's hot and disposable: caches, rate limits, the turn lock and the job queue. Every Redis use fails safe. A cache error is a miss, and a rate-limiter error fails open, because availability matters more than a per-minute counter."
