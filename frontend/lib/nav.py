"""Which pages a user may see (pure, so it is unit-testable)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from frontend.views import admin_evals, admin_observability, chat, coverage, memory


@dataclass(frozen=True)
class PageSpec:
    render: Callable[[], None]
    title: str
    icon: str
    url_path: str
    default: bool = False


def pages_for(user: dict[str, Any]) -> dict[str, list[PageSpec]]:
    sections = {
        "Assistant": [
            PageSpec(chat.render, "Chat", ":material/chat:", "chat", default=True),
            PageSpec(coverage.render, "My coverage", ":material/badge:", "coverage"),
            PageSpec(memory.render, "Memory", ":material/neurology:", "memory"),
        ]
    }
    if user.get("role") == "admin":
        sections["Admin"] = [
            PageSpec(admin_observability.render, "Observability", ":material/monitoring:", "observability"),
            PageSpec(admin_evals.render, "Evaluations", ":material/fact_check:", "evaluations"),
        ]
    return sections
