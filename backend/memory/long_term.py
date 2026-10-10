"""
Long-term memory: durable, NON-clinical user preferences (language, location,
provider preferences, plan interests, communication style).

Extraction is an LLM call, so a deterministic filter backs up the prompt's
rules: anything that looks like an identifier, amount, date, contact detail or
clinical information is dropped before storage. Users can list, delete and
disable their memories.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import settings
from backend.core.logging import get_logger
from backend.db.models import User, UserMemory
from backend.llm.client import get_llm
from backend.llm.embeddings import cosine, get_embedder
from backend.llm.prompts.memory import build_memory_messages
from backend.llm.prompts.schemas import MemoryExtraction
from backend.llm.registry import Role

log = get_logger(__name__)

MAX_MEMORIES_PER_USER = 50
DUPLICATE_SIMILARITY = 0.9

_SENSITIVE = re.compile(
    r"\bM\d{6}\b"  # member ids
    r"|\bCLM-\d|\bPA-\d"  # claim / prior-auth ids
    r"|\$\s?\d|\b\d+(\.\d+)?\s?(dollars|usd)\b"  # amounts
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b"  # dates
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2}\b"
    r"|\d{5,}"  # long digit runs (phone numbers, NPIs, policy numbers) — 5-digit zips are allowed below
    r"|[\w.+-]+@[\w-]+\.[\w.]+"  # emails
    r"|\(\d{3}\)|\b\d{3}[-.]\d{3}[-.]\d{4}\b",  # phone numbers
    re.IGNORECASE,
)
_ZIP = re.compile(r"\bzip(?: code)?\s*\d{5}\b|\b[A-Z]{2}\s\d{5}\b|\(\s*zip\s*\d{5}\s*\)", re.IGNORECASE)
_CLINICAL = re.compile(
    r"\b(diagnos\w*|pregnan\w*|diabet\w*|cancer|tumou?r|depress\w*|anxiety|bipolar|hiv|aids|asthma|"
    r"hypertension|cholesterol|insulin|ozempic|medication\w*|prescri\w*|dose|dosage|surgery|surgical|"
    r"symptom\w*|condition\w*|disorder\w*|disease\w*|injur\w*|biopsy|mri|x-ray|chemo\w*|rehab\w*|"
    r"addiction|overdose|suicid\w*|self-harm|mental health)\b",
    re.IGNORECASE,
)


def is_storable(content: str) -> bool:
    """Deterministic backstop for the extraction prompt's privacy rules."""
    text = content.strip()
    if not 3 <= len(text) <= 300:
        return False
    scrubbed = _ZIP.sub("", text)  # a home zip code is a useful, non-sensitive preference
    return not (_SENSITIVE.search(scrubbed) or _CLINICAL.search(text))


async def list_memories(session: AsyncSession, user_id: uuid.UUID) -> list[UserMemory]:
    rows = await session.execute(
        select(UserMemory).where(UserMemory.user_id == user_id).order_by(UserMemory.created_at.desc())
    )
    return list(rows.scalars())


async def delete_memory(session: AsyncSession, user_id: uuid.UUID, memory_id: uuid.UUID) -> bool:
    result = await session.execute(
        delete(UserMemory).where(UserMemory.id == memory_id, UserMemory.user_id == user_id)
    )
    await session.commit()
    return bool(getattr(result, "rowcount", 0))


async def clear_memories(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(delete(UserMemory).where(UserMemory.user_id == user_id))
    await session.commit()


async def search(session: AsyncSession, user_id: uuid.UUID, query_embedding: list[float]) -> list[UserMemory]:
    """Top-k memories by cosine similarity, above the minimum similarity."""
    distance = UserMemory.embedding.cosine_distance(query_embedding)
    rows = await session.execute(
        select(UserMemory, distance.label("distance"))
        .where(UserMemory.user_id == user_id)
        .order_by(distance)
        .limit(settings.long_term_memory_top_k)
    )
    return [m for m, d in rows.all() if 1 - float(d) >= settings.long_term_memory_min_similarity]


async def extract_and_store(
    session: AsyncSession,
    user: User,
    *,
    user_message: str,
    assistant_message: str,
    source_message_id: uuid.UUID | None = None,
) -> list[UserMemory]:
    """Extract durable preferences from one exchange and store the new, safe, non-duplicate ones."""
    if not user.memory_enabled:
        return []
    existing = await list_memories(session, user.id)
    extraction = await get_llm().structured(
        Role.FAST,
        build_memory_messages(
            existing="\n".join(f"- {m.content}" for m in existing),
            user_message=user_message,
            assistant_message=assistant_message,
        ),
        MemoryExtraction,
        name="memory",
    )
    candidates = [c for c in extraction.memories if is_storable(c.content)]
    dropped = len(extraction.memories) - len(candidates)
    if dropped:
        log.info("memory_candidates_filtered", dropped=dropped)
    if not candidates:
        return []
    vectors = await get_embedder().embed([c.content for c in candidates])
    known: list[list[float]] = [list(m.embedding) for m in existing]
    stored: list[UserMemory] = []
    for cand, vec in zip(candidates, vectors, strict=True):
        if any(cosine(vec, k) > DUPLICATE_SIMILARITY for k in known):
            continue
        memory = UserMemory(
            user_id=user.id,
            content=cand.content.strip(),
            category=cand.category,
            embedding=vec,
            source_message_id=source_message_id,
        )
        session.add(memory)
        stored.append(memory)
        known.append(vec)
    await session.flush()
    await _enforce_cap(session, user.id)
    await session.commit()
    return stored


async def _enforce_cap(session: AsyncSession, user_id: uuid.UUID) -> None:
    count = (
        await session.execute(
            select(func.count()).select_from(UserMemory).where(UserMemory.user_id == user_id)
        )
    ).scalar_one()
    overflow = int(count) - MAX_MEMORIES_PER_USER
    if overflow > 0:
        oldest = (
            select(UserMemory.id)
            .where(UserMemory.user_id == user_id)
            .order_by(UserMemory.created_at)
            .limit(overflow)
        )
        await session.execute(delete(UserMemory).where(UserMemory.id.in_(oldest)))


def format_memories(memories: list[UserMemory]) -> str:
    return "\n".join(f"- {m.content} ({m.category})" for m in memories)


def as_dict(m: UserMemory) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "content": m.content,
        "category": m.category,
        "created_at": m.created_at.isoformat(),
    }
