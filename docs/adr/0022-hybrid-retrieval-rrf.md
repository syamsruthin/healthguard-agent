# ADR-0022: Hybrid retrieval: pgvector + Postgres full-text, fused with RRF

- **Status:** Accepted (`a718a96`)
- **Area:** RAG

## Context

Policy questions mix two kinds of query:

- **Semantic:** "what happens if I go to an out-of-network ER?" should find the No Surprises Act section even though the words differ.
- **Lexical / exact:** "denial code D002", "Form HG-AR1", "PPO-SLV-PLS", "COBRA" must match exact tokens that embeddings blur.

The corpus is 20 hand-written policy documents plus **40 plan Summaries of Benefits and Coverage (SBCs)** that are nearly identical apart from numbers.

## Decision

`hybrid_search` ([rag/retriever.py](../../backend/rag/retriever.py)):

1. **Dense:** pgvector cosine distance over an **HNSW** index on `rag.chunks.embedding` (`text-embedding-3-small`, 1536-d), top `RAG_CANDIDATE_K = 20`.
2. **Sparse:** Postgres full-text search on a **generated, GIN-indexed `tsvector`** (`section_path || content`). The query's stemmed lexemes are OR-ed (`a | b | c`) and ranked with `ts_rank_cd`, top 20.
3. **Reciprocal Rank Fusion:** `score = Σ 1 / (60 + rank)` across both lists; keep the top `RAG_TOP_K = 6`.
4. **Optional LLM listwise re-ranking** (`RAG_RERANK=true`): the FAST model orders 12 fused candidates; any failure falls back to the fused order. **Off by default.**
5. **SBC filter:** SBC chunks are searched only for the **member's own plan** or a plan id **named in the query** (regex `PPO|HMO|EPO|HDHP-…`).

Both queries run in one database session; the query embedding is cached in Redis for 7 days.

## Why

- **Each retriever covers the other's blind spot.** Dense retrieval handles paraphrase; full-text handles codes, form numbers and plan ids.
- **RRF needs no score calibration.** Cosine similarity and `ts_rank_cd` live on different scales; rank-based fusion avoids normalising them and is robust with the standard `k = 60`.
- **OR-ed lexemes, not `plainto_tsquery`.** `plainto_tsquery` ANDs every term, so long natural questions match nothing. OR-ing the stemmed lexemes and letting `ts_rank_cd` rank by coverage behaves like BM25-style retrieval.
- **The SBC filter fixed a real failure mode.** Forty near-identical SBCs crowded out the policy documents and let "my plan" answers cite another plan's SBC. Filtering by plan is both a relevance fix and a correctness fix.
- **Re-ranking is off by default** because it adds a model call to every policy search; hybrid + RRF was good enough (context recall 1.0 on the baseline). It stays available as a switch.
- **All in Postgres:** no second store, and generated columns keep the `tsvector` in sync automatically.

## Alternatives considered

| Option | Why not |
|---|---|
| Dense only | Misses exact codes and ids |
| BM25 only | Misses paraphrases |
| Weighted score sum | Needs score normalisation and tuning per corpus |
| Cross-encoder re-ranker | Better precision, but another model to host; not needed at this corpus size |
| A separate search engine (Elasticsearch/OpenSearch) | Another system for 434 chunks |

## Consequences

- **Positive:** robust retrieval for both natural and code-heavy queries; context recall 1.0 on the baseline.
- **Negative:** two queries per search (≈ a few ms each at this size); the OR-query can match common words, which RRF then down-weights.
- **Caveat:** with `EMBEDDING_PROVIDER=hash` (CI only) the dense side is lexical; never judge retrieval quality on that setting.

## Interview soundbite

> "Retrieval is hybrid: pgvector HNSW for semantics and Postgres full-text for exact codes and plan ids, fused with Reciprocal Rank Fusion so I don't have to calibrate scores. I also filter the 40 near-identical plan SBCs to the member's own plan, which fixed both relevance and a correctness bug where answers cited the wrong plan."
