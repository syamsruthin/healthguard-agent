"""Background prompts: long-term memory extraction, rolling summary, conversation title."""

from __future__ import annotations

import json

from backend.llm.prompts.schemas import MemoryExtraction, Title

_MEMORY_STATIC = f"""You maintain long-term memory for a health-insurance assistant. From the latest exchange, extract DURABLE facts or preferences about the user that would help in future conversations.

Keep (examples):
- location: "Lives in Austin, TX (zip 78704)"
- language: "Prefers Spanish-speaking providers"
- provider_preference: "Prefers female doctors", "Wants providers available within a week", "Prefers telehealth"
- plan_interest: "Comparing Gold PPO plans for 2027 open enrollment"
- communication_style: "Wants short answers with tables"

NEVER store: diagnoses, conditions, symptoms, medications, procedures, claim details, amounts, dates of birth, IDs, or anything about other people. Health details belong only in the member record, not in memory.
Do not repeat facts already in <existing_memories>. Return an empty list if nothing new and durable was said.

Return ONLY JSON matching: {json.dumps(MemoryExtraction.model_json_schema())}"""

_SUMMARY_STATIC = """Summarize the earlier part of a conversation between a health-insurance member and the HealthGuard assistant so the assistant can continue it later.
Keep: the user's goals, decisions, entities referenced (plan IDs and names, claim IDs, provider names, drug names, locations), facts already established, and open questions. Drop pleasantries. Write 4–10 compact bullet points. If a previous summary is provided, merge it."""

_TITLE_STATIC = f"""Create a short, specific title (3–6 words, no quotes, no trailing period) for a conversation that starts with the user message below. Return ONLY JSON: {json.dumps(Title.model_json_schema())}"""


def build_memory_messages(*, existing: str, user_message: str, assistant_message: str) -> list[dict]:
    return [
        {"role": "system", "content": _MEMORY_STATIC, "cache": True},
        {
            "role": "user",
            "content": (
                f"<existing_memories>\n{existing or '(none)'}\n</existing_memories>\n\n"
                f"<user>\n{user_message}\n</user>\n\n<assistant>\n{assistant_message[:3000]}\n</assistant>"
            ),
        },
    ]


def build_summary_messages(*, previous_summary: str, transcript: str) -> list[dict]:
    return [
        {"role": "system", "content": _SUMMARY_STATIC, "cache": True},
        {
            "role": "user",
            "content": (
                f"<previous_summary>\n{previous_summary or '(none)'}\n</previous_summary>\n\n"
                f"<transcript>\n{transcript}\n</transcript>"
            ),
        },
    ]


def build_title_messages(message: str) -> list[dict]:
    return [
        {"role": "system", "content": _TITLE_STATIC},
        {"role": "user", "content": message[:500]},
    ]
