"""Synthetic member population with realistic, condition-driven utilisation."""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from faker import Faker

from backend.data_gen.claims_engine import (
    ClaimFactory,
    Encounter,
    FamilyLedger,
    RxFill,
    accumulator_rows,
    network_ok,
)
from backend.data_gen.reference import AS_OF, CITIES, CITY_WEIGHTS, CONDITIONS, PLAN_YEAR

YEAR_START = date(PLAN_YEAR, 1, 1)


@dataclass
class Directory:
    """Lookup helpers over the CSV data lake."""

    providers: list[dict[str, Any]]
    facilities: list[dict[str, Any]]
    pharmacies: list[dict[str, Any]]
    by_city_spec: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    by_state_spec: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    fac_by_city_type: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    pharm_by_city: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    by_npi: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for p in self.providers:
            self.by_city_spec.setdefault((p["city"], p["specialty"]), []).append(p)
            self.by_state_spec.setdefault((p["state"], p["specialty"]), []).append(p)
            self.by_npi[p["npi"]] = p
        for f in self.facilities:
            self.fac_by_city_type.setdefault((f["city"], f["facility_type"]), []).append(f)
            self.by_npi[f["npi"]] = f
        for ph in self.pharmacies:
            self.pharm_by_city.setdefault(ph["city"], []).append(ph)

    def pick_provider(
        self,
        rng: random.Random,
        city: str,
        state: str,
        specialty: str,
        plan: dict[str, Any],
        p_in: float = 0.92,
    ) -> dict[str, Any]:
        pool = self.by_city_spec.get((city, specialty)) or self.by_state_spec.get((state, specialty))
        if not pool:
            pool = [p for p in self.providers if p["specialty"] == specialty] or self.providers
        in_net = [p for p in pool if network_ok(p, plan)]
        if in_net and rng.random() < p_in:
            return rng.choice(in_net)
        return rng.choice(pool)

    def pick_facility(
        self, rng: random.Random, city: str, ftype: str, plan: dict[str, Any], p_in: float = 0.9
    ) -> dict[str, Any]:
        pool = self.fac_by_city_type.get((city, ftype)) or [
            f for f in self.facilities if f["facility_type"] == ftype
        ]
        in_net = [f for f in pool if network_ok(f, plan)]
        if in_net and rng.random() < p_in:
            return rng.choice(in_net)
        return rng.choice(pool)

    def pick_pharmacy(self, rng: random.Random, city: str, mail_order: bool) -> dict[str, Any]:
        if mail_order:
            return next(p for p in self.pharmacies if p["pharmacy_type"] == "Mail Order")
        return rng.choice(self.pharm_by_city.get(city) or self.pharmacies)


def _rand_date(rng: random.Random, start: date = YEAR_START, end: date = AS_OF) -> date:
    return start + timedelta(days=rng.randint(0, max(0, (end - start).days)))


def _age(dob: date) -> int:
    return AS_OF.year - dob.year - ((AS_OF.month, AS_OF.day) < (dob.month, dob.day))


