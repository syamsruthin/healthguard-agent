# ADR-0032: A background worker (arq) behind an enqueue seam

- **Status:** Accepted (`06ba0bc`)
- **Area:** Runtime

## Context

Some work should not delay the member's answer:

- titling a new conversation,
- folding old turns into the rolling summary,
- extracting long-term memories,
- judging a sample of live answers (online evaluation),
- running the golden evaluation suite from the admin UI (minutes to hours).

## Decision

- **arq** worker ([workers/settings.py](../../backend/workers/settings.py)) on Redis with five tasks: `title_conversation`, `summarize_conversation`, `extract_memories`, `online_eval`, `run_offline_eval` (3 h timeout; others 300 s). `max_jobs = 10`.
- **Enqueue seam** ([workers/queue.py](../../backend/workers/queue.py)): the API depends on an `Enqueuer` protocol with three implementations:
  - `ArqEnqueuer` (production): errors are logged, never raised;
  - `NullEnqueuer` (default when no queue is configured);
  - `RecordingEnqueuer` (tests): records job names and ids.
- **Idempotent job ids:** `title:{cid}`, `summarize:{cid}:{assistant_id}`, `memory:{assistant_id}`, `oe:{assistant_id}`.
- **Jobs are gated** on having work ([ADR-0029](0029-spend-guards-gated-background-work.md)).
- **Every task is cost-accounted:** it runs inside its own usage collector and persists its LLM calls to `app.llm_calls` (without a trace id), so background cost appears on the dashboards.
- **Task-level safety:** `summarize_conversation` takes its own Redis lock so two workers never summarise the same conversation; tasks re-check preconditions (title still default, memory still enabled, conversation not deleted).

## Why

- **The member waits only for the answer.** Titles, summaries and memories improve later turns, not this one.
- **arq fits the stack:** asyncio-native like the API, Redis-backed (already present), small, and it shares the same service code (database sessions, LLM client).
- **The seam keeps the API independent of the worker.** If the worker is down, turns still work; tests assert which jobs were queued without running them.
- **Idempotent ids + re-checked preconditions** make retries and duplicate enqueues harmless.
- **Long evaluation runs belong off the request path** with their own timeout.

## Alternatives considered

| Option | Why not |
|---|---|
| FastAPI `BackgroundTasks` | Runs in the API process; lost on restart; no retries or separate scaling |
| Celery | Sync-first, heavier, needs its own broker setup |
| Do it inline in the turn | Adds seconds and model calls to every answer |

## Consequences

- **Positive:** fast turns, independently scalable background work, testable enqueue behaviour.
- **Negative:** at-least-once execution means tasks must stay idempotent; the worker container has no health check (arq has no HTTP endpoint).

## Interview soundbite

> "Anything that doesn't change this answer, like titles, summaries, memory extraction and answer judging, goes to an arq worker through an enqueue interface. Jobs have idempotent ids, re-check their preconditions, and record their own LLM cost. If the worker is down, chat still works."
