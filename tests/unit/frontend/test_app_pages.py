"""Streamlit pages rendered with AppTest against a fake API client (no backend, no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from streamlit.testing.v1 import AppTest

from frontend.lib.nav import pages_for

APP = str(Path(__file__).resolve().parents[3] / "frontend" / "app.py")
MEMBER = {
    "id": "u1",
    "email": "maria.garcia@demo.healthguard.dev",
    "full_name": "Maria Garcia",
    "role": "member",
    "member_id": "M100001",
    "memory_enabled": True,
}
ADMIN = {
    **MEMBER,
    "email": "admin@demo.healthguard.dev",
    "full_name": "HealthGuard Admin",
    "role": "admin",
    "member_id": None,
}


class FakeApi:
    def conversations(self) -> list[dict[str, Any]]:
        return [{"id": "c1", "title": "Knee MRI denial"}]

    def messages(self, cid: str) -> list[dict[str, Any]]:
        return []

    def coverage(self) -> dict[str, Any]:
        return {
            "member_id": "M100001",
            "as_of": "2026-10-05",
            "plan": {
                "plan_id": "PPO-SLV-PLS",
                "plan_name": "Choice PPO Silver Plus",
                "network_name": "Choice PPO",
                "plan_type": "PPO",
                "metal_tier": "Silver",
                "coverage_level": "individual",
                "hsa_eligible": False,
                "hsa_balance": None,
                "pcp_copay": 25,
                "specialist_copay": 60,
                "urgent_care_copay": 75,
                "er_copay": None,
                "telehealth_copay": 0,
                "coinsurance_pct": 20,
                "requires_referral": False,
                "out_of_network_covered": True,
            },
            "dependents": [],
            "accumulators": [
                {
                    "member_id": "M100001",
                    "plan_year": 2026,
                    "deductible_individual_met": 2700,
                    "deductible_individual_limit": 3400,
                    "oop_individual_met": 4100,
                    "oop_individual_limit": 8000,
                    "deductible_family_limit": None,
                    "oop_family_limit": None,
                }
            ],
            "activity": {
                "claims": 30,
                "denied_claims": 1,
                "pending_claims": 0,
                "pending_prior_auths": 1,
                "member_paid_ytd": 1200,
            },
        }

    def memories(self) -> list[dict[str, Any]]:
        return [{"id": "m1", "content": "Prefers Spanish-speaking providers", "category": "language"}]


def _signed_in(at: AppTest, user: dict[str, Any]) -> AppTest:
    at.session_state["user"] = user
    at.session_state["tokens"] = {"access_token": "x", "refresh_token": "y", "user": user}
    at.session_state["api"] = FakeApi()
    return at


def _coverage_page() -> None:
    from frontend.views import coverage

    coverage.render()


def _memory_page() -> None:
    from frontend.views import memory

    memory.render()


def test_signed_out_users_only_see_login() -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert at.title[0].value == "HealthGuard"
    assert [t.label for t in at.tabs] == ["Sign in", "Demo accounts", "Create account"]


def test_chat_page_shows_persona_prompts_and_conversations() -> None:
    at = _signed_in(AppTest.from_file(APP, default_timeout=30), MEMBER).run()
    assert not at.exception
    assert "Hi Maria" in at.subheader[0].value
    labels = [b.label for b in at.sidebar.button]
    assert "Knee MRI denial" in labels and "Sign out" in labels


def test_coverage_page_escapes_dollars_and_shows_progress() -> None:
    at = _signed_in(AppTest.from_function(_coverage_page, default_timeout=30), MEMBER).run()
    assert not at.exception
    texts = " ".join(m.value for m in at.markdown)
    assert r"\$2,700.00 of \$3,400.00" in texts  # escaped, so markdown does not render it as LaTeX
    assert len(at.get("progress")) == 2


def test_memory_page_lists_memories() -> None:
    at = _signed_in(AppTest.from_function(_memory_page, default_timeout=30), MEMBER).run()
    assert not at.exception
    assert any("Prefers Spanish-speaking providers" in m.value for m in at.markdown)


def test_admin_pages_only_for_admins() -> None:
    assert list(pages_for(MEMBER)) == ["Assistant"]
    assert [p.title for p in pages_for(ADMIN)["Admin"]] == ["Observability", "Evaluations"]
