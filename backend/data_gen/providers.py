"""CSV data lake: providers, facilities, pharmacies and zip centroids (deterministic)."""

from __future__ import annotations

import math
import random
from typing import Any

from faker import Faker

from backend.data_gen.reference import (
    CITIES,
    CITY_WEIGHTS,
    FACILITY_TYPES,
    LANGUAGES,
    NETWORK_STATES,
    PHARMACY_CHAINS,
    SPANISH_HEAVY_STATES,
    SPECIALTIES,
)

MILES_PER_DEG_LAT = 69.0


def _jitter(rng: random.Random, lat: float, lon: float, max_miles: float) -> tuple[float, float]:
    r = max_miles * math.sqrt(rng.random())
    theta = rng.uniform(0, 2 * math.pi)
    dlat = (r * math.cos(theta)) / MILES_PER_DEG_LAT
    dlon = (r * math.sin(theta)) / (MILES_PER_DEG_LAT * math.cos(math.radians(lat)))
    return round(lat + dlat, 5), round(lon + dlon, 5)


def _phone(rng: random.Random) -> str:
    return f"({rng.randint(201, 989)}) 555-{rng.randint(1000, 9999)}"


def generate_zip_centroids(rng: random.Random) -> list[dict[str, Any]]:
    rows = []
    for city, state, lat, lon, base_zip in CITIES:
        for i in range(6):
            zlat, zlon = (lat, lon) if i == 0 else _jitter(rng, lat, lon, 6)
            rows.append(
                {
                    "zip_code": f"{base_zip + i:05d}",
                    "city": city,
                    "state": state,
                    "latitude": zlat,
                    "longitude": zlon,
                }
            )
    # Persona zips that are not base+offset
    extra = [
        ("78704", "Austin", "TX", 30.2449, -97.7680),
        ("98101", "Seattle", "WA", 47.6101, -122.3364),
        ("11201", "Brooklyn", "NY", 40.6943, -73.9903),
        ("33128", "Miami", "FL", 25.7752, -80.1977),
        ("60601", "Chicago", "IL", 41.8858, -87.6229),
        ("80202", "Denver", "CO", 39.7525, -104.9995),
    ]
    have = {r["zip_code"] for r in rows}
    for z, c, s, la, lo in extra:
        if z not in have:
            rows.append({"zip_code": z, "city": c, "state": s, "latitude": la, "longitude": lo})
    return rows


