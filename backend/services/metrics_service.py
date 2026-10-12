"""Admin observability aggregates over persisted turns (evaluation conversations excluded)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.tools.base import jsonable
from backend.db.base import utcnow

_TURNS = """
FROM app.messages m JOIN app.conversations c ON c.id = m.conversation_id
WHERE m.role = 'assistant' AND NOT c.is_eval AND m.created_at >= :since
"""

_SUMMARY_SQL = text(f"""
SELECT count(*) AS turns,
       count(DISTINCT m.user_id) AS active_users,
       coalesce(avg(m.latency_ms), 0) AS latency_avg_ms,
       coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY m.latency_ms), 0) AS latency_p50_ms,
       coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY m.latency_ms), 0) AS latency_p95_ms,
       coalesce(sum(m.cost_usd), 0) AS cost_usd,
       coalesce(avg(m.cost_usd), 0) AS cost_per_turn_usd,
       coalesce(sum(m.prompt_tokens), 0) AS prompt_tokens,
       coalesce(sum(m.cached_tokens), 0) AS cached_tokens,
       coalesce(sum(m.completion_tokens), 0) AS completion_tokens,
       count(*) FILTER (WHERE m.cache_hit) AS semantic_cache_hits,
       count(*) FILTER (WHERE m.blocked) AS blocked
{_TURNS}
""")

_OUTCOMES_SQL = text(f"""
SELECT coalesce(m.metadata->>'outcome', 'unknown') AS outcome, count(*) AS n
{_TURNS}
GROUP BY 1 ORDER BY 2 DESC
""")

_DAILY_SQL = text(f"""
SELECT date_trunc('day', m.created_at)::date AS day, count(*) AS turns, coalesce(sum(m.cost_usd), 0) AS cost_usd,
       coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY m.latency_ms), 0) AS latency_p95_ms,
       count(*) FILTER (WHERE m.cache_hit) AS cache_hits
{_TURNS}
GROUP BY 1 ORDER BY 1
""")

_CALLS_SQL = text("""
SELECT split_part(name, '.', 1) AS call, model, count(*) AS calls,
       sum(prompt_tokens) AS prompt_tokens, sum(cached_tokens) AS cached_tokens,
       sum(cache_write_tokens) AS cache_write_tokens, sum(completion_tokens) AS completion_tokens,
       sum(cost_usd) AS cost_usd, avg(latency_ms) AS latency_avg_ms,
       count(*) FILTER (WHERE error IS NOT NULL) AS errors
FROM app.llm_calls WHERE created_at >= :since
GROUP BY 1, 2 ORDER BY sum(cost_usd) DESC NULLS LAST
""")

_FEEDBACK_SQL = text("""
SELECT count(*) FILTER (WHERE rating > 0) AS thumbs_up, count(*) FILTER (WHERE rating < 0) AS thumbs_down
FROM app.feedback WHERE created_at >= :since
""")

_ONLINE_EVAL_SQL = text("""
SELECT metric, avg(score) AS score, avg(spread) AS spread, count(*) AS n
FROM app.online_eval_scores WHERE created_at >= :since
GROUP BY 1 ORDER BY 1
""")

_TOOLS_SQL = text(f"""
SELECT t->>'tool' AS tool, count(*) AS steps, count(*) FILTER (WHERE (t->>'ok')::boolean) AS ok,
       avg((t->>'duration_ms')::int) AS duration_avg_ms
{_TURNS.replace("FROM app.messages m", "FROM app.messages m CROSS JOIN LATERAL jsonb_array_elements(m.tools) t")}
GROUP BY 1 ORDER BY 2 DESC
""")


async def _rows(session: AsyncSession, sql: Any, since: Any) -> list[dict[str, Any]]:
    return [jsonable(dict(r)) for r in (await session.execute(sql, {"since": since})).mappings()]


async def summary(session: AsyncSession, *, days: int = 7) -> dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    totals = (await _rows(session, _SUMMARY_SQL, since))[0]
    prompt = totals["prompt_tokens"] or 0
    totals["prompt_cache_hit_ratio"] = round((totals["cached_tokens"] or 0) / prompt, 4) if prompt else 0.0
    totals["semantic_cache_hit_rate"] = (
        round(totals["semantic_cache_hits"] / totals["turns"], 4) if totals["turns"] else 0.0
    )
    return {
        "days": days,
        "since": since.isoformat(),
        "totals": totals,
        "outcomes": await _rows(session, _OUTCOMES_SQL, since),
        "daily": await _rows(session, _DAILY_SQL, since),
        "llm_calls": await _rows(session, _CALLS_SQL, since),
        "tools": await _rows(session, _TOOLS_SQL, since),
        "feedback": (await _rows(session, _FEEDBACK_SQL, since))[0],
        "online_eval": await _rows(session, _ONLINE_EVAL_SQL, since),
    }
