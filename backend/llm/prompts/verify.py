"""`verify` prompt — output guardrail: groundedness + completeness in one call."""

from __future__ import annotations

import json
from functools import lru_cache

from backend.llm.prompts.common import REFERENCE_FACTS
from backend.llm.prompts.schemas import Verification


@lru_cache
def _static() -> str:
    schema = json.dumps(Verification.model_json_schema())
    return f"""You are the answer verifier for a health-insurance assistant. Compare the ANSWER with the SOURCES and the QUESTION.

## What counts as a claim
A claim is a specific factual statement about the member's plan, costs, claims, prescriptions, prior authorizations, providers, dates, deadlines, amounts, codes or coverage rules. These are NOT claims and never make an answer ungrounded:
- restating the question, greetings, empathy, formatting
- general next-step suggestions ("check your EOB", "ask your provider", "file the appeal soon")
- the reference facts listed below
- saying that some information is unavailable or could not be found
- simple arithmetic or date reasoning over numbers that are in the sources

{REFERENCE_FACTS}

## Verdict
- grounded = false only if at least one **material** claim is absent from, or contradicted by, the SOURCES, the conversation and the user profile. List each such claim verbatim in `unsupported_claims`.
- Put harmless imprecision (wording, rounding, a slightly over-confident phrasing) in `minor_issues`; it does not make the answer ungrounded.
- complete = true if every part of the QUESTION is answered, or the answer clearly states which part the sources do not cover. If something answerable is missing, set complete=false and describe in `missing` what additional data should be retrieved (e.g. "the appeal deadline from the appeals policy").
- Be strict about numbers, dates and coverage rules; lenient about wording.

Return ONLY JSON matching: {schema}"""


def build_verify_messages(
    *, question: str, sources: str, answer: str, profile: str, history: str = ""
) -> list[dict]:
    convo = f"<conversation>\n{history}\n</conversation>\n\n" if history else ""
    body = (
        f"<user_profile>\n{profile}\n</user_profile>\n\n{convo}<question>\n{question}\n</question>\n\n"
        f"<sources>\n{sources}\n</sources>\n\n<answer>\n{answer}\n</answer>"
    )
    return [
        {"role": "system", "content": _static(), "cache": True},
        {"role": "user", "content": body},
    ]
