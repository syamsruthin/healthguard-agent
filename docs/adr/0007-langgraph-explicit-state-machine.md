# ADR-0007: LangGraph state machine with explicit, deterministic routing

- **Status:** Accepted (`5fd5769`)
- **Area:** Agent

## Context

A member question may need zero, one or several data sources, in a particular order, followed by a check of the answer. The control flow has branches (blocked, small talk, cache hit), loops (rewrite an ungrounded answer, replan an incomplete one) and must be **bounded**: a turn must end within a known number of model calls and seconds.

Two broad designs exist:

1. **Autonomous tool-calling loop (ReAct):** the model decides, call by call, which tool to run next until it decides to stop.
2. **Explicit state machine:** code defines the nodes and the edges; models fill in the content at each node.

## Decision

Use a **LangGraph `StateGraph`** ([backend/agents/graph.py](../../backend/agents/graph.py)) with eight nodes and **pure-Python routing functions**:

```
START ─┬─ opening question → cache_first ─┬─ hit ─────────────→ finalize
       │                                  └─ miss ─┐
       └─ follow-up ───────────────────────────────┴→ understand
understand ─┬─ blocked / model down ───────────────→ finalize
            ├─ small talk (empty plan) → synthesize → finalize
            ├─ opening question (cache already checked) → execute
            └─ follow-up → cache_lookup ─┬─ hit → finalize
                                         └─ miss → execute
execute → synthesize → verify ─┬→ finalize
                               ├→ synthesize   (rewrite without unsupported claims, at most MAX_REVISIONS=1)
                               └→ replan → understand → execute …   (at most MAX_REPLANS=1)
```

Routing functions (`route_start`, `route_after_understand`, `route_after_verify`, …) read state fields such as `outcome`, `plan`, `cache_hit`, `revise` and `verification.complete`. They make **no model calls**.

## Why

- **Bounded cost and latency.** The worst case is countable: one understand, one replan understand, up to 4 steps × (1 + 2 retries) code calls per round, two syntheses, two verifications. A free ReAct loop has no such ceiling.
- **Predictable behaviour for a regulated domain.** Every member question goes through the same checkpoints: guardrail, plan, isolated data access, cited synthesis, verification. A model cannot "decide" to skip verification.
- **Testable routing.** Because the edges are plain functions over state, the graph-flow tests ([tests/unit/agents/test_graph_flows.py](../../tests/unit/agents/test_graph_flows.py)) drive every path with a scripted `FakeLLM`: cache-first hit, blocked, small talk, dependencies, fallbacks, replan, rewrite, event order.
- **LangGraph gives the plumbing for free:** typed state, conditional edges, streaming of custom events (`get_stream_writer`) and updates, and a Postgres checkpointer.
- **Models do what they're good at:** classification, planning, code, prose, judgement. Code does what it's good at: control flow, limits, safety.

## Alternatives considered

| Option | Why not |
|---|---|
| ReAct / native tool-calling loop | Unbounded call count; harder to guarantee verification and isolation steps; harder to test |
| Hand-written orchestrator (the prototype) | Re-implements streaming, state and checkpointing; the prototype's 900-line orchestrator was hard to extend |
| Multi-agent (supervisor + specialists) | More model calls and hand-offs for a problem one planner solves; adds latency and cost |
| Fixed pipeline with no loops | Cannot recover from an incomplete answer or an unsupported claim |

## Consequences

- **Positive:** bounded, inspectable, testable; each node is traced and streamed ([ADR-0035](0035-observability.md)).
- **Negative:** less flexible than an open-ended agent for questions that need many hops. Plans are capped at 4 steps and one replan.
- **Mitigation:** the plan itself is a dependency graph ([ADR-0009](0009-plans-are-untrusted-sanitize-dag.md)), so multi-hop questions ("find my plan's network, then in-network cardiologists") still work within one round.

## Interview soundbite

> "I chose a deterministic state machine over an open ReAct loop. Models plan, write code and judge, but the code decides the control flow, so every answer passes through guardrails, isolated data access and verification, and the worst-case number of model calls per turn is known."