class PopulationGenerator:
    def __init__(
        self,
        rng: random.Random,
        fake: Faker,
        plans: list[dict[str, Any]],
        directory: Directory,
        formulary: list[dict[str, Any]],
        factory: ClaimFactory,
    ):
        self.rng = rng
        self.fake = fake
        self.plans = plans
        self.directory = directory
        self.drugs = {d["drug_name"]: d for d in formulary}
        self.factory = factory
        self.members: list[dict[str, Any]] = []
        self.claims: list[dict[str, Any]] = []
        self.rx_claims: list[dict[str, Any]] = []
        self.prior_auths: list[dict[str, Any]] = []
        self.accumulators: list[dict[str, Any]] = []

    # ── Members ─────────────────────────────────────────────────────────
    def _member_row(
        self,
        member_id: str,
        subscriber_id: str,
        rel: str,
        first: str,
        last: str,
        dob: date,
        gender: str,
        city: str,
        state: str,
        zip_code: str,
        plan: dict[str, Any],
        coverage: str,
        language: str,
        pcp_npi: str | None,
        hsa: float | None,
        enrolled: date,
    ) -> dict[str, Any]:
        return {
            "member_id": member_id,
            "subscriber_id": subscriber_id,
            "relationship_to_subscriber": rel,
            "first_name": first,
            "last_name": last,
            "date_of_birth": dob,
            "gender": gender,
            "phone": f"({self.rng.randint(201, 989)}) 555-{self.rng.randint(1000, 9999)}",
            "address_line": self.fake.street_address(),
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "plan_id": plan["plan_id"],
            "coverage_level": coverage,
            "enrollment_date": enrolled,
            "termination_date": None,
            "status": "active",
            "pcp_npi": pcp_npi,
            "preferred_language": language,
            "hsa_balance": hsa,
        }

    def generate_random_families(self, n_subscribers: int, start_seq: int = 100100) -> None:
        rng = self.rng
        seq = start_seq
        city_w = [CITY_WEIGHTS.get(c[0], 1.5) for c in CITIES]
        for _ in range(n_subscribers):
            city, state, _lat, _lon, base_zip = rng.choices(CITIES, city_w)[0]
            zip_code = f"{base_zip + rng.randint(0, 5):05d}"
            family = rng.random() < 0.27
            age = rng.randint(19, 64)
            dob = date(AS_OF.year - age, rng.randint(1, 12), rng.randint(1, 28))
            if age < 30 and rng.random() < 0.15:
                eligible = [p for p in self.plans if state in p["states"]]
            else:
                eligible = [
                    p for p in self.plans if state in p["states"] and p["metal_tier"] != "Catastrophic"
                ]
            weights = [
                {"Bronze": 3, "Silver": 5, "Gold": 3, "Platinum": 1.2, "Catastrophic": 0.6}[p["metal_tier"]]
                for p in eligible
            ]
            plan = rng.choices(eligible, weights)[0]
            gender = rng.choice(["F", "M"])
            last = self.fake.last_name()
            first = self.fake.first_name_female() if gender == "F" else self.fake.first_name_male()
            language = rng.choices(
                ["English", "Spanish", "Mandarin", "Vietnamese", "Korean", "Hindi"], [80, 12, 3, 2, 1.5, 1.5]
            )[0]
            pcp = self.directory.pick_provider(rng, city, state, "Family Medicine", plan, p_in=1.0)
            sub_id = f"M{seq}"
            seq += 1
            hsa = round(rng.uniform(150, 6500), 2) if plan["hsa_eligible"] else None
            enrolled = (
                date(PLAN_YEAR, 1, 1)
                if rng.random() < 0.85
                else _rand_date(rng, YEAR_START, date(PLAN_YEAR, 6, 30))
            )
            fam = [
                self._member_row(
                    sub_id,
                    sub_id,
                    "self",
                    first,
                    last,
                    dob,
                    gender,
                    city,
                    state,
                    zip_code,
                    plan,
                    "family" if family else "individual",
                    language,
                    pcp["npi"],
                    hsa,
                    enrolled,
                )
            ]
            if family:
                if rng.random() < 0.75:
                    sg = "M" if gender == "F" else "F"
                    sfirst = self.fake.first_name_female() if sg == "F" else self.fake.first_name_male()
                    sdob = date(dob.year + rng.randint(-4, 4), rng.randint(1, 12), rng.randint(1, 28))
                    fam.append(
                        self._member_row(
                            f"M{seq}",
                            sub_id,
                            "spouse",
                            sfirst,
                            last,
                            sdob,
                            sg,
                            city,
                            state,
                            zip_code,
                            plan,
                            "family",
                            language,
                            pcp["npi"],
                            None,
                            enrolled,
                        )
                    )
                    seq += 1
                for _k in range(rng.choice([1, 1, 2, 2, 3])):
                    cg = rng.choice(["F", "M"])
                    cfirst = self.fake.first_name_female() if cg == "F" else self.fake.first_name_male()
                    cage = rng.randint(0, min(17, max(0, age - 19)))
                    cdob = date(AS_OF.year - cage - 1, rng.randint(1, 12), rng.randint(1, 28))
                    ped = self.directory.pick_provider(rng, city, state, "Pediatrics", plan, p_in=1.0)
                    fam.append(
                        self._member_row(
                            f"M{seq}",
                            sub_id,
                            "child",
                            cfirst,
                            last,
                            cdob,
                            cg,
                            city,
                            state,
                            zip_code,
                            plan,
                            "family",
                            language,
                            ped["npi"],
                            None,
                            enrolled,
                        )
                    )
                    seq += 1
            self._simulate_family(fam, plan)

    # ── Utilisation simulation ──────────────────────────────────────────
    def _simulate_family(self, fam: list[dict[str, Any]], plan: dict[str, Any]) -> None:
        rng = self.rng
        encounters: list[Encounter] = []
        fills: list[RxFill] = []
        sub_id = fam[0]["subscriber_id"]
        for m in fam:
            e, f = self._member_utilisation(m, plan)
            encounters.extend(e)
            fills.extend(f)
        ledger = FamilyLedger(plan=plan, family=len(fam) > 1)
        events: list[tuple[date, int, Any]] = [(e.service_date, 0, e) for e in encounters]
        events += [(f.fill_date, 1, f) for f in fills]
        events.sort(key=lambda t: (t[0], t[1]))
        for _d, kind, ev in events:
            if kind == 0:
                claim = self.factory.adjudicate(ledger, ev, sub_id)
                self.claims.append(claim)
                if claim["status"] == "paid" and rng.random() < 0.006:
                    dup = dict(claim)
                    self.factory.claim_seq += 1
                    dup.update(
                        claim_id=f"CLM-2026-{self.factory.claim_seq:07d}",
                        status="denied",
                        denial_code="D004",
                        allowed_amount=0.0,
                        deductible_applied=0.0,
                        copay_amount=0.0,
                        coinsurance_amount=0.0,
                        member_responsibility=0.0,
                        plan_paid=0.0,
                        received_date=min(claim["received_date"] + timedelta(days=12), AS_OF),
                    )
                    self.claims.append(dup)
            else:
                self.rx_claims.append(self.factory.adjudicate_rx(ledger, ev, sub_id))
        self.members.extend(fam)
        self.accumulators.extend(accumulator_rows(ledger, fam, PLAN_YEAR))

    def _member_utilisation(
        self, m: dict[str, Any], plan: dict[str, Any]
    ) -> tuple[list[Encounter], list[RxFill]]:
        rng = self.rng
        d = self.directory
        mid, city, state = m["member_id"], m["city"], m["state"]
        age = _age(m["date_of_birth"])
        start = max(m["enrollment_date"], YEAR_START)
        enc: list[Encounter] = []
        fills: list[RxFill] = []
        child = age < 18
        pcp_spec = "Pediatrics" if child else "Family Medicine"
        pcp = d.by_npi.get(m["pcp_npi"] or "") or d.pick_provider(rng, city, state, pcp_spec, plan)

        # Preventive
        if rng.random() < 0.72:
            cpt = "99392" if age < 5 else "99393" if age < 12 else "99385" if age < 40 else "99396"
            enc.append(Encounter(mid, _rand_date(rng, start), cpt, "Z00.129" if child else "Z00.00", pcp))
        if rng.random() < 0.4:
            enc.append(Encounter(mid, _rand_date(rng, date(PLAN_YEAR, 9, 1)), "90686", "Z23", pcp))
        if m["gender"] == "F" and age >= 40 and rng.random() < 0.55:
            enc.append(
                Encounter(
                    mid,
                    _rand_date(rng, start),
                    "77067",
                    "Z12.31",
                    d.pick_facility(rng, city, "Imaging Center", plan),
                )
            )
        # Acute care
        for _ in range(min(6, int(rng.expovariate(1 / 1.4)))):
            icd = rng.choice(["J06.9", "J02.0", "N39.0", "S93.401A", "J30.9", "K21.9"])
            when = _rand_date(rng, start)
            r = rng.random()
            if r < 0.18:
                enc.append(Encounter(mid, when, "98006", icd, pcp, place_of_service="Telehealth"))
            elif r < 0.30:
                fac = d.pick_facility(rng, city, "Urgent Care", plan)
                enc.append(Encounter(mid, when, "S9083", icd, fac, place_of_service="Urgent Care"))
                if icd == "J02.0":
                    enc.append(Encounter(mid, when, "87880", icd, fac, place_of_service="Urgent Care"))
            elif r < 0.34:
                fac = d.pick_facility(rng, city, "Hospital", plan, p_in=0.75)
                enc.append(
                    Encounter(mid, when, "99283", icd, fac, place_of_service="Emergency Room", emergency=True)
                )
            else:
                enc.append(Encounter(mid, when, "99213", icd, pcp))
            if icd in ("J02.0", "N39.0"):
                drug = self.drugs["Amoxicillin" if icd == "J02.0" else "Nitrofurantoin"]
                fills.append(
                    RxFill(mid, when, drug, d.pick_pharmacy(rng, city, False), pcp["npi"], days_supply=10)
                )

        # Chronic conditions
        for cond, (prev, icd, spec, drugs, cpts) in CONDITIONS.items():
            if child and cond not in ("asthma", "anxiety_depression"):
                continue
            if rng.random() >= prev * (
                1.5 if age > 50 and cond in ("diabetes", "hypertension", "hyperlipidemia") else 1
            ):
                continue
            specialist = d.pick_provider(rng, city, state, spec, plan)
            visit_months = sorted(
                rng.sample(
                    range(start.month, AS_OF.month + 1),
                    k=min(AS_OF.month - start.month + 1, rng.randint(2, 4)),
                )
            )
            for mo in visit_months:
                when = date(PLAN_YEAR, mo, rng.randint(1, 28))
                if when > AS_OF:
                    continue
                for cpt in cpts:
                    prov = specialist if cpt not in ("80053", "80061", "83036") else pcp
                    if cond == "anxiety_depression" and cpt in ("90834", "90837"):
                        for k in range(rng.randint(2, 4)):
                            w = when + timedelta(days=14 * k)
                            if w <= AS_OF:
                                pos = "Telehealth" if rng.random() < 0.55 else "Office"
                                enc.append(Encounter(mid, w, cpt, icd, prov, place_of_service=pos))
                        continue
                    if cond == "back_pain" and cpt == "97110":
                        for k in range(rng.randint(3, 9)):
                            w = when + timedelta(days=7 * k)
                            if w <= AS_OF:
                                enc.append(Encounter(mid, w, cpt, icd, prov))
                        continue
                    referral = rng.random() > 0.06
                    enc.append(Encounter(mid, when, cpt, icd, prov, has_referral=referral))
            # Pharmacy: step through the ladder with realistic PA/step outcomes.
            mail = rng.random() < 0.3
            pharmacy = d.pick_pharmacy(rng, city, mail)
            ladder = drugs[: rng.choice([1, 1, 1, 2, len(drugs)])]
            for idx, drug_name in enumerate(ladder):
                drug = self.drugs[drug_name]
                first = date(PLAN_YEAR, max(start.month, rng.randint(1, 5) + idx * 2), rng.randint(1, 28))
                if first > AS_OF:
                    continue
                if drug["requires_prior_auth"]:
                    approved = rng.random() < 0.82
                    pa = self.factory.prior_auth(
                        mid,
                        m["subscriber_id"],
                        specialist,
                        f"{drug_name} ({drug['generic_name']})",
                        icd,
                        first - timedelta(days=rng.randint(2, 10)),
                        "approved" if approved else "denied",
                        drug_id=drug["drug_id"],
                        valid_days=365,
                        reason=None
                        if approved
                        else "Clinical criteria not met; formulary alternatives not tried",
                    )
                    self.prior_auths.append(pa)
                    if not approved:
                        fills.append(
                            RxFill(
                                mid,
                                first,
                                drug,
                                pharmacy,
                                specialist["npi"],
                                force_reject="Prior authorization denied",
                            )
                        )
                        continue
                elif drug["step_therapy"] and idx == 0 and rng.random() < 0.5:
                    fills.append(
                        RxFill(
                            mid,
                            first,
                            drug,
                            pharmacy,
                            specialist["npi"],
                            force_reject="D011 Step therapy requirement not met",
                        )
                    )
                    continue
                supply = 90 if mail else 30
                when = first
                while when <= AS_OF:
                    fills.append(RxFill(mid, when, drug, pharmacy, specialist["npi"], days_supply=supply))
                    when += timedelta(days=supply + rng.randint(-2, 4))

        # Advanced imaging / procedures with prior-auth outcomes
        if not child and rng.random() < 0.07:
            cpt, icd, spec = rng.choice(
                [
                    ("73721", "M25.561", "Orthopedic Surgery"),
                    ("72148", "M54.50", "Orthopedic Surgery"),
                    ("70553", "G43.909", "Neurology"),
                ]
            )
            ordering = d.pick_provider(rng, city, state, spec, plan)
            fac = d.pick_facility(rng, city, "Imaging Center", plan)
            when = _rand_date(rng, start)
            r = rng.random()
            if r < 0.78:
                self.prior_auths.append(
                    self.factory.prior_auth(
                        mid,
                        m["subscriber_id"],
                        ordering,
                        f"MRI ({cpt})",
                        icd,
                        when - timedelta(days=rng.randint(5, 15)),
                        "approved",
                        cpt=cpt,
                        units=1,
                    )
                )
                enc.append(Encounter(mid, when, cpt, icd, fac, place_of_service="Imaging Center"))
            elif r < 0.9:
                enc.append(
                    Encounter(
                        mid, when, cpt, icd, fac, place_of_service="Imaging Center", has_prior_auth=False
                    )
                )
            else:
                self.prior_auths.append(
                    self.factory.prior_auth(
                        mid,
                        m["subscriber_id"],
                        ordering,
                        f"MRI ({cpt})",
                        icd,
                        when - timedelta(days=7),
                        "denied",
                        cpt=cpt,
                        reason="Conservative therapy (6 weeks) not documented",
                    )
                )
        if not child and rng.random() < 0.025:
            sleep_doc = d.pick_provider(rng, city, state, "Sleep Medicine", plan)
            when = _rand_date(rng, start)
            self.prior_auths.append(
                self.factory.prior_auth(
                    mid,
                    m["subscriber_id"],
                    sleep_doc,
                    "Attended sleep study (95810)",
                    "G47.33",
                    when - timedelta(days=9),
                    "approved",
                    cpt="95810",
                    units=1,
                )
            )
            enc.append(
                Encounter(mid, when, "95810", "G47.33", sleep_doc, place_of_service="Outpatient Hospital")
            )
        if rng.random() < 0.03:
            # Pending PA for an upcoming service
            ortho = d.pick_provider(rng, city, state, "Orthopedic Surgery", plan)
            self.prior_auths.append(
                self.factory.prior_auth(
                    mid,
                    m["subscriber_id"],
                    ortho,
                    "Knee arthroscopy with meniscectomy (29881)",
                    "M23.221",
                    AS_OF - timedelta(days=rng.randint(1, 6)),
                    "pending",
                    cpt="29881",
                )
            )
        # Random claims-status variety
        for e in enc:
            if rng.random() < 0.015:
                e.force_status = "in_review"
            elif rng.random() < 0.012:
                e.force_denial = "D012"
            elif rng.random() < 0.006:
                e.force_denial = "D008"
        enc = [e for e in enc if e.service_date <= AS_OF]
        return enc, fills


def group_claims_by_member(claims: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in claims:
        out[c["member_id"]].append(c)
    return out
