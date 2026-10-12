"""Online LLM-judge prompt (sampled live traffic + every thumbs-down)."""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic import BaseModel, Field

from backend.llm.prompts.common import REFERENCE_FACTS


class JudgeScores(BaseModel):
    faithfulness: float = Field(
        ge=0, le=1, description="Share of the answer's claims supported by the sources"
    )
    relevancy: float = Field(
        ge=0, le=1, description="How directly and completely the answer addresses the question"
    )
    reason: str = ""


@lru_cache
def _static() -> str:
    return f"""You grade answers from a health-insurance assistant. Score two metrics from 0 to 1:

- faithfulness: the fraction of material factual claims in the ANSWER that are supported by the SOURCES (or the reference facts). 1.0 = fully supported; 0.0 = mostly unsupported. Saying information is unavailable is faithful.
- relevancy: how directly and completely the ANSWER addresses the QUESTION. 1.0 = fully answers it; 0.0 = off-topic.

{REFERENCE_FACTS}

Be strict about numbers, dates and coverage rules; ignore style. Return ONLY JSON matching: {json.dumps(JudgeScores.model_json_schema())}"""


def build_judge_messages(*, question: str, sources: str, answer: str) -> list[dict]:
    return [
        {"role": "system", "content": _static(), "cache": True},
        {
            "role": "user",
            "content": (
                f"<question>\n{question}\n</question>\n\n<sources>\n{sources or '(no sources: no data lookup was made)'}\n"
                f"</sources>\n\n<answer>\n{answer}\n</answer>"
            ),
        },
    ]
