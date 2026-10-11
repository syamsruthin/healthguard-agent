from __future__ import annotations

import pytest

from backend.memory.context import format_turn, split_window
from backend.memory.long_term import is_storable, worth_remembering


@pytest.mark.parametrize(
    "content",
    [
        "Prefers Spanish-speaking providers",
        "Lives in Austin, TX (zip 78704)",
        "Wants short answers with tables",
        "Comparing Gold PPO plans for 2027 open enrollment",
        "Prefers telehealth visits when possible",
    ],
)
def test_durable_preferences_are_storable(content: str) -> None:
    assert is_storable(content)


@pytest.mark.parametrize(
    "content",
    [
        "Member ID is M100001",
        "Has type 2 diabetes",
        "Takes Ozempic weekly",
        "Paid $350 for an imaging visit",
        "Born 04/12/1985",
        "Call back at (512) 555-0101",
        "Email is maria@example.com",
        "Claim CLM-2026-0000005 was denied",
        "Is pregnant, due in November",
        "",
    ],
)
def test_sensitive_or_clinical_content_is_rejected(content: str) -> None:
    assert not is_storable(content)


def test_split_window_keeps_newest_within_budget() -> None:
    lines = [f"User: message number {i} " + "word " * 20 for i in range(10)]
    older, window = split_window(lines, budget=80)
    assert older + window == lines
    assert window and window[-1] == lines[-1]
    assert len(window) < len(lines)


def test_split_window_always_keeps_latest_line() -> None:
    older, window = split_window(["User: " + "x " * 5000], budget=10)
    assert older == [] and len(window) == 1


def test_assistant_turns_are_truncated() -> None:
    assert format_turn("assistant", "a" * 5000).endswith(" …")
    assert format_turn("user", "hi") == "User: hi"


@pytest.mark.parametrize(
    "message",
    [
        "Find a Spanish-speaking cardiologist near me",
        "I prefer female doctors",
        "I live in Brooklyn now",
        "Keep answers short please",
        "We're comparing plans for open enrollment",
    ],
)
def test_preference_cues_trigger_memory_extraction(message: str) -> None:
    assert worth_remembering(message)


@pytest.mark.parametrize(
    "message",
    ["Why was my claim denied?", "What is my deductible status?", "How do appeals work?", "thanks!"],
)
def test_plain_questions_skip_memory_extraction(message: str) -> None:
    assert not worth_remembering(message)


def test_summary_needed_only_when_history_overflows(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.core.config import settings
    from backend.memory.context import MemoryContext, needs_summary

    monkeypatch.setattr(settings, "history_token_budget", 50)
    monkeypatch.setattr(settings, "summary_trigger_tokens", 100)
    short = [format_turn("user", "hi"), format_turn("assistant", "hello")]
    assert not needs_summary(short)
    long_turns = [format_turn("user", "word " * 60) for _ in range(4)]
    assert needs_summary(long_turns)
    assert MemoryContext(lines=long_turns[:2]).needs_summary_after("word " * 60, "word " * 60)
    assert not MemoryContext(lines=short).needs_summary_after("thanks", "You're welcome")
