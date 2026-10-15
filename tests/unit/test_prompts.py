"""Prompt content that guards known failure modes (golden eval run 82775a45, case memory-01)."""

from __future__ import annotations

from backend.llm.prompts.synthesize import build_synthesis_messages
from backend.llm.prompts.understand import build_understand_messages
from backend.llm.prompts.verify import build_verify_messages


def _system(messages: list[dict]) -> str:
    return messages[0]["content"]


def test_synthesis_forbids_closing_offers() -> None:
    prompt = _system(build_synthesis_messages(profile="p", question="q", sources="s"))
    assert "Do not close with offers to look up other topics" in prompt
    assert "not an offer of more help" in prompt


def test_understand_resolves_follow_ups_to_the_users_subject() -> None:
    prompt = _system(build_understand_messages(profile="p", message="m"))
    assert "subject of the **user's** previous question" in prompt
    assert "only mentioned, offered or suggested" in prompt
    # the memory-01 shape as a worked example
    assert "How much more do I need to pay before I meet my deductible?" in prompt


def test_understand_replan_feedback_allows_a_corrected_rewrite() -> None:
    body = build_understand_messages(profile="p", message="m", feedback="x")[-1]["content"]
    assert "write a corrected standalone_query" in body


def _verify_body(**kw: str) -> str:
    return build_verify_messages(sources="s", answer="a", profile="p", **kw)[-1]["content"]


def test_verifier_sees_the_raw_message_of_a_rewritten_follow_up() -> None:
    body = _verify_body(
        question="How much more do I need to pay before I hit my out-of-pocket maximum?",
        message="How much more do I need to pay before I hit it?",
        history="User: What is my deductible status?\nAssistant: You have $700 left.",
    )
    assert "<user_message>\nHow much more do I need to pay before I hit it?\n</user_message>" in body
    assert body.index("<user_message>") < body.index("<question>")
    assert "rewrite_mismatch=true" in _system(
        build_verify_messages(question="q", sources="s", answer="a", profile="p")
    )


def test_verifier_omits_the_raw_message_when_it_adds_nothing() -> None:
    assert "<user_message>" not in _verify_body(question="What is my deductible?")
    assert "<user_message>" not in _verify_body(
        question="What is my deductible?", message="What is my deductible? "
    )
