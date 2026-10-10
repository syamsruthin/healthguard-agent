"""
`hg-chat` — run agent turns in-process from the terminal (no API needed).

    hg-chat --as maria.garcia@demo.healthguard.dev "Why was my MRI denied?" "How do I appeal it?"
    hg-chat --as admin@demo.healthguard.dev --repeat 2 "Top 5 denial reasons this year"

Each positional question is one turn of the same conversation. `--repeat N`
asks every question N times (fresh conversation each time) to measure
provider prompt-cache hits. Prints the live event stream, the answer, and
per-call token / prompt-cache / cost accounting.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from backend.agents.graph import build_graph
from backend.agents.runner import run_graph
from backend.agents.state import initial_state
from backend.agents.tools import sandbox
from backend.cache.semantic import maybe_store
from backend.core.logging import configure_logging
from backend.core.redis import close_redis
from backend.db.session import SessionLocal, dispose, get_data_version
from backend.llm.usage import start_collection, stop_collection
from backend.services.user_context import get_user_by_email, load_user_context


def _printer(verbose: bool) -> Any:
    async def sink(event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "token":
            sys.stdout.write(event["text"])
            sys.stdout.flush()
        elif kind == "plan":
            print(f"\n  ▸ plan (round {event['round']}): {event['standalone_query']}")
            for s in event["steps"]:
                deps = f" ← {','.join(s['depends_on'])}" if s["depends_on"] else ""
                print(f"      {s['id']} [{s['tool']}] {s['task']}{deps}")
        elif kind == "tool" and event["status"] != "running":
            extra = f" ({event['error_code']})" if event.get("error_code") else ""
            print(
                f"  ▸ {event['step_id']} {event['tool']}: {event['status']}{extra}, "
                f"{event.get('row_count', 0)} rows, {event.get('duration_ms', 0)} ms, attempts {event.get('attempts')}"
            )
        elif kind in ("answer_reset", "answer_replaced"):
            print(f"\n  ▸ {kind}: {json.dumps({k: v for k, v in event.items() if k != 'type'})[:300]}")
        elif kind == "status" and verbose:
            print(f"  · {event['message']}")

    return sink


async def _run(args: argparse.Namespace) -> int:
    async with SessionLocal() as session:
        user = await get_user_by_email(session, args.user)
        if user is None:
            print(f"No user {args.user!r}; run `hg-seed` first.", file=sys.stderr)
            return 1
        ctx = await load_user_context(session, user)
    data_version = await get_data_version()
    graph = build_graph(InMemorySaver())
    for rep in range(args.repeat):
        conversation_id = str(uuid.uuid4())
        history: list[str] = []
        for question in args.questions:
            print(f"\n━━ [{ctx.email} · run {rep + 1}] {question}")
            collector, token = start_collection()
            try:
                state = initial_state(
                    turn_id=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    user=ctx.as_dict(),
                    message=question,
                    recent_turns="\n".join(history[-6:]),
                    data_version=data_version,
                )
                final = await run_graph(graph, state, thread_id=conversation_id, sink=_printer(args.verbose))
                if final.get("cache_hit") or final.get("fallback"):
                    print(final.get("answer", ""))
                stored = await maybe_store(final, ctx)
            finally:
                stop_collection(token)
            v = final.get("verification") or {}
            print(
                f"\n\n  outcome={final.get('outcome')} fallback={final.get('fallback') or '-'} "
                f"cache_hit={final.get('cache_hit')} stored={stored or '-'} replans={final.get('replans')} "
                f"grounded={v.get('grounded')} complete={v.get('complete')}"
            )
            if v.get("unsupported_claims"):
                print(f"  unsupported: {v['unsupported_claims']}")
            print(
                f"  {'call':<18} {'model':<30} {'prompt':>7} {'cached':>7} {'write':>6} {'out':>5} {'ms':>6} {'$':>9}"
            )
            for c in collector.calls:
                print(
                    f"  {c.name:<18} {c.model[:30]:<30} {c.prompt_tokens:>7} {c.cached_tokens:>7} "
                    f"{c.cache_write_tokens:>6} {c.completion_tokens:>5} {c.latency_ms:>6} {c.cost_usd:>9.5f}"
                    + (" (exact-cache)" if c.exact_cache_hit else "")
                )
            t = collector.totals
            print(
                f"  {'TOTAL':<18} {'':<30} {int(t['prompt_tokens']):>7} {int(t['cached_tokens']):>7} "
                f"{int(t['cache_write_tokens']):>6} {int(t['completion_tokens']):>5} {'':>6} {t['cost_usd']:>9.5f}"
            )
            history += [f"User: {question}", f"Assistant: {final.get('answer', '')[:1200]}"]
    return 0


async def _main(args: argparse.Namespace) -> int:
    configure_logging()
    try:
        return await _run(args)
    finally:
        sandbox.shutdown()
        await close_redis()
        await dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run HealthGuard agent turns in-process")
    parser.add_argument("--as", dest="user", required=True, help="login email of the persona to act as")
    parser.add_argument("--repeat", type=int, default=1, help="run the whole conversation N times")
    parser.add_argument("-v", "--verbose", action="store_true", help="also print node status events")
    parser.add_argument("questions", nargs="+")
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
