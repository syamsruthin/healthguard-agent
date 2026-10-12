"""Row-Level Security and read-only role, exercised exactly as the SQL tool runs queries."""

from __future__ import annotations

import pytest

from backend.agents.tools.base import UserContext
from backend.agents.tools.sql_guard import validate_sql
from backend.agents.tools.sql_tool import execute_scoped

MEMBERS_IN_CLAIMS = validate_sql("SELECT DISTINCT member_id FROM claims", max_rows=5000)
DAVID_FAMILY = {"M100006", "M100007", "M100008", "M100009"}


def _member(member_id: str | None) -> UserContext:
    return UserContext(user_id="u", role="member", email="x@y.z", full_name="Test", member_id=member_id)


async def _ids(user: UserContext, sql: str = MEMBERS_IN_CLAIMS) -> set[str]:
    rows, _ = await execute_scoped(sql, user)
    return {r["member_id"] for r in rows}


async def test_member_sees_only_own_rows() -> None:
    assert await _ids(_member("M100001")) == {"M100001"}


async def test_subscriber_sees_family() -> None:
    ids = await _ids(_member("M100006"))
    assert "M100006" in ids and ids <= DAVID_FAMILY


async def test_admin_sees_everyone() -> None:
    admin = UserContext(user_id="a", role="admin", email="admin@x", full_name="Admin")
    assert len(await _ids(admin)) > len(DAVID_FAMILY) + 2


async def test_no_profile_sees_no_member_rows_but_public_tables() -> None:
    nobody = _member(None)
    assert await _ids(nobody) == set()
    rows, _ = await execute_scoped(validate_sql("SELECT count(*) AS n FROM plans", max_rows=10), nobody)
    assert rows[0]["n"] > 0


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT set_config('app.member_id', 'M100002', true)", "permission denied"),
        ("SELECT count(*) FROM app.users", "permission denied"),
        ("DELETE FROM insurance.claims", "read-only"),
    ],
)
async def test_database_blocks_bypass_attempts(sql: str, message: str) -> None:
    """Even if the static guard were bypassed, the database itself refuses."""
    with pytest.raises(Exception, match=message):
        await execute_scoped(sql, _member("M100001"))
