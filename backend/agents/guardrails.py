"""
Deterministic guardrails (run before any LLM call) and standard fallback replies.

The LLM `understand` step performs the nuanced scope/safety classification;
these cheap checks stop obvious injection attempts and cross-member ID
probing without spending tokens. Row-Level Security is the final backstop.
"""

from __future__ import annotations

import re

from backend.agents.tools.base import UserContext

FALLBACKS: dict[str, str] = {
    "out_of_scope": (
        "I can help with HealthGuard plans, benefits, claims, prescriptions, prior authorizations, providers and "
        "policies — but that question is outside what I can answer. Is there something about your coverage I can help with?"
    ),
    "prompt_injection": (
        "I can't help with that request. I can answer questions about your HealthGuard coverage, claims, "
        "providers and policies."
    ),
    "other_member_data": (
        "For privacy, I can only discuss your own records and those of dependents on your plan. If you are "
        "authorized to act for another member, they can name you as an authorized representative (Form HG-AR1) "
        "through Member Services at 1-800-446-3623."
    ),
    "medical_advice": (
        "I can't give medical advice — please talk with your doctor, or call the 24/7 Nurse Advice Line at "
        "1-800-446-8773. I'm happy to explain what your plan covers for this care or find an in-network provider."
    ),
    "harmful": (
        "If you are thinking about harming yourself or are in crisis, please call or text **988** (Suicide & "
        "Crisis Lifeline) now, or call 911 in an emergency. HealthGuard's Behavioral Health Line is available "
        "24/7 at 1-800-446-3682 and can arrange urgent care for you."
    ),
    "llm_unavailable": (
        "I'm having trouble reaching the AI service right now, so I can't answer this yet. Please try again in "
        "a moment, or call Member Services at 1-800-446-3623."
    ),
    "insufficient_context": (
        "I couldn't verify a complete answer from HealthGuard's records and policy documents, so I'd rather not "
        "guess. Please try rephrasing, or contact Member Services at 1-800-446-3623 for help."
    ),
    "no_data": (
        "I couldn't retrieve the information needed to answer that right now. Please try again, or contact "
        "Member Services at 1-800-446-3623."
    ),
}

_INJECTION_PATTERNS = [
    r"\bignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|your)\s+(instructions|rules|prompts?)",
    r"\b(reveal|print|show|repeat|output)\s+(me\s+)?(your|the)\s+(system|hidden|initial)\s+(prompt|instructions)",
    r"\byou\s+are\s+now\s+(an?\s+)?(admin|administrator|developer|dan|root)\b",
    r"\b(developer|god|jailbreak)\s+mode\b",
    r"\b(drop|truncate|alter)\s+table\b",
    r"\bdelete\s+from\b",
    r"\bupdate\s+\w+\s+set\b",
    r"\b(pg_sleep|set_config|current_setting|pg_read_file|dblink)\s*\(",
    r"<\|im_(start|end)\|>|\[/?INST\]",
    r"\bact\s+as\s+(an?\s+)?(admin|administrator|database|sql)\b",
]
_INJECTION = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)
_MEMBER_ID = re.compile(r"\bM\d{6}\b", re.IGNORECASE)


def prefilter(message: str, user: UserContext) -> str | None:
    """Return a block reason, or None if the message may proceed to the LLM."""
    if _INJECTION.search(message):
        return "prompt_injection"
    if not user.is_admin:
        allowed = {user.member_id, *(d["member_id"] for d in user.dependents)} - {None}
        mentioned = {m.upper() for m in _MEMBER_ID.findall(message)}
        if mentioned - allowed:
            return "other_member_data"
    return None
