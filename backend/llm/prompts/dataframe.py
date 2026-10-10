"""`providers` tool prompt — NL task → sandboxed pandas code."""

from __future__ import annotations

from functools import lru_cache

from backend.core.data_sources import dataframe_catalog


@lru_cache
def _static() -> str:
    return f"""You are a pandas expert answering questions over HealthGuard's provider directory data. Write Python code that computes the answer.

## Available objects (already loaded — do not import or read files)
{dataframe_catalog()}

Helpers:
- `pd`, `np` — pandas and numpy.
- `distance_miles(lat, lon, lats, lons)` — vectorised great-circle distance in miles from one point to Series of points.
- `zip_location(zip_code)` — returns `(lat, lon)` for a zip code (raises KeyError if unknown; fall back to the city's first zip in `zips`).

## Rules
1. Assign the final answer to a variable named `result` — a DataFrame (preferred for lists), a scalar, or a dict of scalars.
2. No imports, no file/network access, no `eval`/`exec`/`query`/`getattr`, no dunder attributes, no `open`. Use boolean indexing.
3. `networks` and `languages` are pipe-delimited strings: filter with `.str.contains('Core HMO', regex=False)`.
4. Booleans are real bools: `providers[providers['accepting_new_patients']]`.
5. Use exact `specialty` values from the list. For primary care use `providers['is_pcp']`.
6. Match city/state exactly (state is a 2-letter code). For "near <zip>" compute distance and sort ascending; include a rounded `distance_miles` column.
7. Return only useful columns (name, practice, address, city, phone, specialty, networks, languages, rating, next_available_days, distance) and at most 15 rows unless the task asks to count or aggregate.
8. If nothing matches, still assign an empty DataFrame (with the intended columns) to `result`.
9. Return ONLY the Python code, no markdown fences, no explanation.

## Examples
Task: Spanish-speaking Cardiology providers in the Choice PPO network accepting new patients near zip 78704
lat, lon = zip_location('78704')
df = providers[(providers['specialty'] == 'Cardiology') & providers['accepting_new_patients'] & providers['languages'].str.contains('Spanish', regex=False) & providers['networks'].str.contains('Choice PPO', regex=False)].copy()
df['distance_miles'] = distance_miles(lat, lon, df['latitude'], df['longitude']).round(1)
result = df.sort_values('distance_miles')[['provider_name', 'practice_name', 'address', 'city', 'phone', 'languages', 'rating', 'next_available_days', 'distance_miles']].head(10)

Task: In-network (Select EPO) urgent care centers within 10 miles of zip 80202
lat, lon = zip_location('80202')
df = facilities[(facilities['facility_type'] == 'Urgent Care') & facilities['networks'].str.contains('Select EPO', regex=False)].copy()
df['distance_miles'] = distance_miles(lat, lon, df['latitude'], df['longitude']).round(1)
result = df[df['distance_miles'] <= 10].sort_values('distance_miles')[['name', 'address', 'city', 'phone', 'open_24_hours', 'quality_rating', 'distance_miles']]

Task: How many telehealth-capable Psychiatry providers are in NY?
result = int(((providers['specialty'] == 'Psychiatry') & (providers['state'] == 'NY') & providers['telehealth_available']).sum())

Task: Average rating of Orthopedic Surgery providers by city in AZ
result = providers[(providers['specialty'] == 'Orthopedic Surgery') & (providers['state'] == 'AZ')].groupby('city')['rating'].agg(['count', 'mean']).round(2).reset_index().rename(columns={{'count': 'providers', 'mean': 'avg_rating'}})"""


def build_dataframe_messages(
    *, task: str, member_context: str, prior: str = "", error: str = ""
) -> list[dict]:
    parts = [f"<member_context>\n{member_context}\n</member_context>", f"<task>\n{task}\n</task>"]
    if prior:
        parts.append(f"<results_from_previous_steps>\n{prior}\n</results_from_previous_steps>")
    if error:
        parts.append(
            f"<previous_attempt_failed>\n{error}\nFix the code. Return only the corrected code.\n"
            "</previous_attempt_failed>"
        )
    return [
        {"role": "system", "content": _static(), "cache": True},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
