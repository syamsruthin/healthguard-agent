"""
`hg-eval` — offline evaluation of the full chat pipeline against the golden set.

    hg-eval --suite golden --dry-run            # plan + cost estimate, no model calls
    hg-eval --suite golden --limit 10 --judges 1 --metrics core
    hg-eval --suite golden                      # full run with the whole jury

Each case runs as its persona through the same `chat_service` pipeline the API
uses (evaluation conversations are flagged `is_eval` and excluded from the
dashboards; the semantic cache is off unless `--with-cache`). Answers are then
scored with deterministic checks and the RAGAS judge jury, results are stored
in `eval_runs` / `eval_results`, reports are written to `backend/evals/reports/`,
and the exit code is 1 when the quality gate in `thresholds.yaml` fails.

In Langfuse, every case's turns are tagged `eval` and `eval-run:<run id>`;
the judging is a second root (`eval:<case id>`) in the judged turn's trace,
and the RAGAS metrics and deterministic checks are scores on that trace.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from statistics import mean, quantiles
from typing import Any

import yaml
from sqlalchemy import select

from backend.agents.tools import sandbox
from backend.agents.trace import synthesis_sources
from backend.core import observability as obs
from backend.core.config import settings
from backend.core.logging import configure_logging, get_logger
from backend.core.redis import close_redis
from backend.db.base import utcnow
from backend.db.models import Conversation, EvalResult, EvalRun, Message, Trace, User
from backend.db.session import SessionLocal, dispose
from backend.evals.build_golden import DATASET, load
from backend.evals.checks import aggregate, case_checks
from backend.evals.ragas_jury import ALL_METRICS, CORE_METRICS, Jury
from backend.evals.report import write_reports
from backend.llm.prompts.common import REFERENCE_FACTS
from backend.services import chat_service, conversation_service
from backend.services.user_context import get_user_by_email, load_user_context

log = get_logger(__name__)
THRESHOLDS = Path(__file__).parent / "thresholds.yaml"
_SOURCE_SPLIT = re.compile(r"\n\n(?=\[[SD]\d+\])")

_EARLIER_TURN = "From an earlier turn of this conversation (the user asked: {question})\n"

# Pre-run estimate (USD), calibrated on runs 82775a45 and 60d20114 (agent cost per turn from app.llm_calls,
# judge cost per call from the judges' Langfuse generations) and rounded up, so it over-estimates by ~20–30%:
# * an agent turn averaged $0.004–0.005 on the default Jev Router (single turns up to $0.018) and
#   $0.013–0.039 on fixed Claude models;
# * a judge call cost ~$0.0022 per source the answer draws on: a two-source answer is about twice as long,
#   so its claim lists (and the judges' output) are about twice as long too;
# * judges differ: Gemini (mostly hidden reasoning tokens) cost 1.8x the jury average, GPT 0.8x, Haiku 0.4x.
_TURN_COST = {"router": 0.006, "fixed": 0.03}
_JUDGE_CALLS_PER_METRIC = {
    "faithfulness": 2,
    "answer_relevancy": 1,
    "context_recall": 1,
    "context_precision": 4,
    "factual_correctness": 2,
}
_JUDGE_CALL_COST = 0.0025  # one call by an average judge, per source the answer draws on
_PRIOR_TURN_PARTS = 0.25  # an earlier turn's sources lengthen the judges' contexts (cheap input tokens)
_JUDGE_WEIGHT = {"google/": 1.8, "openai/": 0.8, "anthropic/": 0.4}
_UNKNOWN_JUDGE_WEIGHT = 1.8  # an unmeasured judge is assumed to be as expensive as the dearest one


def select_cases(
    cases: list[dict[str, Any]],
    *,
    categories: list[str] | None = None,
    per_category: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Filter by category, keep at most `per_category` cases of each (a stratified sample), then cap at `limit`."""
    if categories:
        cases = [c for c in cases if c["category"] in categories]
    if per_category:
        seen: dict[str, int] = {}
        kept = []
        for c in cases:
            seen[c["category"]] = seen.get(c["category"], 0) + 1
            if seen[c["category"]] <= per_category:
                kept.append(c)
        cases = kept
    return cases[:limit] if limit else cases


