# ADR-0027: An exact LLM cache that keeps only validated outputs

- **Status:** Accepted (`80fdde7`); validation rule added in `9a1eb6d`
- **Area:** Caching / reliability

## Context

Many temperature-0 calls repeat byte for byte: the same SQL task for the same member, the same verification of the same answer, the same title prompt. Re-paying for them is waste.

The first version cached the output **before the caller validated it**. The engineering review found the consequence: bad JSON, or SQL that the guard or database rejected, was replayed for 24 hours, and every replay paid for a repair or a retry.

## Decision

[backend/llm/cache.py](../../backend/llm/cache.py) and [backend/llm/client.py](../../backend/llm/client.py):

- Cache only **temperature-0, non-streaming** calls.
- **Key:** SHA-256 over `{model, fallbacks, messages, max_tokens, response_format, PROMPT_VERSION}`; TTL 24 h; prefix `hg:llm:`.
- Every `LLMResult` carries its **`cache_key`**, so the caller can act on it:
  - **`structured()`** forgets an entry whose output fails the schema; after a successful **repair**, it stores the repaired output under the **original** key;
  - **`run_codegen`** forgets a first attempt that the SQL guard, the database or the sandbox rejected; retries never read the cache (`use_cache=False`).
- An exact hit is still recorded in the cost ledger as `exact_cache_hit=True`.

## Why

- **Byte-identical input is a strong safety property.** The messages include the member context and the task, so a hit can only return what that exact request produced. No cross-context leakage is possible.
- **Cache validity, not just output.** A cache that stores invalid outputs turns a one-off model mistake into a 24-hour outage for that question. Forgetting on failure, and storing the repaired output, means the cache only ever holds outputs that worked.
- **Including fallbacks and `PROMPT_VERSION` in the key** means a model change or prompt edit naturally misses instead of returning outputs from the old setup.
- **Retries must not read the cache,** or they'd get the same rejected output back.

## Alternatives considered

| Option | Why not |
|---|---|
| Cache everything, including temperature > 0 | Would freeze sampled outputs; synthesis is streamed and temperature 0.2 |
| No exact cache | Pays repeatedly for identical deterministic calls |
| Validate inside the cache layer | The cache can't know what "valid" means for SQL vs JSON vs pandas; the caller can |

## Consequences

- **Positive:** repeated deterministic calls are free, and a bad output is never replayed.
- **Negative:** callers must remember to `forget` on failure. The shared codegen loop and `structured()` handle it for every current caller.
- **Tests:** "bad outputs are not replayed" is a unit test ([tests/unit/test_tool_cache.py](../../tests/unit/test_tool_cache.py)).

## Interview soundbite

> "The exact cache only stores temperature-0 calls, keyed on a hash of the model, fallbacks, full messages, parameters and prompt version. The lesson from my review was that a cache must store validated outputs, not just outputs: results carry their cache key, and anything the schema, SQL guard, database or sandbox rejects is evicted, while a repaired output replaces the bad one."
