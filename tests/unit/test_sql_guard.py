from __future__ import annotations

import pytest

from backend.agents.tools.sql_guard import SQLGuardError, validate_sql

SAFE = [
    "SELECT plan_id, plan_name FROM plans WHERE metal_tier = 'Gold'",
    "SELECT c.claim_id, d.description FROM insurance.claims c JOIN denial_codes d ON d.code = c.denial_code",
    "WITH denied AS (SELECT * FROM claims WHERE status = 'denied') SELECT count(*) FROM denied",
    "SELECT plan_id FROM plans WHERE plan_type = 'HMO' UNION SELECT plan_id FROM plans WHERE plan_type = 'EPO'",
    "SELECT to_char(service_date, 'YYYY-MM') AS month, sum(billed_amount) FROM claims GROUP BY 1",
]

ATTACKS = [
    "SELECT set_config('app.member_id', 'M100002', false)",
    "SELECT pg_sleep(10)",
    "SELECT current_setting('app.member_id')",
    "SELECT * FROM dblink('host=evil', 'select 1') AS t(a int)",
    "SELECT email, password_hash FROM app.users",
    "SELECT * FROM claims WHERE member_id IN (SELECT member_id FROM app.users)",
    "SELECT rolname FROM pg_catalog.pg_roles",
    "SELECT 1; SELECT 2",
    "DELETE FROM claims",
    "SELECT * INTO stolen FROM claims",
    "SELECT * FROM claims FOR UPDATE",
    "SET app.role = 'admin'",
    "COPY claims TO '/tmp/x.csv'",
    "SELECT * FROM generate_series(1, 1000000000)",
    "UPDATE members SET plan_id = 'X'",
]


@pytest.mark.parametrize("sql", SAFE)
def test_safe_queries_pass(sql: str) -> None:
    assert validate_sql(sql, max_rows=200)


@pytest.mark.parametrize("sql", ATTACKS)
def test_attacks_rejected(sql: str) -> None:
    with pytest.raises(SQLGuardError):
        validate_sql(sql, max_rows=200)


def test_small_limit_is_kept_verbatim() -> None:
    sql = "SELECT plan_id FROM plans LIMIT 10"
    assert validate_sql(sql + ";", max_rows=200) == sql


def test_missing_or_large_limit_is_capped() -> None:
    assert validate_sql("SELECT plan_id FROM plans", max_rows=50).endswith("LIMIT 50")
    assert validate_sql("SELECT plan_id FROM plans LIMIT 5000", max_rows=50).endswith("LIMIT 50")


def test_empty_sql_rejected() -> None:
    with pytest.raises(SQLGuardError):
        validate_sql("  ;  ", max_rows=10)
