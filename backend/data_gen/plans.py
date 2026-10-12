"""Plan catalogue + plan_benefits generator (deterministic)."""

from __future__ import annotations

import random
from typing import Any, cast

from backend.data_gen.reference import (
    HDHP_BASE,
    METAL_BASE,
    METAL_CODES,
    NETWORKS,
    PLAN_YEAR,
    SERVICE_CATEGORIES,
    VARIANTS,
    ServiceCategory,
)


def _r(value: float, step: int = 5) -> float:
    return float(round(value / step) * step)


def _money(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def generate_plans(rng: random.Random) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for net in NETWORKS:
        for metal in net.metals:
            variants = ["Standard"] if metal == "Catastrophic" else ["Value", "Standard", "Plus"]
            for variant in variants:
                vcode, prem_f, ded_f, copay_f = VARIANTS[variant]
                plan_id = f"{net.code}-{METAL_CODES[metal]}-{vcode}"
                if net.code == "HDHP":
                    base = cast(dict[str, Any], HDHP_BASE[metal])
                    deductible = _r(base["deductible"] * ded_f, 50)
                    deductible = max(deductible, 1700.0)  # IRS 2026 HDHP minimum
                    oop = min(_r(base["oop"] * (0.97 if variant == "Plus" else 1.0), 50), 8500.0)
                    premium = base["premium"] * prem_f * net.premium_factor
                    coins = base["coins"] - (5 if variant == "Plus" else 0) + (5 if variant == "Value" else 0)
                    copays: dict[str, float | None] = dict.fromkeys(
                        (
                            "pcp",
                            "spec",
                            "urgent",
                            "er",
                            "tele",
                            "lab",
                            "imaging",
                            "surgery",
                            "inpatient",
                            "rx1",
                            "rx2",
                            "rx3",
                        )
                    )
                    rx4 = rx5 = coins
                else:
                    # values are only None for copays (deductible + coinsurance applies)
                    base = cast(dict[str, Any], METAL_BASE[metal])
                    deductible = _r(float(base["deductible"]) * (ded_f if metal != "Catastrophic" else 1), 50)
                    oop = min(_r(float(base["oop"]) * (0.95 if variant == "Plus" else 1.0), 50), 10150.0)
                    deductible = min(deductible, oop)
                    premium = float(base["premium"]) * prem_f * net.premium_factor
                    coins = int(base["coins"] or 0)
                    copays = {
                        k: (None if base[k] is None else _r(float(base[k]) * copay_f, 5))
                        for k in (
                            "pcp",
                            "spec",
                            "urgent",
                            "er",
                            "tele",
                            "lab",
                            "imaging",
                            "surgery",
                            "inpatient",
                            "rx1",
                            "rx2",
                            "rx3",
                        )
                    }
                    rx4, rx5 = int(base["rx4"] or 0), int(base["rx5"] or 0)
                premium *= 1 + rng.uniform(-0.03, 0.03)
                plans.append(
                    {
                        "plan_id": plan_id,
                        "plan_name": f"{net.product_name} {metal} {variant}",
                        "network_name": net.network_name,
                        "plan_type": net.code,
                        "metal_tier": metal,
                        "variant": variant,
                        "plan_year": PLAN_YEAR,
                        "monthly_premium_individual": round(premium, 2),
                        "monthly_premium_family": round(premium * 2.7, 2),
                        "deductible_individual": deductible,
                        "deductible_family": deductible * 2,
                        "oop_max_individual": oop,
                        "oop_max_family": oop * 2,
                        "coinsurance_pct": coins,
                        "pcp_copay": _money(copays["pcp"]),
                        "specialist_copay": _money(copays["spec"]),
                        "urgent_care_copay": _money(copays["urgent"]),
                        "er_copay": _money(copays["er"]),
                        "telehealth_copay": _money(copays["tele"]),
                        "inpatient_copay": _money(copays["inpatient"]),
                        "outpatient_surgery_copay": _money(copays["surgery"]),
                        "imaging_copay": _money(copays["imaging"]),
                        "lab_copay": _money(copays["lab"]),
                        "rx_tier1_copay": _money(copays["rx1"]),
                        "rx_tier2_copay": _money(copays["rx2"]),
                        "rx_tier3_copay": _money(copays["rx3"]),
                        "rx_tier4_coinsurance_pct": rx4,
                        "rx_tier5_coinsurance_pct": rx5,
                        "hsa_eligible": net.hsa_eligible,
                        "requires_referral": net.requires_referral,
                        "out_of_network_covered": net.out_of_network_covered,
                        "oon_deductible_individual": deductible * 2 if net.out_of_network_covered else None,
                        "oon_coinsurance_pct": min(coins + 20, 60) if net.out_of_network_covered else None,
                        "states": list(net.states),
                        "star_rating": round(rng.choice([3.0, 3.5, 3.5, 4.0, 4.0, 4.0, 4.5, 4.5, 5.0]), 1),
                        # Internal (not a DB column): copay-vs-deductible design flags.
                        "_copay_based": copays["pcp"] is not None,
                        "_catastrophic": metal == "Catastrophic",
                    }
                )
    return plans


# ── Cost-sharing semantics shared by plan_benefits AND the claims engine ──
def copay_for(plan: dict[str, Any], category: ServiceCategory) -> float | None:
    """Return the copay that applies (None → deductible + coinsurance applies)."""
    if plan["plan_type"] == "HDHP" or category.mode != "copay":
        return None
    if plan["_catastrophic"]:
        return 0.0 if category.copay_field in ("pcp_copay", "telehealth_copay") else None
    value = plan.get(category.copay_field or "")
    return None if value is None else float(value)


def is_covered(plan: dict[str, Any], category: ServiceCategory) -> bool:
    if category.mode == "not_covered":
        return False
    return category.covered_metals is None or plan["metal_tier"] in category.covered_metals


def describe_in_network(
    plan: dict[str, Any], category: ServiceCategory
) -> tuple[str, float | None, int | None, bool]:
    """(text, copay, coinsurance_pct, deductible_applies) for the in-network column."""
    if not is_covered(plan, category):
        return "Not covered", None, None, False
    if category.mode in ("preventive", "free"):
        return "No charge", 0.0, None, False
    copay = copay_for(plan, category)
    coins = int(plan["coinsurance_pct"])
    if copay is not None:
        if plan["_catastrophic"] and category.copay_field == "pcp_copay":
            return "No charge for first 3 visits, then deductible applies", 0.0, None, False
        return (f"${copay:,.0f} copay" if copay else "No charge"), copay, None, False
    if plan["_catastrophic"]:
        return "No charge after deductible", None, 0, True
    return f"{coins}% coinsurance after deductible", None, coins, True


def describe_out_of_network(plan: dict[str, Any], category: ServiceCategory) -> str:
    if not is_covered(plan, category):
        return "Not covered"
    if category.oon_same_as_in:
        return "Same as in-network (No Surprises Act)"
    if category.mode in ("preventive", "free") and not plan["out_of_network_covered"]:
        return "Not covered"
    if not plan["out_of_network_covered"]:
        return "Not covered"
    return f"{plan['oon_coinsurance_pct']}% coinsurance after out-of-network deductible"


def generate_plan_benefits(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for plan in plans:
        for cat in SERVICE_CATEGORIES:
            text, copay, coins, ded = describe_in_network(plan, cat)
            notes = cat.limit_notes
            if cat.name == "Specialist visit" and not plan["requires_referral"]:
                notes = "No referral required"
            rows.append(
                {
                    "plan_id": plan["plan_id"],
                    "service_category": cat.name,
                    "covered": is_covered(plan, cat),
                    "in_network_cost": text,
                    "in_network_copay": copay,
                    "in_network_coinsurance_pct": coins,
                    "deductible_applies": ded,
                    "out_of_network_cost": describe_out_of_network(plan, cat),
                    "prior_auth_required": cat.prior_auth and is_covered(plan, cat),
                    "visit_limit": cat.visit_limit if is_covered(plan, cat) else None,
                    "limit_notes": notes,
                }
            )
    return rows


def public_plan_row(plan: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in plan.items() if not k.startswith("_")}
