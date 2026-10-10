"""Conversation context, rolling summary and long-term memory against Postgres + pgvector."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from backend.core.config import settings
from backend.core.security import hash_password
from backend.db.base import utcnow
from backend.db.models import Conversation, Message, User, UserMemory
from backend.db.session import SessionLocal
from backend.memory import long_term
from backend.memory.context import build_context
from backend.memory.summarizer import maybe_summarize


@pytest.fixture
async def user() -> AsyncIterator[User]:
    async with SessionLocal() as session:
        u = User(
            email=f"mem-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("not-used-in-this-test"),
            full_name="Memory Tester",
        )
        session.add(u)
        await session.commit()
    yield u
    async with SessionLocal() as session:
        await session.delete(await session.get(User, u.id))
        await session.commit()


async def _conversation(user: User, turns: list[tuple[str, str]]) -> tuple[Conversation, list[Message]]:
    async with SessionLocal() as session:
        conv = Conversation(user_id=user.id, title="t")
        session.add(conv)
        await session.flush()
        base = utcnow() - timedelta(minutes=len(turns))
        msgs = []
        for i, (role, content) in enumerate(turns):
            m = Message(
                conversation_id=conv.id,
                user_id=user.id,
                role=role,
                content=content,
                created_at=base + timedelta(seconds=i),
            )
            session.add(m)
            msgs.append(m)
        await session.commit()
        return conv, msgs


async def test_context_excludes_current_message(user: User) -> None:
    conv, msgs = await _conversation(
        user, [("user", "What is my deductible?"), ("assistant", "$3,400."), ("user", "And OOP?")]
    )
    async with SessionLocal() as session:
        ctx = await build_context(
            session, conversation=conv, user=user, message="And OOP?", exclude_message_id=msgs[-1].id
        )
    assert ctx.recent_turns == "User: What is my deductible?\nAssistant: $3,400."


async def test_summary_folds_old_turns(user: User, fake_llm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "history_token_budget", 60)
    monkeypatch.setattr(settings, "summary_trigger_tokens", 30)
    fake_llm({"summary": "- User asked about deductibles and MRI denial"})
    turns = [("user" if i % 2 == 0 else "assistant", f"turn {i} " + "detail " * 25) for i in range(8)]
    conv, _ = await _conversation(user, turns)
    async with SessionLocal() as session:
        assert await maybe_summarize(session, conv.id) is True
        refreshed = await session.get(Conversation, conv.id)
        assert refreshed.summary.startswith("- User asked")
        assert refreshed.summary_upto is not None
        ctx = await build_context(session, conversation=refreshed, user=user, message="next")
    assert ctx.summary.startswith("- User asked")
    assert "turn 0 " not in ctx.recent_turns and "turn 7 " in ctx.recent_turns
    async with SessionLocal() as session:
        assert await maybe_summarize(session, conv.id) is False  # nothing new to fold


async def test_memory_extraction_filters_dedupes_and_retrieves(user: User, fake_llm) -> None:
    fake_llm(
        {
            "memory": [
                {
                    "memories": [
                        {"content": "Prefers Spanish-speaking providers", "category": "language"},
                        {"content": "Has type 2 diabetes", "category": "other"},
                    ]
                },
                {
                    "memories": [
                        {"content": "Prefers Spanish-speaking providers", "category": "language"},
                        {"content": "Wants short answers with tables", "category": "communication_style"},
                    ]
                },
            ]
        }
    )
    async with SessionLocal() as session:
        u = await session.get(User, user.id)
        first = await long_term.extract_and_store(
            session, u, user_message="Find a doctor who speaks Spanish", assistant_message="..."
        )
        second = await long_term.extract_and_store(
            session, u, user_message="Keep it short", assistant_message="..."
        )
        stored = await long_term.list_memories(session, user.id)
    assert [m.content for m in first] == ["Prefers Spanish-speaking providers"]  # clinical fact dropped
    assert [m.content for m in second] == ["Wants short answers with tables"]  # duplicate skipped
    assert len(stored) == 2

    conv, _ = await _conversation(user, [])
    async with SessionLocal() as session:
        ctx = await build_context(
            session, conversation=conv, user=user, message="Prefers Spanish-speaking providers"
        )
    assert "Prefers Spanish-speaking providers" in ctx.memories


async def test_memory_disabled_skips_extraction_and_retrieval(user: User, fake_llm) -> None:
    llm = fake_llm(
        {"memory": {"memories": [{"content": "Prefers telehealth", "category": "provider_preference"}]}}
    )
    async with SessionLocal() as session:
        u = await session.get(User, user.id)
        await long_term.extract_and_store(
            session, u, user_message="I like telehealth", assistant_message="..."
        )
        u.memory_enabled = False
        await session.commit()
        assert await long_term.extract_and_store(session, u, user_message="x", assistant_message="y") == []
        conv, _ = await _conversation(u, [])
        ctx = await build_context(session, conversation=conv, user=u, message="Prefers telehealth")
    assert ctx.memories == ""
    assert llm.names() == ["memory"]


async def test_memory_cap(user: User, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(long_term, "MAX_MEMORIES_PER_USER", 3)
    async with SessionLocal() as session:
        for i in range(5):
            session.add(
                UserMemory(user_id=user.id, content=f"pref {i}", embedding=[0.0] * settings.embedding_dim)
            )
            await session.flush()
        await long_term._enforce_cap(session, user.id)
        await session.commit()
        assert len(await long_term.list_memories(session, user.id)) == 3
