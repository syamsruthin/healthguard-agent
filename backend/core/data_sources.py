"""
Data Source Configurations
===========================
Each data source is defined here with its schema, path, and system prompt.
To add a new data source, add a new config dict and register it.
"""

from backend.core.config import CSV_PATH, DB_PATH

# ═══════════════════════════════════════════════════════════════════════════
# SQL Data Sources
# ═══════════════════════════════════════════════════════════════════════════

PLAN_BENEFITS_SOURCE = {
    "name": "plan_benefits",
    "type": "sql",
    "db_path": str(DB_PATH),
    "table": "plan_benefits",
    "columns": [
        "plan_id",
        "plan_name",
        "monthly_premium",
        "individual_deductible",
        "specialist_copay",
        "emergency_room_copay",
        "pharmacy_tier_1_copay",
    ],
    "system_prompt": (
        "You are a SQL expert. You have access to a SQLite table `plan_benefits` "
        "with columns: [plan_id, plan_name, monthly_premium, individual_deductible, "
        "specialist_copay, emergency_room_copay, pharmacy_tier_1_copay]. "
        "Output ONLY valid SQLite code. Do not explain. Do not use Markdown blocks."
    ),
}


# ═══════════════════════════════════════════════════════════════════════════
# CSV / DataFrame Data Sources
# ═══════════════════════════════════════════════════════════════════════════

PROVIDERS_SOURCE = {
    "name": "providers",
    "type": "csv",
    "csv_path": str(CSV_PATH),
    "columns": [
        "provider_npi",
        "doctor_name",
        "specialty",
        "city",
        "state",
        "zip_code",
        "network_tier",
        "is_accepting_new_patients",
    ],
    "type_coercions": {
        "is_accepting_new_patients": "bool",
        "zip_code": "str",
    },
    "system_prompt": (
        "You are a Data Analyst. You have a Pandas DataFrame `df` loaded from `providers.csv`. "
        "Columns: [provider_npi, doctor_name, specialty, city, state, zip_code, network_tier, "
        "is_accepting_new_patients]. "
        "IMPORTANT — exact values in the data:\n"
        "  specialty values: 'PCP', 'Cardiologist', 'Dermatologist', 'Orthopedic Surgeon', "
        "'Neurologist', 'Pediatrician', 'Psychiatrist', 'Oncologist', 'Endocrinologist', "
        "'Gastroenterologist', 'Pulmonologist', 'Rheumatologist', 'Ophthalmologist', "
        "'Urologist', 'ENT Specialist'.\n"
        "  city/state examples: ('Austin','TX'), ('Houston','TX'), ('Dallas','TX'), "
        "('New York','NY'), ('Brooklyn','NY'), ('Los Angeles','CA'), ('San Francisco','CA'), "
        "('Seattle','WA'), ('Chicago','IL'), ('Miami','FL'), ('Denver','CO'), "
        "('Phoenix','AZ'), ('Portland','OR'), ('Atlanta','GA'), ('Boston','MA').\n"
        "  network_tier values: 'Tier 1', 'Tier 2'.\n"
        "  is_accepting_new_patients: True or False (boolean).\n"
        "Your task: Generate Python code using `df` to answer the user query. "
        "Output ONLY the python code. The code must produce a variable called `result` "
        "that holds the final answer (a DataFrame or a scalar). "
        "Use exact string matching for city, state, and specialty columns. "
        "When the user says 'PCP' or 'primary care', use specialty == 'PCP'."
    ),
}


# ═══════════════════════════════════════════════════════════════════════════
# Registry — all data sources in one place
# ═══════════════════════════════════════════════════════════════════════════

SQL_SOURCES = {
    "plan_benefits": PLAN_BENEFITS_SOURCE,
    # Add more SQL sources here, e.g.:
    # "claims_history": CLAIMS_HISTORY_SOURCE,
}

CSV_SOURCES = {
    "providers": PROVIDERS_SOURCE,
    # Add more CSV sources here, e.g.:
    # "pharmacies": PHARMACIES_SOURCE,
}

ALL_SOURCES = {**SQL_SOURCES, **CSV_SOURCES}
