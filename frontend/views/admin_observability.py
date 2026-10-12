"""Admin observability: latency, cost, caching, guardrails, feedback and online-eval quality."""

from __future__ import annotations

import streamlit as st

from frontend.lib import charts
from frontend.lib.api import client
from frontend.lib.ui import money


def _pct(value: float | None) -> str:
    return f"{100 * (value or 0):.1f}%"


def render() -> None:
    st.title("Observability")
    days = st.segmented_control("Time range", [1, 7, 30, 90], default=7, format_func=lambda d: f"{d}d")
    data = client().metrics(int(days or 7))
    t = data["totals"]
    fb = data["feedback"]

    row1 = st.columns(4)
    row1[0].metric(
        "Turns", f"{t['turns']:,}", help=f"{t['active_users']} active users; evaluation runs excluded"
    )
    row1[1].metric("Latency p50", f"{t['latency_p50_ms'] / 1000:.1f}s")
    row1[2].metric("Latency p95", f"{t['latency_p95_ms'] / 1000:.1f}s")
    row1[3].metric("Cost", money(t["cost_usd"]), help=f"{money(t['cost_per_turn_usd'])} per turn")
    row2 = st.columns(4)
    row2[0].metric(
        "Prompt cache",
        _pct(t["prompt_cache_hit_ratio"]),
        help=f"{t['cached_tokens']:,} of {t['prompt_tokens']:,} prompt tokens served from the provider cache",
    )
    row2[1].metric(
        "Semantic cache",
        _pct(t["semantic_cache_hit_rate"]),
        help=f"{t['semantic_cache_hits']} answers replayed from the semantic cache",
    )
    row2[2].metric(
        "Blocked", f"{t['blocked']:,}", help="Guardrail blocks: out of scope, injection, other members"
    )
    row2[3].metric("👍 / 👎", f"{fb['thumbs_up']} / {fb['thumbs_down']}", help="User feedback")

    daily = data["daily"]
    if daily:
        left, right = st.columns(2)
        with left:
            st.markdown("**Turns per day**")
            charts.bars([d["day"] for d in daily], [d["turns"] for d in daily], hover_label="Turns: ")
        with right:
            st.markdown("**Cost per day (USD)**")
            charts.line(
                [d["day"] for d in daily],
                [d["cost_usd"] for d in daily],
                value_format="$,.4f",
                hover_label="Cost: ",
            )

    calls = data["llm_calls"]
    if calls:
        st.subheader("Model calls")
        by_call: dict[str, dict[str, float]] = {}
        for c in calls:
            agg = by_call.setdefault(c["call"], {"prompt": 0, "cached": 0})
            agg["prompt"] += c["prompt_tokens"] or 0
            agg["cached"] += c["cached_tokens"] or 0
        ratios = sorted(
            ((name, a["cached"] / a["prompt"]) for name, a in by_call.items() if a["prompt"]),
            key=lambda x: -x[1],
        )
        if ratios:
            st.markdown("**Share of prompt tokens served from the provider cache, by call**")
            charts.bars(
                [r[0] for r in ratios],
                [100 * r[1] for r in ratios],
                horizontal=True,
                value_format=".1f",
                hover_label="Cached %: ",
                height=40 * len(ratios) + 40,
            )
        st.dataframe(
            [
                {
                    "call": c["call"],
                    "model": c["model"],
                    "calls": c["calls"],
                    "prompt tokens": c["prompt_tokens"],
                    "cached": c["cached_tokens"],
                    "cache writes": c["cache_write_tokens"],
                    "output": c["completion_tokens"],
                    "cost": money(c["cost_usd"]),
                    "avg ms": round(c["latency_avg_ms"] or 0),
                    "errors": c["errors"],
                }
                for c in calls
            ],
            hide_index=True,
            use_container_width=True,
        )

    left, right = st.columns(2)
    with left:
        st.subheader("Outcomes")
        if data["outcomes"]:
            charts.bars(
                [o["outcome"] for o in data["outcomes"]],
                [o["n"] for o in data["outcomes"]],
                horizontal=True,
                hover_label="Turns: ",
                height=40 * len(data["outcomes"]) + 40,
            )
    with right:
        st.subheader("Tools")
        if data["tools"]:
            st.dataframe(
                [
                    {
                        "tool": x["tool"],
                        "steps": x["steps"],
                        "success": _pct(x["ok"] / x["steps"] if x["steps"] else 0),
                        "avg ms": round(x["duration_avg_ms"] or 0),
                    }
                    for x in data["tools"]
                ],
                hide_index=True,
                use_container_width=True,
            )
    st.subheader("Online evaluation (LLM-judge jury)")
    if data["online_eval"]:
        cols = st.columns(len(data["online_eval"]))
        for col, row in zip(cols, data["online_eval"], strict=True):
            col.metric(
                row["metric"].title(),
                f"{row['score']:.2f}",
                help=f"{row['n']} scored answers; mean judge spread {row['spread']:.2f}",
            )
    else:
        st.caption(
            "No sampled answers have been scored yet (the worker scores a sample of turns and every thumbs-down)."
        )
