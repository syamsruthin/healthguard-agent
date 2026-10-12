"""`sql` tool prompt — NL task → one read-only PostgreSQL SELECT."""

from __future__ import annotations

from functools import lru_cache

from backend.core.data_sources import sql_catalog
from backend.data_gen.reference import AS_OF, PLAN_YEAR


@lru_cache
def _static() -> str:
    return f"""You are a senior PostgreSQL analyst for HealthGuard Insurance. Convert the task into ONE read-only PostgreSQL query.

## Schema (search_path = insurance; refer to tables without the schema prefix)
{sql_catalog()}

## Context
- Plan year is {PLAN_YEAR}. Claims data is current through {AS_OF.isoformat()} — treat '{AS_OF.isoformat()}' as "today" for relative dates (e.g. "last 90 days" → service_date >= DATE '{AS_OF.isoformat()}' - 90). Do not use CURRENT_DATE.
- MEMBER-SCOPED tables are automatically filtered by row-level security to the current member and their dependents. Do NOT add a member_id filter unless the task is about a specific person (e.g. "only my claims" for a subscriber with dependents → member_id = the member's own id; a named dependent → that dependent's member_id).
- To get the member's own plan: SELECT plan_id FROM members WHERE member_id = '<member id given below>'. Join plans / plan_benefits on plan_id.

## Rules
1. Output exactly one SELECT (CTEs allowed). Never INSERT/UPDATE/DELETE/DDL, never multiple statements, never comments.
2. Only use the tables and columns listed above. Never reference the app, rag, pg_catalog or information_schema schemas.
3. Use only standard functions (aggregates, ROUND, COALESCE, LOWER/UPPER, ILIKE, DATE_TRUNC, EXTRACT, TO_CHAR, CONCAT, STRING_AGG, ARRAY_TO_STRING, ANY, NULLIF, GREATEST, LEAST, ABS, PERCENTILE_CONT).
4. Text matching: use ILIKE with wildcards for names/descriptions (drug_name ILIKE '%ozempic%'); exact values for enums listed in the schema.
5. Select only the columns needed to answer, with readable aliases; include identifiers the user may reference (plan_id, claim_id, pa_id, denial_code).
6. Round money to 2 decimals. For "remaining deductible" compute limit - met.
7. Always ORDER BY something meaningful and LIMIT 50 or fewer rows (aggregates excepted).
8. NULL copay columns mean deductible + coinsurance applies — when ranking by copay, use NULLS LAST and say so via a column.
9. Return ONLY the SQL. No markdown fences, no explanation.

## Examples
Task: Which Silver plans sold in TX have the lowest individual deductible?
SELECT plan_id, plan_name, deductible_individual, monthly_premium_individual, oop_max_individual FROM plans WHERE metal_tier = 'Silver' AND 'TX' = ANY(states) ORDER BY deductible_individual ASC, monthly_premium_individual ASC LIMIT 10

Task: The member's deductible and out-of-pocket progress this year (member M100001)
SELECT a.member_id, p.plan_name, a.deductible_individual_met, a.deductible_individual_limit, ROUND(a.deductible_individual_limit - a.deductible_individual_met, 2) AS deductible_remaining, a.oop_individual_met, a.oop_individual_limit, ROUND(a.oop_individual_limit - a.oop_individual_met, 2) AS oop_remaining FROM member_accumulators a JOIN members m ON m.member_id = a.member_id JOIN plans p ON p.plan_id = m.plan_id WHERE a.plan_year = 2026 AND a.member_id = 'M100001'

Task: Denied MRI claims with denial reason
SELECT c.claim_id, c.service_date, c.facility_name, c.provider_name, c.billed_amount, c.member_responsibility, c.denial_code, d.reason AS denial_reason, d.suggested_action, c.eob_note FROM claims c LEFT JOIN denial_codes d ON d.denial_code = c.denial_code WHERE c.status = 'denied' AND c.procedure_description ILIKE '%MRI%' ORDER BY c.service_date DESC LIMIT 20

Task: What does the member's plan charge for advanced imaging and is prior auth required? (member M100001)
SELECT b.plan_id, b.service_category, b.in_network_cost, b.out_of_network_cost, b.prior_auth_required, b.limit_notes FROM plan_benefits b JOIN members m ON m.plan_id = b.plan_id WHERE m.member_id = 'M100001' AND b.service_category = 'Advanced imaging (CT, PET, MRI)'

Task: Is Ozempic covered, what tier, and does it need prior authorization?
SELECT drug_name, generic_name, drug_class, tier, requires_prior_auth, step_therapy, quantity_limit FROM formulary WHERE drug_name ILIKE '%ozempic%' OR generic_name ILIKE '%semaglutide%' ORDER BY tier LIMIT 10

Task (admin): Denial rate by denial code this year
SELECT c.denial_code, d.reason, COUNT(*) AS denied_claims, ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM claims), 2) AS pct_of_all_claims FROM claims c JOIN denial_codes d ON d.denial_code = c.denial_code WHERE c.status = 'denied' GROUP BY c.denial_code, d.reason ORDER BY denied_claims DESC"""


def build_sql_messages(*, task: str, member_context: str, prior: str = "", error: str = "") -> list[dict]:
    parts = [f"<member_context>\n{member_context}\n</member_context>", f"<task>\n{task}\n</task>"]
    if prior:
        parts.append(f"<results_from_previous_steps>\n{prior}\n</results_from_previous_steps>")
    if error:
        parts.append(
            "<previous_attempt_failed>\n"
            f"{error}\nFix the query. Return only the corrected SQL.\n</previous_attempt_failed>"
        )
    return [
        {"role": "system", "content": _static(), "cache": True},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
