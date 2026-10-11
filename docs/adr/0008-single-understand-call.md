# ADR-0008: One "understand" call for scope, safety, rewrite and plan

- **Status:** Accepted (`5fd5769`)
- **Area:** Agent

## Context

Before any data is fetched, the system must answer four questions about a message:

1. Is it **in scope** (health insurance) or not?
2. Is it **safe** (prompt injection, another member's data, medical advice, self-harm)?
3. What does it mean **on its own**? "What about the second one?" must become "What is the copay for HMO-GLD-STD?" using the conversation.
4. Which **tools**, in which **order**, with which **self-contained tasks**?

The prototype used separate steps for each (validator, rewriter, router), which meant 3–4 sequential model calls before any work started.

## Decision

A single `FAST` structured call returns an `Understanding` object ([schemas.py](../../backend/llm/prompts/schemas.py), [nodes/understand.py](../../backend/agents/nodes/understand.py)):

```python
class Understanding(BaseModel):
    in_scope: bool
    safety: Literal["ok", "prompt_injection", "other_member_data", "medical_advice", "harmful"]
    standalone_query: str          # coreference-resolved, normalised rewrite
    needs_member_data: bool        # drives cache scope
    plan: list[PlanStep]           # 1–4 steps (empty for small talk)
    reason: str                    # one-sentence rationale, stored in the trace
```

The node:

1. runs the **deterministic prefilter** first ([ADR-0019](0019-layered-input-guardrails.md)); a hit blocks with no model call;
2. makes the structured call with the member profile, recent turns, summary, memories and (on a replan) verifier feedback;
3. blocks if `safety != "ok"` or `in_scope` is false, returning a fixed fallback message;
4. repairs the plan with `sanitize_plan` ([ADR-0009](0009-plans-are-untrusted-sanitize-dag.md));
5. sets `cache_scope` from `needs_member_data` ([ADR-0026](0026-scoped-semantic-answer-cache.md)) and emits a `plan` event to the UI.

## Why

- **Latency and cost.** One call instead of three or four. Planning is the first thing on the critical path, and on the router it costs about $0.0002.
- **The decisions share context.** Resolving "that plan" and deciding to query `plan_benefits` need the same history; a safety decision benefits from the same rewrite. Splitting them repeats the context in every call.
- **The rewrite is a product in itself:**
  - it is the semantic-cache key for follow-ups,
  - it is the question shown to the synthesizer and the verifier,
  - it normalises inputs that tools need exactly ("Texas" → "TX", "heart doctor" → "Cardiology", "near me" → the member's zip).
- **Tasks must be self-contained.** Tools do not see the conversation. The prompt requires each task to carry every identifier it needs (plan IDs, network names, dates). This keeps tool prompts small and cacheable.
- **A large, stable prompt prefix** (data catalog, rules, few-shot examples, ≈4.9k tokens) is flagged for provider prompt caching ([ADR-0028](0028-provider-prompt-caching.md)); measured 54–60 % of planner prompt tokens served from cache.

## Alternatives considered

| Option | Why not |
|---|---|
| Separate guard, rewrite and route calls | 3–4× the latency on the critical path; context repeated each time |
| Native function calling to pick tools | Gives "which tool", but not dependencies, a standalone rewrite or a safety verdict in one structured object |
| Rule-based rewriting (the prototype's first version) | Could not resolve pronouns or colloquial specialty names |
| Embedding-based intent router | Cheap, but cannot produce multi-step plans with parameters |

## Consequences

- **Positive:** one call yields everything needed to proceed; the trace stores the model's `reason` for audit.
- **Negative:** a single prompt with many jobs is longer and must be maintained carefully. A model that is bad at one job affects all four.
- **Mitigations:** the deterministic prefilter backs up the safety decision; `sanitize_plan` backs up the plan; the verifier backs up the outcome; the eval suite measures routing accuracy and refusal accuracy separately.

## Interview soundbite

> "A single structured call does guardrail classification, coreference-resolving rewrite and dependency-aware planning. They need the same context, so splitting them would triple latency on the critical path. Its prompt prefix is prompt-cached, and every output is backed by a deterministic check: a regex prefilter for safety and a sanitizer for the plan."
