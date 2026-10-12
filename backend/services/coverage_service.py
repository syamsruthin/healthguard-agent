"""The member's coverage card: plan, deductible / out-of-pocket progress, dependents, recent activity.

Queries are fixed (not model-generated) but still run on the read-only agent
role with the caller's RLS context, so a member can only ever see their own data.
"""

from __future__ import annotations

from typing import Any

from backend.agents.tools.base import UserContext
from backend.agents.tools.sql_tool import execute_scoped
from backend.data_gen.reference import AS_OF

_PLAN_SQL = """
SELECT p.plan_id, p.plan_name, p.network_name, p.plan_type, p.metal_tier, p.plan_year,
       p.monthly_premium_individual, p.monthly_premium_family, p.deductible_individual, p.deductible_family,
       p.oop_max_individual, p.oop_max_family, p.coinsurance_pct, p.pcp_copay, p.specialist_copay,
       p.urgent_care_copay, p.er_copay, p.telehealth_copay, p.hsa_eligible, p.requires_referral,
       p.out_of_network_covered, m.hsa_balance, m.coverage_level, m.enrollment_date
FROM members m JOIN plans p ON p.plan_id = m.plan_id
WHERE m.member_id = $1
"""
_ACCUM_SQL = """
SELECT a.member_id, m.first_name, a.plan_year, a.deductible_individual_met, a.deductible_individual_limit,
       a.oop_individual_met, a.oop_individual_limit, a.deductible_family_met, a.deductible_family_limit,
       a.oop_family_met, a.oop_family_limit
FROM member_accumulators a JOIN members m ON m.member_id = a.member_id
ORDER BY (a.member_id = $1) DESC, a.member_id
"""
_ACTIVITY_SQL = """
SELECT
  (SELECT count(*) FROM claims) AS claims,
  (SELECT count(*) FROM claims WHERE status = 'denied') AS denied_claims,
  (SELECT count(*) FROM claims WHERE status IN ('pending', 'in_review')) AS pending_claims,
  (SELECT count(*) FROM prior_authorizations WHERE status = 'pending') AS pending_prior_auths,
  (SELECT coalesce(sum(member_responsibility), 0) FROM claims) AS member_paid_ytd
"""


async def member_card(ctx: UserContext) -> dict[str, Any]:
    if ctx.is_admin or not ctx.member_id:
        return {
            "member_id": None,
            "as_of": AS_OF.isoformat(),
            "plan": None,
            "accumulators": [],
            "activity": None,
        }
    plan_rows, _ = await _scoped(_PLAN_SQL, ctx, ctx.member_id)
    accum_rows, _ = await _scoped(_ACCUM_SQL, ctx, ctx.member_id)
    activity_rows, _ = await _scoped(_ACTIVITY_SQL, ctx)
    return {
        "member_id": ctx.member_id,
        "full_name": ctx.full_name,
        "as_of": AS_OF.isoformat(),
        "plan": plan_rows[0] if plan_rows else None,
        "dependents": ctx.dependents,
        "accumulators": accum_rows,
        "activity": activity_rows[0] if activity_rows else None,
    }


async def _scoped(sql: str, ctx: UserContext, *params: Any) -> tuple[list[dict[str, Any]], bool]:
    return await execute_scoped(sql, ctx, *params)
