# ADR-0004: A cost-optimising router as the default model; fixed models for judges

- **Status:** Accepted (`4d8604d`), evaluation on the router still open
- **Area:** LLM layer / cost

## Context

With fixed Claude models, Maria's MRI-denial turn cost **$0.023–0.039**. Most of that went to calls that do not need a frontier model (planning, verification, simple SQL). OpenRouter offers routers (TypeSafe's Jev Router, `openrouter/auto`) that pick the serving model and reasoning effort per request and bill at the serving model's price.

## Decision

- `MODEL_FAST`, `MODEL_CODE` and `MODEL_SYNTH` default to `typesafe/jev-router`, each with **fixed fallbacks** from different providers.
- Router calls get a **4,096-token output budget** (`ROUTER_MAX_TOKENS`), because a router may choose a reasoning mode whose thinking tokens count against `max_tokens`.
- A **structured-output repair** goes to the **first fixed fallback**, not back to the router.
- Routers receive `cache_control` breakpoints, because they may serve the request with Anthropic or Gemini, which only cache with explicit markers.
- **Judges stay on fixed models** from three families (GPT, Gemini, Claude).

## Why

- **Measured saving.** The same MRI turn on the router cost **$0.0086–0.017**, grounded and complete. The router chose DeepSeek V4.1 Flash for planning, SQL and verification, and once Gemini 3.8 Flash for synthesis.
- **Why 4,096 tokens.** A live run showed the verifier's JSON cut off mid-object: the router's reasoning used up the 1,200-token cap. Only generated tokens are billed, so a higher cap costs nothing when unused.
- **Why repair on a fixed model.** If the router keeps choosing a model that ignores the schema, asking the router again can fail the same way. A known, schema-reliable model breaks the loop.
- **Why judges are not routed.** A jury exists to get independent opinions from different model families. A router could send all three judges to the same model, silently turning a jury into one judge.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep fixed frontier models | 2–3× the cost per turn for no measured quality gain on planning or verification |
| Hand-pick a cheap fixed model per role | Works, but needs re-tuning whenever prices or models change; the router does this per request |
| Route the judges too | Defeats the purpose of a multi-family jury |

## Consequences

- **Positive:** roughly half the cost per turn.
- **Negative:**
  - Cost and latency **vary per request** (one synthesis took 18.5 s).
  - Provider prompt caching helps less when the serving model changes between turns.
  - Calls record the **served** model only, so "router chose X" and "fell back to X" look the same. A `requested_model` column is an open item.
  - **Quality on the router is not yet measured** by the golden suite. The baseline was measured on fixed models ([IMPLEMENTATION_STATUS §7](../IMPLEMENTATION_STATUS.md#7-open-items)).
- **Mitigation:** every role is one environment variable away from a fixed model.

## Interview soundbite

> "The default is a cost-optimising router with fixed cross-provider fallbacks. It halved the cost of a typical turn. I gave router calls a bigger output budget because reasoning tokens truncated my verifier's JSON, I repair structured output on a fixed model, and I deliberately keep the evaluation judges off the router so the jury stays independent. The honest caveat: quality on the router still needs a golden-suite run."
