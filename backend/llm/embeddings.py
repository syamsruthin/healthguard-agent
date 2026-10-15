"""
Embedding providers.

* `openrouter` (default) — OpenRouter /embeddings (e.g. openai/text-embedding-3-small, 1536-d).
* `local` — sentence-transformers (optional extra `local-embeddings`).
* `hash` — deterministic feature-hashing embedder for tests/offline CI. It captures
  lexical overlap only; never use it to judge retrieval quality.

Single-text embeddings (queries) are cached in Redis because the same
standalone query is embedded for the semantic cache, retrieval and memory lookup.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import math
import re
from typing import Protocol

import numpy as np
from openai import AsyncOpenAI

from backend.core import observability as obs
from backend.core.config import settings
from backend.core.logging import get_logger
from backend.core.redis import get_redis
from backend.llm.usage import LLMCallRecord, parse_usage, record

log = get_logger(__name__)
_TOKEN = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dim: int
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    model = "hash"

    def __init__(self, dim: int):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        vec = np.zeros(self.dim, dtype=np.float32)
        toks = _TOKEN.findall(text.lower())
        feats = [(t, 1.0) for t in toks] + [(f"{a}_{b}", 0.5) for a, b in itertools.pairwise(toks)]
        for feat, weight in feats:
            h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
            vec[h % self.dim] += weight if (h >> 63) & 1 else -weight
        norm = float(np.linalg.norm(vec)) or 1.0
        return (vec / norm).tolist()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class OpenRouterEmbedder:
    def __init__(self, model: str, dim: int):
        self.model = model
        self.dim = dim
        self._client = AsyncOpenAI(
            api_key=settings.openrouter_api_key or "not-configured",
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        bs = settings.embedding_batch_size
        for i in range(0, len(texts), bs):
            batch = [t[:8000] for t in texts[i : i + bs]]
            # Only inside a trace: corpus ingestion would otherwise open one trace per batch.
            with obs.observe(
                "embed",
                as_type="embedding",
                require_parent=True,
                input=batch if len(batch) > 1 else batch[0],
                model=self.model,
                metadata={"texts": len(batch), "dim": self.dim},
            ) as span:
                resp = await self._client.embeddings.create(model=self.model, input=batch)
                usage = parse_usage(getattr(resp, "usage", None))
                span.update(usage_details=obs.usage_details(usage), cost_details=obs.cost_details(usage))
            record(
                LLMCallRecord(
                    role="embed",
                    name="embed",
                    model=self.model,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    cost_usd=usage.get("cost_usd", 0.0),
                )
            )
            out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        return out


class LocalEmbedder:  # pragma: no cover - optional dependency
    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model)
        self.model = model
        self.dim = int(self._model.get_sentence_embedding_dimension() or 384)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        vecs = await asyncio.to_thread(self._model.encode, texts, normalize_embeddings=True)
        return [v.tolist() for v in vecs]


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        if settings.embedding_provider == "hash":
            _embedder = HashEmbedder(settings.embedding_dim)
        elif settings.embedding_provider == "local":
            _embedder = LocalEmbedder(settings.embedding_model)
        else:
            _embedder = OpenRouterEmbedder(settings.embedding_model, settings.embedding_dim)
    return _embedder


def set_embedder(embedder: Embedder | None) -> None:
    global _embedder
    _embedder = embedder


def normalize_query(text: str) -> str:
    """Lower-cased, whitespace-collapsed text: the semantic-cache key and memory-search query share it,
    so one embedding (cached in Redis) serves both."""
    return " ".join(text.lower().split())


def _pack(vec: list[float]) -> str:
    return base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode()


def _unpack(blob: str) -> list[float]:
    return np.frombuffer(base64.b64decode(blob), dtype=np.float32).tolist()


async def embed_query(text: str) -> list[float]:
    """Embed one query string with a Redis cache in front."""
    emb = get_embedder()
    key = f"hg:emb:{emb.model}:{hashlib.sha256(text.encode()).hexdigest()}"
    try:
        cached = await get_redis().get(key)
        if cached:
            obs.event("embedding.cache_hit", input=text, model=emb.model)
            return _unpack(cached)
    except Exception as exc:
        log.debug("embedding_cache_unavailable", error=str(exc))
    vec = (await emb.embed([text]))[0]
    try:
        await get_redis().set(key, _pack(vec), ex=7 * 86_400)
    except Exception as exc:
        log.debug("embedding_cache_unavailable", error=str(exc))
    return vec


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
