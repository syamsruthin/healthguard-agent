# ADR-0009: Plans are untrusted input, sanitized into a DAG

- **Status:** Accepted (`f49a526`, `5fd5769`)
- **Area:** Agent

## Context

The planner returns a list of steps with `id`, `tool`, `task` and `depends_on`. Structured output guarantees JSON shape, not meaning. Observed and possible defects:

- unknown tool names or empty tasks,
- duplicate or blank ids,
- dependencies on unknown steps, on **later** steps, on **itself**, or cycles,
- more steps than the budget allows,
- on a replan, ids that collide with the first round's ids.

Executing such a plan could deadlock, crash, or overspend.

## Decision

Treat the plan like any untrusted input. `sanitize_plan` ([backend/agents/planning.py](../../backend/agents/planning.py)) returns `(valid_plan, warnings)`:

- drops steps with an unknown tool or empty task;
- truncates to `MAX_PLAN_STEPS` (4);
- renames blank or duplicate ids (`s1`, `s2`, …);
- keeps only dependencies on **earlier, kept** steps, so the result is acyclic by construction;
- prefixes every id and dependency with the replan round (`r1-`) on a replan.

`plan_levels` then groups the DAG into **dependency levels**; steps in one level run concurrently ([ADR-0010](0010-parallel-execution-inside-one-node.md)). Warnings go into the trace.

## Why

- **"Only depend on earlier steps" makes cycles impossible** without a cycle-detection algorithm. It is a one-pass, O(n) repair.
- **Repair beats reject.** Throwing away a whole plan because one dependency was wrong would waste the planning call and fail the turn. Dropping the bad edge usually leaves a valid plan.
- **The step budget is a cost control.** Four steps × up to three code-generation attempts bounds the code calls per round.
- **Round prefixes** keep replan results distinguishable, so `route_after_execute` can tell whether a replan found anything new (`r1-` results) and keep the previous answer if not.
- **Warnings are observable.** A sanitized plan is visible in the admin trace, which makes planner regressions easy to spot.

## Alternatives considered

| Option | Why not |
|---|---|
| Trust the structured output | Schema-valid JSON can still describe an invalid graph |
| Reject invalid plans and ask again | Costs another model call and adds latency for a defect code can fix |
| Full topological sort with cycle detection | More code for the same result; the "earlier steps only" rule is simpler and also matches how the prompt asks the model to order steps |

## Consequences

- **Positive:** the executor only ever sees a valid DAG; planner mistakes degrade gracefully.
- **Negative:** a dependency the model listed out of order (a later step) is dropped, so two steps that should be sequential may run in parallel. The dependent step then lacks the prior output and may fail or return less. The verifier and replan cover this case.

## Interview soundbite

> "I treat the model's plan as untrusted input. A one-pass sanitizer drops unknown tools, renames duplicate ids, enforces the step budget, and keeps only dependencies on earlier steps. That makes the plan acyclic by construction, so the executor only ever sees a valid DAG."
