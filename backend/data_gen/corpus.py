"""
Render the policy corpus: hand-written markdown (with data-driven tables
injected from the shared reference data) + one generated Summary of
Benefits and Coverage (SBC) per plan.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from backend.data_gen.reference import (
    DENIAL_CODES,
    NETWORK_STATES,
    PLAN_YEAR,
    PRIOR_AUTH_SERVICES,
    STATE_NAMES,
)

CORPUS_SRC = Path(__file__).resolve().parent / "corpus"
FRONT_MATTER = re.compile(r"^---\n(.*?)\n---\n", re.S)


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    match = FRONT_MATTER.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, text[match.end() :]


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def _placeholders(formulary: list[dict[str, Any]]) -> dict[str, str]:
    denial = _table(
        ["Code", "Reason", "What it means", "Suggested action"],
        [[c, r, d, a] for c, r, d, a, _ in DENIAL_CODES],
    )
    pa = _table(["Service", "Notes"], [[name, notes] for name, notes in PRIOR_AUTH_SERVICES])
    states = _table(
        ["Network", "States"],
        [[net, ", ".join(f"{STATE_NAMES[s]} ({s})" for s in sts)] for net, sts in NETWORK_STATES.items()],
    )
    steps = [d for d in formulary if d["step_therapy"]]
    step_table = _table(
        ["Drug", "Class", "Tier", "Prior auth"],
        [
            [d["drug_name"], d["drug_class"], str(d["tier"]), "Yes" if d["requires_prior_auth"] else "No"]
            for d in steps
        ],
    )
    return {
        "{{DENIAL_CODE_TABLE}}": denial,
        "{{PRIOR_AUTH_TABLE}}": pa,
        "{{NETWORK_STATES_TABLE}}": states,
        "{{STEP_THERAPY_TABLE}}": "**Drugs subject to step therapy:**\n\n" + step_table,
    }


def _fmt_money(v: Any) -> str:
    return "Deductible, then coinsurance" if v is None else f"${float(v):,.0f}"


def render_sbc(plan: dict[str, Any], benefits: list[dict[str, Any]]) -> str:
    p = plan
    copay_plan = p["plan_type"] != "HDHP"
    lines = [
        "---",
        f"doc_id: sbc-{p['plan_id'].lower()}",
        f"title: Summary of Benefits and Coverage — {p['plan_name']} ({p['plan_id']})",
        "category: sbc",
        f"plan_id: {p['plan_id']}",
        f"effective_date: {PLAN_YEAR}-01-01",
        "---",
        f"# Summary of Benefits and Coverage: {p['plan_name']}",
        "",
        f"**Plan ID:** {p['plan_id']} · **Plan type:** {p['plan_type']} · **Network:** {p['network_name']} · "
        f"**Metal tier:** {p['metal_tier']} · **Coverage period:** 01/01/{PLAN_YEAR} – 12/31/{PLAN_YEAR}",
        "",
        f"Available in: {', '.join(p['states'])}. HSA-eligible: {'Yes' if p['hsa_eligible'] else 'No'}. "
        f"Referrals required: {'Yes (PCP referral for specialists)' if p['requires_referral'] else 'No'}. "
        f"Out-of-network coverage: {'Yes' if p['out_of_network_covered'] else 'No (emergencies only)'}.",
        "",
        "## Important questions",
        "",
        _table(
            ["Question", "Answer"],
            [
                [
                    "Monthly premium",
                    f"${p['monthly_premium_individual']:,.2f} individual / "
                    f"${p['monthly_premium_family']:,.2f} family",
                ],
                [
                    "What is the overall deductible?",
                    f"${p['deductible_individual']:,.0f} individual / ${p['deductible_family']:,.0f} family",
                ],
                [
                    "Are there services covered before you meet your deductible?",
                    "Yes — preventive care"
                    + (
                        " and services with copays (office visits, urgent care, ER, tier 1–3 drugs)"
                        if copay_plan
                        else " only (HDHP)"
                    ),
                ],
                [
                    "What is the out-of-pocket limit?",
                    f"${p['oop_max_individual']:,.0f} individual / ${p['oop_max_family']:,.0f} family",
                ],
                ["Coinsurance after deductible", f"{p['coinsurance_pct']}%"],
                [
                    "Out-of-network deductible / coinsurance",
                    f"${p['oon_deductible_individual']:,.0f} / {p['oon_coinsurance_pct']}%"
                    if p["out_of_network_covered"]
                    else "Not covered (except emergencies)",
                ],
            ],
        ),
        "",
        "## Common medical events",
        "",
        _table(
            ["Service", "In-network cost", "Out-of-network cost", "Prior auth", "Limits / notes"],
            [
                [
                    b["service_category"],
                    b["in_network_cost"],
                    b["out_of_network_cost"],
                    "Yes" if b["prior_auth_required"] else "No",
                    b["limit_notes"] or "",
                ]
                for b in benefits
            ],
        ),
        "",
        "## Prescription drugs",
        "",
        _table(
            ["Tier", "Cost (30-day retail)"],
            [
                [
                    "Tier 1 — preferred generics",
                    _fmt_money(p["rx_tier1_copay"]) if copay_plan else "Deductible, then coinsurance",
                ],
                [
                    "Tier 2 — non-preferred generics",
                    _fmt_money(p["rx_tier2_copay"]) if copay_plan else "Deductible, then coinsurance",
                ],
                [
                    "Tier 3 — preferred brands",
                    _fmt_money(p["rx_tier3_copay"]) if copay_plan else "Deductible, then coinsurance",
                ],
                ["Tier 4 — non-preferred brands", f"{p['rx_tier4_coinsurance_pct']}% coinsurance"],
                ["Tier 5 — specialty", f"{p['rx_tier5_coinsurance_pct']}% coinsurance"],
            ],
        ),
        "",
        "90-day mail-order supplies cost 2.5× the 30-day copay. Insulin is capped at $35 per 30-day supply.",
        "",
        "## Excluded services",
        "",
        ", ".join(b["service_category"] for b in benefits if not b["covered"]) + ".",
        "",
        f"*Quality rating: {p['star_rating']} stars. This SBC is a summary; the Evidence of Coverage governs.*",
    ]
    return "\n".join(lines) + "\n"


def render_corpus(
    out_dir: Path,
    plans: list[dict[str, Any]],
    benefits: list[dict[str, Any]],
    formulary: list[dict[str, Any]],
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.rglob("*.md"):
        old.unlink()
    subs = _placeholders(formulary)
    count = 0
    for src in sorted(CORPUS_SRC.glob("*.md")):
        text = src.read_text()
        for key, value in subs.items():
            text = text.replace(key, value)
        (out_dir / src.name).write_text(text)
        count += 1
    sbc_dir = out_dir / "sbc"
    sbc_dir.mkdir(exist_ok=True)
    by_plan: dict[str, list[dict[str, Any]]] = {}
    for b in benefits:
        by_plan.setdefault(b["plan_id"], []).append(b)
    for plan in plans:
        (sbc_dir / f"sbc_{plan['plan_id'].lower()}.md").write_text(render_sbc(plan, by_plan[plan["plan_id"]]))
        count += 1
    return count