def judged_question(case: dict[str, Any], result: dict[str, Any]) -> str:
    """A follow-up ("…before I hit it?") is judged against its resolved, standalone form."""
    if case.get("prior_turns") and result.get("standalone_query"):
        return str(result["standalone_query"])
    return str(case["question"])


def turn_cost(models: Iterable[str]) -> float:
    """Estimated USD per agent turn: cheap when every role runs on a router, dearer on fixed models."""
    return _TURN_COST["router"] if all("router" in m for m in models) else _TURN_COST["fixed"]


def judge_weight(judge: str) -> float:
    """A judge's measured cost relative to the jury average."""
    return next((w for prefix, w in _JUDGE_WEIGHT.items() if judge.startswith(prefix)), _UNKNOWN_JUDGE_WEIGHT)


def answer_parts(case: dict[str, Any]) -> float:
    """How many sources the judged answer draws on (judge cost grows with it), plus its earlier turns."""
    earlier = len(case.get("prior_turns") or [])
    return max(1, len(case.get("expected_tools") or [])) + _PRIOR_TURN_PARTS * earlier


def estimate_cost(
    cases: list[dict[str, Any]],
    metrics: tuple[str, ...],
    judges: list[str],
    *,
    per_turn: float | None = None,
) -> dict[str, float]:
    """{"agent", "judges", "total"} in USD; blocked cases are not judged."""
    if per_turn is None:
        per_turn = turn_cost((settings.model_fast, settings.model_code, settings.model_synth))
    agent = sum(1 + len(c.get("prior_turns") or []) for c in cases) * per_turn
    parts = sum(answer_parts(c) for c in cases if c["expected_outcome"] != "blocked")
    calls = sum(_JUDGE_CALLS_PER_METRIC[m] for m in metrics)
    judging = parts * calls * _JUDGE_CALL_COST * sum(judge_weight(j) for j in judges)
    return {"agent": round(agent, 2), "judges": round(judging, 2), "total": round(agent + judging, 2)}


def conversation_contexts(turns: list[tuple[str, list[str]]]) -> list[str]:
    """Judge contexts for a case from its turns' (user message, sources), oldest first.

    The judged (last) turn's sources come first. A follow-up builds on facts an earlier turn looked
    up (the agent sees that exchange in its recent turns), so each earlier turn's sources follow,
    labelled with the question that retrieved them. The earlier answers are not added: they are the
    agent's own words, not evidence, and would let a repeated mistake look supported.
    """
    if not turns:
        return []
    *earlier, (_, last) = turns
    return [
        *last,
        *(
            f"{_EARLIER_TURN.format(question=question)}{source}"
            for question, sources in earlier
            for source in sources
        ),
    ]


async def _sources(trace_id: str | None) -> list[str]:
    """The turn's sources, one context per [S#] / [D#] block."""
    if not trace_id:
        return []
    async with SessionLocal() as session:
        trace = await session.get(Trace, uuid.UUID(trace_id))
    text = synthesis_sources(trace.steps if trace else [])
    return [s.strip() for s in _SOURCE_SPLIT.split(text) if s.strip()]


async def grounding_for(session: Any, user: Any) -> list[str]:
    """What the assistant may state without a source tag: the member profile and the reference facts."""
    ctx = await load_user_context(session, user)
    return [f"Member profile: {ctx.profile_text()}", REFERENCE_FACTS]


