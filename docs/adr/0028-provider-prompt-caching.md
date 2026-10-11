# ADR-0028: Provider prompt caching through stable prompt prefixes

- **Status:** Accepted (`80fdde7`)
- **Area:** Caching / cost / prompts

## Context

The planner's instructions (role, rules, the full data catalog, few-shot examples) are ≈4.9k tokens; the SQL generator's (schema catalog, rules, examples) ≈5.8k. They are identical on every call. Providers discount cached input tokens heavily, but only for a **byte-identical prefix**, and Anthropic and Gemini only cache at explicit `cache_control` breakpoints (OpenAI-family models cache automatically).

## Decision

- **Every prompt builder** ([backend/llm/prompts/](../../backend/llm/prompts/)) puts the large, static part first as a system message flagged `cache: True`, and the per-request part (profile, question, sources) last.
- Static prefixes are built once (`@lru_cache`) from the data-source catalog, so they are byte-stable across calls and processes.
- The synthesis prompt adds a **second breakpoint** after the conversation context (memories, summary, recent turns), which grows turn by turn.
- `prepare_messages` converts flagged messages into `cache_control: {type: "ephemeral"}` content parts for models that need markers (Anthropic, Google, and **routers**, which may serve with either), keeping at most the **last 4** (Anthropic's limit).
- Usage parsing records `cached_tokens` and `cache_write_tokens` per call; dashboards show the prompt-cache ratio.

## Why

- **Large static prefixes are the dominant input cost** for planning and SQL; caching them is the cheapest optimisation available.
- **Order matters:** anything variable placed before static text breaks the prefix match. Putting per-request data last is a design rule for every prompt.
- **Routers get markers too:** a router may pick Claude or Gemini; providers that cache automatically ignore the markers.
- **Measured:** 54–60 % of planner and SQL prompt tokens served from cache; a repeated turn cost 40 % less ($0.038 → $0.023).
- **Tools don't see the conversation** ([ADR-0008](0008-single-understand-call.md)), which keeps their prompts short and their prefixes reusable across members.

## Alternatives considered

| Option | Why not |
|---|---|
| Shrink prompts instead | The catalog and examples are what make planning and SQL accurate |
| Fine-tune to remove instructions | Expensive, provider-specific, and slower to iterate than prompt edits |
| Markers only for Anthropic | Routers and Gemini would miss the discount |

## Consequences

- **Positive:** large, automatic savings on every call with a stable prefix.
- **Negative:** prompt edits must preserve prefix stability (no timestamps or per-request data in the static part). The as-of date is a constant (`AS_OF`), which keeps the prefixes that mention it stable.
- **Router caveat:** when the router serves consecutive turns with different models, provider caches don't carry over, so cache hit rates are lower than on a fixed model.

## Interview soundbite

> "Every prompt is laid out static-first: the multi-thousand-token rules, catalog and examples form a byte-stable prefix with explicit cache breakpoints, and per-request data goes last. That served 54–60 % of planner and SQL input tokens from the provider cache and cut a repeated turn's cost by 40 %."
