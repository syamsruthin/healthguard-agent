from __future__ import annotations

from backend.db.session import SessionLocal
from backend.services.user_context import get_user_by_email, load_user_context


async def test_member_profile_and_dependents_load() -> None:
    async with SessionLocal() as session:
        user = await get_user_by_email(session, "david.kim@demo.healthguard.dev")
        assert user is not None
        ctx = await load_user_context(session, user)
    assert ctx.member_id == "M100006" and ctx.plan_id == "EPO-GLD-PLS"
    assert ctx.network_name and ctx.zip_code
    assert {d["member_id"] for d in ctx.dependents} == {"M100007", "M100008", "M100009"}
    assert all(isinstance(d["age"], int) for d in ctx.dependents)
    assert "M100008" in ctx.profile_text()


async def test_admin_has_no_member_profile() -> None:
    async with SessionLocal() as session:
        user = await get_user_by_email(session, "admin@demo.healthguard.dev")
        assert user is not None
        ctx = await load_user_context(session, user)
    assert ctx.is_admin and ctx.member_id is None and ctx.dependents == []
