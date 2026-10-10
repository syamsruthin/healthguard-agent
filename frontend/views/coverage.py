"""My Coverage: plan summary, deductible and out-of-pocket progress, family members, activity."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend.lib.api import client
from frontend.lib.ui import md, money


def _progress(label: str, met: Any, limit: Any) -> None:
    met_f, limit_f = float(met or 0), float(limit or 0)
    ratio = min(met_f / limit_f, 1.0) if limit_f else 0.0
    st.markdown(md(f"**{label}** — {money(met_f)} of {money(limit_f)}"))
    st.progress(ratio, text=f"{money(max(limit_f - met_f, 0))} remaining")


def render() -> None:
    st.title("My coverage")
    card = client().coverage()
    plan = card.get("plan")
    if not plan:
        st.info(
            "No member record is linked to this account, so there is no coverage to show. "
            "You can still ask general questions about HealthGuard plans and policies in Chat."
        )
        return
    st.caption(f"Data current through {card['as_of']}")
    st.subheader(f"{plan['plan_name']} · {plan['plan_id']}")
    hsa = f"HSA balance {money(plan['hsa_balance'])}" if plan.get("hsa_eligible") else "Not HSA-eligible"
    st.markdown(
        md(
            f":blue-badge[{plan['network_name']} network] :gray-badge[{plan['plan_type']} · {plan['metal_tier']}] "
            f":gray-badge[{(plan.get('coverage_level') or '').title()} coverage] :gray-badge[{hsa}]"
        )
    )

    own = card["accumulators"][0] if card["accumulators"] else None
    if own:
        st.subheader(f"{own['plan_year']} progress")
        left, right = st.columns(2)
        with left:
            _progress(
                "Individual deductible", own["deductible_individual_met"], own["deductible_individual_limit"]
            )
            if own.get("deductible_family_limit"):
                _progress("Family deductible", own["deductible_family_met"], own["deductible_family_limit"])
        with right:
            _progress("Individual out-of-pocket max", own["oop_individual_met"], own["oop_individual_limit"])
            if own.get("oop_family_limit"):
                _progress("Family out-of-pocket max", own["oop_family_met"], own["oop_family_limit"])

    st.subheader("What you pay")
    st.dataframe(
        [
            {
                "Service": "Primary care visit",
                "You pay": money(plan["pcp_copay"])
                if plan["pcp_copay"] is not None
                else "Deductible, then coinsurance",
            },
            {
                "Service": "Specialist visit",
                "You pay": money(plan["specialist_copay"])
                if plan["specialist_copay"] is not None
                else "Deductible, then coinsurance",
            },
            {
                "Service": "Urgent care",
                "You pay": money(plan["urgent_care_copay"])
                if plan["urgent_care_copay"] is not None
                else "Deductible, then coinsurance",
            },
            {
                "Service": "Emergency room",
                "You pay": money(plan["er_copay"])
                if plan["er_copay"] is not None
                else "Deductible, then coinsurance",
            },
            {
                "Service": "Telehealth",
                "You pay": money(plan["telehealth_copay"])
                if plan["telehealth_copay"] is not None
                else "Deductible, then coinsurance",
            },
            {"Service": "Coinsurance after deductible", "You pay": f"{plan['coinsurance_pct']}%"},
        ],
        hide_index=True,
        use_container_width=True,
    )
    flags = []
    flags.append(
        "Referrals required for specialists" if plan.get("requires_referral") else "No referrals needed"
    )
    flags.append(
        "Out-of-network care covered"
        if plan.get("out_of_network_covered")
        else "In-network care only (except emergencies)"
    )
    st.caption(" · ".join(flags))

    if card.get("dependents"):
        st.subheader("Family members on your plan")
        st.dataframe(
            [
                {
                    "Name": d["first_name"],
                    "Relationship": d["relationship"].title(),
                    "Member ID": d["member_id"],
                    "Age": d["age"],
                }
                for d in card["dependents"]
            ],
            hide_index=True,
            use_container_width=True,
        )
    activity = card.get("activity") or {}
    if activity:
        st.subheader("Claims activity this year")
        cols = st.columns(5)
        cols[0].metric("Claims", activity["claims"])
        cols[1].metric("Denied", activity["denied_claims"])
        cols[2].metric("Pending", activity["pending_claims"])
        cols[3].metric("Pending prior auths", activity["pending_prior_auths"])
        cols[4].metric("You've paid", money(activity["member_paid_ytd"]))
