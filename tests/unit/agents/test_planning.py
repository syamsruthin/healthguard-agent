from __future__ import annotations

from backend.agents.planning import plan_levels, sanitize_plan


def _step(sid: str, tool: str = "sql", deps: list[str] | None = None, task: str = "do it") -> dict:
    return {"id": sid, "tool": tool, "task": task, "depends_on": deps or []}


def test_valid_plan_is_unchanged() -> None:
    plan, warnings = sanitize_plan([_step("s1"), _step("s2", "providers", ["s1"])], max_steps=4)
    assert plan == [_step("s1"), _step("s2", "providers", ["s1"])]
    assert warnings == []


def test_invalid_steps_dropped_and_plan_truncated() -> None:
    raw = [_step("s1", tool="shell"), _step("s2", task=" "), *[_step(f"s{i}") for i in range(3, 9)]]
    plan, warnings = sanitize_plan(raw, max_steps=4)
    assert [s["id"] for s in plan] == ["s3", "s4", "s5", "s6"]
    assert any("truncated" in w for w in warnings)
    assert sum("dropped invalid" in w for w in warnings) == 2


def test_duplicate_and_blank_ids_renamed() -> None:
    plan, _ = sanitize_plan([_step("s1"), _step("s1"), _step("")], max_steps=4)
    ids = [s["id"] for s in plan]
    assert len(set(ids)) == 3 and ids[0] == "s1"


def test_forward_self_and_unknown_dependencies_dropped() -> None:
    raw = [_step("a", deps=["b"]), _step("b", deps=["b", "zzz", "a", "a"])]
    plan, warnings = sanitize_plan(raw, max_steps=4)
    assert plan[0]["depends_on"] == []  # forward reference
    assert plan[1]["depends_on"] == ["a"]  # self / unknown dropped, duplicate collapsed
    assert len(warnings) == 3


def test_replan_prefix_applies_to_ids_and_dependencies() -> None:
    plan, _ = sanitize_plan([_step("s1"), _step("s2", deps=["s1"])], max_steps=4, prefix="r1-")
    assert [s["id"] for s in plan] == ["r1-s1", "r1-s2"]
    assert plan[1]["depends_on"] == ["r1-s1"]


def test_plan_levels_group_independent_steps() -> None:
    plan = [_step("a"), _step("b"), _step("c", deps=["a"]), _step("d", deps=["c", "b"])]
    assert [[s["id"] for s in level] for level in plan_levels(plan)] == [["a", "b"], ["c"], ["d"]]
