"""
Self-correcting code generation, shared by the `sql` and `providers` tools.

generate (CODE model) → validate + execute → on a guard, sandbox or database
error, feed the error back and regenerate, up to MAX_TOOL_RETRIES times. A
model outage stops at once. A rejected first attempt is dropped from the exact
LLM cache so it is never replayed.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from backend.agents.tools.base import ToolResult
from backend.core import observability as obs
from backend.core.config import settings
from backend.core.logging import get_logger
from backend.core.telemetry import TOOL_EVENTS
from backend.llm import cache as llm_cache
from backend.llm.client import LLMError, get_llm, strip_fences
from backend.llm.registry import Role

log = get_logger(__name__)


@dataclass
class Execution:
    data: Any
    formatted: str
    row_count: int
    truncated: bool = False


@dataclass
class CodegenTool:
    name: str  # tool name, also the LLM call name ("sql", "providers")
    build_messages: Callable[[str], list[dict[str, Any]]]  # previous error ("" first) → prompt
    execute: Callable[[str], Awaitable[Execution]]  # generated code → result (raises on rejection)
    # failure → (error fed back to the model, ToolResult.error_code, metrics outcome label)
    describe_failure: Callable[[Exception, str], tuple[str, str, str]]


async def run_codegen(tool: CodegenTool, *, step_id: str, task: str) -> ToolResult:
    t0 = time.monotonic()
    llm = get_llm()
    error = error_code = code = ""
    attempts = 0
    for attempt in range(settings.max_tool_retries + 1):
        attempts = attempt + 1
        gen = None
        # One span per attempt (generation → validation / execution); the error feeds the next attempt.
        with obs.observe(
            f"attempt {attempts}",
            input={"previous_error": error} if error else None,
            metadata={"tool": tool.name},
        ) as attempt_obs:
            try:
                gen = await llm.complete(
                    Role.CODE,
                    tool.build_messages(error),
                    name=tool.name if attempt == 0 else f"{tool.name}.retry{attempt}",
                    use_cache=attempt == 0,
                )
                code = strip_fences(gen.text)
                result = await tool.execute(code)
                TOOL_EVENTS.labels(tool=tool.name, outcome="ok" if attempt == 0 else "ok_after_retry").inc()
                attempt_obs.update(output={"ok": True, "rows": result.row_count}, metadata={"code": code})
                return ToolResult(
                    step_id=step_id,
                    tool=tool.name,
                    task=task,
                    ok=True,
                    code=code,
                    data=result.data,
                    row_count=result.row_count,
                    truncated=result.truncated,
                    formatted=result.formatted,
                    attempts=attempts,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )
            except LLMError as exc:
                error, error_code = str(exc), "llm_unavailable"
                TOOL_EVENTS.labels(tool=tool.name, outcome="llm_unavailable").inc()
                attempt_obs.fail(exc)
                break
            except Exception as exc:  # guard / sandbox / database rejections are fed back to the model
                error, error_code, outcome = tool.describe_failure(exc, code)
                TOOL_EVENTS.labels(tool=tool.name, outcome=outcome).inc()
                attempt_obs.update(output={"ok": False, "error_code": error_code}, metadata={"code": code})
                attempt_obs.fail(f"{outcome}: {str(exc)[:300]}", level="WARNING")
        if gen is not None:
            await llm_cache.forget(gen.cache_key)  # a rejected first attempt must not be replayed
        log.info("tool_retry", tool=tool.name, step=step_id, attempt=attempts, error=error[:200])
    return ToolResult(
        step_id=step_id,
        tool=tool.name,
        task=task,
        ok=False,
        code=code,
        error=error,
        error_code=error_code,
        attempts=attempts,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
