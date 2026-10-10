"""
`hg-seed` — generate and load the full synthetic dataset.

    hg-seed                 # full scale (~1.3k members, ~20k claims, 5k providers)
    hg-seed --scale small   # CI scale

Steps: generate plans/benefits/formulary/codes → CSV data lake (providers,
facilities, pharmacies, zip centroids) → personas + random population with
an adjudication engine → load Postgres (insurance schema) → render the policy
corpus → upsert demo users → bump the data version (invalidates caches).
Everything is deterministic (seed 42).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import random
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
from faker import Faker

from backend.core.config import settings
from backend.core.security import hash_password
from backend.data_gen.claims_engine import ClaimFactory
from backend.data_gen.corpus import render_corpus
from backend.data_gen.members import Directory, PopulationGenerator
from backend.data_gen.personas import build_personas
from backend.data_gen.plans import generate_plan_benefits, generate_plans, public_plan_row
from backend.data_gen.providers import (
    generate_facilities,
    generate_pharmacies,
    generate_providers,
    generate_zip_centroids,
    scripted_providers,
)
from backend.data_gen.reference import DENIAL_CODES, DIAGNOSES, FORMULARY, PERSONAS, PROCEDURES, SEED

SCALES = {"full": {"subscribers": 900, "providers": 5000}, "small": {"subscribers": 120, "providers": 1200}}

LOAD_ORDER = [
    "plans",
    "plan_benefits",
    "formulary",
    "procedure_codes",
    "diagnosis_codes",
    "denial_codes",
    "members",
    "member_accumulators",
    "claims",
    "pharmacy_claims",
    "prior_authorizations",
]


def build_formulary() -> list[dict[str, Any]]:
    rows = []
    for i, (name, generic, bog, cls, cond, tier, pa, step, ql, spec, cost) in enumerate(FORMULARY, start=1):
        rows.append(
            {
                "drug_id": f"RX{i:05d}",
                "drug_name": name,
                "generic_name": generic,
                "brand_or_generic": bog,
                "drug_class": cls,
                "condition": cond,
                "tier": tier,
                "requires_prior_auth": pa,
                "step_therapy": step,
                "quantity_limit": ql,
                "specialty": spec,
                "avg_retail_cost_30day": cost,
                "mail_order_available": not spec,
            }
        )
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _coerce(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(round(value, 2)))
    return value


async def _load(conn: asyncpg.Connection, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    records = [tuple(_coerce(r[c]) for c in cols) for r in rows]
    await conn.copy_records_to_table(table, records=records, columns=cols, schema_name="insurance")


async def _upsert_users(conn: asyncpg.Connection) -> list[tuple[str, str]]:
    pw_hash = hash_password(settings.demo_password)
    created = []
    accounts = [(p.email, f"{p.first_name} {p.last_name}", "member", p.member_id) for p in PERSONAS]
    accounts.append((settings.admin_email, "HealthGuard Admin", "admin", None))
    for email, name, role, member_id in accounts:
        await conn.execute(
            """
            INSERT INTO app.users (id, email, password_hash, full_name, role, member_id, is_active, memory_enabled,
                                   created_at)
            VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, true, true, now())
            ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role, member_id = EXCLUDED.member_id,
                full_name = EXCLUDED.full_name, password_hash = EXCLUDED.password_hash
            """,
            email,
            pw_hash,
            name,
            role,
            member_id,
        )
        created.append((email, role))
    return created


async def seed(scale: str = "full") -> dict[str, int]:
    t0 = time.monotonic()
    cfg = SCALES[scale]
    rng = random.Random(SEED)
    fake = Faker("en_US")
    fake.seed_instance(SEED)

    plans = generate_plans(rng)
    benefits = generate_plan_benefits(plans)
    formulary = build_formulary()
    procedures = [
        {"cpt_code": k, "description": v[0], "category": v[1], "typical_allowed_amount": v[2]}
        for k, v in PROCEDURES.items()
    ]
    diagnoses = [{"icd10_code": k, "description": v[0], "category": v[1]} for k, v in DIAGNOSES.items()]
    denials = [
        {"denial_code": c, "reason": r, "description": d, "suggested_action": a, "appealable": ap}
        for c, r, d, a, ap in DENIAL_CODES
    ]

    zips = generate_zip_centroids(rng)
    facilities = generate_facilities(rng, fake, zips)
    providers = scripted_providers(fake, zips) + generate_providers(
        rng, fake, zips, facilities, cfg["providers"]
    )
    pharmacies = generate_pharmacies(rng, fake, zips)

    directory = Directory(providers=providers, facilities=facilities, pharmacies=pharmacies)
    gen = PopulationGenerator(rng, fake, plans, directory, formulary, ClaimFactory(rng))
    build_personas(gen)
    gen.generate_random_families(cfg["subscribers"])

    lake = settings.lake_dir
    _write_csv(lake / "providers.csv", providers)
    _write_csv(lake / "facilities.csv", facilities)
    _write_csv(lake / "pharmacies.csv", pharmacies)
    _write_csv(lake / "zip_centroids.csv", zips)

    data = {
        "plans": [public_plan_row(p) for p in plans],
        "plan_benefits": benefits,
        "formulary": formulary,
        "procedure_codes": procedures,
        "diagnosis_codes": diagnoses,
        "denial_codes": denials,
        "members": gen.members,
        "member_accumulators": gen.accumulators,
        "claims": gen.claims,
        "pharmacy_claims": gen.rx_claims,
        "prior_authorizations": gen.prior_auths,
    }
    conn = await asyncpg.connect(settings.sync_database_url)
    try:
        async with conn.transaction():
            await conn.execute(
                "TRUNCATE "
                + ", ".join(f"insurance.{t}" for t in reversed(LOAD_ORDER))
                + " RESTART IDENTITY CASCADE"
            )
            for table in LOAD_ORDER:
                await _load(conn, table, data[table])
            users = await _upsert_users(conn)
            await conn.execute(
                "INSERT INTO rag.data_version (id, version, updated_at) VALUES (1, 1, now()) "
                "ON CONFLICT (id) DO UPDATE SET version = rag.data_version.version + 1, updated_at = now()"
            )
        await conn.execute("ANALYZE")
    finally:
        await conn.close()

    n_docs = render_corpus(settings.corpus_dir, plans, benefits, formulary)
    stats = {k: len(v) for k, v in data.items()}
    stats.update(
        providers=len(providers),
        facilities=len(facilities),
        pharmacies=len(pharmacies),
        zip_centroids=len(zips),
        corpus_documents=n_docs,
        demo_users=len(users),
    )
    print(f"✅ Seed complete ({scale}) in {time.monotonic() - t0:.1f}s")
    for key, value in stats.items():
        print(f"   {key:<22} {value:>7,}")
    print(f"   Demo logins (password from DEMO_PASSWORD): {', '.join(e for e, _ in users)}")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and load HealthGuard synthetic data")
    parser.add_argument("--scale", choices=sorted(SCALES), default="full")
    args = parser.parse_args()
    asyncio.run(seed(args.scale))


if __name__ == "__main__":
    main()
