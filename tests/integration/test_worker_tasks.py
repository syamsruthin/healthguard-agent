"""Worker tasks run directly (no arq process) against Postgres + Redis with a scripted LLM."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select

from backend.core.config import settings
from backend.core.redis import get_redis
from backend.core.security import hash_password
from backend.db.models import Conversation, LLMCall, Message, OnlineEvalScore, Trace, User, UserMemory
from backend.db.session import SessionLocal
from backend.llm.client import LLMError
from backend.workers import tasks


@pytest.fixture
async def convo() -> AsyncIterator[dict[str, uuid.UUID]]:
    async with SessionLocal() as session:
        user = User(
            email=f"wk-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("x" * 12),
            full_name="Worker Test",
        )
        session.add(user)
        await session.flush()
        conv = Conversation(user_id=user.id)
        session.add(conv)
        await session.flush()
        trace_id = uuid.uuid4()
        q = Message(
            conversation_id=conv.id,
            user_id=user.id,
            role="user",
            content="Find a Spanish-speaking cardiologist",
        )
        session.add(q)
        await session.flush()
        a = Message(
            conversation_id=conv.id,
            user_id=user.id,
            role="assistant",
            trace_id=trace_id,
            content="Dr. Henderson is 8.3 miles away [S1].",
            standalone_query="Spanish-speaking cardiologists near 78704",
        )
        session.add(a)
        await session.flush()
        session.add(
            Trace(
                id=trace_id,
                conversation_id=conv.id,
                message_id=a.id,
                user_id=user.id,
                steps=[{"node": "synthesize", "metadata": {"sources": "[S1] Henderson, 8.3 mi"}}],
            )
        )
        await session.commit()
        ids = {"user": user.id, "conversation": conv.id, "question": q.id, "answer": a.id}
    yield ids
    async with SessionLocal() as session:
        await session.delete(await session.get(User, ids["user"]))
        await session.commit()


async def _background_calls(name: str) -> list[LLMCall]:
    async with SessionLocal() as session:
        rows = await session.execute(
            select(LLMCall)
            .where(LLMCall.name == name, LLMCall.trace_id.is_(None))
            .order_by(LLMCall.id.desc())
            .limit(5)
        )
        return list(rows.scalars())


async def test_title_conversation(convo, fake_llm) -> None:
    fake_llm({"title": {"title": "Spanish-speaking cardiologist search."}})
    assert (
        await tasks.title_conversation({}, str(convo["conversation"]))
        == "Spanish-speaking cardiologist search"
    )
    assert await tasks.title_conversation({}, str(convo["conversation"])) is None  # already titled
    assert await _background_calls("title")


async def test_summarize_respects_lock(convo) -> None:
    lock = f"hg:lock:summarize:{convo['conversation']}"
    await get_redis().set(lock, "1", ex=30)
    try:
        assert await tasks.summarize_conversation({}, str(convo["conversation"])) is False
    finally:
        await get_redis().delete(lock)
    assert (
        await tasks.summarize_conversation({}, str(convo["conversation"])) is False
    )  # too short to summarize


async def test_extract_memories(convo, fake_llm) -> None:
    fake_llm(
        {"memory": {"memories": [{"content": "Prefers Spanish-speaking providers", "category": "language"}]}}
    )
    n = await tasks.extract_memories({}, str(convo["user"]), str(convo["question"]), str(convo["answer"]))
    assert n == 1
    async with SessionLocal() as session:
        rows = (
            (await session.execute(select(UserMemory).where(UserMemory.user_id == convo["user"])))
            .scalars()
            .all()
        )
    assert [m.source_message_id for m in rows] == [convo["answer"]]


async def test_online_eval_jury(convo, fake_llm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "online_eval_judges", ["openai/judge-a", "google/judge-b", "x/judge-c"])
    llm = fake_llm(
        {
            "online_eval": [
                {"faithfulness": 1.0, "relevancy": 0.8},
                {"faithfulness": 0.6, "relevancy": 1.0},
                LLMError("judge down"),
            ]
        }
    )
    scores = await tasks.online_eval({}, str(convo["answer"]), "thumbs_down")
    assert scores == {"faithfulness": 0.8, "relevancy": 0.9}
    judge_prompt = llm.calls[0]["messages"][-1]["content"]
    assert "Henderson, 8.3 mi" in judge_prompt and "Spanish-speaking cardiologists near 78704" in judge_prompt
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(OnlineEvalScore).where(OnlineEvalScore.message_id == convo["answer"])
                )
            )
            .scalars()
            .all()
        )
    by_metric = {r.metric: r for r in rows}
    assert by_metric["faithfulness"].spread == pytest.approx(0.4)
    assert set(by_metric["faithfulness"].judge_scores) == {"openai/judge-a", "google/judge-b"}
    calls = len(llm.calls)
    assert await tasks.online_eval({}, str(convo["answer"]), "thumbs_down") is None  # judged once only
    assert len(llm.calls) == calls


async def test_jobs_are_traced_and_jury_scores_the_turn_trace(
    convo, fake_llm, monkeypatch: pytest.MonkeyPatch, langfuse_spans
) -> None:
    from backend.core import observability as obs
    from tests.langfuse_tree import collect

    sent: list[dict] = []
    monkeypatch.setattr(obs.get_langfuse(), "create_score", lambda **kw: sent.append(kw))
    monkeypatch.setattr(settings, "online_eval_judges", ["openai/judge-a", "google/judge-b"])
    fake_llm(
        {
            "online_eval": [{"faithfulness": 1.0, "relevancy": 0.8}, {"faithfulness": 0.6, "relevancy": 1.0}],
            "title": {"title": "Cardiologist search"},
        }
    )
    await tasks.title_conversation({}, str(convo["conversation"]))
    await tasks.online_eval({}, str(convo["answer"]), "thumbs_down")

    title, judged = collect(langfuse_spans)
    assert title.name == "job:title" and title.attrs["session.id"] == str(convo["conversation"])
    assert title.child("llm.title").type == "generation" and title.output == "Cardiologist search"
    assert judged.name == "job:online_eval" and judged.meta("reason") is None
    assert sorted(judged.child_names()) == ["judge · google/judge-b", "judge · openai/judge-a"]
    assert judged.child("judge · openai/judge-a").type == "evaluator"

    async with SessionLocal() as session:
        turn_trace = obs.trace_id_for((await session.get(Message, convo["answer"])).trace_id)
    assert judged.meta("turn_trace_id") == turn_trace
    by_name = {s["name"]: s for s in sent}
    assert by_name["online_faithfulness"]["trace_id"] == turn_trace
    assert by_name["online_faithfulness"]["value"] == pytest.approx(0.8)
    assert by_name["online_relevancy"]["metadata"]["spread"] == pytest.approx(0.2)
