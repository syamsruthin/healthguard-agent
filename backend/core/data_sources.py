"""
Data-source catalog — the single description of every queryable source.

It feeds the *stable* (prompt-cached) prefix of the understand / SQL /
dataframe prompts, and the SQL guard's table allow-list. Adding a new table
or CSV is a matter of describing it here (the execution engines are
data-source agnostic).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.data_gen.reference import (
    AS_OF,
    DENIAL_CODES,
    NETWORK_STATES,
    PLAN_YEAR,
    SERVICE_CATEGORIES,
    SPECIALTIES,
)


@dataclass(frozen=True)
class TableDoc:
    name: str
    description: str
    columns: dict[str, str]
    member_scoped: bool = False
    notes: list[str] = field(default_factory=list)


SQL_TABLES: list[TableDoc] = [
    TableDoc(
        "plans",
        f"All {PLAN_YEAR} HealthGuard medical plans (public). One row per plan.",
        {
            "plan_id": "text PK, pattern TYPE-METAL-VARIANT e.g. 'PPO-SLV-PLS', 'HDHP-BRZ-VAL', 'HMO-CAT-STD'",
            "plan_name": "text e.g. 'Choice PPO Silver Plus', 'Saver HDHP Bronze Value'",
            "network_name": "text: 'Choice PPO' | 'Core HMO' | 'Select EPO' (Saver HDHP plans use 'Choice PPO')",
            "plan_type": "text: 'PPO' | 'HMO' | 'EPO' | 'HDHP'",
            "metal_tier": "text: 'Catastrophic' | 'Bronze' | 'Silver' | 'Gold' | 'Platinum'",
            "variant": "text: 'Value' | 'Standard' | 'Plus'",
            "plan_year": "int (2026)",
            "monthly_premium_individual / monthly_premium_family": "numeric USD per month",
            "deductible_individual / deductible_family": "numeric USD per plan year",
            "oop_max_individual / oop_max_family": "numeric USD out-of-pocket maximum",
            "coinsurance_pct": "int, member share after deductible (e.g. 30 = 30%)",
            "pcp_copay, specialist_copay, urgent_care_copay, er_copay, telehealth_copay, lab_copay": (
                "numeric USD copay; NULL = no copay (deductible + coinsurance applies, e.g. HDHP)"
            ),
            "imaging_copay, outpatient_surgery_copay, inpatient_copay": (
                "numeric USD copay; NULL = deductible + coinsurance applies"
            ),
            "rx_tier1_copay, rx_tier2_copay, rx_tier3_copay": "numeric USD; NULL = deductible applies",
            "rx_tier4_coinsurance_pct, rx_tier5_coinsurance_pct": "int percent",
            "hsa_eligible": "bool (true only for Saver HDHP plans)",
            "requires_referral": "bool (true for Core HMO)",
            "out_of_network_covered": "bool (true for Choice PPO and Saver HDHP)",
            "oon_deductible_individual": "numeric or NULL",
            "oon_coinsurance_pct": "int or NULL",
            "states": "text[] of 2-letter state codes where the plan is sold — filter with 'TX' = ANY(states)",
            "star_rating": "numeric quality rating 1-5",
        },
    ),
    TableDoc(
        "plan_benefits",
        "Per-plan cost sharing for ~40 service categories (public). One row per (plan_id, service_category).",
        {
            "plan_id": "FK plans.plan_id",
            "service_category": "text — exact values listed below",
            "covered": "bool",
            "in_network_cost": "text summary e.g. '$35 copay', '30% coinsurance after deductible', 'No charge'",
            "in_network_copay": "numeric or NULL",
            "in_network_coinsurance_pct": "int or NULL",
            "deductible_applies": "bool",
            "out_of_network_cost": "text",
            "prior_auth_required": "bool",
            "visit_limit": "int or NULL (per year)",
            "limit_notes": "text",
        },
    ),
    TableDoc(
        "formulary",
        "Covered prescription drugs (public).",
        {
            "drug_id": "text PK 'RX00001'",
            "drug_name": "text brand or generic name e.g. 'Ozempic', 'Atorvastatin'",
            "generic_name": "text",
            "brand_or_generic": "'brand' | 'generic' | 'biosimilar'",
            "drug_class": "text e.g. 'GLP-1 receptor agonist', 'Statin'",
            "condition": "text treated condition",
            "tier": "int 1-5",
            "requires_prior_auth": "bool",
            "step_therapy": "bool",
            "quantity_limit": "text or NULL",
            "specialty": "bool",
            "avg_retail_cost_30day": "numeric USD",
            "mail_order_available": "bool",
        },
    ),
    TableDoc(
        "procedure_codes",
        "CPT/HCPCS reference (public).",
        {
            "cpt_code": "text PK",
            "description": "text",
            "category": "text",
            "typical_allowed_amount": "numeric",
        },
    ),
    TableDoc(
        "diagnosis_codes",
        "ICD-10 reference (public).",
        {"icd10_code": "text PK", "description": "text", "category": "text"},
    ),
    TableDoc(
        "denial_codes",
        "Claim denial reason codes D001-D012 (public).",
        {
            "denial_code": "text PK",
            "reason": "text",
            "description": "text",
            "suggested_action": "text",
            "appealable": "bool",
        },
    ),
    TableDoc(
        "members",
        "Member profiles (MEMBER-SCOPED by row-level security).",
        {
            "member_id": "text PK e.g. 'M100001'",
            "subscriber_id": "text (the policy holder's member_id)",
            "relationship_to_subscriber": "'self' | 'spouse' | 'child'",
            "first_name, last_name": "text",
            "date_of_birth": "date",
            "gender": "'F' | 'M'",
            "phone, address_line, city": "text",
            "state": "2-letter",
            "zip_code": "text",
            "plan_id": "FK plans.plan_id",
            "coverage_level": "'individual' | 'family'",
            "enrollment_date": "date",
            "termination_date": "date|NULL",
            "status": "'active' | 'terminated'",
            "pcp_npi": "text NPI of assigned PCP (provider directory)",
            "preferred_language": "text",
            "hsa_balance": "numeric USD or NULL (HDHP members)",
        },
        member_scoped=True,
    ),
    TableDoc(
        "member_accumulators",
        "Deductible / out-of-pocket progress for the plan year (MEMBER-SCOPED). One row per member per year.",
        {
            "member_id, subscriber_id": "text",
            "plan_year": "int",
            "deductible_individual_met, deductible_individual_limit": "numeric USD",
            "oop_individual_met, oop_individual_limit": "numeric USD",
            "deductible_family_met, deductible_family_limit, oop_family_met, oop_family_limit": (
                "numeric USD, NULL for individual coverage"
            ),
        },
        member_scoped=True,
    ),
    TableDoc(
        "claims",
        "Medical claims / EOB lines (MEMBER-SCOPED).",
        {
            "claim_id": "text PK 'CLM-2026-0000001'",
            "member_id, subscriber_id": "text",
            "claim_type": "'professional' | 'institutional'",
            "service_date, received_date": "date",
            "processed_date": "date|NULL",
            "provider_npi, provider_name, provider_specialty": "text",
            "facility_name": "text|NULL",
            "in_network": "bool",
            "place_of_service": "text e.g. 'Office','Telehealth','Urgent Care','Emergency Room','Imaging Center'",
            "cpt_code, procedure_description, icd10_code, diagnosis_description": "text",
            "billed_amount, allowed_amount": "numeric",
            "deductible_applied, copay_amount, coinsurance_amount": "numeric",
            "member_responsibility": "numeric (what the member owes)",
            "plan_paid": "numeric",
            "status": "'paid' | 'denied' | 'pending' | 'in_review' | 'appealed'",
            "denial_code": "text FK denial_codes or NULL",
            "eob_note": "text explanation or NULL",
        },
        member_scoped=True,
    ),
    TableDoc(
        "pharmacy_claims",
        "Prescription fills (MEMBER-SCOPED).",
        {
            "rx_claim_id": "text PK",
            "member_id, subscriber_id": "text",
            "fill_date": "date",
            "drug_id": "FK formulary",
            "drug_name": "text",
            "tier, days_supply, quantity": "int",
            "pharmacy_id, pharmacy_name, prescriber_npi": "text",
            "total_cost, member_paid, plan_paid": "numeric",
            "status": "'paid' | 'rejected'",
            "reject_reason": "text|NULL",
        },
        member_scoped=True,
    ),
    TableDoc(
        "prior_authorizations",
        "Prior authorization requests (MEMBER-SCOPED).",
        {
            "pa_id": "text PK",
            "member_id, subscriber_id": "text",
            "requesting_provider_npi, requesting_provider_name": "text",
            "service_description": "text",
            "cpt_code, drug_id": "text|NULL",
            "icd10_code": "text",
            "urgency": "'standard' | 'expedited'",
            "status": "'approved' | 'denied' | 'pending' | 'partially_approved' | 'withdrawn'",
            "submitted_date": "date",
            "decision_date, valid_through": "date|NULL",
            "units_approved": "int|NULL",
            "decision_reason": "text|NULL",
        },
        member_scoped=True,
    ),
]

SQL_TABLE_NAMES = frozenset(t.name for t in SQL_TABLES)
MEMBER_SCOPED_SQL_TABLES = frozenset(t.name for t in SQL_TABLES if t.member_scoped)

DATAFRAMES: dict[str, tuple[str, dict[str, str]]] = {
    "providers": (
        "Provider directory (~5,000 clinicians). One row per provider.",
        {
            "npi": "str",
            "provider_name": "str e.g. 'Elena Ramirez, MD'",
            "first_name, last_name": "str",
            "credentials": "str MD|DO|NP|PA-C|PhD|PsyD|LCSW|DPT|DC|OD|DPM|CNM|PMHNP",
            "gender": "'F'|'M'",
            "specialty": "str (exact values below)",
            "is_pcp": "bool (Family Medicine, Internal Medicine, Pediatrics)",
            "practice_name, address, city": "str",
            "state": "2-letter str",
            "zip_code": "str",
            "latitude, longitude": "float",
            "phone": "str",
            "languages": "pipe-delimited str e.g. 'English|Spanish'",
            "accepting_new_patients": "bool",
            "networks": "pipe-delimited str subset of 'Choice PPO|Core HMO|Select EPO'",
            "network_tier": "'Tier 1' | 'Tier 2'",
            "board_certified": "bool",
            "years_experience": "int",
            "rating": "float 2.5-5.0",
            "review_count": "int",
            "telehealth_available": "bool",
            "hospital_affiliation": "str (may be empty)",
            "next_available_days": "int days to next new-patient appointment",
        },
    ),
    "facilities": (
        "Facilities (~440): hospitals, urgent care, imaging, labs, surgery centers, SNFs.",
        {
            "facility_id, npi, name": "str",
            "facility_type": (
                "'Hospital'|'Urgent Care'|'Imaging Center'|'Laboratory'|'Ambulatory Surgery Center'|"
                "'Skilled Nursing Facility'"
            ),
            "address, city, state, zip_code, phone": "str",
            "latitude, longitude": "float",
            "networks": "pipe-delimited str",
            "has_emergency_room": "bool",
            "trauma_level": "str|NaN",
            "open_24_hours": "bool",
            "quality_rating": "float",
            "cost_tier": "'$'|'$$'|'$$$'",
            "accepts_walk_ins": "bool",
        },
    ),
    "pharmacies": (
        "Pharmacies (~430) incl. HealthGuard Home Delivery (mail order) and Specialty pharmacy.",
        {
            "pharmacy_id, name, chain": "str",
            "pharmacy_type": "'Retail'|'Mail Order'|'Specialty'",
            "address, city, state, zip_code, phone": "str",
            "latitude, longitude": "float",
            "network_status": "'Preferred'|'Standard'",
            "open_24_hours, drive_through, delivery_available, vaccinations_offered, compounding": "bool",
        },
    ),
    "zips": (
        "Zip-code centroids for distance search.",
        {"zip_code, city, state": "str", "latitude, longitude": "float"},
    ),
}

POLICY_DOC_CATEGORIES = {
    "plans": "plan types, networks, metal tiers, variants",
    "coverage": "exclusions & limitations (cosmetic, bariatric, IVF, experimental, dental/vision)",
    "claims": "claims filing, timelines, EOBs, denial codes, coordination of benefits",
    "appeals": "internal appeals, external review (IRO), grievances, timelines",
    "utilization_management": "prior authorization list & timelines, step therapy",
    "benefits": (
        "preventive care, mental health, maternity, telehealth, emergency/urgent care & No Surprises Act, "
        "therapies/DME/home health"
    ),
    "pharmacy": "drug tiers, mail order, GLP-1 rules, insulin cap, exceptions",
    "networks": "in/out-of-network, referrals, balance billing, continuity of care",
    "enrollment": "open enrollment, special enrollment, grace periods, COBRA",
    "financial": "HSA / HDHP rules and 2026 IRS limits",
    "rights": "member rights & responsibilities, contacts",
    "privacy": "HIPAA notice",
    "glossary": "insurance terms",
    "faq": "frequently asked questions",
    "sbc": "per-plan Summary of Benefits and Coverage (filterable by plan_id)",
}


def _cols(cols: dict[str, str]) -> str:
    return "\n".join(f"    - {k}: {v}" for k, v in cols.items())


def sql_catalog() -> str:
    parts = []
    for t in SQL_TABLES:
        scope = " [MEMBER-SCOPED]" if t.member_scoped else ""
        parts.append(f"  insurance.{t.name}{scope} — {t.description}\n{_cols(t.columns)}")
    categories = "; ".join(f"'{c.name}'" for c in SERVICE_CATEGORIES)
    denials = "; ".join(f"{c} = {r}" for c, r, *_ in DENIAL_CODES)
    networks = "; ".join(f"{n}: {', '.join(s)}" for n, s in NETWORK_STATES.items())
    return (
        "\n\n".join(parts)
        + f"\n\nplan_benefits.service_category exact values: {categories}"
        + f"\n\nDenial codes: {denials}"
        + f"\n\nNetwork availability by state: {networks}"
    )


def dataframe_catalog() -> str:
    parts = [f"  `{name}` — {desc}\n{_cols(cols)}" for name, (desc, cols) in DATAFRAMES.items()]
    specialties = ", ".join(f"'{s}'" for s in SPECIALTIES)
    return "\n\n".join(parts) + f"\n\nproviders.specialty exact values: {specialties}"


def data_overview() -> str:
    """Compact overview for the understand/planner prompt."""
    member_tables = ", ".join(sorted(MEMBER_SCOPED_SQL_TABLES))
    public_tables = ", ".join(sorted(SQL_TABLE_NAMES - MEMBER_SCOPED_SQL_TABLES))
    docs = "\n".join(f"    - {k}: {v}" for k, v in POLICY_DOC_CATEGORIES.items())
    return (
        f"Plan year {PLAN_YEAR}; claims data current through {AS_OF.isoformat()}.\n"
        f"- sql (Postgres): public tables [{public_tables}]; member-scoped tables [{member_tables}] "
        "(row-level security limits these to the logged-in member and their dependents; admins see all).\n"
        "- providers (pandas over CSV): providers (specialty, location/geo, languages, networks, accepting new "
        "patients, telehealth, ratings, next availability), facilities (hospitals, urgent care, imaging, labs), "
        "pharmacies (retail, 24h, mail order, specialty), zip centroids for 'near me' distance search.\n"
        f"- policy_search (hybrid vector + keyword search over policy documents):\n{docs}"
    )
