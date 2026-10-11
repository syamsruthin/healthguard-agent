"""
Online evaluation: a jury of judges from different model families scores a
live answer for faithfulness and relevancy. Runs in the worker for a sample
of turns (ONLINE_EVAL_SAMPLE_RATE) and for every thumbs-down.
"""

from __future__ import annotations

import asyncio
import uuid
from statistics import mean
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import settings
from backend.core.logging import get_logger
from backend.db.models import Message, OnlineEvalScore, Trace
from backend.llm.client import get_llm
from backend.llm.prompts.judge import JudgeScores, build_judge_messages
from backend.llm.registry import Role

log = get_logger(__name__)
METRICS = ("faithfulness", "relevancy")


async def _turn_inputs(session: AsyncSession, message: Message) -> tuple[str, str]:
    """(question, sources) for an assistant message."""
    question = message.standalone_query or ""
    if not question:
        prev = await session.execute(
            select(Message.content)
            .where(
                Message.conversation_id == message.conversation_id,
                Message.role == "user",
                Message.created_at <= message.created_at,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        question = prev.scalar_one_or_none() or ""
    sources = ""
    if message.trace_id is not None:
        trace = await session.get(Trace, message.trace_id)
        for step in reversed(trace.steps if trace else []):
            if step.get("node") == "synthesize":
                sources = step.get("metadata", {}).get("sources", "")
                break
    return question, sources


async def _judge(model: str, messages: list[dict[str, Any]]) -> JudgeScores | None:
    try:
        return await get_llm().structured(Role.JUDGE, messages, JudgeScores, name="online_eval", model=model)
    except Exception as exc:  # one failing judge must not sink the jury
        log.warning("judge_failed", judge=model, error=str(exc)[:200])
        return None


async def score_message(session: AsyncSession, message_id: uuid.UUID) -> dict[str, float] | None:
    message = await session.get(Message, message_id)
    if message is None or message.role != "assistant" or message.blocked:
        return None
    already = await session.execute(select(exists().where(OnlineEvalScore.message_id == message_id)))
    if already.scalar():  # a sampled answer later thumbed down (or re-rated) is not judged again
        return None
    question, sources = await _turn_inputs(session, message)
    messages = build_judge_messages(question=question, sources=sources, answer=message.content)
    judges = settings.online_eval_judges
    verdicts = await asyncio.gather(*(_judge(j, messages) for j in judges))
    scored = {j: v for j, v in zip(judges, verdicts, strict=True) if v is not None}
    if not scored:
        return None
    result: dict[str, float] = {}
    for metric in METRICS:
        per_judge = {j: float(getattr(v, metric)) for j, v in scored.items()}
        values = list(per_judge.values())
        result[metric] = round(mean(values), 4)
        session.add(
            OnlineEvalScore(
                message_id=message.id,
                metric=metric,
                score=result[metric],
                judge_scores=per_judge,
                spread=round(max(values) - min(values), 4),
            )
        )
    await session.commit()
    return result
