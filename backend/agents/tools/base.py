"""Shared tool types and the per-request user context."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from backend.data_gen.reference import AS_OF

MEMBER_ID_RE = re.compile(r"^M\d{6}$")


def jsonable(value: Any) -> Any:
    """Convert DB / numpy values into JSON-serialisable Python values."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if hasattr(value, "item") and callable(value.item):  # numpy scalar
        try:
            return value.item()
        except Exception:
            return str(value)
    return value


@dataclass
class UserContext:
    user_id: str
    role: str  # member | admin
    email: str
    full_name: str
    member_id: str | None = None
    subscriber_id: str | None = None
    plan_id: str | None = None
    plan_name: str | None = None
    network_name: str | None = None
    plan_type: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    coverage_level: str | None = None
    dependents: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def profile_text(self) -> str:
        if self.is_admin:
            return (
                f"role: admin (HealthGuard staff, may analyse all members); name: {self.full_name}; "
                f"data current through {AS_OF.isoformat()}"
            )
        if not self.member_id:
            return f"role: member; name: {self.full_name}; no member profile linked (public information only)"
        deps = (
            ", ".join(
                f"{d['first_name']} ({d['relationship']}, {d['member_id']}, age {d['age']})"
                for d in self.dependents
            )
            or "none"
        )
        return (
            f"role: member; name: {self.full_name}; member_id: {self.member_id}; "
            f"plan: {self.plan_id} ({self.plan_name}); plan type: {self.plan_type}; network: {self.network_name}; "
            f"coverage: {self.coverage_level}; location: {self.city}, {self.state} {self.zip_code}; "
            f"dependents on plan: {deps}"
        )

    def tool_context(self) -> str:
        """Context handed to code-generating tools (no conversation, only what they need)."""
        if self.is_admin:
            return "Caller is an ADMIN: member-scoped tables contain all members. Aggregate across members."
        if not self.member_id:
            return "Caller has no linked member profile: member-scoped tables return no rows."
        deps = "; ".join(
            f"{d['first_name']} = {d['member_id']} ({d['relationship']})" for d in self.dependents
        )
        return (
            f"Current member: {self.member_id} ({self.full_name}), plan_id {self.plan_id} ({self.plan_name}), "
            f"network '{self.network_name}', home zip {self.zip_code} ({self.city}, {self.state})."
            + (f" Dependents on the plan: {deps}." if deps else "")
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserContext:
        return cls(**data)

    async def load_member_profile(self, session: Any) -> UserContext:
        if not self.member_id or not MEMBER_ID_RE.match(self.member_id):
            return self
        row = (
            (
                await session.execute(
                    text(
                        "SELECT m.member_id, m.subscriber_id, m.city, m.state, m.zip_code, m.coverage_level, m.plan_id, "
                        "p.plan_name, p.network_name, p.plan_type FROM insurance.members m "
                        "JOIN insurance.plans p ON p.plan_id = m.plan_id WHERE m.member_id = :mid"
                    ),
                    {"mid": self.member_id},
                )
            )
            .mappings()
            .first()
        )
        if not row:
            return self
        for key in (
            "subscriber_id",
            "city",
            "state",
            "zip_code",
            "coverage_level",
            "plan_id",
            "plan_name",
            "network_name",
            "plan_type",
        ):
            setattr(self, key, row[key])
        deps = (
            (
                await session.execute(
                    text(
                        "SELECT member_id, first_name, relationship_to_subscriber AS relationship, "
                        "date_part('year', age(DATE :asof, date_of_birth))::int AS age FROM insurance.members "
                        "WHERE subscriber_id = :mid AND member_id <> :mid ORDER BY date_of_birth"
                    ),
                    {"mid": self.member_id, "asof": AS_OF},
                )
            )
            .mappings()
            .all()
        )
        self.dependents = [dict(d) for d in deps]
        return self


@dataclass
class ToolResult:
    step_id: str
    tool: str
    task: str
    ok: bool = True
    code: str = ""
    data: Any = None
    row_count: int = 0
    truncated: bool = False
    formatted: str = ""
    chunks: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    attempts: int = 1
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return jsonable(asdict(self))
