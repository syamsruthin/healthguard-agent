# ADR-0018: A process sandbox for generated pandas code

- **Status:** Accepted (`a718a96`, warm-up fix in `de78c35`)
- **Area:** Security / tools

## Context

The `providers` tool asks the CODE model for pandas code over the provider directory, facilities, pharmacies and zip centroids (distance search, language and network filters, ranking). Executing model-written Python is the classic remote-code-execution risk: `__import__('os').system(...)`, reading files, infinite loops, exhausting memory, or escaping through dunder attributes (`().__class__.__mro__[1].__subclasses__()`).

## Decision

Layered containment ([tools/sandbox.py](../../backend/agents/tools/sandbox.py)):

1. **AST validation** (in the parent, and again in the worker):
   - forbidden nodes: `import`, `global`/`nonlocal`, `class`, `async`/`await`, `yield`, `with`, `raise`, `try`;
   - forbidden names: `eval`, `exec`, `compile`, `open`, `__import__`, `getattr`/`setattr`/`delattr`, `globals`, `locals`, `vars`, `type`, `object`, `super`, `os`, `sys`, `subprocess`, …; any name starting with `__`;
   - forbidden attributes: anything starting with `_`; `read_*`; any `to_*` except a safe allow-list (`to_dict`, `to_list`, `to_numpy`, …); `eval`, `query`, `pipe`, `load(s)`, `save*`, `tofile`, `memmap`, `system`, `popen`, …;
   - string constants containing `__`;
   - the code must assign `result`.
2. **Restricted builtins:** only `abs`, `len`, `min`, `max`, `sorted`, `sum`, `round`, basic types, `zip`, `range` and a few exceptions.
3. **Separate spawned processes:** a `ProcessPoolExecutor` with the `spawn` start method (`SANDBOX_WORKERS = 2`). Each worker loads the CSV lake **once** into memory; each run gets **copies** of the frames, plus two helpers (`distance_miles`, `zip_location`).
4. **Resource limits:** a wall-clock timeout (`SANDBOX_TIMEOUT_SECONDS = 10`); on timeout or crash the **pool is killed and recreated**. On Linux, an address-space `RLIMIT_AS` of current usage + 1 GB.
5. **Output caps:** at most 100 rows serialised, floats rounded, values converted to plain JSON types.
6. **Warm-up outside the timeout:** spawning a worker (importing pandas, loading CSVs) takes about 6 s on a cold machine. `warm_up()` starts every worker at API startup with a separate 120 s budget, so start-up time never counts against the user code's 10 s.

## Why

- **AST allow/deny lists stop the known escape routes** (imports, dunder traversal, file and pickle I/O, `DataFrame.eval`/`query` which evaluate strings).
- **A separate process is the real isolation boundary** for CPU, memory and crashes. A runaway loop or a segfault kills a worker, not the API.
- **`spawn`, not `fork`**, so workers don't inherit the API's memory, open sockets or database connections.
- **Kill on timeout.** Python cannot interrupt a running thread safely; killing the process is the reliable way to stop runaway code.
- **Warm-up fixes a real bug.** Without it, the ~6 s cold start (importing pandas, loading the CSVs) could make the first provider query after a restart exceed its 10 s budget and "time out".
- **The data is read-only and non-sensitive** (a public provider directory), which is why an in-process-pool sandbox is an acceptable trade-off here, unlike for member data.

## Alternatives considered

| Option | Why not (here) |
|---|---|
| `exec` in the API process | One bad snippet takes down or compromises the API |
| Containers / gVisor / Firecracker per execution | Strongest isolation, but heavy for a demo and adds seconds of start-up; the right choice for untrusted code over sensitive data in production |
| Hosted code interpreters (e.g. a provider's code-execution tool) | Data leaves the system; less control; not provider-agnostic |
| No code: fixed provider-search API | Safe, but cannot express "nearest Spanish-speaking cardiologist with telehealth and rating ≥ 4.5" combinations without many parameters |
| SQL over the directory instead of pandas | Viable; pandas was kept to show a second, non-SQL data-lake engine and its sandboxing |

## Consequences

- **Positive:** bounded CPU time, crash containment, no imports or file access, no API memory exposure.
- **Negative / honest limits:**
  - The memory limit is **Linux-only** (it reads `/proc`); on macOS development relies on the timeout.
  - A Python-level AST sandbox is **not** a security boundary against a determined attacker in general; it is defence in depth combined with process isolation and non-sensitive data. In production with sensitive data, use a container or microVM sandbox.

## Interview soundbite

> "Generated pandas runs in a spawned process pool, never in the API process. An AST check blocks imports, dunders, file I/O and string-evaluating methods; builtins are restricted; each run has a 10-second wall clock, and on timeout the pool is killed and rebuilt. I'm explicit that this is acceptable because the directory data is public; for sensitive data I'd move to a gVisor or Firecracker sandbox."
