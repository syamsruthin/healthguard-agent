# ADR-0002: OpenRouter behind an OpenAI-compatible client

- **Status:** Accepted (`80fdde7`)
- **Area:** LLM layer

## Context

The agent needs several kinds of model call: fast structured classification, code generation, streamed prose, embeddings and evaluation judges. The best model for each changes often, and any single provider has outages and rate limits. The prototype imported a provider SDK directly in several modules.

## Decision

- All chat calls go through **one client**, `OpenRouterClient` ([backend/llm/client.py](../../backend/llm/client.py)), built on the official `openai` SDK pointed at `LLM_BASE_URL` (default `https://openrouter.ai/api/v1`).
- The client exposes three methods that every caller uses: `complete`, `structured` (JSON schema → Pydantic) and `stream`.
- No other module imports a provider SDK. Code depends on a `Protocol` (`LLMClient`) and gets the instance through `get_llm()` / `set_llm()`, which is also the test seam for `FakeLLM` ([ADR-0036](0036-testing-strategy.md)).
- OpenRouter-specific features are added only when the base URL is OpenRouter: `models` fallback lists and `usage: {include: true}` for per-call cost.

## Why

- **One API, every family.** OpenRouter serves OpenAI, Anthropic, Google, DeepSeek and others behind the OpenAI wire format. Switching a role from Claude to Gemini is an environment variable.
- **Server-side fallbacks.** OpenRouter's `models: [primary, fallback1, fallback2]` retries on another model when the first fails, with no extra client code and no extra round-trip from our side.
- **Exact cost per call.** OpenRouter returns `usage.cost` on each response. That feeds the per-call cost ledger ([ADR-0035](0035-observability.md)) without maintaining a price table.
- **Portability.** Because only the base URL is OpenRouter-specific, the same client works with vLLM, Ollama, Azure OpenAI or any OpenAI-compatible endpoint.
- **Cross-cutting concerns live in one place:** prompt-cache markers, exact cache, usage accounting, OpenTelemetry spans, retries with backoff (`max_retries=3`), structured-output repair, and stream cleanup.

## Details worth knowing

- **Structured output with a fallback.** `structured()` sends `response_format: json_schema`. If a provider rejects it (`BadRequestError`), the call is retried without it and the schema in the prompt carries the contract. The output is then validated with Pydantic. On failure there is exactly **one repair call** that shows the model its bad output and the validation error.
- **Streams are always accounted.** The `finally` block in `stream()` closes the HTTP stream and records the call even when the consumer stops early (client left, turn deadline hit). Before this fix an abandoned stream left its span open and its cost unrecorded.
- **Errors are normalised** to `LLMError`, so the graph can tell "model unavailable" from "bad data" ([ADR-0013](0013-error-coded-fallbacks.md)).

## Alternatives considered

| Option | Why not |
|---|---|
| Call each provider's SDK directly | N integrations to maintain; fallbacks across providers become our problem |
| LangChain chat-model wrappers | Extra abstraction over the same HTTP calls; harder to control cache markers, usage parsing and stream cleanup precisely |
| LiteLLM proxy | A good option, but one more service to run; OpenRouter already provides routing, fallbacks and cost |
| A single provider | No protection against outages, rate limits or price changes |

## Consequences

- **Positive:** model choice is configuration; outages degrade to fallbacks; cost is exact.
- **Negative:** OpenRouter is a dependency and a data processor in the request path (relevant for real PHI; see the HIPAA question in the [interview guide](../interview/INTERVIEW_GUIDE.md)).
- **Mitigation:** `LLM_BASE_URL` can point at a self-hosted or BAA-covered endpoint without code changes.

## Interview soundbite

> "Every model call goes through one OpenAI-compatible client. That single choke point is where I put fallbacks, caching, cost accounting, tracing and structured-output repair, and it's why changing a model is a config change, not a code change."
