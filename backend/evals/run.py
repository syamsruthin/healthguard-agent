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
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from statistics import mean, quantiles
from typing import Any

import yaml
from sqlalchemy import select

from backend.agents.tools import sandbox
from backend.core.config import settings
from backend.core.logging import configure_logging, get_logger
from backend.core.redis import close_redis
from backend.db.base import utcnow
from backend.db.models import EvalResult, EvalRun, Trace
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

# Rough per-call costs (USD) for the default models, used only for the pre-run estimate.
_TURN_COST = 0.025
_JUDGE_CALLS_PER_METRIC = {
    "faithfulness": 2,
    "answer_relevancy": 1,
    "context_recall": 1,
    "context_precision": 4,
    "factual_correctness": 2,
}
_JUDGE_CALL_COST = 0.0015


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


def estimate_cost(cases: list[dict[str, Any]], metrics: tuple[str, ...], judges: int) -> float:
    turns = sum(1 + len(c.get("prior_turns") or []) for c in cases)
    scored = sum(1 for c in cases if c["expected_outcome"] != "blocked")
    judge_calls = scored * judges * sum(_JUDGE_CALLS_PER_METRIC[m] for m in metrics)
    return round(turns * _TURN_COST + judge_calls * _JUDGE_CALL_COST, 2)


async def _sources(trace_id: str | None) -> list[str]:
    if not trace_id:
        return []
    async with SessionLocal() as session:
        trace = await session.get(Trace, uuid.UUID(trace_id))
    for step in reversed(trace.steps if trace else []):
        if step.get("node") == "synthesize":
            text = step.get("metadata", {}).get("sources", "")
            return [s.strip() for s in _SOURCE_SPLIT.split(text) if s.strip()]
    return []


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
        for message in [*(case.get("prior_turns") or []), case["question"]]:
            prepared = await chat_service.prepare_turn(session, user, conv.id, message)
            payload = await chat_service.run_turn(prepared)
            cost += float(payload["usage"].get("cost_usd", 0))
    return {
        "answer": payload["answer"],
        "outcome": payload["outcome"],
        "fallback": payload.get("fallback"),
        "tools": payload.get("tools", []),
        "cache_hit": payload.get("cache_hit", False),
        "contexts": await _sources(payload.get("trace_id")),
        "grounding": grounding,
        "latency_ms": payload.get("latency_ms") or int((time.monotonic() - t0) * 1000),
        "cost_usd": round(cost, 6),
    }


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
                scores = await jury.score(
                    question=case["question"],
                    answer=result["answer"],
                    contexts=result["contexts"],
                    reference=case.get("reference", ""),
                    grounding=result.get("grounding", []),
                )
            return {"case": case, "result": result, "checks": checks, "scores": scores}

        rows = await asyncio.gather(*(one(c) for c in cases))
        summary = _summarize(rows, len(judges) if use_ragas else 0)
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
        result: dict[str, Any] = {
            "answer": r.answer,
            "outcome": "answered" if answered else "blocked",
            "tools": [{"tool": t} for t in r.actual_tools],
            "cache_hit": False,
            "contexts": r.contexts,
            "grounding": grounding,
            "latency_ms": r.latency_ms or 0,
            "cost_usd": float(r.cost_usd or 0),
        }
        scores = (
            await jury.score(
                question=r.question,
                answer=r.answer,
                contexts=r.contexts,
                reference=case.get("reference", ""),
                grounding=grounding,
            )
            if answered
            else {}
        )
        return {"case": case, "result": result, "checks": r.checks, "scores": scores}

    rows = await asyncio.gather(*(one(r) for r in stored))
    summary = _summarize(rows, len(judges))
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
    estimate = estimate_cost(cases, metrics if not args.no_ragas else (), 0 if args.no_ragas else len(judges))
    print(
        f"Golden suite: {len(cases)} cases · metrics {list(metrics) if not args.no_ragas else 'none'} · "
        f"judges {judges if not args.no_ragas else 'none'} · estimated cost ≈ ${estimate:.2f}"
    )
    if args.rescore:
        try:
            outcome = await rescore(uuid.UUID(args.rescore), metrics=metrics, judges=judges)
        finally:
            await close_redis()
            await dispose()
        return _print_outcome(outcome)
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
        await close_redis()
        await dispose()
    return _print_outcome(outcome)


def _print_outcome(outcome: dict[str, Any]) -> int:
    s = outcome["summary"]
    print(
        json.dumps(
            {k: s[k] for k in ("cases", "errors", "metrics", "cost_usd", "latency_p50_ms", "latency_p95_ms")},
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