def _city_zips(zips: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for z in zips:
        out.setdefault(z["city"], []).append(z)
    return out


def _networks_for(rng: random.Random, state: str) -> list[str]:
    nets = []
    if rng.random() < 0.82:
        nets.append("Choice PPO")
    if state in NETWORK_STATES["Core HMO"] and rng.random() < 0.55:
        nets.append("Core HMO")
    if state in NETWORK_STATES["Select EPO"] and rng.random() < 0.5:
        nets.append("Select EPO")
    if not nets:
        nets.append("Choice PPO")
    return nets


def _languages(rng: random.Random, state: str) -> list[str]:
    langs = ["English"]
    if rng.random() < (0.42 if state in SPANISH_HEAVY_STATES else 0.15):
        langs.append("Spanish")
    for lang in LANGUAGES[1:]:
        if rng.random() < 0.035:
            langs.append(lang)
    return langs


def generate_facilities(rng: random.Random, fake: Faker, zips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_city = _city_zips(zips)
    rows: list[dict[str, Any]] = []
    counts = {
        "Hospital": (2, 4),
        "Urgent Care": (2, 4),
        "Imaging Center": (1, 2),
        "Laboratory": (1, 2),
        "Ambulatory Surgery Center": (1, 2),
        "Skilled Nursing Facility": (0, 1),
    }
    seq = 0
    for city, state, _lat, _lon, _ in CITIES:
        for ftype, (lo, hi) in counts.items():
            n = rng.randint(lo, hi)
            for _i in range(n):
                seq += 1
                suffix = rng.choice(FACILITY_TYPES[ftype])
                prefix = rng.choice(
                    [
                        city,
                        f"{fake.last_name()}",
                        f"St. {fake.first_name()}'s",
                        f"{city} Valley",
                        f"North {city}",
                        f"{fake.last_name()} Family",
                    ]
                )
                z = rng.choice(by_city[city])
                flat, flon = _jitter(rng, z["latitude"], z["longitude"], 2.5)
                is_hosp = ftype == "Hospital"
                rows.append(
                    {
                        "facility_id": f"F{seq:05d}",
                        "npi": str(1_500_000_000 + seq * 7919 % 400_000_000),
                        "name": f"{prefix} {suffix}",
                        "facility_type": ftype,
                        "address": fake.street_address(),
                        "city": city,
                        "state": state,
                        "zip_code": z["zip_code"],
                        "latitude": flat,
                        "longitude": flon,
                        "phone": _phone(rng),
                        "networks": "|".join(_networks_for(rng, state)),
                        "has_emergency_room": is_hosp,
                        "trauma_level": rng.choice(["I", "II", "III", None]) if is_hosp else None,
                        "open_24_hours": is_hosp or (ftype == "Urgent Care" and rng.random() < 0.2),
                        "quality_rating": round(rng.choice([2.5, 3, 3.5, 4, 4, 4.5, 5]), 1),
                        "cost_tier": rng.choice(["$", "$$", "$$", "$$$"]),
                        "accepts_walk_ins": ftype in ("Urgent Care", "Laboratory"),
                    }
                )
    # Scripted facilities used by persona stories.
    scripted = [
        ("Austin Imaging Center", "Imaging Center", "Austin", "TX", "78704", "Choice PPO|Core HMO"),
        ("Bayfront Regional Medical Center", "Hospital", "Miami", "FL", "33128", "Select EPO"),
        ("Mile High Walk-In Clinic", "Urgent Care", "Denver", "CO", "80202", "Choice PPO"),
        ("Front Range Urgent Care", "Urgent Care", "Denver", "CO", "80203", "Choice PPO|Select EPO|Core HMO"),
        ("Puget Sound Medical Center", "Hospital", "Seattle", "WA", "98101", "Choice PPO|Core HMO"),
        ("Brooklyn Heights Women's Hospital", "Hospital", "Brooklyn", "NY", "11201", "Choice PPO|Core HMO"),
        ("Denver Diagnostic Imaging", "Imaging Center", "Denver", "CO", "80202", "Choice PPO|Select EPO"),
    ]
    for name, ftype, city, state, zip_code, nets in scripted:
        seq += 1
        z = next((r for r in zips if r["zip_code"] == zip_code), by_city[city][0])
        flat, flon = _jitter(rng, z["latitude"], z["longitude"], 0.8)
        rows.append(
            {
                "facility_id": f"F{seq:05d}",
                "npi": str(1_900_000_000 + seq),
                "name": name,
                "facility_type": ftype,
                "address": fake.street_address(),
                "city": city,
                "state": state,
                "zip_code": zip_code,
                "latitude": flat,
                "longitude": flon,
                "phone": _phone(rng),
                "networks": nets,
                "has_emergency_room": ftype == "Hospital",
                "trauma_level": "II" if ftype == "Hospital" else None,
                "open_24_hours": ftype == "Hospital",
                "quality_rating": 4.5,
                "cost_tier": "$$",
                "accepts_walk_ins": ftype == "Urgent Care",
            }
        )
    return rows


def generate_providers(
    rng: random.Random, fake: Faker, zips: list[dict[str, Any]], facilities: list[dict[str, Any]], n: int
) -> list[dict[str, Any]]:
    by_city = _city_zips(zips)
    hospitals: dict[str, list[str]] = {}
    for f in facilities:
        if f["facility_type"] == "Hospital":
            hospitals.setdefault(f["city"], []).append(f["name"])
    specialties = list(SPECIALTIES)
    weights = [SPECIALTIES[s][0] for s in specialties]
    cities = CITIES
    city_w = [CITY_WEIGHTS.get(c[0], 1.5) for c in cities]
    rows: list[dict[str, Any]] = []
    used_npis: set[str] = set()
    for _ in range(n):
        specialty = rng.choices(specialties, weights)[0]
        _, is_pcp, creds = SPECIALTIES[specialty]
        cred = rng.choice(creds)
        city, state, *_rest = rng.choices(cities, city_w)[0]
        gender = (
            rng.choice(["F", "M"]) if specialty != "Obstetrics & Gynecology" else rng.choice(["F", "F", "M"])
        )
        first = fake.first_name_female() if gender == "F" else fake.first_name_male()
        last = fake.last_name()
        npi = str(rng.randint(1_000_000_000, 1_499_999_999))
        while npi in used_npis:
            npi = str(rng.randint(1_000_000_000, 1_499_999_999))
        used_npis.add(npi)
        z = rng.choice(by_city[city])
        plat, plon = _jitter(rng, z["latitude"], z["longitude"], 4)
        practice_suffix = rng.choice(
            [
                "Associates",
                "Medical Group",
                "Clinic",
                "Health Partners",
                "Specialists",
                "Family Practice",
                "Care Center",
            ]
        )
        rows.append(
            {
                "npi": npi,
                "provider_name": f"{first} {last}, {cred}",
                "first_name": first,
                "last_name": last,
                "credentials": cred,
                "gender": gender,
                "specialty": specialty,
                "is_pcp": is_pcp,
                "practice_name": f"{rng.choice([city, last, fake.last_name()])} {specialty.split(' (')[0]} {practice_suffix}",
                "address": fake.street_address(),
                "city": city,
                "state": state,
                "zip_code": z["zip_code"],
                "latitude": plat,
                "longitude": plon,
                "phone": _phone(rng),
                "languages": "|".join(_languages(rng, state)),
                "accepting_new_patients": rng.random() < 0.68,
                "networks": "|".join(_networks_for(rng, state)),
                "network_tier": "Tier 1" if rng.random() < 0.6 else "Tier 2",
                "board_certified": cred in ("MD", "DO") and rng.random() < 0.9,
                "years_experience": rng.randint(2, 38),
                "rating": round(min(5.0, max(2.5, rng.gauss(4.35, 0.4))), 1),
                "review_count": int(rng.expovariate(1 / 90)) + 3,
                "telehealth_available": rng.random()
                < (0.85 if specialty in ("Psychiatry", "Psychology", "Clinical Social Work") else 0.45),
                "hospital_affiliation": rng.choice(hospitals.get(city, [""]) or [""])
                if cred in ("MD", "DO")
                else "",
                "next_available_days": int(rng.expovariate(1 / 12)) + 1,
            }
        )
    return rows


def scripted_providers(fake: Faker, zips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Named providers referenced by persona claims (stable NPIs)."""
    zmap = {z["zip_code"]: z for z in zips}

    def p(
        npi: str,
        first: str,
        last: str,
        cred: str,
        gender: str,
        specialty: str,
        is_pcp: bool,
        zip_code: str,
        practice: str,
        networks: str,
        langs: str = "English",
        accepting: bool = True,
        tele: bool = True,
        rating: float = 4.7,
    ) -> dict[str, Any]:
        z = zmap[zip_code]
        return {
            "npi": npi,
            "provider_name": f"{first} {last}, {cred}",
            "first_name": first,
            "last_name": last,
            "credentials": cred,
            "gender": gender,
            "specialty": specialty,
            "is_pcp": is_pcp,
            "practice_name": practice,
            "address": fake.street_address(),
            "city": z["city"],
            "state": z["state"],
            "zip_code": zip_code,
            "latitude": z["latitude"] + 0.003,
            "longitude": z["longitude"] - 0.002,
            "phone": "(512) 555-0142",
            "languages": langs,
            "accepting_new_patients": accepting,
            "networks": networks,
            "network_tier": "Tier 1",
            "board_certified": cred in ("MD", "DO"),
            "years_experience": 14,
            "rating": rating,
            "review_count": 212,
            "telehealth_available": tele,
            "hospital_affiliation": "",
            "next_available_days": 6,
        }

    return [
        p(
            "1700000001",
            "Elena",
            "Ramirez",
            "MD",
            "F",
            "Family Medicine",
            True,
            "78704",
            "South Austin Family Medicine",
            "Choice PPO|Core HMO|Select EPO",
            "English|Spanish",
        ),
        p(
            "1700000002",
            "Marcus",
            "Whitfield",
            "MD",
            "M",
            "Orthopedic Surgery",
            False,
            "78704",
            "Lone Star Orthopedics",
            "Choice PPO|Select EPO",
        ),
        p(
            "1700000003",
            "Hannah",
            "Cole",
            "DPT",
            "F",
            "Physical Therapy",
            False,
            "78704",
            "Barton Creek Physical Therapy",
            "Choice PPO|Core HMO",
            "English|Spanish",
            tele=False,
        ),
        p(
            "1700000004",
            "Wei",
            "Zhang",
            "MD",
            "M",
            "Endocrinology",
            False,
            "98101",
            "Puget Sound Endocrine Clinic",
            "Choice PPO|Core HMO",
            "English|Mandarin",
        ),
        p(
            "1700000005",
            "Laura",
            "Bennett",
            "MD",
            "F",
            "Internal Medicine",
            True,
            "98101",
            "Pike Place Internal Medicine",
            "Choice PPO|Core HMO",
        ),
        p(
            "1700000006",
            "Noah",
            "Feldman",
            "MD",
            "M",
            "Dermatology",
            False,
            "98101",
            "Emerald City Dermatology",
            "Choice PPO|Core HMO",
        ),
        p(
            "1700000007",
            "Anjali",
            "Rao",
            "MD",
            "F",
            "Obstetrics & Gynecology",
            False,
            "11201",
            "Brooklyn Heights OB/GYN",
            "Choice PPO|Core HMO",
            "English|Hindi",
        ),
        p(
            "1700000008",
            "Gabriel",
            "Santos",
            "MD",
            "M",
            "Cardiology",
            False,
            "33128",
            "Biscayne Heart Institute",
            "Choice PPO|Select EPO",
            "English|Spanish|Portuguese",
        ),
        p(
            "1700000009",
            "Renee",
            "Okoye",
            "PsyD",
            "F",
            "Psychology",
            False,
            "60601",
            "Lakeshore Counseling Group",
            "Choice PPO|Core HMO",
        ),
        p(
            "1700000010",
            "Samuel",
            "Park",
            "MD",
            "M",
            "Psychiatry",
            False,
            "60601",
            "Loop Psychiatric Associates",
            "Choice PPO|Core HMO",
            "English|Korean",
        ),
        p(
            "1700000011",
            "Grace",
            "Lee",
            "MD",
            "F",
            "Pediatrics",
            True,
            "80202",
            "Union Station Pediatrics",
            "Choice PPO|Select EPO",
            "English|Korean",
        ),
        p(
            "1700000012",
            "Thomas",
            "Hughes",
            "MD",
            "M",
            "Family Medicine",
            True,
            "80202",
            "LoDo Family Health",
            "Choice PPO|Select EPO",
        ),
        p(
            "1700000013",
            "Priyanka",
            "Desai",
            "MD",
            "F",
            "Allergy & Immunology",
            False,
            "80202",
            "Denver Allergy & Asthma",
            "Choice PPO|Select EPO",
        ),
        p(
            "1700000014",
            "Monica",
            "Alvarez",
            "MD",
            "F",
            "Family Medicine",
            True,
            "33128",
            "Little Havana Primary Care",
            "Choice PPO|Select EPO|Core HMO",
            "English|Spanish",
        ),
        p(
            "1700000015",
            "Daniel",
            "Brooks",
            "MD",
            "M",
            "Family Medicine",
            True,
            "60601",
            "Millennium Park Primary Care",
            "Choice PPO|Core HMO",
        ),
        p(
            "1700000016",
            "Olivia",
            "Grant",
            "MD",
            "F",
            "Family Medicine",
            True,
            "11201",
            "Cobble Hill Family Practice",
            "Choice PPO|Core HMO",
        ),
    ]


def generate_pharmacies(rng: random.Random, fake: Faker, zips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_city = _city_zips(zips)
    rows: list[dict[str, Any]] = []
    seq = 0
    for city, state, *_ in CITIES:
        n = rng.randint(10, 18) if CITY_WEIGHTS.get(city) else rng.randint(7, 12)
        for _ in range(n):
            seq += 1
            chain = rng.choice(PHARMACY_CHAINS)
            z = rng.choice(by_city[city])
            plat, plon = _jitter(rng, z["latitude"], z["longitude"], 4)
            independent = chain in ("Main Street Pharmacy", "Neighborhood Apothecary")
            rows.append(
                {
                    "pharmacy_id": f"P{seq:05d}",
                    "name": f"{chain} #{rng.randint(100, 9999)}"
                    if not independent
                    else f"{fake.last_name()} {chain}",
                    "chain": chain if not independent else "Independent",
                    "pharmacy_type": "Retail",
                    "address": fake.street_address(),
                    "city": city,
                    "state": state,
                    "zip_code": z["zip_code"],
                    "latitude": plat,
                    "longitude": plon,
                    "phone": _phone(rng),
                    "network_status": "Preferred" if rng.random() < 0.55 else "Standard",
                    "open_24_hours": rng.random() < 0.12,
                    "drive_through": rng.random() < 0.4,
                    "delivery_available": rng.random() < 0.3,
                    "vaccinations_offered": rng.random() < 0.8,
                    "compounding": rng.random() < 0.08,
                }
            )
    for name, ptype in (
        ("HealthGuard Home Delivery Pharmacy", "Mail Order"),
        ("HealthGuard Specialty Pharmacy", "Specialty"),
    ):
        seq += 1
        rows.append(
            {
                "pharmacy_id": f"P{seq:05d}",
                "name": name,
                "chain": "HealthGuard",
                "pharmacy_type": ptype,
                "address": "500 Commerce Way",
                "city": "Columbus",
                "state": "OH",
                "zip_code": "43215",
                "latitude": 39.9612,
                "longitude": -82.9988,
                "phone": "1-800-447-4276",
                "network_status": "Preferred",
                "open_24_hours": True,
                "drive_through": False,
                "delivery_available": True,
                "vaccinations_offered": False,
                "compounding": ptype == "Specialty",
            }
        )
    return rows
