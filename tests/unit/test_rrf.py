from __future__ import annotations

from backend.rag.retriever import RetrievedChunk, reciprocal_rank_fusion


def _chunk(cid: str, **kw: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid, doc_id="d", title="t", category="c", section_path="s", content=cid, **kw
    )


def test_chunk_in_both_lists_ranks_first() -> None:
    vector = [_chunk("a", vector_score=0.9), _chunk("b", vector_score=0.8)]
    keyword = [_chunk("c", keyword_score=0.5), _chunk("b", keyword_score=0.4)]
    fused = reciprocal_rank_fusion([vector, keyword], top_n=3)
    assert fused[0].chunk_id == "b"
    assert fused[0].vector_score == 0.8 and fused[0].keyword_score == 0.4
    assert {c.chunk_id for c in fused} == {"a", "b", "c"}
    assert fused[0].rrf_score > fused[1].rrf_score


def test_top_n_limits_output() -> None:
    hits = [_chunk(str(i)) for i in range(10)]
    assert len(reciprocal_rank_fusion([hits], top_n=4)) == 4
