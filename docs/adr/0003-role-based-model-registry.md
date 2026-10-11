# ADR-0003: Role-based model registry with fallback routing

- **Status:** Accepted (`80fdde7`)
- **Area:** LLM layer

## Context

A single turn makes very different kinds of call:

| Call | Needs | Volume |
|---|---|---|
| `understand`, `verify`, summary, memory, title | Fast, cheap, reliable JSON | Every turn, sometimes twice |
| SQL / pandas generation | Strong at code, deterministic | Once per data step, plus retries |
| Answer synthesis | Good writing, follows citation rules, streams | Once per turn (twice on a rewrite) |
| Evaluation judges | Independent of the agent's model | Offline and sampled |

Using the strongest model everywhere wastes money; using the cheapest everywhere hurts SQL and prose.

## Decision

Map **roles**, not call sites, to models ([backend/llm/registry.py](../../backend/llm/registry.py)):

| Role | Env vars | Temperature | Max tokens | Used by |
|---|---|---|---|---|
| `FAST` | `MODEL_FAST`, `MODEL_FAST_FALLBACKS` | 0.0 | 1,200 | understand, verify, rerank, summaries, memory, titles |
| `CODE` | `MODEL_CODE`, `MODEL_CODE_FALLBACKS` | 0.0 | 1,200 | SQL and pandas generation and self-correction |
| `SYNTH` | `MODEL_SYNTH`, `MODEL_SYNTH_FALLBACKS` | 0.2 | 1,600 | streamed answer |
| `JUDGE` | `EVAL_JUDGES`, `ONLINE_EVAL_JUDGES` | 0.0 | 2,048 | judges (model chosen per call) |

`spec_for(role)` returns a frozen `ModelSpec` (model, fallbacks, temperature, max tokens). Each call site passes a role and a **call name** (`"understand"`, `"sql.retry1"`, `"synthesize"`), and the name is what appears in the cost ledger and in traces.

## Why

- **Right-size each job.** Classification and verification are short structured outputs, so a fast model is enough. Code generation benefits from a stronger model. Prose needs good writing but runs once.
- **Temperature 0 where determinism matters.** Planning, SQL and verification should be repeatable, and temperature-0 calls are the only ones the exact cache stores ([ADR-0027](0027-exact-llm-cache-validated-outputs.md)). Synthesis gets 0.2 for slightly more natural prose.
- **Fallbacks per role.** Each role lists fixed fallback models from **different providers**, so one provider's outage never takes down a role.
- **Names make cost attributable.** "SQL generation is the most expensive call at ~$0.010" ([IMPLEMENTATION_STATUS §3](../IMPLEMENTATION_STATUS.md#3-live-measurements)) is only knowable because every call is named.
- **`Role(role)` is strict.** A plain string that is not a role raises instead of silently falling through to the judge spec.

## Alternatives considered

| Option | Why not |
|---|---|
| One model for everything | Over-pays on the frequent small calls or under-performs on code |
| Choose the model at each call site | Scatters model IDs through the code; no single place to change them |
| Client-side fallback loop | Slower (extra round-trips) and duplicates what OpenRouter's `models` routing does server-side |

## Consequences

- **Positive:** cost tuning is configuration; a role can be moved to a new model and measured by the eval suite.
- **Negative:** three to five model IDs to keep valid. `hg-models-check` verifies every configured ID exists on OpenRouter.

## Interview soundbite

> "I route by role, not by call site: a fast tier for classification and verification, a code tier for SQL and pandas, a synthesis tier for the streamed answer, and fixed judges for evaluation. Each role has cross-provider fallbacks, and each call is named, so cost is attributable to a specific step."
