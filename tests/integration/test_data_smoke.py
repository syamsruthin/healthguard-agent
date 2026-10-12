"""Smoke checks on the seeded + ingested database (runs after `hg-seed --scale small && hg-ingest` in CI)."""

from __future__ import annotations

from sqlalchemy import text

from backend.core.config import settings
from backend.data_gen.reference import PERSONAS
from backend.db.session import SessionLocal, get_data_version


async def _scalar(sql: str, **params: object) -> object:
    async with SessionLocal() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def test_reference_data_and_personas() -> None:
    assert await _scalar("SELECT count(*) FROM insurance.plans") == 40
    assert await _scalar("SELECT count(*) FROM insurance.denial_codes") == 12
    personas = {p.email: p.member_id for p in PERSONAS}
    async with SessionLocal() as session:
        rows = (await session.execute(text("SELECT email, member_id, role FROM app.users"))).all()
    users = {r.email: (r.member_id, r.role) for r in rows}
    for email, member_id in personas.items():
        assert users[email] == (member_id, "member")
    assert users[settings.admin_email] == (None, "admin")


async def test_persona_stories_are_seeded() -> None:
    # Maria's April knee MRI was denied for missing prior authorization (D002).
    assert (
        await _scalar(
            "SELECT count(*) FROM insurance.claims WHERE member_id = 'M100001' AND denial_code = 'D002'"
        )
        >= 1
    )
    # Robert reached his out-of-pocket maximum.
    assert (
        await _scalar(
            "SELECT oop_individual_met >= oop_individual_limit FROM insurance.member_accumulators WHERE member_id = 'M100004'"
        )
        is True
    )


async def test_corpus_is_ingested_with_embeddings() -> None:
    assert await _scalar("SELECT count(*) FROM rag.documents") == 60
    chunks = await _scalar("SELECT count(*) FROM rag.chunks")
    assert isinstance(chunks, int) and chunks > 300
    assert await _scalar("SELECT count(*) FROM rag.chunks WHERE embedding IS NULL") == 0
    assert await _scalar("SELECT vector_dims(embedding) FROM rag.chunks LIMIT 1") == settings.embedding_dim
    assert await get_data_version() >= 1
