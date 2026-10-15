"""
Hybrid retrieval over the policy corpus.

1. Dense: pgvector cosine distance on an HNSW index.
2. Sparse: Postgres full-text search (OR-query of the stemmed lexemes,
   ranked with ts_rank_cd) on a GIN-indexed generated tsvector column.
3. Reciprocal Rank Fusion of both lists.
4. Optional LLM listwise re-ranking (RAG_RERANK=true).

Plan SBCs are only searched for the member's own plan or plans named
explicitly in the query, so "my plan" answers never cite another plan's SBC
and the 40 near-identical SBCs do not crowd out policy documents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from sqlalchemy import text

from backend.core import observability as obs
from backend.core.config import settings
from backend.core.logging import get_logger
from backend.db.session import SessionLocal
from backend.llm.client import get_llm
from backend.llm.embeddings import embed_query
from backend.llm.registry import Role

log = get_logger(__name__)


@dataclass
class RetrievedChunk:
    chunk_id: str
    doc_id: str
    title: str
    category: str
    section_path: str
    content: str
    plan_id: str | None = None
    vector_score: float | None = None
    keyword_score: float | None = None
    rrf_score: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


_FILTER = """
  (d.category <> 'sbc' OR d.plan_id = CAST(:plan_id AS text) OR d.plan_id = ANY(CAST(:named_plans AS text[])))
  AND (CAST(:categories AS text[]) IS NULL OR d.category = ANY(CAST(:categories AS text[])))
"""
_PLAN_ID = re.compile(r"\b(PPO|HMO|EPO|HDHP)-(CAT|BRZ|SLV|GLD|PLT)-(VAL|STD|PLS)\b", re.I)

_VECTOR_SQL = text(f"""
SELECT c.chunk_id, c.doc_id, d.title, d.category, d.plan_id, c.section_path, c.content,
       1 - (c.embedding <=> CAST(:embedding AS vector)) AS score
FROM rag.chunks c JOIN rag.documents d ON d.doc_id = c.doc_id
WHERE {_FILTER}
ORDER BY c.embedding <=> CAST(:embedding AS vector)
LIMIT :k
""")

_KEYWORD_SQL = text(f"""
WITH q AS (
  SELECT to_tsquery('english', COALESCE(NULLIF(
           (SELECT string_agg(quote_literal(lexeme), ' | ')
              FROM unnest(tsvector_to_array(to_tsvector('english', :query))) AS lexeme), ''), 'zzzznomatch')) AS tsq
)
SELECT c.chunk_id, c.doc_id, d.title, d.category, d.plan_id, c.section_path, c.content,
       ts_rank_cd(c.tsv, q.tsq) AS score
