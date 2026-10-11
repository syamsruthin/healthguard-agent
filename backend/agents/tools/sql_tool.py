"""
`sql` tool — NL task → validated SQL → read-only, RLS-scoped execution,
with the shared self-correction loop (`codegen.py`) feeding guard / database
errors back to the model.
"""

from __future__ import annotations

from typing import Any

from tabulate import tabulate

from backend.agents.tools.base import MEMBER_ID_RE, ToolResult, UserContext, jsonable
from backend.agents.tools.codegen import CodegenTool, Execution, run_codegen
from backend.agents.tools.sql_guard import SQLGuardError, validate_sql
from backend.core.config import settings
from backend.db.session import get_agent_pool
from backend.llm.prompts.sql import build_sql_messages

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


def _describe_failure(exc: Exception, sql: str) -> tuple[str, str, str]:
    if isinstance(exc, SQLGuardError):
        return f"Rejected by the SQL safety guard: {exc}. SQL was:\n{sql}", "guard_rejected", "guard_rejected"
    first_line = (str(exc).splitlines() or [exc.__class__.__name__])[0][:300]
    return f"Database error: {first_line}. SQL was:\n{sql}", "db_error", "db_error"


async def run_sql_tool(step_id: str, task: str, user: UserContext, prior: str = "") -> ToolResult:
    async def execute(sql: str) -> Execution:
        rows, truncated = await execute_scoped(validate_sql(sql, max_rows=settings.sql_max_rows), user)
        return Execution(rows, format_rows(rows, len(rows), truncated), len(rows), truncated)

    tool = CodegenTool(
        name="sql",
        build_messages=lambda error: build_sql_messages(
            task=task, member_context=user.tool_context(), prior=prior, error=error
        ),
        execute=execute,
        describe_failure=_describe_failure,
    )
    return await run_codegen(tool, step_id=step_id, task=task)