async def run_case(case: dict[str, Any]) -> dict[str, Any]:
    """Run one golden case (including prior turns) through the chat pipeline as its persona."""
    t0 = time.monotonic()
    async with SessionLocal() as session:
        user = await get_user_by_email(session, case["persona"])
        if user is None:
            raise RuntimeError(f"persona {case['persona']} not found; seed the database first")
        grounding = await grounding_for(session, user)
        conv = await conversation_service.create(session, user, title=f"eval {case['id']}", is_eval=True)
        cost = 0.0
        payload: dict[str, Any] = {}
        turns: list[tuple[str, str | None]] = []
        for message in [*(case.get("prior_turns") or []), case["question"]]:
            prepared = await chat_service.prepare_turn(session, user, conv.id, message)
            payload = await chat_service.run_turn(prepared)
            cost += float(payload["usage"].get("cost_usd", 0))
            turns.append((message, payload.get("trace_id")))
    return {
        "answer": payload["answer"],
        "outcome": payload["outcome"],
        "fallback": payload.get("fallback"),
        "tools": payload.get("tools", []),
        "cache_hit": payload.get("cache_hit", False),
        "contexts": conversation_contexts([(message, await _sources(trace)) for message, trace in turns]),
        "grounding": grounding,
        "standalone_query": payload.get("standalone_query", ""),
        "latency_ms": payload.get("latency_ms") or int((time.monotonic() - t0) * 1000),
        "cost_usd": round(cost, 6),
        "trace_id": payload.get("trace_id"),
    }


def _score_case(trace_id: str | None, checks: dict[str, Any], scores: dict[str, Any]) -> None:
    """Deterministic checks and jury metrics as scores on the judged turn's Langfuse trace."""
    if not trace_id:
        return
    lf_trace = obs.trace_id_for(trace_id)
    for name, value in checks.items():
        if isinstance(value, bool):
            obs.score(lf_trace, f"check_{name}", value)
    for metric, entry in scores.items():
        if entry.get("score") is not None:
            obs.score(
                lf_trace,
                f"ragas_{metric}",
                entry["score"],
                data_type="NUMERIC",
                metadata={
                    "judges": entry.get("judges"),
                    "spread": entry.get("spread"),
                    "failed": entry.get("failed"),
                },
            )


def _gate(metrics: dict[str, float | None]) -> tuple[bool, dict[str, float], list[str]]:
    thresholds: dict[str, float] = yaml.safe_load(THRESHOLDS.read_text())
    failures = [
        f"{name} {value:.3f} < {thresholds[name]:.2f}"
        for name, value in metrics.items()
        if value is not None and name in thresholds and value < thresholds[name]
    ]
    return not failures, thresholds, failures


async def execute(
    *,
    run_id: uuid.UUID | None = None,
    limit: int | None = None,
    categories: list[str] | None = None,
    per_category: int | None = None,
    metrics: tuple[str, ...] = CORE_METRICS,
    judges: list[str] | None = None,
    use_ragas: bool = True,
    concurrency: int = 3,
    triggered_by: uuid.UUID | None = None,
    dataset: Path = DATASET,
    use_semantic_cache: bool = False,
) -> dict[str, Any]:
    previous = settings.semantic_cache_enabled
    settings.semantic_cache_enabled = use_semantic_cache  # replayed answers would hide regressions
    try:
        return await _execute(
            run_id=run_id,
            limit=limit,
            categories=categories,
            per_category=per_category,
            metrics=metrics,
            judges=judges,
            use_ragas=use_ragas,
            concurrency=concurrency,
            triggered_by=triggered_by,
            dataset=dataset,
        )
    finally:
        settings.semantic_cache_enabled = previous


