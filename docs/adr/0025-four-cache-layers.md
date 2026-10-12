# ADR-0025: Four cache layers, each with its own key and safety rule

- **Status:** Accepted (`80fdde7`, `5fd5769`, `9a1eb6d`, `f2bc4e2`)
- **Area:** Caching / cost

## Context

A turn makes several model calls, the largest of which re-send the same multi-thousand-token instructions every time. Many questions repeat ("what's covered for telehealth?"). But caching in a multi-tenant health app is dangerous: a cached answer must never cross from one member to another, and a cached **wrong** answer is worse than no cache.

## Decision

Four layers, from the coarsest (whole answers) to the finest (prompt prefixes):

| Layer | What it saves | Key | Safety rule | ADR |
|---|---|---|---|---|
| **Semantic answer cache** | The whole turn (planning, tools, synthesis, verification) | Embedding of the normalised opening question, or of the standalone rewrite for follow-ups | Scoped (`public` / `member:{id}` / `role:admin`); only verified, fully successful answers; tagged with data and prompt versions | [0026](0026-scoped-semantic-answer-cache.md) |
| **Exact LLM cache** | One temperature-0 call | SHA-256 of model, fallbacks, messages, params, prompt version | Byte-identical input only; outputs that fail validation are forgotten | [0027](0027-exact-llm-cache-validated-outputs.md) |
| **Provider prompt cache** | The stable prefix of each prompt | Provider-side, on the prefix | Per provider; no cross-user data in the cached prefix | [0028](0028-provider-prompt-caching.md) |
| **Embedding cache** | Query embeddings | Model + text hash, 7 days | No answers stored; one embedding serves cache lookup and memory search | — |

Every cache:

- is **optional** (an error is logged, counted as `hg_cache_events_total{layer, outcome="error"}`, and treated as a miss);
- has a **switch** (`SEMANTIC_CACHE_ENABLED`, `LLM_EXACT_CACHE_ENABLED`, `PROMPT_CACHE_ENABLED`);
- is **versioned** where it can go stale (`PROMPT_VERSION`, `rag.data_version`).

## Why

- **Different granularities catch different repetition.** A repeated question hits the semantic cache; a new question that plans the same SQL task hits the exact cache; every call benefits from the prompt cache.
- **Correctness and isolation come first.** Each layer's key is chosen so a hit can only return something the caller was already entitled to see.
- **Measured effect:**
  - prompt cache: 54–60 % of planner and SQL prompt tokens served from cache; a repeated turn cost 40 % less ($0.038 → $0.023);
  - semantic cache: a general question already answered cost $0.0002 instead of ~$0.01 (when planning still ran first), and opening questions now hit with no model call at all.

## Alternatives considered

| Option | Why not |
|---|---|
| Only a semantic cache | Misses partial reuse (same SQL task inside a new question) and prefix savings |
| One global (unscoped) answer cache | Cross-member leakage |
| HTTP-level response caching | Responses are personalised, streamed and POST; not cacheable at that layer |

## Consequences

- **Positive:** large savings on repeats, modest savings on everything, no correctness or isolation regressions.
- **Negative:** four invalidation stories to understand. `PROMPT_VERSION` (bump on any prompt change) and `data_version` (bumped by ingest) are the two levers.

## Interview soundbite

> "I cache at four granularities: whole verified answers in a scoped semantic cache, deterministic calls in an exact cache, prompt prefixes at the provider, and query embeddings. Each layer's key is designed so a hit can only return something the caller could already see, and every layer is versioned and fails open as a miss."
