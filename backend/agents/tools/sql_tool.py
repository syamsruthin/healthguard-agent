"""
`sql` tool — NL task → validated SQL → read-only, RLS-scoped execution,
with a self-correction loop that feeds guard/DB errors back to the model.
"""

from __future__ import annotations

import time
from typing import Any

from tabulate import tabulate

from backend.agents.tools.base import MEMBER_ID_RE, ToolResult, UserContext, jsonable
from backend.agents.tools.sql_guard import SQLGuardError, validate_sql
from backend.core.config import settings
from backend.core.logging import get_logger
from backend.core.telemetry import TOOL_EVENTS
from backend.db.session import get_agent_pool
from backend.llm import cache as llm_cache
from backend.llm.client import LLMError, get_llm, strip_fences
from backend.llm.prompts.sql import build_sql_messages
from backend.llm.registry import Role

log = get_logger(__name__)
_DISPLAY_ROWS = 50


def format_rows(rows: list[dict[str, Any]], total: int, truncated: bool) -> str:
    if not rows:
        return "(query returned no rows)"
    shown = rows[:_DISPLAY_ROWS]
    table = tabulate([list(r.values()) for r in shown], headers=list(shown[0].keys()), tablefmt="github")
    note = ""
    if total > len(shown) or truncated:
        note = f"\n({len(shown)} of {total}{'+' if truncated else ''} rows shown)"
    return table + note


async def execute_scoped(sql: str, user: UserContext, *params: Any) -> tuple[list[dict[str, Any]], bool]:
    """Run validated (or fixed, parameterised) SQL as the read-only agent role with this user's RLS context."""
    role = "admin" if user.is_admin else "member"
    member_id = user.member_id if (user.member_id and MEMBER_ID_RE.match(user.member_id)) else ""
    pool = await get_agent_pool()
    async with pool.acquire() as conn, conn.transaction(readonly=True):
        # Values are validated above (regex / fixed set) — SET does not accept bind parameters.
        await conn.execute(f"SET LOCAL app.role = '{role}'")
        await conn.execute(f"SET LOCAL app.member_id = '{member_id}'")
        await conn.execute(f"SET LOCAL statement_timeout = {int(settings.sql_statement_timeout_ms)}")
        records = await conn.fetch(sql, *params)
    rows = [jsonable(dict(r)) for r in records]
    truncated = len(rows) >= settings.sql_max_rows
    return rows, truncated


async def run_sql_tool(step_id: str, task: str, user: UserContext, prior: str = "") -> ToolResult:
    t0 = time.monotonic()
    llm = get_llm()
    error = ""
    error_code = ""
    sql = ""
    attempts = 0
    for attempt in range(settings.max_tool_retries + 1):
        attempts = attempt + 1
        gen = None
        try:
            gen = await llm.complete(
                Role.CODE,
                build_sql_messages(task=task, member_context=user.tool_context(), prior=prior, error=error),
                name="sql" if attempt == 0 else f"sql.retry{attempt}",
                use_cache=attempt == 0,
            )
            sql = strip_fences(gen.text)
            safe = validate_sql(sql, max_rows=settings.sql_max_rows)
            rows, truncated = await execute_scoped(safe, user)
            TOOL_EVENTS.labels(tool="sql", outcome="ok" if attempt == 0 else "ok_after_retry").inc()
            return ToolResult(
                step_id=step_id,
                tool="sql",
                task=task,
                ok=True,
                code=sql,
                data=rows,
                row_count=len(rows),
                truncated=truncated,
                formatted=format_rows(rows, len(rows), truncated),
                attempts=attempts,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        except SQLGuardError as exc:
            error, error_code = f"Rejected by the SQL safety guard: {exc}. SQL was:\n{sql}", "guard_rejected"
            TOOL_EVENTS.labels(tool="sql", outcome="guard_rejected").inc()
        except LLMError as exc:
            error, error_code = str(exc), "llm_unavailable"
            TOOL_EVENTS.labels(tool="sql", outcome="llm_unavailable").inc()
            break
        except Exception as exc:  # database errors
            error, error_code = (
                f"Database error: {str(exc).splitlines()[0][:300]}. SQL was:\n{sql}",
                "db_error",
            )
            TOOL_EVENTS.labels(tool="sql", outcome="db_error").inc()
        if gen is not None:
            await llm_cache.forget(gen.cache_key)  # a rejected first attempt must not be replayed
        log.info("sql_tool_retry", step=step_id, attempt=attempts, error=error[:200])
    return ToolResult(
        step_id=step_id,
        tool="sql",
        task=task,
        ok=False,
        code=sql,
        error=error,
        error_code=error_code,
        attempts=attempts,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