async def _execute(
    *,
    run_id: uuid.UUID | None,
    limit: int | None,
    categories: list[str] | None,
    per_category: int | None,
    metrics: tuple[str, ...],
    judges: list[str] | None,
    use_ragas: bool,
    concurrency: int,
    triggered_by: uuid.UUID | None,
    dataset: Path,
) -> dict[str, Any]:
    cases = select_cases(load(dataset), categories=categories, per_category=per_category, limit=limit)
    judges = judges or list(settings.eval_judges)
    config = {
        "suite": "golden",
        "cases": len(cases),
        "categories": categories,
        "per_category": per_category,
        "metrics": list(metrics) if use_ragas else [],
        "judges": judges if use_ragas else [],
        "semantic_cache": settings.semantic_cache_enabled,
        "models": {"fast": settings.model_fast, "code": settings.model_code, "synth": settings.model_synth},
    }
    async with SessionLocal() as session:
        run = await session.get(EvalRun, run_id) if run_id else None
        if run is None:
            run = EvalRun(id=run_id or uuid.uuid4(), suite="golden", triggered_by=triggered_by)
            session.add(run)
        run.status, run.config, run.started_at = "running", config, utcnow()
        await session.commit()
        run_id = run.id
    log.info("eval_started", run_id=str(run_id), cases=len(cases))
    try:
        jury = Jury(judges, metrics) if use_ragas else None
        sem = asyncio.Semaphore(concurrency)

        async def one(case: dict[str, Any]) -> dict[str, Any]:
            with obs.propagate(
                tags=["eval", f"eval-run:{run_id}"],
                metadata={"eval_run_id": run_id, "eval_case": case["id"], "eval_category": case["category"]},
            ):
                async with sem:
                    try:
                        result = await run_case(case)
                    except Exception as exc:
                        log.exception("eval_case_failed", case=case["id"])
                        return {"case": case, "error": f"{exc.__class__.__name__}: {exc}"[:500]}
                checks = case_checks(case, result)
                scores: dict[str, Any] = {}
                if (
                    jury is not None
                    and case["expected_outcome"] != "blocked"
                    and result["outcome"] in ("answered", "smalltalk")
                ):
                    with obs.observe(
                        f"eval:{case['id']}",
                        as_type="evaluator",
                        trace_id=obs.trace_id_for(result["trace_id"]) if result.get("trace_id") else None,
                        input={"question": judged_question(case, result), "reference": case.get("reference")},
                        metadata={"metrics": list(metrics), "judges": judges},
                    ) as judging:
                        scores = await jury.score(
                            question=judged_question(case, result),
                            answer=result["answer"],
                            contexts=result["contexts"],
                            reference=case.get("reference", ""),
                            grounding=result.get("grounding", []),
                        )
                        judging.update(output={m: e.get("score") for m, e in scores.items()})
                _score_case(result.get("trace_id"), checks, scores)
            return {"case": case, "result": result, "checks": checks, "scores": scores}

        rows = await asyncio.gather(*(one(c) for c in cases))
        summary = _summarize(rows, len(judges) if use_ragas else 0)
        summary["judge_cost_usd"] = judge_cost(jury)
        passed, thresholds, failures = _gate(summary["metrics"])
        summary.update(thresholds=thresholds, gate_failures=failures)
        await _store(run_id, rows, len(judges) if use_ragas else 0)
        report_path = write_reports(run_id, config, summary, rows, passed)
        async with SessionLocal() as session:
            run = await session.get(EvalRun, run_id)
            assert run is not None
            run.status, run.summary, run.passed = "completed", summary, passed
            run.report_path, run.finished_at = str(report_path), utcnow()
            await session.commit()
        log.info("eval_finished", run_id=str(run_id), passed=passed, failures=failures)
        return {"run_id": str(run_id), "passed": passed, "summary": summary, "report": str(report_path)}
    except Exception as exc:
        async with SessionLocal() as session:
            run = await session.get(EvalRun, run_id)
            if run is not None:
                run.status, run.error, run.finished_at = "failed", str(exc)[:2000], utcnow()
                await session.commit()
        raise


def judge_cost(jury: Any) -> float:
    """USD the jury's judge calls cost in this process (cached judge responses are free)."""
    spend = getattr(jury, "spend", None)
    return round(spend.cost_usd, 4) if spend is not None else 0.0


