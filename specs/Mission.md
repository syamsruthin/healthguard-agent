# Mission: HealthGuard Agentic RAG

## Vision
Give US health-insurance members an assistant they can trust with real questions about their coverage. It should combine their own records, the provider directory and the policy documents, cite where every fact came from, and never expose another member's data.

## Problem statement
Members' questions cross data sources, as in "Why was my MRI denied, and how do I appeal?" or "Find an in-network cardiologist near me who speaks Spanish." Answering means three things: querying the member's claims and accumulators, searching policy rules, and filtering the provider directory. All of it must be scoped to what that member may see.

## Primary objectives
1. **Grounded answers.** Every factual sentence cites a source. A verifier checks groundedness and completeness. Unsupported claims are rewritten out, or the answer is replaced with an honest fallback.
2. **Isolation by construction.** Generated SQL runs on a read-only role under Postgres Row-Level Security, behind a static SQL guard. Generated pandas code runs in a process sandbox. Cross-member probes are refused before any model call.
3. **Agentic but bounded.** A dependency-aware plan runs independent steps in parallel, with at most one replan and one rewrite. Fallback models keep turns working through provider outages.
4. **Auditability.** Every turn stores its plan, tool code, sources, verification, per-call tokens, cache hits and cost.
5. **Cost efficiency.** A cost-optimising model router per role with fixed-model fallbacks, provider prompt caching, a scoped semantic answer cache that answers repeated questions without a model call, background model calls only when they have work to do, and spend guards (daily turn cap, sampled online judging, one evaluation run at a time).
6. **Measured quality.** A golden set built from the data, deterministic checks, a jury of LLM judges from different model families (RAGAS), a CI gate, and online sampling of live answers.

## Data simulation profile
- **Insurance database:** 40 plans (PPO, HMO, EPO, HDHP × metal tiers), 1,600 benefit rows, a 117-drug formulary, 1,504 members in families, 14k adjudicated medical claims, 9.6k pharmacy claims, prior authorizations and accumulators.
- **Data lake (CSV):** 5,016 providers across 30 specialties, 442 facilities and 433 pharmacies in 40 cities.
- **Policy corpus:** 20 hand-written policy documents plus 40 generated plan Summaries of Benefits and Coverage.
- **Personas:** six members with scripted coverage stories and one admin. The data is deterministic (seed 42), so evaluation ground truth stays stable.
