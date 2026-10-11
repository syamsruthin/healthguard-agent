"""`providers` tool — NL task → pandas code → sandboxed execution with self-correction."""

from __future__ import annotations

import time
from typing import Any

from tabulate import tabulate

from backend.agents.tools.base import ToolResult, UserContext
from backend.agents.tools.sandbox import SandboxError, run_code
from backend.core.config import settings
from backend.core.logging import get_logger
from backend.core.telemetry import TOOL_EVENTS
from backend.llm import cache as llm_cache
from backend.llm.client import LLMError, get_llm, strip_fences
from backend.llm.prompts.dataframe import build_dataframe_messages
from backend.llm.registry import Role

log = get_logger(__name__)


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


async def run_dataframe_tool(step_id: str, task: str, user: UserContext, prior: str = "") -> ToolResult:
    t0 = time.monotonic()
    llm = get_llm()
    error = ""
    error_code = ""
    code = ""
    attempts = 0
    for attempt in range(settings.max_tool_retries + 1):
        attempts = attempt + 1
        gen = None
        try:
            gen = await llm.complete(
                Role.CODE,
                build_dataframe_messages(
                    task=task, member_context=user.tool_context(), prior=prior, error=error
                ),
                name="providers" if attempt == 0 else f"providers.retry{attempt}",
                use_cache=attempt == 0,
            )
            code = strip_fences(gen.text)
            result = await run_code(code)
            formatted, count, truncated = format_result(result)
            TOOL_EVENTS.labels(tool="providers", outcome="ok" if attempt == 0 else "ok_after_retry").inc()
            return ToolResult(
                step_id=step_id,
                tool="providers",
                task=task,
                ok=True,
                code=code,
                data=result,
                row_count=count,
                truncated=truncated,
                formatted=formatted,
                attempts=attempts,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        except SandboxError as exc:
            error, error_code = (
                f"Sandbox rejected or failed the code: {exc}. Code was:\n{code}",
                "sandbox_error",
            )
            TOOL_EVENTS.labels(tool="providers", outcome="sandbox_error").inc()
        except LLMError as exc:
            error, error_code = str(exc), "llm_unavailable"
            TOOL_EVENTS.labels(tool="providers", outcome="llm_unavailable").inc()
            break
        except Exception as exc:
            error = f"Execution error: {exc.__class__.__name__}: {str(exc)[:300]}. Code was:\n{code}"
            error_code = "sandbox_error"
            TOOL_EVENTS.labels(tool="providers", outcome="exec_error").inc()
        if gen is not None:
            await llm_cache.forget(gen.cache_key)  # a rejected first attempt must not be replayed
        log.info("providers_tool_retry", step=step_id, attempt=attempts, error=error[:200])
    return ToolResult(
        step_id=step_id,
        tool="providers",
        task=task,
        ok=False,
        code=code,
        error=error,
        error_code=error_code,
        attempts=attempts,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