def flag_disagreement(scores: dict[str, Any], n_judges: int) -> bool:
    """Judges disagree beyond the threshold, or fewer than two judges could score a metric."""
    for metric in scores.values():
        if (metric.get("spread") or 0) > settings.judge_disagreement_threshold:
            return True
        if n_judges > 1 and len(metric.get("judges", {})) < 2:
            return True
    return False


def _summarize(rows: list[dict[str, Any]], n_judges: int) -> dict[str, Any]:
    ok = [r for r in rows if "error" not in r]
    metrics: dict[str, float | None] = (
        aggregate([r["case"] for r in ok], [r["checks"] for r in ok]) if ok else {}
    )
    for metric in ALL_METRICS:
        values = [
            r["scores"][metric]["score"] for r in ok if r["scores"].get(metric, {}).get("score") is not None
        ]
        if values:
            metrics[metric] = round(mean(values), 4)
    latencies = sorted(r["result"]["latency_ms"] for r in ok)
    costs = [r["result"]["cost_usd"] for r in ok]
    flagged = [r["case"]["id"] for r in ok if flag_disagreement(r["scores"], n_judges)]
    by_category: dict[str, dict[str, Any]] = {}
    for r in ok:
        cat = by_category.setdefault(r["case"]["category"], {"cases": 0, "passed": 0})
        cat["cases"] += 1
        cat["passed"] += int(all(v is not False for v in r["checks"].values()))
    return {
        "cases": len(rows),
        "errors": len(rows) - len(ok),
        "metrics": {k: v for k, v in metrics.items() if v is not None},
        "latency_p50_ms": int(quantiles(latencies, n=2)[0])
        if len(latencies) > 1
        else (latencies[0] if latencies else 0),
        "latency_p95_ms": int(quantiles(latencies, n=20)[-1])
        if len(latencies) > 1
        else (latencies[0] if latencies else 0),
        "cost_usd": round(sum(costs), 4),
        "cost_per_case_usd": round(mean(costs), 4) if costs else 0.0,
        "cache_hit_rate": round(mean(1.0 if r["result"]["cache_hit"] else 0.0 for r in ok), 4) if ok else 0.0,
        "flagged": flagged,
        "by_category": by_category,
    }


async def _store(run_id: uuid.UUID, rows: list[dict[str, Any]], n_judges: int) -> None:
    async with SessionLocal() as session:
        for r in rows:
            case, result = r["case"], r.get("result", {})
            session.add(
                EvalResult(
                    run_id=run_id,
                    case_id=case["id"],
                    category=case["category"],
                    persona=case["persona"],
                    question=case["question"],
                    answer=result.get("answer", ""),
                    reference=case.get("reference") or None,
                    contexts=result.get("contexts", []),
                    expected_tools=case.get("expected_tools", []),
                    actual_tools=sorted({t["tool"] for t in result.get("tools", [])}),
                    scores=r.get("scores", {}),
                    checks=r.get("checks", {}),
                    latency_ms=result.get("latency_ms"),
                    cost_usd=Decimal(str(result.get("cost_usd", 0))),
                    flagged=flag_disagreement(r.get("scores", {}), n_judges),
                    error=r.get("error"),
                )
            )
        await session.commit()


async def _eval_turns(
    session: Any, source: EvalRun, result: EvalResult
) -> tuple[list[tuple[str, str | None]], str] | None:
    """(user message, trace id) per turn of the conversation `source` ran a case in, and its last standalone query.

    Results don't store trace ids, so the conversation is found by the runner's title, persona and time window.
    """
    query = (
        select(Conversation.id)
        .join(User, User.id == Conversation.user_id)
        .where(
            Conversation.title == f"eval {result.case_id}",
            Conversation.is_eval.is_(True),
            User.email == result.persona,
            Conversation.created_at <= result.created_at,
        )
        .order_by(Conversation.created_at.desc())
        .limit(1)
    )
    if source.started_at is not None:
        query = query.where(Conversation.created_at >= source.started_at)
    conversation_id = (await session.execute(query)).scalar_one_or_none()
    if conversation_id is None:
        return None
    messages = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.role.desc())
        )
    ).scalars()
    turns: list[tuple[str, str | None]] = []
    question, standalone = "", ""
    for m in messages:
        if m.role == "user":
            question = m.content
        else:
            turns.append((question, str(m.trace_id) if m.trace_id else None))
            standalone = m.standalone_query or ""
    return (turns, standalone) if turns else None


