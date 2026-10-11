"""Chat turns through the HTTP API: SSE contract, persistence, ownership, RLS-scoped real SQL."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from backend.agents.graph import get_graph
from backend.core.redis import get_redis
from backend.db.models import EvalRun, LLMCall, Message, Trace
from backend.db.session import SessionLocal
from tests.integration.api.conftest import API, TEST_TITLE, auth, login, parse_sse, register

DENIED_CLAIMS_SQL = "SELECT claim_id, member_id, status, denial_code FROM claims WHERE status = 'denied'"


def script(answer: str = "Your claim was denied for missing prior authorization [S1].") -> dict:
    return {
        "understand": {
            "in_scope": True,
            "standalone_query": "Why was my claim denied?",
            "needs_member_data": True,
            "plan": [{"id": "s1", "tool": "sql", "task": "List my denied claims", "depends_on": []}],
            "reason": "member claim lookup",
        },
        "sql": DENIED_CLAIMS_SQL,
        "synthesize": answer,
        "verify": {"grounded": True, "complete": True},
    }


async def _conversation(client, tokens) -> str:
    resp = await client.post(f"{API}/conversations", json={"title": TEST_TITLE}, headers=auth(tokens))
    assert resp.status_code == 201
    return resp.json()["id"]


async def test_streamed_turn_contract_and_persistence(client, fake_llm, jobs) -> None:
    fake_llm(script())
    tokens = await login(client, "maria.garcia@demo.healthguard.dev")
    cid = await _conversation(client, tokens)
    resp = await client.post(
        f"{API}/conversations/{cid}/messages/stream",
        json={"message": "Why was my claim denied?"},
        headers=auth(tokens),
    )
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "meta" and kinds[-1] == "final"
    for kind in ("status", "plan", "tool", "token", "citations"):
        assert kind in kinds
    final = events[-1]
    assert final["outcome"] == "answered" and final["conversation_id"] == cid
    tool = final["tools"][0]
    assert tool["tool"] == "sql" and tool["ok"] and tool["row_count"] >= 1
    # Row-Level Security: the generated SQL had no member filter, yet only Maria's claims came back.
    sql_step = next(s for s in final["trace"] if s["node"] == "execute")
    assert sql_step["output"][0]["ok"]

    async with SessionLocal() as session:
        msgs = (
            (await session.execute(select(Message).where(Message.conversation_id == uuid.UUID(cid))))
            .scalars()
            .all()
        )
        assert sorted(m.role for m in msgs) == ["assistant", "user"]
        assistant = next(m for m in msgs if m.role == "assistant")
        assert str(assistant.id) == final["message_id"] and assistant.meta["outcome"] == "answered"
        trace = await session.get(Trace, assistant.trace_id)
        assert trace is not None and [s["node"] for s in trace.steps][-1] == "finalize"
        calls = (
            await session.execute(
                select(func.count()).select_from(LLMCall).where(LLMCall.trace_id == assistant.trace_id)
            )
        ).scalar_one()
        assert calls >= 4  # understand, sql, synthesize, verify
    # Background jobs run only when they have work: a short first turn with no stated preference
    # needs a title but no summary and no memory extraction.
    assert set(jobs.names()) == {"title_conversation"}
    assert not await get_redis().exists(f"hg:turn:{cid}")  # lock released


async def test_stated_preferences_queue_memory_extraction(client, fake_llm, jobs) -> None:
    fake_llm(script())
    tokens = await login(client, "maria.garcia@demo.healthguard.dev")
    cid = await _conversation(client, tokens)
    await client.post(
        f"{API}/conversations/{cid}/messages",
        json={"message": "I prefer Spanish-speaking doctors. Why was my claim denied?"},
        headers=auth(tokens),
    )
    assert "extract_memories" in jobs.names()


async def test_daily_turn_limit_applies_to_members_only(client, fake_llm, monkeypatch) -> None:
    from backend.core.config import settings

    fake_llm(script())
    monkeypatch.setattr(settings, "daily_turn_limit", 1)
    redis = get_redis()
    async for key in redis.scan_iter(match="hg:daily:*"):
        await redis.delete(key)
    member = await login(client, "maria.garcia@demo.healthguard.dev")
    cid = await _conversation(client, member)
    url = f"{API}/conversations/{cid}/messages"
    assert (await client.post(url, json={"message": "hello"}, headers=auth(member))).status_code == 200
    blocked = await client.post(url, json={"message": "hello again"}, headers=auth(member))
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "daily_limit"
    assert int(blocked.headers["retry-after"]) > 0
    admin = await login(client, "admin@demo.healthguard.dev")
    admin_cid = await _conversation(client, admin)
    for _ in range(2):
        resp = await client.post(
            f"{API}/conversations/{admin_cid}/messages", json={"message": "hello"}, headers=auth(admin)
        )
        assert resp.status_code == 200


async def test_rls_applies_to_generated_sql(client, fake_llm) -> None:
    fake_llm(script())
    tokens = await login(client, "maria.garcia@demo.healthguard.dev")
    cid = await _conversation(client, tokens)
    final = (
        await client.post(
            f"{API}/conversations/{cid}/messages",
            json={"message": "Why was my claim denied?"},
            headers=auth(tokens),
        )
    ).json()
    async with SessionLocal() as session:
        msg = await session.get(Message, uuid.UUID(final["message_id"]))
        trace = await session.get(Trace, msg.trace_id)
    synth = next(s for s in trace.steps if s["node"] == "synthesize")
    sources = synth["metadata"]["sources"]
    assert "M100001" in sources and "M100002" not in sources and "M100004" not in sources


async def test_message_history_and_second_turn_context(client, fake_llm) -> None:
    llm = fake_llm(script())
    tokens = await login(client, "maria.garcia@demo.healthguard.dev")
    cid = await _conversation(client, tokens)
    for q in ("Why was my claim denied?", "And how do I appeal it?"):
        resp = await client.post(
            f"{API}/conversations/{cid}/messages", json={"message": q}, headers=auth(tokens)
        )
        assert resp.status_code == 200
    history = (await client.get(f"{API}/conversations/{cid}/messages", headers=auth(tokens))).json()
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    second_understand = [c for c in llm.calls if c["name"] == "understand"][1]
    prompt = second_understand["messages"][-1]["content"]
    assert (
        "User: Why was my claim denied?" in prompt and "<current_message>\nAnd how do I appeal it?" in prompt
    )
    assert (
        "And how do I appeal it?" not in prompt.split("<current_message>")[0]
    )  # current message not duplicated


async def test_other_users_cannot_touch_a_conversation(client, fake_llm) -> None:
    owner = await login(client, "maria.garcia@demo.healthguard.dev")
    intruder = await register(client)
    cid = await _conversation(client, owner)
    h = auth(intruder)
    assert (await client.get(f"{API}/conversations/{cid}/messages", headers=h)).status_code == 404
    assert (
        await client.patch(f"{API}/conversations/{cid}", json={"title": "x"}, headers=h)
    ).status_code == 404
    assert (await client.delete(f"{API}/conversations/{cid}", headers=h)).status_code == 404
    send = await client.post(f"{API}/conversations/{cid}/messages", json={"message": "hi"}, headers=h)
    assert send.status_code == 404
    assert cid not in [c["id"] for c in (await client.get(f"{API}/conversations", headers=h)).json()]


async def test_delete_conversation_clears_checkpoint(client, fake_llm) -> None:
    fake_llm(script())
    tokens = await login(client, "maria.garcia@demo.healthguard.dev")
    cid = await _conversation(client, tokens)
    await client.post(
        f"{API}/conversations/{cid}/messages",
        json={"message": "Why was my claim denied?"},
        headers=auth(tokens),
    )
    config = {"configurable": {"thread_id": cid}}
    assert (await get_graph().aget_state(config)).values
    assert (await client.delete(f"{API}/conversations/{cid}", headers=auth(tokens))).status_code == 204
    assert (await get_graph().aget_state(config)).values == {}
    listed = (await client.get(f"{API}/conversations", headers=auth(tokens))).json()
    assert cid not in [c["id"] for c in listed]


async def test_concurrent_turn_rejected_and_input_validation(client) -> None:
    tokens = await login(client, "maria.garcia@demo.healthguard.dev")
    cid = await _conversation(client, tokens)
    await get_redis().set(f"hg:turn:{cid}", "1", ex=30)
    busy = await client.post(
        f"{API}/conversations/{cid}/messages", json={"message": "hi"}, headers=auth(tokens)
    )
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "turn_in_progress"
    await get_redis().delete(f"hg:turn:{cid}")
    long = await client.post(
        f"{API}/conversations/{cid}/messages", json={"message": "x" * 2500}, headers=auth(tokens)
    )
    assert long.status_code == 400 and long.json()["error"]["code"] == "message_too_long"


async def test_feedback(client, fake_llm, jobs) -> None:
    fake_llm(script())
    tokens = await login(client, "maria.garcia@demo.healthguard.dev")
    cid = await _conversation(client, tokens)
    final = (
        await client.post(
            f"{API}/conversations/{cid}/messages",
            json={"message": "Why was my claim denied?"},
            headers=auth(tokens),
        )
    ).json()
    mid = final["message_id"]
    assert (
        await client.post(f"{API}/messages/{mid}/feedback", json={"rating": 1}, headers=auth(tokens))
    ).status_code == 204
    down = await client.post(
        f"{API}/messages/{mid}/feedback", json={"rating": -1, "comment": "wrong"}, headers=auth(tokens)
    )
    assert down.status_code == 204
    assert ("online_eval", (mid, "thumbs_down"), f"oe:{mid}") in jobs.jobs
    other = await register(client)
    assert (
        await client.post(f"{API}/messages/{mid}/feedback", json={"rating": 1}, headers=auth(other))
    ).status_code == 404


async def test_coverage_card(client) -> None:
    maria = await login(client, "maria.garcia@demo.healthguard.dev")
    card = (await client.get(f"{API}/me/coverage", headers=auth(maria))).json()
    assert card["plan"]["plan_id"] == "PPO-SLV-PLS"
    own = card["accumulators"][0]
    assert own["member_id"] == "M100001" and own["deductible_individual_met"] == 2700.0
    assert {a["member_id"] for a in card["accumulators"]} == {"M100001"}
    admin = await login(client, "admin@demo.healthguard.dev")
    assert (await client.get(f"{API}/me/coverage", headers=auth(admin))).json()["plan"] is None


async def test_admin_endpoints_require_admin(client, fake_llm) -> None:
    fake_llm(script())
    member = await login(client, "maria.garcia@demo.healthguard.dev")
    assert (await client.get(f"{API}/admin/metrics/summary", headers=auth(member))).status_code == 403
    cid = await _conversation(client, member)
    final = (
        await client.post(
            f"{API}/conversations/{cid}/messages",
            json={"message": "Why was my claim denied?"},
            headers=auth(member),
        )
    ).json()
    admin = await login(client, "admin@demo.healthguard.dev")
    summary = (await client.get(f"{API}/admin/metrics/summary?days=1", headers=auth(admin))).json()
    assert summary["totals"]["turns"] >= 1
    assert any(c["call"] == "understand" for c in summary["llm_calls"])
    trace = await client.get(f"{API}/admin/traces/{final['trace_id']}", headers=auth(admin))
    assert trace.status_code == 200 and trace.json()["steps"]
    assert (await client.get(f"{API}/admin/evals/runs", headers=auth(admin))).status_code == 200


async def test_admin_can_queue_an_eval_run(client, jobs) -> None:
    member = await login(client, "maria.garcia@demo.healthguard.dev")
    assert (
        await client.post(f"{API}/admin/evals/run", json={"limit": 5}, headers=auth(member))
    ).status_code == 403
    admin = await login(client, "admin@demo.healthguard.dev")
    resp = await client.post(f"{API}/admin/evals/run", json={"limit": 5}, headers=auth(admin))
    assert resp.status_code == 202 and resp.json()["status"] == "queued"
    run_id = resp.json()["id"]
    assert ("run_offline_eval", (run_id, 5), f"eval:{run_id}") in jobs.jobs
    runs = (await client.get(f"{API}/admin/evals/runs", headers=auth(admin))).json()
    assert runs[0]["id"] == run_id and runs[0]["status"] == "queued"
    second = await client.post(f"{API}/admin/evals/run", json={"limit": 5}, headers=auth(admin))
    assert second.status_code == 409 and second.json()["error"]["code"] == "eval_in_progress"
    async with SessionLocal() as session:
        await session.delete(await session.get(EvalRun, uuid.UUID(run_id)))
        await session.commit()


async def test_turn_deadline_returns_a_fallback_and_frees_the_conversation(
    client, fake_llm, monkeypatch
) -> None:
    import asyncio

    from backend.core.config import settings
    from backend.services import chat_service

    async def slow_graph(*_args, **_kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(settings, "turn_timeout_seconds", 0.2)
    monkeypatch.setattr(chat_service, "run_graph", slow_graph)
    tokens = await login(client, "maria.garcia@demo.healthguard.dev")
    cid = await _conversation(client, tokens)
    resp = await client.post(
        f"{API}/conversations/{cid}/messages/stream", json={"message": "hello"}, headers=auth(tokens)
    )
    events = parse_sse(resp.text)
    assert any(e["type"] == "error" and e["code"] == "turn_timeout" for e in events)
    final = events[-1]
    assert final["type"] == "final" and final["outcome"] == "error" and final["fallback"] == "timeout"
    assert not await get_redis().exists(f"hg:turn:{cid}")
