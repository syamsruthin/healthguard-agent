"""
Scripted demo personas — hand-crafted claim histories that make the demo
questions interesting (denials, prior auths, No Surprises Act, OOP max,
family accumulators, HSA, telehealth behavioural health).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from backend.data_gen.claims_engine import ClaimFactory, Encounter, FamilyLedger, RxFill, accumulator_rows
from backend.data_gen.members import Directory, PopulationGenerator
from backend.data_gen.reference import PERSONAS, PLAN_YEAR, Persona

D = date  # brevity


def _fac(directory: Directory, name: str) -> dict[str, Any]:
    return next(f for f in directory.facilities if f["name"] == name)


def _prov(directory: Directory, npi: str) -> dict[str, Any]:
    return directory.by_npi[npi]


def build_personas(gen: PopulationGenerator) -> None:
    d = gen.directory
    plans = {p["plan_id"]: p for p in gen.plans}
    drugs = gen.drugs
    f: ClaimFactory = gen.factory
    for persona in PERSONAS:
        plan = plans[persona.plan_id]
        members = _persona_members(gen, persona, plan)
        sub = persona.member_id
        enc: list[Encounter] = []
        fills: list[RxFill] = []
        pas: list[dict[str, Any]] = []
        script = SCRIPTS[persona.key]
        script(d, drugs, f, sub, enc, fills, pas)
        ledger = FamilyLedger(plan=plan, family=len(members) > 1)
        events = [(e.service_date, 0, e) for e in enc] + [(x.fill_date, 1, x) for x in fills]
        events.sort(key=lambda t: (t[0], t[1]))
        for _d, _kind, ev in events:
            if isinstance(ev, Encounter):
                gen.claims.append(f.adjudicate(ledger, ev, sub))
            else:
                gen.rx_claims.append(f.adjudicate_rx(ledger, ev, sub))
        gen.prior_auths.extend(pas)
        gen.members.extend(members)
        gen.accumulators.extend(accumulator_rows(ledger, members, PLAN_YEAR))


def _persona_members(gen: PopulationGenerator, p: Persona, plan: dict[str, Any]) -> list[dict[str, Any]]:
    pcp_by_persona = {
        "maria": "1700000001",
        "james": "1700000005",
        "priya": "1700000016",
        "robert": "1700000014",
        "aisha": "1700000015",
        "david": "1700000012",
    }
    hsa = 2150.00 if p.key == "priya" else None
    rows = [
        gen._member_row(
            p.member_id,
            p.member_id,
            "self",
            p.first_name,
            p.last_name,
            p.dob,
            p.gender,
            p.city,
            p.state,
            p.zip_code,
            plan,
            p.coverage_level,
            p.language,
            pcp_by_persona[p.key],
            hsa,
            D(PLAN_YEAR, 1, 1),
        )
    ]
    for mid, first, rel, dob, gender in p.dependents:
        pcp = "1700000011" if rel == "child" else pcp_by_persona[p.key]
        rows.append(
            gen._member_row(
                mid,
                p.member_id,
                rel,
                first,
                p.last_name,
                dob,
                gender,
                p.city,
                p.state,
                p.zip_code,
                plan,
                p.coverage_level,
                p.language,
                pcp,
                None,
                D(PLAN_YEAR, 1, 1),
            )
        )
    return rows


# ═══════════════════════════════════════════════════════════════════════════
# Scripts
# ═══════════════════════════════════════════════════════════════════════════
def _maria(d, drugs, f, sub, enc, fills, pas):
    pcp, ortho, pt = _prov(d, "1700000001"), _prov(d, "1700000002"), _prov(d, "1700000003")
    imaging = _fac(d, "Austin Imaging Center")
    enc += [
        Encounter(sub, D(2026, 1, 14), "99395", "Z00.00", pcp, allowed=185.0),
        Encounter(sub, D(2026, 3, 2), "99213", "M25.561", pcp, allowed=112.0),
        Encounter(sub, D(2026, 3, 2), "73562", "M25.561", pcp, allowed=104.0),
        Encounter(sub, D(2026, 3, 18), "99204", "M25.561", ortho, allowed=228.0),
        Encounter(
            sub,
            D(2026, 4, 6),
            "73721",
            "M23.221",
            imaging,
            place_of_service="Imaging Center",
            allowed=1450.0,
            billed=2450.0,
            has_prior_auth=False,
            note="Prior authorization was not on file for this MRI. Because Austin Imaging Center is in-network, "
            "you cannot be billed for this denial. You or your provider may request a retrospective "
            "review or file a Level 1 appeal within 180 days of this notice.",
        ),
        Encounter(sub, D(2026, 5, 11), "97161", "M23.221", pt, allowed=118.0),
    ]
    for k in range(10):
        enc.append(
            Encounter(sub, D(2026, 5, 18) + timedelta(days=7 * k), "97110", "M23.221", pt, allowed=46.0)
        )
    pas.append(
        f.prior_auth(
            sub,
            sub,
            ortho,
            "MRI right knee without contrast (73721)",
            "M23.221",
            D(2026, 7, 27),
            "approved",
            cpt="73721",
            decision_days=3,
            valid_days=60,
            units=1,
        )
    )
    enc += [
        Encounter(
            sub,
            D(2026, 8, 4),
            "73721",
            "M23.221",
            imaging,
            place_of_service="Imaging Center",
            allowed=1450.0,
            billed=2450.0,
        ),
        Encounter(
            sub,
            D(2026, 7, 15),
            "74177",
            "K21.9",
            _fac(d, "Austin Imaging Center"),
            place_of_service="Imaging Center",
            allowed=1250.0,
            billed=2100.0,
        ),
        Encounter(sub, D(2026, 8, 20), "99214", "M23.221", ortho, allowed=162.0),
    ]
    pas.append(
        f.prior_auth(
            sub,
            sub,
            ortho,
            "CT abdomen and pelvis with contrast (74177)",
            "K21.9",
            D(2026, 7, 8),
            "approved",
            cpt="74177",
            decision_days=2,
            valid_days=60,
            units=1,
        )
    )
    pas.append(
        f.prior_auth(
            sub,
            sub,
            ortho,
            "Knee arthroscopy with meniscectomy, right knee (29881)",
            "M23.221",
            D(2026, 10, 1),
            "pending",
            cpt="29881",
        )
    )
    pharmacy = next(p for p in d.pharmacies if p["city"] == "Austin")
    when = D(2026, 3, 3)
    while when <= D(2026, 10, 3):
        fills.append(RxFill(sub, when, drugs["Meloxicam"], pharmacy, pcp["npi"]))
        when += timedelta(days=30)


def _james(d, drugs, f, sub, enc, fills, pas):
    pcp, endo, derm = _prov(d, "1700000005"), _prov(d, "1700000004"), _prov(d, "1700000006")
    enc.append(Encounter(sub, D(2026, 2, 3), "99396", "Z00.00", pcp, allowed=198.0))
    for mo in (1, 4, 7):
        when = D(2026, mo, 20)
        enc += [
            Encounter(sub, when, "99214", "E11.9", endo, allowed=160.0),
            Encounter(sub, when, "83036", "E11.9", endo, allowed=30.0),
        ]
    enc.append(Encounter(sub, D(2026, 9, 29), "99214", "E11.65", endo, allowed=160.0))
    enc.append(
        Encounter(
            sub,
            D(2026, 5, 19),
            "11102",
            "L57.0",
            derm,
            allowed=160.0,
            billed=395.0,
            has_referral=False,
            note="Core HMO specialist visits require an active referral from your PCP. Your PCP may submit "
            "a retroactive referral within 60 days of service and request reprocessing.",
        )
    )
    pharmacy = next(p for p in d.pharmacies if p["city"] == "Seattle")
    for drug in ("Metformin ER", "Jardiance"):
        when = D(2026, 1, 6)
        while when <= D(2026, 10, 2):
            fills.append(RxFill(sub, when, drugs[drug], pharmacy, endo["npi"]))
            when += timedelta(days=30)
    fills.append(
        RxFill(
            sub,
            D(2026, 3, 5),
            drugs["Ozempic"],
            pharmacy,
            endo["npi"],
            force_reject="Prior authorization required (GLP-1 receptor agonist)",
        )
    )
    pas.append(
        f.prior_auth(
            sub,
            sub,
            endo,
            "Ozempic (semaglutide) 1 mg weekly",
            "E11.65",
            D(2026, 3, 6),
            "approved",
            drug_id=drugs["Ozempic"]["drug_id"],
            decision_days=3,
            valid_days=365,
            reason="Approved: A1c 8.4% on metformin + SGLT2 inhibitor (step therapy satisfied)",
        )
    )
    when = D(2026, 3, 10)
    while when <= D(2026, 10, 2):
        fills.append(RxFill(sub, when, drugs["Ozempic"], pharmacy, endo["npi"], days_supply=28))
        when += timedelta(days=28)


def _priya(d, drugs, f, sub, enc, fills, pas):
    obgyn, pcp = _prov(d, "1700000007"), _prov(d, "1700000016")
    enc.append(Encounter(sub, D(2026, 1, 28), "99395", "Z00.00", pcp, allowed=185.0))
    for k, when in enumerate(
        [
            D(2026, 3, 4),
            D(2026, 4, 8),
            D(2026, 5, 6),
            D(2026, 6, 10),
            D(2026, 7, 8),
            D(2026, 8, 5),
            D(2026, 8, 26),
            D(2026, 9, 16),
            D(2026, 9, 30),
        ]
    ):
        enc.append(
            Encounter(
                sub, when, "99213", "Z34.80", obgyn, category="Prenatal and postnatal care", allowed=110.0
            )
        )
        if k in (0, 4):
            enc.append(Encounter(sub, when, "80053", "Z34.80", obgyn, allowed=38.0))
            enc.append(Encounter(sub, when, "85025", "Z34.80", obgyn, allowed=22.0))
    enc.append(Encounter(sub, D(2026, 6, 10), "76805", "Z34.80", obgyn, allowed=290.0))
    enc.append(Encounter(sub, D(2026, 7, 8), "82947", "Z34.80", obgyn, allowed=25.0))
    enc.append(Encounter(sub, D(2026, 9, 16), "59025", "Z34.80", obgyn, allowed=110.0))
    pas.append(
        f.prior_auth(
            sub,
            sub,
            obgyn,
            "Non-invasive prenatal genetic screening (81420)",
            "Z36.0",
            D(2026, 4, 9),
            "approved",
            cpt="81420",
            decision_days=2,
            units=1,
        )
    )
    enc.append(Encounter(sub, D(2026, 4, 20), "81420", "Z36.0", obgyn, allowed=795.0, billed=1650.0))
    pas.append(
        f.prior_auth(
            sub,
            sub,
            obgyn,
            "Inpatient delivery admission — Brooklyn Heights Women's Hospital (expected delivery 2026-11-12)",
            "Z34.80",
            D(2026, 9, 15),
            "approved",
            cpt="59400",
            decision_days=1,
            valid_days=90,
            units=1,
            reason="Pre-certified: 48h vaginal / 96h cesarean stay; notify within 24h of admission",
        )
    )
    pharmacy = next(p for p in d.pharmacies if p["city"] == "Brooklyn")
    when = D(2026, 2, 2)
    while when <= D(2026, 10, 1):
        fills.append(RxFill(sub, when, drugs["Prenatal vitamins (Rx)"], pharmacy, obgyn["npi"]))
        when += timedelta(days=30)


def _robert(d, drugs, f, sub, enc, fills, pas):
    cardio, pcp = _prov(d, "1700000008"), _prov(d, "1700000014")
    er = _fac(d, "Bayfront Regional Medical Center")
    enc += [
        Encounter(
            sub,
            D(2026, 3, 8),
            "99285",
            "R07.9",
            er,
            place_of_service="Emergency Room",
            emergency=True,
            allowed=1650.0,
            billed=4800.0,
        ),
        Encounter(
            sub,
            D(2026, 3, 8),
            "A0427",
            "R07.9",
            er,
            place_of_service="Ambulance",
            emergency=True,
            allowed=950.0,
            billed=2300.0,
            note="Out-of-network air/ground ambulance for an emergency is covered at in-network cost sharing.",
        ),
        Encounter(sub, D(2026, 3, 20), "99204", "I21.4", cardio, allowed=230.0),
        Encounter(sub, D(2026, 3, 27), "93306", "I21.4", cardio, allowed=780.0),
        Encounter(sub, D(2026, 4, 2), "93015", "I21.4", cardio, allowed=420.0),
        Encounter(
            sub,
            D(2026, 4, 14),
            "93458",
            "I25.10",
            cardio,
            place_of_service="Inpatient Hospital",
            allowed=6800.0,
            billed=15400.0,
        ),
        Encounter(
            sub,
            D(2026, 5, 6),
            "33533",
            "I25.10",
            cardio,
            place_of_service="Inpatient Hospital",
            allowed=42000.0,
            billed=96000.0,
        ),
    ]
    for code, desc, when in (
        ("93306", "Echocardiogram", D(2026, 3, 20)),
        ("93458", "Cardiac catheterization", D(2026, 4, 6)),
        ("33533", "Coronary artery bypass graft (CABG)", D(2026, 4, 24)),
    ):
        pas.append(
            f.prior_auth(
                sub,
                sub,
                cardio,
                f"{desc} ({code})",
                "I25.10",
                when,
                "approved",
                cpt=code,
                decision_days=2,
                units=1,
                urgency="expedited" if code != "93306" else "standard",
            )
        )
    for k in range(24):
        enc.append(
            Encounter(sub, D(2026, 6, 2) + timedelta(days=3 * k), "93798", "Z98.890", cardio, allowed=110.0)
        )
    enc.append(Encounter(sub, D(2026, 9, 9), "99214", "I25.10", cardio, allowed=160.0))
    enc.append(
        Encounter(
            sub,
            D(2026, 9, 9),
            "99214",
            "I25.10",
            cardio,
            allowed=160.0,
            force_denial="D004",
            note="Duplicate of a claim already processed for this date of service. No action needed.",
        )
    )
    enc.append(Encounter(sub, D(2026, 2, 11), "99396", "Z00.00", pcp, allowed=200.0))
    pharmacy = next(p for p in d.pharmacies if p["city"] == "Miami")
    for drug in ("Atorvastatin", "Metoprolol succinate", "Clopidogrel", "Aspirin 81 mg"):
        when = D(2026, 3, 12)
        while when <= D(2026, 10, 2):
            fills.append(RxFill(sub, when, drugs[drug], pharmacy, cardio["npi"]))
            when += timedelta(days=30)


def _aisha(d, drugs, f, sub, enc, fills, pas):
    pcp, psych, psychiatrist = _prov(d, "1700000015"), _prov(d, "1700000009"), _prov(d, "1700000010")
    enc.append(Encounter(sub, D(2026, 1, 21), "99213", "F41.1", pcp, allowed=110.0))
    enc.append(
        Encounter(
            sub, D(2026, 2, 4), "90791", "F41.1", psychiatrist, place_of_service="Telehealth", allowed=260.0
        )
    )
    when = D(2026, 2, 11)
    while when <= D(2026, 9, 30):
        status = "pending" if when >= D(2026, 9, 30) else None
        enc.append(
            Encounter(
                sub,
                when,
                "90834",
                "F41.1",
                psych,
                place_of_service="Telehealth",
                allowed=125.0,
                force_status=status,
            )
        )
        when += timedelta(days=14)
    enc.append(
        Encounter(sub, D(2026, 6, 3), "98006", "J06.9", pcp, place_of_service="Telehealth", allowed=95.0)
    )
    enc.append(Encounter(sub, D(2026, 8, 12), "99213", "F41.1", pcp, allowed=110.0))
    pharmacy = next(p for p in d.pharmacies if p["city"] == "Chicago")
    when = D(2026, 2, 5)
    while when <= D(2026, 10, 2):
        fills.append(RxFill(sub, when, drugs["Sertraline"], pharmacy, psychiatrist["npi"]))
        when += timedelta(days=30)


def _david(d, drugs, f, sub, enc, fills, pas):
    pcp, ped, allergy = _prov(d, "1700000012"), _prov(d, "1700000011"), _prov(d, "1700000013")
    urgent_in, urgent_oon = _fac(d, "Front Range Urgent Care"), _fac(d, "Mile High Walk-In Clinic")
    imaging = _fac(d, "Denver Diagnostic Imaging")
    sarah, ethan, lily = "M100007", "M100008", "M100009"
    enc += [
        Encounter(sub, D(2026, 2, 9), "99396", "Z00.00", pcp, allowed=200.0),
        Encounter(
            sub, D(2026, 4, 2), "S9083", "S93.401A", urgent_in, place_of_service="Urgent Care", allowed=165.0
        ),
        Encounter(
            sub, D(2026, 4, 2), "73610", "S93.401A", urgent_in, place_of_service="Urgent Care", allowed=95.0
        ),
        Encounter(sarah, D(2026, 3, 16), "99395", "Z00.00", pcp, allowed=185.0),
        Encounter(
            sarah,
            D(2026, 3, 30),
            "77067",
            "Z12.31",
            imaging,
            place_of_service="Imaging Center",
            allowed=180.0,
        ),
        Encounter(sarah, D(2026, 7, 21), "92014", "H52.4", pcp, category="Specialist visit", allowed=140.0),
        Encounter(ethan, D(2026, 2, 24), "99393", "Z00.129", ped, allowed=175.0),
        Encounter(ethan, D(2026, 5, 12), "95004", "J30.9", allergy, allowed=210.0),
        Encounter(ethan, D(2026, 9, 22), "90686", "Z23", ped, allowed=28.0),
        Encounter(lily, D(2026, 3, 3), "99392", "Z00.129", ped, allowed=175.0),
        Encounter(
            lily, D(2026, 6, 14), "S9083", "J02.0", urgent_in, place_of_service="Urgent Care", allowed=165.0
        ),
        Encounter(
            lily, D(2026, 6, 14), "87880", "J02.0", urgent_in, place_of_service="Urgent Care", allowed=25.0
        ),
        Encounter(
            lily,
            D(2026, 8, 23),
            "S9083",
            "J06.9",
            urgent_oon,
            place_of_service="Urgent Care",
            allowed=165.0,
            billed=210.0,
            note="Mile High Walk-In Clinic is not in the Select EPO network. EPO plans do not cover "
            "non-emergency out-of-network care. Front Range Urgent Care (in-network) is nearby.",
        ),
        Encounter(lily, D(2026, 9, 22), "90686", "Z23", ped, allowed=28.0),
    ]
    pas.append(
        f.prior_auth(
            sub,
            sub,
            pcp,
            "MRI left ankle (requested, then withdrawn after x-ray)",
            "S93.401A",
            D(2026, 4, 3),
            "withdrawn",
            decision_days=1,
        )
    )
    pharmacy = next(p for p in d.pharmacies if p["city"] == "Denver")
    fills.append(
        RxFill(lily, D(2026, 6, 14), drugs["Amoxicillin"], pharmacy, urgent_in["npi"], days_supply=10)
    )
    fills.append(RxFill(ethan, D(2026, 5, 12), drugs["Cetirizine (Rx)"], pharmacy, allergy["npi"]))
    fills.append(RxFill(ethan, D(2026, 5, 12), drugs["Fluticasone nasal spray"], pharmacy, allergy["npi"]))
    fills.append(
        RxFill(ethan, D(2026, 5, 20), drugs["Epinephrine auto-injector (generic)"], pharmacy, allergy["npi"])
    )


SCRIPTS = {
    "maria": _maria,
    "james": _james,
    "priya": _priya,
    "robert": _robert,
    "aisha": _aisha,
    "david": _david,
}
