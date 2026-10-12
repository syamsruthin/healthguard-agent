"""Markdown + JSON reports for an evaluation run (`backend/evals/reports/<run>.md|.json`)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

REPORTS = Path(__file__).parent / "reports"


def _fmt(value: Any) -> str:
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def write_reports(
    run_id: uuid.UUID,
    config: dict[str, Any],
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
    passed: bool,
) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    stem = REPORTS / f"{run_id}"
    stem.with_suffix(".json").write_text(
        json.dumps(
            {
                "run_id": str(run_id),
                "passed": passed,
                "config": config,
                "summary": summary,
                "cases": [
                    {
                        "id": r["case"]["id"],
                        "category": r["case"]["category"],
                        **{k: r.get(k) for k in ("checks", "scores", "error")},
                        "answer": r.get("result", {}).get("answer"),
                        "outcome": r.get("result", {}).get("outcome"),
                    }
                    for r in rows
                ],
            },
            indent=2,
            default=str,
        )
    )
    thresholds = summary.get("thresholds", {})
    lines = [
        f"# Evaluation run `{run_id}` — {'PASSED' if passed else 'FAILED'}",
        "",
        f"- Cases: {summary['cases']} ({summary['errors']} errors) · agent cost ${summary['cost_usd']:.4f} "
        f"(${summary['cost_per_case_usd']:.4f}/case) · judge cost ${summary.get('judge_cost_usd', 0.0):.4f} · "
        f"latency p50 {summary['latency_p50_ms'] / 1000:.1f}s / p95 {summary['latency_p95_ms'] / 1000:.1f}s",
        f"- Judges: {', '.join(config.get('judges') or []) or 'none'} · metrics: {', '.join(config.get('metrics') or []) or 'none'}",
        "",
        "## Metrics",
        "",
        "| metric | value | gate |",
        "|---|---|---|",
    ]
    for name, value in sorted(summary["metrics"].items()):
        gate = thresholds.get(name)
        mark = "" if gate is None else ("✅" if value >= gate else "❌")
        lines.append(f"| {name} | {_fmt(value)} | {'' if gate is None else f'≥ {gate:.2f} {mark}'} |")
    lines += ["", "## By category", "", "| category | cases | all checks passed |", "|---|---|---|"]
    for cat, c in sorted(summary["by_category"].items()):
        lines.append(f"| {cat} | {c['cases']} | {c['passed']} |")
    failing = [r for r in rows if "error" in r or any(v is False for v in r.get("checks", {}).values())]
    if failing:
        lines += ["", "## Cases needing attention", ""]
        for r in failing:
            case = r["case"]
            failed = [k for k, v in r.get("checks", {}).items() if v is False]
            reason = r.get("error") or f"failed checks: {', '.join(failed)}"
            answer = (r.get("result", {}).get("answer") or "").replace("\n", " ")[:300]
            lines.append(
                f"- **{case['id']}** ({case['category']}) — {reason}  \n  Q: {case['question']}  \n  A: {answer}"
            )
    if summary.get("flagged"):
        lines += ["", f"Judge disagreement flagged on: {', '.join(summary['flagged'])}"]
    path = stem.with_suffix(".md")
    path.write_text("\n".join(lines) + "\n")
    return path
