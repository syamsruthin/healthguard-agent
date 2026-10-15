"""Langfuse through the API: one trace per turn keyed by the persisted trace id, turn scores, feedback scores."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.core import observability as obs
from backend.db.models import Message, User
from backend.db.session import SessionLocal
from tests.integration.api.conftest import API, TEST_TITLE, auth, login
from tests.integration.api.test_chat_api import DENIED_CLAIMS_SQL, script
from tests.langfuse_tree import collect

MARIA = "maria.garcia@demo.healthguard.dev"


@pytest.fixture
def scores(langfuse_spans, monkeypatch) -> list[dict[str, Any]]:
    """Scores sent to Langfuse (they use the ingestion API, not OTel spans)."""
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(obs.get_langfuse(), "create_score", lambda **kw: sent.append(kw))
    return sent


async def _turn(client, tokens: dict[str, Any], message: str = "Why was my claim denied?") -> dict[str, Any]:
    cid = (
        await client.post(f"{API}/conversations", json={"title": TEST_TITLE}, headers=auth(tokens))
    ).json()["id"]
    resp = await client.post(
        f"{API}/conversations/{cid}/messages", json={"message": message}, headers=auth(tokens)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_turn_is_one_trace_keyed_by_the_persisted_trace_id(
    client, fake_llm, jobs, langfuse_spans, scores
) -> None:
    fake_llm(script())
    tokens = await login(client, MARIA)
    final = await _turn(client, tokens)
    assert final["outcome"] == "answered"

    [turn] = collect(langfuse_spans)
    assert turn.name == "chat-turn" and turn.trace_id == uuid.UUID(final["trace_id"]).hex
    async with SessionLocal() as session:
        msg = await session.get(Message, uuid.UUID(final["message_id"]))
        assert msg is not None and msg.trace_id == uuid.UUID(final["trace_id"])
        maria = await session.get(User, msg.user_id)
    # Users are identified by id, the conversation is the session; no email leaves the app.
    assert turn.attrs["user.id"] == str(maria.id) and turn.attrs["session.id"] == final["conversation_id"]
    assert set(turn.attrs["langfuse.trace.tags"]) >= {"chat", "member"}
    assert "maria.garcia" not in str([n.attrs for n in turn.find("chat-turn")])
    assert turn.output == final["answer"] and turn.meta("outcome") == "answered"

    names = turn.child_names()
    assert names[0] == "load-context" and names[-3:] == [
        "persist",
        "semantic_cache.store",
        "followups.enqueued",
    ]
    sql = turn.find("sql.execute")[0]
    guard = turn.find("sql.guard")[0]
    # The guard's input is the generated SQL; execution runs the guard's capped rewrite of it.
    assert guard.type == "guardrail" and guard.input == DENIED_CLAIMS_SQL and guard.meta("rewritten") is True
    assert sql.input == guard.output and DENIED_CLAIMS_SQL in sql.input and "LIMIT 200" in sql.input
    assert sql.meta("rls_role") == "member" and sql.meta("rows") >= 1
    # Member ids in tool output are masked before export.
    assert "M100001" not in str(sql.output)

    by_name = {s["name"]: s for s in scores}
    assert by_name["outcome"]["value"] == "answered" and by_name["outcome"]["trace_id"] == turn.trace_id
    assert by_name["grounded"]["value"] == 1.0 and by_name["grounded"]["data_type"] == "BOOLEAN"
    assert by_name["cache_hit"]["value"] == 0.0
    assert {"complete", "replans", "revisions"} <= by_name.keys()


async def test_feedback_becomes_a_score_on_the_turn_trace(client, fake_llm, jobs, scores) -> None:
    fake_llm(script())
    tokens = await login(client, MARIA)
    final = await _turn(client, tokens)
    scores.clear()
    resp = await client.post(
        f"{API}/messages/{final['message_id']}/feedback",
        json={"rating": -1, "comment": "wrong claim"},
        headers=auth(tokens),
    )
    assert resp.status_code == 204
    [fb] = scores
    assert fb["name"] == "user_feedback" and fb["value"] == -1.0 and fb["comment"] == "wrong claim"
    assert fb["trace_id"] == uuid.UUID(final["trace_id"]).hex


async def test_admin_trace_view_links_to_langfuse(
    client, fake_llm, jobs, langfuse_spans, monkeypatch
) -> None:
    monkeypatch.setattr(
        obs.get_langfuse(), "get_trace_url", lambda trace_id: f"https://lf.test/trace/{trace_id}"
    )
    fake_llm(
        {
            **script(),
            "understand": {**script()["understand"], "standalone_query": "How many members are enrolled?"},
        }
    )
    admin = await login(client, "admin@demo.healthguard.dev")
    final = await _turn(client, admin, "How many members are enrolled?")
    hex_id = uuid.UUID(final["trace_id"]).hex
    assert final["langfuse_url"] == f"https://lf.test/trace/{hex_id}"
    view = (await client.get(f"{API}/admin/traces/{final['trace_id']}", headers=auth(admin))).json()
    assert view["langfuse_url"] == f"https://lf.test/trace/{hex_id}"


async def test_member_turn_has_no_langfuse_link(client, fake_llm, jobs, langfuse_spans) -> None:
    fake_llm(script())
    final = await _turn(client, await login(client, MARIA))
    assert final["langfuse_url"] is None
