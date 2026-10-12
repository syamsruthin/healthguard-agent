from __future__ import annotations

from backend.core.config import settings
from backend.db.models import User, UserMemory
from backend.db.session import SessionLocal
from tests.integration.api.conftest import API, auth, register


async def test_memory_endpoints(client) -> None:
    tokens = await register(client)
    uid = tokens["user"]["id"]
    async with SessionLocal() as session:
        for text in ("Prefers telehealth", "Wants short answers"):
            session.add(UserMemory(user_id=uid, content=text, embedding=[0.1] * settings.embedding_dim))
        await session.commit()
    h = auth(tokens)
    memories = (await client.get(f"{API}/me/memories", headers=h)).json()
    assert {m["content"] for m in memories} == {"Prefers telehealth", "Wants short answers"}
    assert (await client.delete(f"{API}/me/memories/{memories[0]['id']}", headers=h)).status_code == 204
    assert (await client.delete(f"{API}/me/memories/{memories[0]['id']}", headers=h)).status_code == 404
    assert len((await client.get(f"{API}/me/memories", headers=h)).json()) == 1
    assert (await client.delete(f"{API}/me/memories", headers=h)).status_code == 204
    assert (await client.get(f"{API}/me/memories", headers=h)).json() == []
    patched = await client.patch(f"{API}/me/settings", json={"memory_enabled": False}, headers=h)
    assert patched.status_code == 200 and patched.json()["memory_enabled"] is False
    async with SessionLocal() as session:
        assert (await session.get(User, uid)).memory_enabled is False


async def test_cannot_delete_another_users_memory(client) -> None:
    owner, other = await register(client), await register(client)
    async with SessionLocal() as session:
        mem = UserMemory(
            user_id=owner["user"]["id"],
            content="Prefers telehealth",
            embedding=[0.1] * settings.embedding_dim,
        )
        session.add(mem)
        await session.commit()
    assert (await client.delete(f"{API}/me/memories/{mem.id}", headers=auth(other))).status_code == 404
    assert len((await client.get(f"{API}/me/memories", headers=auth(owner))).json()) == 1