FROM rag.chunks c JOIN rag.documents d ON d.doc_id = c.doc_id, q
WHERE c.tsv @@ q.tsq AND {_FILTER}
ORDER BY score DESC
LIMIT :k
""")


def reciprocal_rank_fusion(
    ranked_lists: list[list[RetrievedChunk]], k: int = 60, top_n: int = 6
) -> list[RetrievedChunk]:
    """RRF score = Σ 1 / (k + rank) over every list the chunk appears in."""
    scores: dict[str, float] = {}
    by_id: dict[str, RetrievedChunk] = {}
    for ranked in ranked_lists:
        for rank, hit in enumerate(ranked):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank + 1)
            if hit.chunk_id in by_id:
                existing = by_id[hit.chunk_id]
                existing.vector_score = existing.vector_score or hit.vector_score
                existing.keyword_score = existing.keyword_score or hit.keyword_score
            else:
                by_id[hit.chunk_id] = hit
    fused = sorted(by_id.values(), key=lambda h: scores[h.chunk_id], reverse=True)[:top_n]
    for hit in fused:
        hit.rrf_score = round(scores[hit.chunk_id], 6)
    return fused


def _row_to_chunk(row: Any, kind: str) -> RetrievedChunk:
    chunk = RetrievedChunk(
        chunk_id=row.chunk_id,
        doc_id=row.doc_id,
        title=row.title,
        category=row.category,
        section_path=row.section_path,
        content=row.content,
        plan_id=row.plan_id,
    )
    if kind == "vector":
        chunk.vector_score = float(row.score)
    else:
        chunk.keyword_score = float(row.score)
    return chunk


class _Rerank(BaseModel):
    ranking: list[int]


async def _llm_rerank(query: str, hits: list[RetrievedChunk], top_n: int) -> list[RetrievedChunk]:
    listing = "\n\n".join(f"[{i}] {h.section_path}\n{h.content[:700]}" for i, h in enumerate(hits))
    messages = [
        {
            "role": "system",
            "content": "Rank passages by how directly they answer the query. "
            'Return JSON {"ranking": [passage indices, most relevant first]}.',
        },
        {"role": "user", "content": f"Query: {query}\n\nPassages:\n{listing}"},
    ]
    try:
        out = await get_llm().structured(Role.FAST, messages, _Rerank, name="rerank")
        order = [i for i in out.ranking if 0 <= i < len(hits)]
        seen = set(order)
        order += [i for i in range(len(hits)) if i not in seen]
        return [hits[i] for i in order[:top_n]]
    except Exception as exc:
        log.warning("rerank_failed", error=str(exc)[:200])
        return hits[:top_n]


def _ranked(hits: list[RetrievedChunk]) -> list[dict[str, Any]]:
    """Compact view of a ranked list for tracing."""
    return [
        {
            "chunk_id": h.chunk_id,
            "section": h.section_path,
            "vector": None if h.vector_score is None else round(h.vector_score, 4),
            "keyword": None if h.keyword_score is None else round(h.keyword_score, 4),
            "rrf": h.rrf_score or None,
        }
        for h in hits
    ]


async def hybrid_search(
    query: str,
    *,
    top_k: int | None = None,
    plan_id: str | None = None,
    categories: list[str] | None = None,
) -> list[RetrievedChunk]:
    top_k = top_k or settings.rag_top_k
    cand = settings.rag_candidate_k
    # SBCs: only the member's own plan, plus any plan explicitly named in the query.
    named = sorted({m.group(0).upper() for m in _PLAN_ID.finditer(query)})
    params = {"plan_id": plan_id, "named_plans": named, "categories": categories, "k": cand}
    with obs.observe(
        "hybrid_search",
        as_type="retriever",
        input=query,
        metadata={
            "plan_id": plan_id,
            "named_plans": named,
            "categories": categories,
            "top_k": top_k,
            "candidate_k": cand,
            "rerank": settings.rag_rerank,
        },
    ) as o:
        embedding = await embed_query(query)
        async with SessionLocal() as session:
            with obs.observe("vector_search", as_type="retriever", metadata={"k": cand}) as v:
                vec_rows = (await session.execute(_VECTOR_SQL, {**params, "embedding": str(embedding)})).all()
                vector_hits = [_row_to_chunk(r, "vector") for r in vec_rows]
                v.update(output=_ranked(vector_hits[:10]), metadata={"hits": len(vector_hits)})
            with obs.observe("keyword_search", as_type="retriever", input=query, metadata={"k": cand}) as k:
                kw_rows = (await session.execute(_KEYWORD_SQL, {**params, "query": query})).all()
                keyword_hits = [_row_to_chunk(r, "keyword") for r in kw_rows]
                k.update(output=_ranked(keyword_hits[:10]), metadata={"hits": len(keyword_hits)})
        pool = top_k * 2 if settings.rag_rerank else top_k
        fused = reciprocal_rank_fusion([vector_hits, keyword_hits], top_n=pool)
        obs.event("rrf_fusion", output=_ranked(fused), pool=pool)
        if settings.rag_rerank and len(fused) > top_k:
            fused = await _llm_rerank(query, fused, top_k)
        o.update(output=_ranked(fused), metadata={"returned": len(fused)})
        return fused
