"""
Claims adjudication engine for synthetic data.

Applies the *same* cost-sharing rules the plan_benefits rows describe
(copay vs deductible+coinsurance, catastrophic PCP visits, HDHP, OOP caps,
family accumulators, No Surprises Act emergency treatment, HMO referrals,
EPO/HMO out-of-network exclusion). Every claim, pharmacy fill and
accumulator row is therefore internally consistent with the plan design.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from backend.data_gen.plans import copay_for, is_covered
from backend.data_gen.reference import AS_OF, DIAGNOSES, PROCEDURES, SERVICE_CATEGORIES, ServiceCategory

CATEGORY_BY_NAME = {c.name: c for c in SERVICE_CATEGORIES}

# Statuses that count toward accumulators
_COUNTED = {"paid", "appealed"}


@dataclass
class Encounter:
    member_id: str
    service_date: date
    cpt: str
    icd: str
    provider: dict[str, Any]  # provider or facility row
    category: str | None = None  # override benefit category
    place_of_service: str = "Office"
    allowed: float | None = None
    billed: float | None = None
    force_status: str | None = None
    force_denial: str | None = None
    note: str | None = None
    has_referral: bool = True
    has_prior_auth: bool = True
    emergency: bool = False


@dataclass
class RxFill:
    member_id: str
    fill_date: date
    drug: dict[str, Any]
    pharmacy: dict[str, Any]
    prescriber_npi: str
    days_supply: int = 30
    force_reject: str | None = None


@dataclass
class FamilyLedger:
    plan: dict[str, Any]
    family: bool
    ded_ind: dict[str, float] = field(default_factory=dict)
    oop_ind: dict[str, float] = field(default_factory=dict)
    ded_fam: float = 0.0
    oop_fam: float = 0.0
    pcp_visits: dict[str, int] = field(default_factory=dict)
    benefit_counts: dict[tuple[str, str], int] = field(default_factory=dict)

    def ded_remaining(self, mid: str) -> float:
        rem = float(self.plan["deductible_individual"]) - self.ded_ind.get(mid, 0.0)
        if self.family:
            rem = min(rem, float(self.plan["deductible_family"]) - self.ded_fam)
        return max(0.0, rem)

    def oop_remaining(self, mid: str) -> float:
        rem = float(self.plan["oop_max_individual"]) - self.oop_ind.get(mid, 0.0)
        if self.family:
            rem = min(rem, float(self.plan["oop_max_family"]) - self.oop_fam)
        return max(0.0, rem)

    def apply(self, mid: str, deductible: float, member: float) -> None:
        self.ded_ind[mid] = self.ded_ind.get(mid, 0.0) + deductible
        self.oop_ind[mid] = self.oop_ind.get(mid, 0.0) + member
        self.ded_fam += deductible
        self.oop_fam += member


def network_ok(provider: dict[str, Any], plan: dict[str, Any]) -> bool:
    return plan["network_name"] in str(provider.get("networks", "")).split("|")


def _cost_share(
    ledger: FamilyLedger, mid: str, cat: ServiceCategory, allowed: float, in_network: bool, emergency: bool
) -> tuple[float, float, float]:
    """Return (deductible_applied, copay, coinsurance) BEFORE the OOP cap."""
    plan = ledger.plan
    if cat.mode in ("preventive", "free") and in_network:
        return 0.0, 0.0, 0.0
    treat_in = in_network or (emergency and cat.oon_same_as_in)
    coins_pct = plan["coinsurance_pct"] if treat_in else (plan["oon_coinsurance_pct"] or 50)
    copay = copay_for(plan, cat) if treat_in else None
    if plan["_catastrophic"] and cat.copay_field == "pcp_copay":
        n = ledger.pcp_visits.get(mid, 0)
        ledger.pcp_visits[mid] = n + 1
        copay = 0.0 if n < 3 else None
    if copay is not None:
        return 0.0, min(copay, allowed), 0.0
    ded = min(allowed, ledger.ded_remaining(mid))
    coins = round((allowed - ded) * coins_pct / 100, 2)
    return round(ded, 2), 0.0, coins


def _cap(
    ledger: FamilyLedger, mid: str, ded: float, copay: float, coins: float
) -> tuple[float, float, float]:
    remaining = ledger.oop_remaining(mid)
    total = ded + copay + coins
    if total <= remaining:
        return ded, copay, coins
    excess = total - remaining
    for _ in range(3):
        if excess <= 0:
            break
        if coins > 0:
            cut = min(coins, excess)
            coins -= cut
            excess -= cut
        elif copay > 0:
            cut = min(copay, excess)
            copay -= cut
            excess -= cut
        else:
            cut = min(ded, excess)
            ded -= cut
            excess -= cut
    return round(ded, 2), round(copay, 2), round(coins, 2)


class ClaimFactory:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.claim_seq = 0
        self.rx_seq = 0
        self.pa_seq = 0

    # ── Medical ─────────────────────────────────────────────────────────
    def adjudicate(self, ledger: FamilyLedger, enc: Encounter, subscriber_id: str) -> dict[str, Any]:
        rng = self.rng
        plan = ledger.plan
        desc, _cat, typical, default_category, _spec = PROCEDURES[enc.cpt]
        cat = CATEGORY_BY_NAME[enc.category or default_category]
        in_network = network_ok(enc.provider, plan)
        allowed = enc.allowed if enc.allowed is not None else round(typical * rng.uniform(0.88, 1.12), 2)
        billed = enc.billed if enc.billed is not None else round(allowed * rng.uniform(1.6, 2.6), 2)
        received = enc.service_date + timedelta(days=rng.randint(2, 20))
        status, denial, note = "paid", None, enc.note
        key = (enc.member_id, cat.name)
        count = ledger.benefit_counts.get(key, 0) + 1
        ledger.benefit_counts[key] = count

        if enc.force_denial:
            status, denial = "denied", enc.force_denial
        elif not is_covered(plan, cat):
            status, denial = "denied", "D001"
        elif (
            not in_network
            and not plan["out_of_network_covered"]
            and not (enc.emergency and cat.oon_same_as_in)
        ):
            status, denial = "denied", "D009"
        elif plan["requires_referral"] and cat.name == "Specialist visit" and not enc.has_referral:
            status, denial = "denied", "D010"
        elif cat.prior_auth and not enc.has_prior_auth and not enc.emergency:
            status, denial = "denied", "D002"
        elif cat.visit_limit and count > cat.visit_limit:
            status, denial = "denied", "D007"
        if enc.force_status:
            status = enc.force_status
            if status in ("pending", "in_review"):
                denial = None
        elif status == "paid" and (AS_OF - enc.service_date).days <= 14 and rng.random() < 0.6:
            status = "pending"

        ded = copay = coins = member = plan_paid = 0.0
        processed: date | None = received + timedelta(days=rng.randint(4, 24))
        if status in ("pending", "in_review"):
            processed = None
            allowed_out = 0.0
        elif status == "denied":
            allowed_out = 0.0
            # Provider-liability denials: in-network providers cannot bill the member.
            member = (
                0.0 if denial in ("D002", "D004", "D010", "D012", "D008", "D005") and in_network else billed
            )
            if denial in ("D004",):
                member = 0.0
        else:
            allowed_out = allowed
            ded, copay, coins = _cost_share(ledger, enc.member_id, cat, allowed, in_network, enc.emergency)
            ded, copay, coins = _cap(ledger, enc.member_id, ded, copay, coins)
            member = round(ded + copay + coins, 2)
            plan_paid = round(allowed - member, 2)
            if status in _COUNTED:
                ledger.apply(enc.member_id, ded, member)
            if enc.emergency and not in_network:
                note = note or (
                    "Out-of-network emergency services processed at in-network cost sharing under the "
                    "No Surprises Act. The provider may not balance bill you."
                )
        if processed and processed > AS_OF:
            processed = AS_OF
        self.claim_seq += 1
        is_facility = "facility_type" in enc.provider
        return {
            "claim_id": f"CLM-2026-{self.claim_seq:07d}",
            "member_id": enc.member_id,
            "subscriber_id": subscriber_id,
            "claim_type": "institutional"
            if is_facility or cat.name.startswith("Inpatient")
            else "professional",
            "service_date": enc.service_date,
            "received_date": min(received, AS_OF),
            "processed_date": processed,
            "provider_npi": enc.provider["npi"],
            "provider_name": enc.provider.get("provider_name") or enc.provider.get("name"),
            "provider_specialty": enc.provider.get("specialty") or enc.provider.get("facility_type"),
            "facility_name": enc.provider.get("name") if is_facility else None,
            "in_network": in_network,
            "place_of_service": enc.place_of_service,
            "cpt_code": enc.cpt,
            "procedure_description": desc,
            "icd10_code": enc.icd,
            "diagnosis_description": DIAGNOSES[enc.icd][0],
            "billed_amount": billed,
            "allowed_amount": allowed_out,
            "deductible_applied": ded,
            "copay_amount": copay,
            "coinsurance_amount": coins,
            "member_responsibility": member,
            "plan_paid": plan_paid,
            "status": status,
            "denial_code": denial,
            "eob_note": note,
        }

    # ── Pharmacy ────────────────────────────────────────────────────────
    def adjudicate_rx(self, ledger: FamilyLedger, fill: RxFill, subscriber_id: str) -> dict[str, Any]:
        plan = ledger.plan
        drug = fill.drug
        months = fill.days_supply / 30
        total = round(float(drug["avg_retail_cost_30day"]) * months * self.rng.uniform(0.92, 1.05), 2)
        tier = int(drug["tier"])
        status, reason = "paid", None
        member = 0.0
        if fill.force_reject:
            status, reason = "rejected", fill.force_reject
        else:
            ded = 0.0
            if plan["plan_type"] == "HDHP" or plan["_catastrophic"]:
                ded = min(total, ledger.ded_remaining(fill.member_id))
                rest = total - ded
                pct = plan["coinsurance_pct"] if plan["plan_type"] == "HDHP" else 0
                member = ded + rest * pct / 100
            elif tier <= 3:
                copay = float(plan[f"rx_tier{tier}_copay"] or 0) * (2.5 if fill.days_supply >= 90 else 1)
                member = min(copay, total)
            else:
                pct = plan[f"rx_tier{tier}_coinsurance_pct"]
                member = total * pct / 100
            member = round(min(member, ledger.oop_remaining(fill.member_id)), 2)
            ded = min(ded, member)
            ledger.apply(fill.member_id, ded, member)
        self.rx_seq += 1
        return {
            "rx_claim_id": f"RX-2026-{self.rx_seq:07d}",
            "member_id": fill.member_id,
            "subscriber_id": subscriber_id,
            "fill_date": fill.fill_date,
            "drug_id": drug["drug_id"],
            "drug_name": drug["drug_name"],
            "tier": tier,
            "days_supply": fill.days_supply,
            "quantity": fill.days_supply,
            "pharmacy_id": fill.pharmacy["pharmacy_id"],
            "pharmacy_name": fill.pharmacy["name"],
            "prescriber_npi": fill.prescriber_npi,
            "total_cost": total,
            "member_paid": member if status == "paid" else 0.0,
            "plan_paid": round(total - member, 2) if status == "paid" else 0.0,
            "status": status,
            "reject_reason": reason,
        }

    # ── Prior authorization ─────────────────────────────────────────────
    def prior_auth(
        self,
        member_id: str,
        subscriber_id: str,
        provider: dict[str, Any],
        service: str,
        icd: str,
        submitted: date,
        status: str,
        *,
        cpt: str | None = None,
        drug_id: str | None = None,
        decision_days: int = 3,
        valid_days: int = 90,
        units: int | None = None,
        reason: str | None = None,
        urgency: str = "standard",
    ) -> dict[str, Any]:
        self.pa_seq += 1
        decided = status not in ("pending",)
        decision = submitted + timedelta(days=decision_days) if decided else None
        return {
            "pa_id": f"PA-2026-{self.pa_seq:06d}",
            "member_id": member_id,
            "subscriber_id": subscriber_id,
            "requesting_provider_npi": provider["npi"],
            "requesting_provider_name": provider.get("provider_name") or provider.get("name"),
            "service_description": service,
            "cpt_code": cpt,
            "drug_id": drug_id,
            "icd10_code": icd,
            "urgency": urgency,
            "status": status,
            "submitted_date": submitted,
            "decision_date": decision,
            "valid_through": (decision + timedelta(days=valid_days))
            if status == "approved" and decision
            else None,
            "units_approved": units if status == "approved" else None,
            "decision_reason": reason,
        }


def accumulator_rows(
    ledger: FamilyLedger, members: list[dict[str, Any]], plan_year: int
) -> list[dict[str, Any]]:
    plan = ledger.plan
    rows = []
    for m in members:
        mid = m["member_id"]
        rows.append(
            {
                "member_id": mid,
                "subscriber_id": m["subscriber_id"],
                "plan_year": plan_year,
                "deductible_individual_met": round(ledger.ded_ind.get(mid, 0.0), 2),
                "deductible_individual_limit": float(plan["deductible_individual"]),
                "oop_individual_met": round(ledger.oop_ind.get(mid, 0.0), 2),
                "oop_individual_limit": float(plan["oop_max_individual"]),
                "deductible_family_met": round(ledger.ded_fam, 2) if ledger.family else None,
                "deductible_family_limit": float(plan["deductible_family"]) if ledger.family else None,
                "oop_family_met": round(ledger.oop_fam, 2) if ledger.family else None,
                "oop_family_limit": float(plan["oop_max_family"]) if ledger.family else None,
            }
        )
    return rows
