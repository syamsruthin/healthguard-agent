"""`providers` tool — NL task → pandas code → sandboxed execution, with the shared self-correction loop."""

from __future__ import annotations

from typing import Any

from tabulate import tabulate

from backend.agents.tools.base import ToolResult, UserContext
from backend.agents.tools.codegen import CodegenTool, Execution, run_codegen
from backend.agents.tools.sandbox import SandboxError, run_code
from backend.core import observability as obs
from backend.llm.prompts.dataframe import build_dataframe_messages


def format_result(result: dict[str, Any]) -> tuple[str, int, bool]:
    kind = result.get("type")
    if kind == "table":
        rows = result["rows"]
        total = int(result.get("total_rows", len(rows)))
        if not rows:
            return "(no matching rows)", 0, False
        table = tabulate([list(r.values()) for r in rows], headers=result["columns"], tablefmt="github")
        more = f"\n({len(rows)} of {total} rows shown)" if total > len(rows) else ""
        return table + more, total, total > len(rows)
    if kind == "dict":
        return "\n".join(f"- {k}: {v}" for k, v in result["value"].items()), len(result["value"]), False
    if kind == "list":
        return "\n".join(f"- {v}" for v in result["value"]), len(result["value"]), False
    return f"Result: {result.get('value')}", 1, False


def _describe_failure(exc: Exception, code: str) -> tuple[str, str, str]:
    if isinstance(exc, SandboxError):
        return (
            f"Sandbox rejected or failed the code: {exc}. Code was:\n{code}",
            "sandbox_error",
            "sandbox_error",
        )
    detail = f"{exc.__class__.__name__}: {str(exc)[:300]}"
    return f"Execution error: {detail}. Code was:\n{code}", "sandbox_error", "exec_error"


async def _execute(code: str) -> Execution:
    with obs.observe("sandbox.run", input=code) as o:
        result = await run_code(code)
        formatted, count, truncated = format_result(result)
        o.update(
            output=formatted,
            metadata={"result_type": result.get("type"), "rows": count, "truncated": truncated},
        )
    return Execution(result, formatted, count, truncated)


async def run_dataframe_tool(step_id: str, task: str, user: UserContext, prior: str = "") -> ToolResult:
    tool = CodegenTool(
        name="providers",
        build_messages=lambda error: build_dataframe_messages(
            task=task, member_context=user.tool_context(), prior=prior, error=error
        ),
        execute=_execute,
        describe_failure=_describe_failure,
    )
    return await run_codegen(tool, step_id=step_id, task=task)
