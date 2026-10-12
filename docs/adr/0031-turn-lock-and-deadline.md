# ADR-0031: One turn per conversation: an owned Redis lock and a turn deadline

- **Status:** Accepted (`ea1eaeb`, hardened in `9a1eb6d`)
- **Area:** API / reliability

## Context

Two concurrent turns in the same conversation (double-click, two tabs, a retrying client) would both read the same history, both write answers, and interleave in the UI. The original design used a 180 s Redis lock with **no turn deadline** and an **unconditional delete** on release. The review found two bugs:

1. A slow turn could outlive the lock; a second turn would then acquire it; when the first turn finished, its unconditional `DEL` released **the second turn's** lock.
2. Nothing bounded how long a turn could run.

## Decision

- **Acquire:** `SET hg:turn:{conversation_id} {turn_id} NX EX (TURN_TIMEOUT_SECONDS + 30)`. If the key exists, return `409 turn_in_progress`.
- **Deadline:** the graph runs inside `asyncio.timeout(TURN_TIMEOUT_SECONDS)` (150 s). On timeout the turn ends with the `timeout` fallback, an `error` event (`turn_timeout`, retryable) and an `answer_replaced` event; the partial state is still persisted.
- **Release:** **compare-and-delete** under `WATCH`/`MULTI`: delete only if the value is still this turn's id; a `WatchError` means the lock changed hands, so leave it.
- The lock is released in a `finally` (`run_turn`), and also if persisting the user message fails.

## Why

- **The lock must outlive the work it protects.** With a 150 s deadline and a 180 s lock, the lock can only expire if the process died. That is exactly when it should expire on its own.
- **Owned locks** (value = turn id) plus compare-and-delete is the standard safe-release pattern: a turn can only release its own lock.
- **A deadline bounds cost and user wait.** Without it, a stuck provider or a pathological plan could hold the conversation indefinitely.
- **Per-step timeouts are not enough** on their own: a turn has several model calls and possibly a replan; only a turn-level deadline bounds the sum.

## Alternatives considered

| Option | Why not |
|---|---|
| No lock | Interleaved turns and inconsistent history |
| Postgres advisory locks | Tied to a DB connection held for the whole turn; Redis is the shared fast-state layer |
| Redlock across several Redis nodes | Unnecessary for a single Redis; the failure mode (rare double turn) is not safety-critical |
| Queue turns instead of rejecting | Hides latency; a 409 lets the UI tell the user a reply is still coming |

## Consequences

- **Positive:** at most one turn per conversation; no cross-turn lock theft; every turn ends within 150 s.
- **Negative:** if the process dies, the conversation is blocked for up to 180 s (the TTL).
- **Tests:** lock ownership and compare-and-delete are unit-tested ([tests/unit/test_turn_lock.py](../../tests/unit/test_turn_lock.py)); the deadline is an integration test.

## Interview soundbite

> "Each conversation runs one turn at a time under a Redis lock whose value is the turn id. Release is compare-and-delete, so a slow turn can never delete someone else's lock, and the lock TTL is the turn deadline plus 30 seconds, so it only expires on its own if the process died."
