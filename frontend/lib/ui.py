"""Shared rendering helpers: answer badges, citations, plan/tool/trace views."""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

OUTCOME_BADGE = {
    "answered": ("green", "Answered"),
    "smalltalk": ("blue", "Chat"),
    "cache_hit": ("violet", "From cache"),
    "blocked": ("orange", "Declined"),
    "insufficient": ("orange", "Not verified"),
    "error": ("red", "Unavailable"),
}
TOOL_LABEL = {
    "sql": "Insurance records",
    "providers": "Provider directory",
    "policy_search": "Policy documents",
}


def md(text: str) -> str:
    """Escape `$` so Streamlit markdown does not render dollar amounts as LaTeX math."""
    return text.replace("$", r"\$")


def money(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def answer_badges(meta: dict[str, Any]) -> None:
    outcome = meta.get("outcome") or "answered"
    color, label = OUTCOME_BADGE.get(outcome, ("gray", outcome))
    parts = [f":{color}-badge[{label}]"]
    for tool in {t["tool"] for t in meta.get("tools", [])}:
        parts.append(f":gray-badge[{TOOL_LABEL.get(tool, tool)}]")
    if meta.get("latency_ms"):
        parts.append(f":gray-badge[:material/timer: {meta['latency_ms'] / 1000:.1f}s]")
    usage = meta.get("usage") or {}
    if usage.get("cost_usd"):
        cached = usage.get("cached_tokens", 0)
        prompt = usage.get("prompt_tokens", 0) or 1
        parts.append(f":gray-badge[\\${usage['cost_usd']:.4f} · {100 * cached / prompt:.0f}% prompt cached]")
    st.markdown(" ".join(parts))


def citations(items: list[dict[str, Any]]) -> None:
    if not items:
        return
    with st.expander(f"Sources ({len(items)})", icon=":material/menu_book:"):
        for c in items:
            if c.get("kind") == "document":
                st.markdown(md(f"**[{c['tag']}] {c.get('title', '')}** — {c.get('section', '')}"))
                st.caption(md(c.get("snippet", "")))
            else:
                st.markdown(md(f"**[{c['tag']}] {c.get('title', '')}** · {c.get('row_count', 0)} rows"))
                if c.get("detail"):
                    st.code(c["detail"], language="sql" if c.get("tool") == "sql" else "python")


def reasoning(meta: dict[str, Any], trace: list[dict[str, Any]] | None = None) -> None:
    """Plan, tool steps and verification for one answer (plus the full trace for admins)."""
    with st.expander("How this answer was produced", icon=":material/account_tree:"):
        plan = meta.get("plan") or []
        if plan:
            st.markdown("**Plan**")
            for s in plan:
                deps = f" ← {', '.join(s['depends_on'])}" if s.get("depends_on") else ""
                st.markdown(md(f"- `{s['id']}` {TOOL_LABEL.get(s['tool'], s['tool'])}: {s['task']}{deps}"))
        tools = meta.get("tools") or []
        if tools:
            st.markdown("**Steps**")
            st.dataframe(
                [
                    {
                        "step": t["step_id"],
                        "tool": TOOL_LABEL.get(t["tool"], t["tool"]),
                        "ok": t.get("ok"),
                        "rows": t.get("row_count", 0),
                        "attempts": t.get("attempts", 1),
                        "ms": t.get("duration_ms", 0),
                        "error": t.get("error_code") or "",
                    }
                    for t in tools
                ],
                hide_index=True,
                use_container_width=True,
            )
        verification = meta.get("verification") or {}
        if verification:
            if verification.get("skipped"):
                st.caption("Verification skipped (verifier unavailable).")
            else:
                st.markdown(
                    f"**Verification** — grounded: `{verification.get('grounded')}`, "
                    f"complete: `{verification.get('complete')}`"
                )
                for claim in verification.get("unsupported_claims") or []:
                    st.caption(md(f"Unsupported: {claim}"))
        if trace:
            st.markdown("**Trace**")
            for s in trace:
                st.markdown(f"`{s['node']}` · {s.get('duration_ms', 0):.0f} ms")
                body = s.get("output")
                if body not in ("", None, [], {}):
                    st.code(
                        body if isinstance(body, str) else json.dumps(body, indent=2)[:3000], language=None
                    )