async def rescore(
    source_run_id: uuid.UUID,
    *,
    metrics: tuple[str, ...] = CORE_METRICS,
    judges: list[str] | None = None,
    dataset: Path = DATASET,
) -> dict[str, Any]:
    """Re-judge the stored answers of an earlier run (no agent calls) into a new run."""
    golden = {c["id"]: c for c in load(dataset)}
    judges = judges or list(settings.eval_judges)
    async with SessionLocal() as session:
        source = await session.get(EvalRun, source_run_id)
        if source is None:
            raise ValueError(f"evaluation run {source_run_id} not found")
        stored = (
            (await session.execute(select(EvalResult).where(EvalResult.run_id == source_run_id)))
            .scalars()
            .all()
        )
        groundings: dict[str, list[str]] = {}
        for r in stored:
            if r.persona and r.persona not in groundings:
                user = await get_user_by_email(session, r.persona)
                groundings[r.persona] = await grounding_for(session, user) if user else [REFERENCE_FACTS]
        # Follow-ups are judged like `execute` judges them: on the resolved question, with the earlier turns'
        # sources (runs from before those were stored only kept the last turn's).
        followups: dict[str, tuple[list[str], str]] = {}
        for r in stored:
            if r.error or not (golden.get(r.case_id) or {}).get("prior_turns"):
                continue
            found = await _eval_turns(session, source, r)
            if found:
                turns, standalone = found
                contexts = conversation_contexts([(m, await _sources(t)) for m, t in turns])
                followups[r.case_id] = (contexts, standalone or r.question)
        config = {
            **source.config,
            "rescore_of": str(source_run_id),
            "metrics": list(metrics),
            "judges": judges,
        }
        run = EvalRun(suite=source.suite, status="running", config=config, started_at=utcnow())
        session.add(run)
        await session.commit()
        run_id = run.id
    jury = Jury(judges, metrics)

    async def one(r: EvalResult) -> dict[str, Any]:
        case = golden.get(r.case_id) or {
            "id": r.case_id,
            "category": r.category,
            "persona": r.persona,
            "question": r.question,
            "reference": r.reference or "",
            "expected_outcome": "answered",
            "expected_tools": r.expected_tools,
        }
        if r.error:
            return {"case": case, "error": r.error}
        answered = case["expected_outcome"] != "blocked" and bool(r.checks.get("outcome"))
        grounding = groundings.get(r.persona or "", [REFERENCE_FACTS])
        contexts, question = followups.get(r.case_id, (r.contexts, r.question))
        result: dict[str, Any] = {
            "answer": r.answer,
            "outcome": "answered" if answered else "blocked",
            "tools": [{"tool": t} for t in r.actual_tools],
            "cache_hit": False,
            "contexts": contexts,
            "grounding": grounding,
            "latency_ms": r.latency_ms or 0,
            "cost_usd": float(r.cost_usd or 0),
        }
        scores = (
            await jury.score(
                question=question,
                answer=r.answer,
                contexts=contexts,
                reference=case.get("reference", ""),
                grounding=grounding,
            )
            if answered
            else {}
        )
        return {"case": case, "result": result, "checks": r.checks, "scores": scores}

    rows = await asyncio.gather(*(one(r) for r in stored))
    summary = _summarize(rows, len(judges))
    summary["judge_cost_usd"] = judge_cost(jury)
    passed, thresholds, failures = _gate(summary["metrics"])
    summary.update(thresholds=thresholds, gate_failures=failures)
    await _store(run_id, rows, len(judges))
    report_path = write_reports(run_id, config, summary, rows, passed)
    async with SessionLocal() as session:
        finished = await session.get(EvalRun, run_id)
        assert finished is not None
        finished.status, finished.summary, finished.passed = "completed", summary, passed
        finished.report_path, finished.finished_at = str(report_path), utcnow()
        await session.commit()
    return {"run_id": str(run_id), "passed": passed, "summary": summary, "report": str(report_path)}


