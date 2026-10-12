# ADR-0010: Parallel execution inside one node, not as graph branches

- **Status:** Accepted (`5fd5769`)
- **Area:** Agent

## Context

"Why was my MRI denied and how do I appeal?" needs a SQL lookup (the claim and denial code) and a policy search (the appeal process). They are independent, so running them in parallel roughly halves tool latency. Dependent steps ("find my network, then search providers in it") must wait.

LangGraph offers parallelism through fan-out (`Send`, multiple edges). Parallel branches that write the same state key need **reducers** (for example `operator.add` on a list) to merge their updates.

## Decision

A single `execute` node ([nodes/execute.py](../../backend/agents/nodes/execute.py)) runs the whole plan:

```python
for level in plan_levels(plan):
    outs = await asyncio.gather(*(_run_step(s, user, results) for s in level))
    results.extend(outs)
```

Per step:

- emits a `tool` event (`running`, then `ok`/`error` with row count, duration, attempts, error code);
- skips the step with `dependency_failed` if any dependency did not succeed;
- passes the **formatted output of its dependencies** as `prior` context to the tool;
- enforces `TOOL_STEP_TIMEOUT_SECONDS` (90 s, including self-correction retries) with `asyncio.wait_for`;
- catches any crash as `tool_error` (tools normally report failures in `ToolResult`);
- drops raw rows (`data`) before the result enters state; the `formatted` table is what synthesis needs.

If **every** step fails, the node sets an error-coded fallback ([ADR-0013](0013-error-coded-fallbacks.md)).

## Why

- **State stays simple.** `AgentState` has **no reducers**, and every key is reset at the start of each turn ([ADR-0015](0015-per-turn-state-reset-checkpointer.md)). Reducers on `results` would accumulate across turns in a checkpointed thread unless reset carefully, a classic source of state leaks.
- **Dependency levels are a scheduling problem, not a graph-topology problem.** The plan is only known at runtime; expressing it as dynamic `Send` branches adds complexity for no benefit.
- **`asyncio.gather` is real concurrency** for I/O-bound tools (model calls, Postgres, the sandbox process pool). The graph tests assert that independent steps overlap in time.
- **Keeping raw rows out of state** keeps checkpoints small (≈50 KB per turn) and avoids persisting up to 200 rows of member data in the checkpoint table.

## Alternatives considered

| Option | Why not |
|---|---|
| LangGraph `Send` fan-out with reducers | Needs reducers and careful per-turn resets; harder to enforce per-level ordering and dependency skipping |
| Run steps sequentially | Simpler, but doubles latency for typical two-source questions |
| A node per tool | Fixed topology can't express "two SQL steps then a provider search" |

## Consequences

- **Positive:** parallelism with simple state; dependency failure handling is explicit; per-step deadlines.
- **Negative:** the graph view shows one `execute` node, so per-step timing comes from the trace and `tool` events rather than LangGraph's node view.
- **Mitigation:** each step's timing, attempts and error code are in the trace and the live `tool` events; OpenTelemetry spans cover each LLM call inside the step.

## Interview soundbite

> "I run the plan's dependency levels with asyncio.gather inside one node instead of LangGraph fan-out. The state has no reducers and is reset every turn, so there's nothing to merge or leak. Independent steps overlap, dependent steps get their prerequisites' output, and a failed dependency skips its dependents with an explicit error code."
