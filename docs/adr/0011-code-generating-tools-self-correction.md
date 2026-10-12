# ADR-0011: Code-generating tools with a shared self-correction loop

- **Status:** Accepted (`a718a96`, refactored in `651bdbd`)
- **Area:** Agent / tools

## Context

Members ask open-ended questions over structured data: "How much have I spent on specialists this year?", "Which of my claims were denied and why?", "Spanish-speaking cardiologists within 10 miles accepting new patients". Admins ask portfolio analytics ("top 5 denial reasons"). A fixed set of parameterised API functions would need dozens of endpoints and still miss combinations.

## Decision

Three tools, two of which **generate code**:

| Tool | Data | How |
|---|---|---|
| `sql` | `insurance` schema in Postgres | CODE model writes one PostgreSQL `SELECT` from the task and a schema catalog; it runs behind four safety layers ([ADR-0016](0016-defence-in-depth-for-generated-sql.md)) |
| `providers` | CSV lake (providers, facilities, pharmacies, zip centroids) | CODE model writes pandas that assigns `result`; it runs in a process sandbox ([ADR-0018](0018-process-sandbox-for-pandas.md)) |
| `policy_search` | `rag.chunks` | No code; hybrid retrieval ([ADR-0022](0022-hybrid-retrieval-rrf.md)) |

Both code tools share one loop, `run_codegen` ([tools/codegen.py](../../backend/agents/tools/codegen.py)):

```
for attempt in 0..MAX_TOOL_RETRIES (2):
    code = CODE model(task, member context, prior step output, previous error)
    try: validate + execute → return ToolResult(ok)
    except LLMError: stop at once (model outage, error_code = llm_unavailable)
    except guard / sandbox / database error: feed the error and the code back; retry
    forget the failed output from the exact cache
```

Each tool supplies only a `CodegenTool`: `build_messages(error)`, `execute(code)` and `describe_failure(exc, code)`.

The **data-source catalog** ([core/data_sources.py](../../backend/core/data_sources.py)) is the single description of every table and CSV. It feeds the stable prefix of the understand, SQL and pandas prompts and the SQL guard's table allow-list.

## Why

- **Coverage of open questions.** Text-to-SQL and text-to-pandas answer combinations no one wrote an endpoint for.
- **Self-correction fixes most failures cheaply.** Typical errors (a wrong column name, a missing cast, a disallowed function) are obvious from the error message. Feeding it back usually fixes the code on the next attempt. Metrics distinguish `ok` from `ok_after_retry`.
- **Model outages stop immediately.** Retrying a dead provider wastes the step's deadline; `LLMError` breaks the loop with `llm_unavailable`.
- **Retries bypass the exact cache** (`use_cache=attempt == 0`), and a rejected first attempt is **forgotten**, so the next identical turn does not replay bad SQL ([ADR-0027](0027-exact-llm-cache-validated-outputs.md)).
- **One loop, two tools.** The duplication between SQL and pandas was removed in the engineering review; a new code tool is a few functions.
- **Tools get minimal context.** `UserContext.tool_context()` gives only the member id, plan, network, zip and dependents, not the conversation. This keeps prompts small and cacheable and limits what a code model can leak.

## Alternatives considered

| Option | Why not |
|---|---|
| Fixed parameterised functions only | Safer by construction, but cannot answer open analytical questions; would need constant new endpoints |
| One generic "python" tool over everything | Loses SQL's set-based power on large tables and the database's RLS isolation |
| No self-correction | Many recoverable failures would become "I couldn't find that" |
| Unlimited retries | Unbounded cost and latency |

## Consequences

- **Positive:** broad question coverage, measured self-repair, a small surface for new tools.
- **Negative:** generated code is the riskiest part of the system. It is safe only because of the guard, role, RLS and sandbox layers, which must be maintained.
- **Cost note:** SQL generation was measured as the **most expensive call** (~$0.010 on fixed models; ~5.9k prompt tokens, ~4.2k of them served from the prompt cache).

## Interview soundbite

> "The data tools generate SQL and pandas instead of calling fixed endpoints, because member questions are open-ended. Both share one self-correction loop: errors from the guard, sandbox or database are fed back for up to two retries, model outages stop immediately, and failed outputs are evicted from the cache so they're never replayed. Safety doesn't depend on the code being right: that's the job of the guard, the read-only role, RLS and the sandbox."
