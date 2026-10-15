"""
RAGAS scoring with a jury of judges from different model families.

Every metric is scored once per judge (EVAL_JUDGES, via OpenRouter); the case
score is the mean over the judges that succeeded, the spread (max − min)
flags disagreement, and `failed` says why a judge has no score. Judge responses are cached on disk, so re-running
unchanged cases costs nothing. RAGAS calls OpenRouter with its own client, so
a response hook (`SpendMeter`) adds up the cost OpenRouter reports per call
and records each judge call as a Langfuse generation under its metric.
(`langfuse.openai` is not used: it patches the OpenAI SDK process-wide and
would double-trace the agent's own calls.)
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from statistics import mean
from typing import Any

from instructor.core import IncompleteOutputException
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from ragas.cache import DiskCacheBackend
from ragas.embeddings import OpenAIEmbeddings
from ragas.llms import llm_factory
from ragas.metrics.collections import (
    AnswerRelevancy,
    ContextPrecisionWithReference,
    ContextRecall,
    FactualCorrectness,
    Faithfulness,
)

from backend.core import observability as obs
from backend.core.config import settings
from backend.core.logging import get_logger

log = get_logger(__name__)
ALL_METRICS = (
    "faithfulness",
    "answer_relevancy",
    "context_recall",
    "context_precision",
    "factual_correctness",
)
CORE_METRICS = ("faithfulness", "answer_relevancy", "context_recall")  # cheapest informative set
CACHE_DIR = Path(__file__).parent / ".cache"
JUDGE_MAX_TOKENS = 8192  # long answers produce long claim lists (4,096 still truncated 2 of 21 calls)
# Hidden reasoning counts against max_tokens. In run 60d20114 both truncated calls were Gemini faithfulness
# verdicts on long multi-part answers: 8,185 output tokens, of which under 1,000 were the visible JSON. A
# bigger cap would only buy more thinking (Gemini was already 60% of the judge bill), so its effort is capped.
# GPT barely reasons on these prompts and Haiku doesn't by default (a reasoning setting would switch it on).
JUDGE_REASONING: dict[str, dict[str, str]] = {"google/": {"effort": "low"}}


def judge_model_args(judge: str) -> dict[str, Any]:
    """Model arguments for a judge's calls (OpenRouter's `reasoning` goes in the request body)."""
    args: dict[str, Any] = {"max_tokens": JUDGE_MAX_TOKENS}
    reasoning = next((r for prefix, r in JUDGE_REASONING.items() if judge.startswith(prefix)), None)
    if reasoning:
        args["extra_body"] = {"reasoning": reasoning}
    return args


def failure_reason(exc: Exception) -> str:
    """A short label for why a judge produced no score ("max_tokens" when its output was cut off)."""
    if isinstance(exc, IncompleteOutputException) or "max_tokens" in str(exc):
        return "max_tokens"
    return exc.__class__.__name__


class SpendMeter:
    """Sums `usage.cost` from every judge response (OpenRouter includes it on each call)."""

    def __init__(self) -> None:
        self.cost_usd = 0.0
        self.calls = 0

    async def on_response(self, response: Any) -> None:  # the SDK's httpx response
        if response.request.method != "POST" or response.status_code != 200:
            return
        if "text/event-stream" in response.headers.get("content-type", ""):
            return
        await response.aread()  # httpx keeps the body, so the SDK can still parse it
        try:
            body = response.json()
        except ValueError:
            return
        usage = body.get("usage") or {}
        self.calls += 1
        self.cost_usd += float(usage.get("cost") or 0.0)
        _trace_judge_call(response, body)


def _trace_judge_call(response: Any, body: dict[str, Any]) -> None:
    """A judge call as a generation under the current metric observation (only inside a trace)."""
    if not obs.current().active:
        return
    try:
        request = json.loads(response.request.content or b"{}")
    except ValueError:
        request = {}
    usage = body.get("usage") or {}
    choices = body.get("choices") or []
    gen = obs.start(
        "llm.judge" if "messages" in request else "embed.judge",
        as_type="generation" if "messages" in request else "embedding",
        input=request.get("messages") or request.get("input"),
        model=body.get("model") or request.get("model"),
        model_parameters={"max_tokens": request.get("max_tokens"), "temperature": request.get("temperature")},
        metadata={
            "requested_model": request.get("model"),
            "latency_ms": int(response.elapsed.total_seconds() * 1000),
        },
    )
    gen.update(
        output=choices[0].get("message", {}).get("content") if choices else None,
        usage_details=obs.usage_details(
            {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
            }
        ),
        cost_details=obs.cost_details({"cost_usd": usage.get("cost") or 0.0}),
    )
    gen.end()


class Jury:
    def __init__(self, judges: list[str], metrics: tuple[str, ...] = CORE_METRICS, concurrency: int = 4):
        unknown = set(metrics) - set(ALL_METRICS)
        if unknown:
            raise ValueError(f"unknown metrics: {sorted(unknown)}")
        self.judges = judges
        self.metrics = metrics
        self._sem = asyncio.Semaphore(concurrency)
        self.spend = SpendMeter()
        client = AsyncOpenAI(
            api_key=settings.openrouter_api_key or "not-configured",
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            default_headers={"HTTP-Referer": settings.openrouter_app_url, "X-Title": "HealthGuard evals"},
            http_client=DefaultAsyncHttpxClient(event_hooks={"response": [self.spend.on_response]}),
        )
        embeddings = OpenAIEmbeddings(client=client, model=settings.embedding_model)
        self._scorers: dict[str, dict[str, Any]] = {}
        for judge in judges:
            # One cache per judge so a judge can never be served another judge's response; a generous
            # output budget because faithfulness lists every claim (the default cap truncated long answers).
            cache = DiskCacheBackend(str(CACHE_DIR / re.sub(r"[^a-z0-9]+", "_", judge.lower())))
            llm = llm_factory(
                judge, client=client, adapter="instructor", cache=cache, **judge_model_args(judge)
            )
            self._scorers[judge] = {
                "faithfulness": Faithfulness(llm=llm),
                "answer_relevancy": AnswerRelevancy(llm=llm, embeddings=embeddings),
                "context_recall": ContextRecall(llm=llm),
                "context_precision": ContextPrecisionWithReference(llm=llm),
                "factual_correctness": FactualCorrectness(llm=llm),
            }

    @staticmethod
    def _inputs(
        metric: str, q: str, a: str, ctx: list[str], ref: str, grounding: list[str]
    ) -> dict[str, Any] | None:
        """Arguments for a metric, or None when the case lacks what the metric needs.

        Faithfulness also sees `grounding` (the member profile and the reference facts the
        assistant is allowed to use); recall and precision judge retrieval, so they see `ctx` only.
        """
        if metric == "faithfulness":
            return {"user_input": q, "response": a, "retrieved_contexts": [*ctx, *grounding]} if ctx else None
        if metric == "answer_relevancy":
            return {"user_input": q, "response": a}
        if metric == "context_recall":
            return {"user_input": q, "retrieved_contexts": ctx, "reference": ref} if ctx and ref else None
        if metric == "context_precision":
            return {"user_input": q, "reference": ref, "retrieved_contexts": ctx} if ctx and ref else None
        if metric == "factual_correctness":
            return {"response": a, "reference": ref} if ref else None
        return None

    async def _one(self, judge: str, metric: str, kwargs: dict[str, Any]) -> float | str | None:
        """The judge's score, or why it failed (None when the metric returned no value)."""
        async with self._sem:
            with obs.observe(
                f"{metric} · {judge}", as_type="evaluator", metadata={"metric": metric, "judge": judge}
            ) as o:
                try:
                    result = await self._scorers[judge][metric].ascore(**kwargs)
                    value = float(result.value) if result.value is not None else None
                    o.update(output=value, metadata={"reason": getattr(result, "reason", None)})
                    return value
                except Exception as exc:  # one failing judge / metric must not abort the run
                    reason = failure_reason(exc)
                    log.warning(
                        "judge_metric_failed", judge=judge, metric=metric, reason=reason, error=str(exc)[:200]
                    )
                    o.fail(exc, level="WARNING")
                    return reason

    async def score(
        self,
        *,
        question: str,
        answer: str,
        contexts: list[str],
        reference: str,
        grounding: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """{metric: {"score": mean, "judges": {judge: value}, "spread": max-min}} for applicable metrics,
        plus "failed": {judge: reason} when a judge errored (e.g. "max_tokens")."""
        jobs: list[tuple[str, str, Any]] = []
        for metric in self.metrics:
            kwargs = self._inputs(metric, question, answer, contexts, reference, grounding or [])
            if kwargs is None:
                continue
            for judge in self.judges:
                jobs.append((metric, judge, self._one(judge, metric, kwargs)))
        values = await asyncio.gather(*(j[2] for j in jobs))
        out: dict[str, dict[str, Any]] = {}
        for (metric, judge, _), value in zip(jobs, values, strict=True):
            entry = out.setdefault(metric, {"judges": {}})
            if isinstance(value, str):
                entry.setdefault("failed", {})[judge] = value
            elif value is not None:
                entry["judges"][judge] = round(value, 4)
        for entry in out.values():
            scores = list(entry["judges"].values())
            entry["score"] = round(mean(scores), 4) if scores else None
            entry["spread"] = round(max(scores) - min(scores), 4) if len(scores) > 1 else 0.0
        return out
