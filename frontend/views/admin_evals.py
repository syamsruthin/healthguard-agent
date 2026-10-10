"""Admin evaluations: offline RAGAS jury runs against the golden set."""

from __future__ import annotations

import streamlit as st

from frontend.lib.api import ApiError, client


def render() -> None:
    api = client()
    st.title("Evaluations")
    st.write(
        "Offline evaluation replays the golden question set as each persona and scores the answers with a jury of "
        "judges from different model families (RAGAS), plus deterministic checks for routing, refusals and data isolation."
    )
    with st.form("run_eval"):
        limit = st.number_input("Cases (0 = full golden set)", min_value=0, max_value=500, value=10, step=5)
        if st.form_submit_button("Start evaluation run", type="primary"):
            try:
                run = api.start_eval(int(limit) or None)
                st.success(
                    f"Evaluation queued (run {run.get('id', '')[:8]}). Results appear below when it finishes."
                )
            except ApiError as exc:
                st.error(exc.message)

    runs = api.eval_runs()
    if not runs:
        st.info("No evaluation runs yet.")
        return
    latest = runs[0]
    st.subheader(f"Latest run · {latest['suite']} · {latest['status']}")
    summary = latest.get("summary") or {}
    metrics = summary.get("metrics") or {}
    if metrics:
        cols = st.columns(min(len(metrics), 4))
        for i, (name, value) in enumerate(metrics.items()):
            threshold = (summary.get("thresholds") or {}).get(name)
            delta = None if threshold is None else f"{value - threshold:+.2f} vs gate {threshold:.2f}"
            cols[i % len(cols)].metric(name.replace("_", " ").title(), f"{value:.2f}", delta=delta)
    if latest.get("passed") is not None:
        (st.success if latest["passed"] else st.error)(
            "Quality gate " + ("passed" if latest["passed"] else "failed")
        )
    st.subheader("History")
    st.dataframe(
        [
            {
                "started": r["started_at"][:16].replace("T", " "),
                "suite": r["suite"],
                "status": r["status"],
                "passed": r["passed"],
                "cases": (r.get("summary") or {}).get("cases"),
                **{k: round(v, 3) for k, v in ((r.get("summary") or {}).get("metrics") or {}).items()},
            }
            for r in runs
        ],
        hide_index=True,
        use_container_width=True,
    )