def _parse_metrics(value: str) -> tuple[str, ...]:
    if value == "core":
        return CORE_METRICS
    if value == "all":
        return ALL_METRICS
    return tuple(m.strip() for m in value.split(",") if m.strip())


async def _main(args: argparse.Namespace) -> int:
    configure_logging()
    metrics = _parse_metrics(args.metrics)
    judges = list(settings.eval_judges)[: args.judges] if args.judges else list(settings.eval_judges)
    categories = args.categories.split(",") if args.categories else None
    cases = select_cases(load(), categories=categories, per_category=args.per_category, limit=args.limit)
    if args.rescore:
        print(f"Re-judging stored answers of run {args.rescore} · metrics {list(metrics)} · judges {judges}")
        try:
            outcome = await rescore(uuid.UUID(args.rescore), metrics=metrics, judges=judges)
        finally:
            obs.shutdown()
            await close_redis()
            await dispose()
        return _print_outcome(outcome)
    estimate = estimate_cost(cases, () if args.no_ragas else metrics, [] if args.no_ragas else judges)
    print(
        f"Golden suite: {len(cases)} cases · metrics {list(metrics) if not args.no_ragas else 'none'} · "
        f"judges {judges if not args.no_ragas else 'none'} · estimated cost ≈ ${estimate['total']:.2f} "
        f"(agent ${estimate['agent']:.2f} + judges ${estimate['judges']:.2f})"
    )
    if args.dry_run:
        return 0
    try:
        outcome = await execute(
            limit=args.limit,
            categories=categories,
            per_category=args.per_category,
            metrics=metrics,
            judges=judges,
            use_ragas=not args.no_ragas,
            concurrency=args.concurrency,
            use_semantic_cache=args.with_cache,
        )
    finally:
        sandbox.shutdown()
        obs.shutdown()
        await close_redis()
        await dispose()
    return _print_outcome(outcome)


def _print_outcome(outcome: dict[str, Any]) -> int:
    s = outcome["summary"]
    print(
        json.dumps(
            {
                k: s.get(k)
                for k in (
                    "cases",
                    "errors",
                    "metrics",
                    "cost_usd",
                    "judge_cost_usd",
                    "latency_p50_ms",
                    "latency_p95_ms",
                )
            },
            indent=2,
        )
    )
    print(f"{'✅ PASSED' if outcome['passed'] else '❌ FAILED'} — report: {outcome['report']}")
    for failure in s["gate_failures"]:
        print(f"   gate: {failure}")
    return 0 if outcome["passed"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the golden evaluation suite")
    parser.add_argument("--suite", default="golden", choices=["golden"])
    parser.add_argument("--limit", type=int, default=None, help="run only the first N cases")
    parser.add_argument(
        "--per-category", type=int, default=None, help="at most N cases per category (stratified sample)"
    )
    parser.add_argument("--categories", default="", help="comma-separated categories to include")
    parser.add_argument("--metrics", default="core", help="core | all | comma-separated RAGAS metric names")
    parser.add_argument("--judges", type=int, default=0, help="use only the first N judges of EVAL_JUDGES")
    parser.add_argument("--no-ragas", action="store_true", help="deterministic checks only (no judge calls)")
    parser.add_argument("--with-cache", action="store_true", help="keep the semantic answer cache on")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="print the plan and cost estimate only")
    parser.add_argument(
        "--rescore", default="", help="re-judge the stored answers of this run id (no agent calls)"
    )
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
