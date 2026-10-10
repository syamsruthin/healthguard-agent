"""`verify` prompt — output guardrail: groundedness + completeness in one call."""

from __future__ import annotations

import json
from functools import lru_cache

from backend.llm.prompts.schemas import Verification


@lru_cache
def _static() -> str:
    schema = json.dumps(Verification.model_json_schema())
    return f"""You are the answer verifier for a health-insurance assistant. Compare the ANSWER with the SOURCES and the QUESTION.

- grounded = true only if every factual claim (numbers, dates, names, rules, coverage statements) is supported by the SOURCES, the conversation, or the user profile. Restating the question, generic politeness, and pointing to Member Services/Nurse Line phone numbers is fine. Admitting that information is unavailable is grounded.
- List each unsupported claim verbatim in `unsupported_claims`.
- complete = true if every part of the QUESTION is answered, or the answer clearly states which part the sources do not cover. If something answerable is missing, set complete=false and describe in `missing` what additional data should be retrieved (e.g. "the appeal deadline from the appeals policy").
- Be strict about numbers and coverage rules, lenient about wording.

Return ONLY JSON matching: {schema}"""


def build_verify_messages(*, question: str, sources: str, answer: str, profile: str) -> list[dict]:
    body = (
        f"<user_profile>\n{profile}\n</user_profile>\n\n<question>\n{question}\n</question>\n\n"
        f"<sources>\n{sources}\n</sources>\n\n<answer>\n{answer}\n</answer>"
    )
    return [
        {"role": "system", "content": _static(), "cache": True},
        {"role": "user", "content": body},
    ]
