"""
Plan validation and scheduling.

The FAST model's plan is untrusted input: ids may repeat or be blank,
`depends_on` may point at unknown or later steps (or form a cycle), and the
plan may exceed the step budget. `sanitize_plan` repairs it so the graph only
ever holds a valid DAG; `plan_levels` groups it into dependency levels that
run concurrently.
"""

from __future__ import annotations

from typing import Any, get_args

from backend.llm.prompts.schemas import ToolName

TOOL_NAMES: frozenset[str] = frozenset(get_args(ToolName))


def sanitize_plan(
    raw: list[dict[str, Any]], *, max_steps: int, prefix: str = ""
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (valid plan, warnings).

    * steps with an unknown tool or empty task are dropped
    * the plan is cut to `max_steps`
    * blank / duplicate ids are renamed; `prefix` (the replan round, e.g. "r1-")
      is applied to every id and dependency
    * only dependencies on *earlier* kept steps survive, so the result is acyclic
    """
    plan: list[dict[str, Any]] = []
    warnings: list[str] = []
    id_map: dict[str, str] = {}  # raw id -> final id (first occurrence wins)
    used: set[str] = set()
    for step in raw:
        tool = step.get("tool")
        task = str(step.get("task") or "").strip()
        raw_id = str(step.get("id") or "").strip()
        if tool not in TOOL_NAMES or not task:
            warnings.append(f"dropped invalid step {raw_id or '?'} (tool={tool!r})")
            continue
        if len(plan) >= max_steps:
            warnings.append(f"plan truncated to {max_steps} steps")
            break
        deps: list[str] = []
        for dep in step.get("depends_on") or []:
            target = id_map.get(str(dep))
            if target is None:
                warnings.append(f"step {raw_id or '?'}: dropped dependency on unknown or later step {dep!r}")
            elif target not in deps:
                deps.append(target)
        final_id = f"{prefix}{raw_id}" if raw_id else ""
        if not raw_id or final_id in used:
            n = len(plan) + 1
            while f"{prefix}s{n}" in used:
                n += 1
            final_id = f"{prefix}s{n}"
            if raw_id:
                warnings.append(f"renamed duplicate step id {raw_id!r} to {final_id!r}")
        used.add(final_id)
        if raw_id and raw_id not in id_map:
            id_map[raw_id] = final_id
        plan.append({"id": final_id, "tool": tool, "task": task, "depends_on": deps})
    return plan, warnings


def plan_levels(plan: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group steps into dependency levels; every step's dependencies are in an earlier level."""
    level_of: dict[str, int] = {}
    levels: list[list[dict[str, Any]]] = []
    for step in plan:  # sanitized plans only depend on earlier steps
        deps = [level_of[d] for d in step.get("depends_on", []) if d in level_of]
        level = max(deps) + 1 if deps else 0
        level_of[step["id"]] = level
        while len(levels) <= level:
            levels.append([])
        levels[level].append(step)
    return levels
