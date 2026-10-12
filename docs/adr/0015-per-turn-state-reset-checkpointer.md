# ADR-0015: Reset agent state every turn; keep the Postgres checkpointer as an audit copy

- **Status:** Accepted (`5fd5769`); reviewed and kept in the engineering review
- **Area:** Agent / persistence

## Context

LangGraph checkpointers persist graph state per `thread_id`, so the next invocation continues from where the last one stopped. The prototype relied on this for memory (`SqliteSaver` with `add_messages`).

That model has problems for a chat product:

- one turn's `results`, `plan`, `verification` or `cache_hit` can **leak into the next turn** unless every field is cleared;
- conversation history becomes an opaque blob that the API, the summary job and the memory job can't query;
- a member deleting a conversation must also delete its checkpoints.

## Decision

- **History lives in `app.messages`**, the source of truth that the API, memory and summary all read ([ADR-0033](0033-three-tier-memory.md)).
- **Every turn starts from `initial_state()`** ([agents/state.py](../../backend/agents/state.py)), which sets **every** key, inputs and outputs alike. The chat service passes in the recent turns, summary and memories it built.
- The **`AsyncPostgresSaver` stays**, keyed by `thread_id = conversation_id`, with `durability="exit"`, so the **final state of each turn** is written once.
- Deleting a conversation soft-deletes it **and** calls `checkpointer.adelete_thread(conversation_id)`.
- Raw tool rows are dropped before they enter state ([ADR-0010](0010-parallel-execution-inside-one-node.md)), so a checkpoint is ≈50 KB.

## Why

- **No cross-turn leaks, by construction.** A test asserts state isolation between turns. Resetting everything is simpler and safer than auditing which fields might carry over.
- **Relational history is queryable.** Windowing by token budget, summarising older turns and extracting memories are all SQL queries over `app.messages`.
- **Why keep a checkpointer at all?** It is effectively an **audit copy** of each turn's final state and demonstrates LangGraph persistence; it is also the hook for future human-in-the-loop interrupts (pause before an action, resume later). The cost is one write per turn.
- **`durability="exit"`** writes once at the end instead of after every node, which keeps write load low.

## Alternatives considered

| Option | Why not |
|---|---|
| Rely on the checkpointer for memory (`add_messages`) | Opaque history; state leaks between turns; hard to window or summarise |
| Drop the checkpointer | Loses the audit copy and the future interrupt/resume hook; the engineering review weighed this and kept it ("showcase value, small cost") |
| Per-node checkpoints (`durability="sync"`) | More writes for no current use (no mid-turn resume) |

## Consequences

- **Positive:** each turn is a pure function of its inputs plus tool results; easy to reason about and test.
- **Negative:** the checkpointer is write-only today: nothing reads it back. It uses a second database driver (psycopg) and pool.

## Interview soundbite

> "Every turn starts from a fully reset state, so nothing leaks between turns, and conversation history lives in a relational messages table that memory and summarisation can query. I kept LangGraph's Postgres checkpointer as an audit copy of each turn's final state and as the hook for future interrupt-and-resume, and deleting a conversation deletes its checkpoints."
