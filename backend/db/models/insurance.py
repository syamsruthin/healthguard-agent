"""
Insurance domain tables (schema `insurance`).

Public reference data: plans, plan_benefits, formulary, procedure/diagnosis/
denial codes. Member-scoped data: members, member_accumulators, claims,
pharmacy_claims, prior_authorizations — protected by Postgres Row-Level
Security (see the initial migration) so LLM-generated SQL can only ever see
the authenticated member's (and their dependents') rows.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import INSURANCE_SCHEMA, Base

_S = {"schema": INSURANCE_SCHEMA}
Money = Numeric(12, 2)

MEMBER_SCOPED_TABLES = (
    "members",
    "member_accumulators",
    "claims",
    "pharmacy_claims",
    "prior_authorizations",
)
PUBLIC_TABLES = ("plans", "plan_benefits", "formulary", "procedure_codes", "diagnosis_codes", "denial_codes")


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = _S

    plan_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    plan_name: Mapped[str] = mapped_column(String(100))
    network_name: Mapped[str] = mapped_column(String(40))
    plan_type: Mapped[str] = mapped_column(String(8))
    metal_tier: Mapped[str] = mapped_column(String(16))
    variant: Mapped[str] = mapped_column(String(16))
    plan_year: Mapped[int] = mapped_column(Integer)
    monthly_premium_individual: Mapped[Decimal] = mapped_column(Money)
    monthly_premium_family: Mapped[Decimal] = mapped_column(Money)
    deductible_individual: Mapped[Decimal] = mapped_column(Money)
    deductible_family: Mapped[Decimal] = mapped_column(Money)
    oop_max_individual: Mapped[Decimal] = mapped_column(Money)
    oop_max_family: Mapped[Decimal] = mapped_column(Money)
    coinsurance_pct: Mapped[int] = mapped_column(Integer)
    # Copay columns: NULL means "no copay — deductible + coinsurance applies" (HDHP etc.).
    pcp_copay: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    specialist_copay: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    urgent_care_copay: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    er_copay: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    telehealth_copay: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    inpatient_copay: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    outpatient_surgery_copay: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    imaging_copay: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    lab_copay: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    rx_tier1_copay: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    rx_tier2_copay: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    rx_tier3_copay: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    rx_tier4_coinsurance_pct: Mapped[int] = mapped_column(Integer)
    rx_tier5_coinsurance_pct: Mapped[int] = mapped_column(Integer)
    hsa_eligible: Mapped[bool] = mapped_column(Boolean)
    requires_referral: Mapped[bool] = mapped_column(Boolean)
    out_of_network_covered: Mapped[bool] = mapped_column(Boolean)
    oon_deductible_individual: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    oon_coinsurance_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    states: Mapped[list[str]] = mapped_column(ARRAY(String(2)))
    star_rating: Mapped[Decimal] = mapped_column(Numeric(2, 1))


class PlanBenefit(Base):
    __tablename__ = "plan_benefits"
    __table_args__ = (
        UniqueConstraint("plan_id", "service_category", name="uq_plan_benefits_plan_service"),
        _S,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey(f"{INSURANCE_SCHEMA}.plans.plan_id"), index=True)
    service_category: Mapped[str] = mapped_column(String(64))
    covered: Mapped[bool] = mapped_column(Boolean)
    in_network_cost: Mapped[str] = mapped_column(String(120))
    in_network_copay: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    in_network_coinsurance_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deductible_applies: Mapped[bool] = mapped_column(Boolean)
    out_of_network_cost: Mapped[str] = mapped_column(String(120))
    prior_auth_required: Mapped[bool] = mapped_column(Boolean)
    visit_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    limit_notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Formulary(Base):
    __tablename__ = "formulary"
    __table_args__ = _S

    drug_id: Mapped[str] = mapped_column(String(10), primary_key=True)
    drug_name: Mapped[str] = mapped_column(String(80), index=True)
    generic_name: Mapped[str] = mapped_column(String(80))
    brand_or_generic: Mapped[str] = mapped_column(String(12))
    drug_class: Mapped[str] = mapped_column(String(80))
    condition: Mapped[str] = mapped_column(String(80))
    tier: Mapped[int] = mapped_column(Integer)
    requires_prior_auth: Mapped[bool] = mapped_column(Boolean)
    step_therapy: Mapped[bool] = mapped_column(Boolean)
    quantity_limit: Mapped[str | None] = mapped_column(String(80), nullable=True)
    specialty: Mapped[bool] = mapped_column(Boolean)
    avg_retail_cost_30day: Mapped[Decimal] = mapped_column(Money)
    mail_order_available: Mapped[bool] = mapped_column(Boolean)


class ProcedureCode(Base):
    __tablename__ = "procedure_codes"
    __table_args__ = _S

    cpt_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    description: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(64))
    typical_allowed_amount: Mapped[Decimal] = mapped_column(Money)


class DiagnosisCode(Base):
    __tablename__ = "diagnosis_codes"
    __table_args__ = _S

    icd10_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    description: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(64))


class DenialCode(Base):
    __tablename__ = "denial_codes"
    __table_args__ = _S

    denial_code: Mapped[str] = mapped_column(String(5), primary_key=True)
    reason: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    suggested_action: Mapped[str] = mapped_column(Text)
    appealable: Mapped[bool] = mapped_column(Boolean)


class Member(Base):
    __tablename__ = "members"
    __table_args__ = _S

    member_id: Mapped[str] = mapped_column(String(12), primary_key=True)
    subscriber_id: Mapped[str] = mapped_column(String(12), index=True)
    relationship_to_subscriber: Mapped[str] = mapped_column(String(10))  # self | spouse | child
    first_name: Mapped[str] = mapped_column(String(60))
    last_name: Mapped[str] = mapped_column(String(60))
    date_of_birth: Mapped[date] = mapped_column(Date)
    gender: Mapped[str] = mapped_column(String(1))
    phone: Mapped[str] = mapped_column(String(20))
    address_line: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(60))
    state: Mapped[str] = mapped_column(String(2))
    zip_code: Mapped[str] = mapped_column(String(5))
    plan_id: Mapped[str] = mapped_column(ForeignKey(f"{INSURANCE_SCHEMA}.plans.plan_id"), index=True)
    coverage_level: Mapped[str] = mapped_column(String(12))  # individual | family
    enrollment_date: Mapped[date] = mapped_column(Date)
    termination_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(12))
    pcp_npi: Mapped[str | None] = mapped_column(String(10), nullable=True)
    preferred_language: Mapped[str] = mapped_column(String(20))
    hsa_balance: Mapped[Decimal | None] = mapped_column(Money, nullable=True)


class MemberAccumulator(Base):
    __tablename__ = "member_accumulators"
    __table_args__ = (
        UniqueConstraint("member_id", "plan_year", name="uq_member_accumulators_member_year"),
        _S,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    member_id: Mapped[str] = mapped_column(ForeignKey(f"{INSURANCE_SCHEMA}.members.member_id"), index=True)
    subscriber_id: Mapped[str] = mapped_column(String(12), index=True)
    plan_year: Mapped[int] = mapped_column(Integer)
    deductible_individual_met: Mapped[Decimal] = mapped_column(Money)
    deductible_individual_limit: Mapped[Decimal] = mapped_column(Money)
    oop_individual_met: Mapped[Decimal] = mapped_column(Money)
    oop_individual_limit: Mapped[Decimal] = mapped_column(Money)
    deductible_family_met: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    deductible_family_limit: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    oop_family_met: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    oop_family_limit: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Claim(Base):
    __tablename__ = "claims"
    __table_args__ = _S

    claim_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey(f"{INSURANCE_SCHEMA}.members.member_id"), index=True)
    subscriber_id: Mapped[str] = mapped_column(String(12), index=True)
    claim_type: Mapped[str] = mapped_column(String(16))
    service_date: Mapped[date] = mapped_column(Date, index=True)
    received_date: Mapped[date] = mapped_column(Date)
    processed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    provider_npi: Mapped[str] = mapped_column(String(10))
    provider_name: Mapped[str] = mapped_column(String(120))
    provider_specialty: Mapped[str] = mapped_column(String(60))
    facility_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    in_network: Mapped[bool] = mapped_column(Boolean)
    place_of_service: Mapped[str] = mapped_column(String(32))
    cpt_code: Mapped[str] = mapped_column(String(8))
    procedure_description: Mapped[str] = mapped_column(String(160))
    icd10_code: Mapped[str] = mapped_column(String(10))
    diagnosis_description: Mapped[str] = mapped_column(String(160))
    billed_amount: Mapped[Decimal] = mapped_column(Money)
    allowed_amount: Mapped[Decimal] = mapped_column(Money)
    deductible_applied: Mapped[Decimal] = mapped_column(Money)
    copay_amount: Mapped[Decimal] = mapped_column(Money)
    coinsurance_amount: Mapped[Decimal] = mapped_column(Money)
    member_responsibility: Mapped[Decimal] = mapped_column(Money)
    plan_paid: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(16), index=True)
    denial_code: Mapped[str | None] = mapped_column(
        ForeignKey(f"{INSURANCE_SCHEMA}.denial_codes.denial_code"), nullable=True
    )
    eob_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class PharmacyClaim(Base):
    __tablename__ = "pharmacy_claims"
    __table_args__ = _S

    rx_claim_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey(f"{INSURANCE_SCHEMA}.members.member_id"), index=True)
    subscriber_id: Mapped[str] = mapped_column(String(12), index=True)
    fill_date: Mapped[date] = mapped_column(Date, index=True)
    drug_id: Mapped[str] = mapped_column(ForeignKey(f"{INSURANCE_SCHEMA}.formulary.drug_id"))
    drug_name: Mapped[str] = mapped_column(String(80))
    tier: Mapped[int] = mapped_column(Integer)
    days_supply: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    pharmacy_id: Mapped[str] = mapped_column(String(10))
    pharmacy_name: Mapped[str] = mapped_column(String(120))
    prescriber_npi: Mapped[str] = mapped_column(String(10))
    total_cost: Mapped[Decimal] = mapped_column(Money)
    member_paid: Mapped[Decimal] = mapped_column(Money)
    plan_paid: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(12))  # paid | rejected
    reject_reason: Mapped[str | None] = mapped_column(String(160), nullable=True)


class PriorAuthorization(Base):
    __tablename__ = "prior_authorizations"
    __table_args__ = _S

    pa_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey(f"{INSURANCE_SCHEMA}.members.member_id"), index=True)
    subscriber_id: Mapped[str] = mapped_column(String(12), index=True)
    requesting_provider_npi: Mapped[str] = mapped_column(String(10))
    requesting_provider_name: Mapped[str] = mapped_column(String(120))
    service_description: Mapped[str] = mapped_column(String(160))
    cpt_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    drug_id: Mapped[str | None] = mapped_column(String(10), nullable=True)
    icd10_code: Mapped[str] = mapped_column(String(10))
    urgency: Mapped[str] = mapped_column(String(12))  # standard | expedited
    status: Mapped[str] = mapped_column(String(20))  # approved | denied | pending | partially_approved
    submitted_date: Mapped[date] = mapped_column(Date)
    decision_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_through: Mapped[date | None] = mapped_column(Date, nullable=True)
    units_approved: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
