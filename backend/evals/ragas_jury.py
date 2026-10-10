"""
RAGAS scoring with a jury of judges from different model families.

Every metric is scored once per judge (EVAL_JUDGES, via OpenRouter); the case
score is the mean over the judges that succeeded, and the spread (max − min)
flags disagreement. Judge responses are cached on disk, so re-running
unchanged cases costs nothing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from statistics import mean
from typing import Any

from openai import AsyncOpenAI
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


class Jury:
    def __init__(self, judges: list[str], metrics: tuple[str, ...] = CORE_METRICS, concurrency: int = 4):
        unknown = set(metrics) - set(ALL_METRICS)
        if unknown:
            raise ValueError(f"unknown metrics: {sorted(unknown)}")
        self.judges = judges
        self.metrics = metrics
        self._sem = asyncio.Semaphore(concurrency)
        client = AsyncOpenAI(
            api_key=settings.openrouter_api_key or "not-configured",
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            default_headers={"HTTP-Referer": settings.openrouter_app_url, "X-Title": "HealthGuard evals"},
        )
        cache = DiskCacheBackend(str(CACHE_DIR))
        embeddings = OpenAIEmbeddings(client=client, model=settings.embedding_model)
        self._scorers: dict[str, dict[str, Any]] = {}
        for judge in judges:
            llm = llm_factory(judge, client=client, adapter="instructor", cache=cache)
            self._scorers[judge] = {
                "faithfulness": Faithfulness(llm=llm),
                "answer_relevancy": AnswerRelevancy(llm=llm, embeddings=embeddings),
                "context_recall": ContextRecall(llm=llm),
                "context_precision": ContextPrecisionWithReference(llm=llm),
                "factual_correctness": FactualCorrectness(llm=llm),
            }

    @staticmethod
    def _inputs(metric: str, q: str, a: str, ctx: list[str], ref: str) -> dict[str, Any] | None:
        """Arguments for a metric, or None when the case lacks what the metric needs."""
        if metric == "faithfulness":
            return {"user_input": q, "response": a, "retrieved_contexts": ctx} if ctx else None
        if metric == "answer_relevancy":
            return {"user_input": q, "response": a}
        if metric == "context_recall":
            return {"user_input": q, "retrieved_contexts": ctx, "reference": ref} if ctx and ref else None
        if metric == "context_precision":
            return {"user_input": q, "reference": ref, "retrieved_contexts": ctx} if ctx and ref else None
        if metric == "factual_correctness":
            return {"response": a, "reference": ref} if ref else None
        return None

    async def _one(self, judge: str, metric: str, kwargs: dict[str, Any]) -> float | None:
        async with self._sem:
            try:
                result = await self._scorers[judge][metric].ascore(**kwargs)
                return float(result.value) if result.value is not None else None
            except Exception as exc:  # one failing judge / metric must not abort the run
                log.warning("judge_metric_failed", judge=judge, metric=metric, error=str(exc)[:200])
                return None

    async def score(
        self, *, question: str, answer: str, contexts: list[str], reference: str
    ) -> dict[str, dict[str, Any]]:
        """{metric: {"score": mean, "judges": {judge: value}, "spread": max-min}} for applicable metrics."""
        jobs: list[tuple[str, str, Any]] = []
        for metric in self.metrics:
            kwargs = self._inputs(metric, question, answer, contexts, reference)
            if kwargs is None:
                continue
            for judge in self.judges:
                jobs.append((metric, judge, self._one(judge, metric, kwargs)))
        values = await asyncio.gather(*(j[2] for j in jobs))
        out: dict[str, dict[str, Any]] = {}
        for (metric, judge, _), value in zip(jobs, values, strict=True):
            entry = out.setdefault(metric, {"judges": {}})
            if value is not None:
                entry["judges"][judge] = round(value, 4)
        for entry in out.values():
            scores = list(entry["judges"].values())
            entry["score"] = round(mean(scores), 4) if scores else None
            entry["spread"] = round(max(scores) - min(scores), 4) if len(scores) > 1 else 0.0
        return out
