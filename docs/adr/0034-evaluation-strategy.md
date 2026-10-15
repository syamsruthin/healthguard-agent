# ADR-0034: Evaluation: golden set, deterministic checks, a multi-family judge jury and online sampling

- **Status:** Accepted (`cd52d39`, judge fixes in `e3d06dc` and `edce9e7`); quality gate not yet met
- **Area:** Quality

## Context

"It looked good in the demo" is not evidence. Each prompt edit, model swap (including the router default) or retrieval change can silently improve one category and break another. LLM-as-judge is the practical way to score open-ended answers, but a single judge is biased (often towards its own family's style) and noisy. Paid evaluation also has to be affordable to run.

## Decision

**Golden set** ([evals/build_golden.py](../../backend/evals/build_golden.py)): `hg-build-golden` builds **71 cases in 12 categories** from the seeded database with **no LLM calls**. Numeric references come from SQL and pandas over the same data the tools see; policy references quote the corpus. Each case has a persona, optional prior turns, `expected_tools`, `expected_outcome`, `must_include` regexes and `forbidden_ids`.

| Category | Cases |
|---|---|
| Plan questions (SQL) | 8 |
| Provider search | 6 |
| Policy RAG | 12 |
| Member-scoped (per persona) | 14 |
| Multi-hop | 4 |
| Memory follow-ups | 2 |
| Out of scope · Prompt injection · Cross-member probes | 3 · 3 · 3 |
| Admin analytics | 4 |

**Runner** (`hg-eval`, [evals/run.py](../../backend/evals/run.py)): each case runs **as its persona through the real `chat_service` pipeline**, in conversations flagged `is_eval` (excluded from dashboards), with the **semantic cache off**.

**Two kinds of scoring:**

1. **Deterministic checks** ([evals/checks.py](../../backend/evals/checks.py)): answer rate, refusal accuracy (blocked with the right fallback), route accuracy (expected tools ⊆ used tools), **isolation** (no forbidden member id in the answer), required content.
2. **RAGAS jury** ([evals/ragas_jury.py](../../backend/evals/ragas_jury.py)): faithfulness, answer relevancy and context recall by default (context precision and factual correctness optional), each scored by **three judges from different families** (GPT, Gemini, Claude). Case score = mean across judges; **spread** (max − min) flags disagreement (`JUDGE_DISAGREEMENT_THRESHOLD = 0.3`).

**Gate:** [thresholds.yaml](../../backend/evals/thresholds.yaml) (faithfulness 0.85, relevancy 0.80, recall 0.75, routing 0.90, refusal 1.0, isolation 1.0, content 0.80, answer rate 0.90); exit code 1 on failure. Results go to `eval_runs` / `eval_results` plus Markdown and JSON reports.

**Cost controls:** `--dry-run` estimate, `--per-category N` stratified samples, `--judges 1`, `--no-ragas`, **`--rescore <run>`** (re-judge stored answers with no agent calls), an on-disk **judge cache per judge**, `SpendMeter` for judge cost, a manual-only CI workflow.

**Online evaluation** ([evals/online.py](../../backend/evals/online.py)): the worker judges a **5 % sample** of live answers and **every 👎** with two judges (faithfulness, relevancy), each answer at most once; scores appear on the admin dashboard.

## Why

- **Ground truth from the data, not from a model,** so references are correct by construction and stable across runs ([ADR-0024](0024-deterministic-synthetic-data-single-source.md)).
- **Same pipeline as production,** so the score describes what members get, including guardrails, caching rules and verification.
- **Deterministic checks for what can be checked deterministically.** Isolation and refusal are binary and must be 1.0; a judge is the wrong tool for them.
- **A jury from different families** reduces self-preference bias and gives a disagreement signal; **fixed judge models** (never the router) keep the jury independent ([ADR-0004](0004-cost-optimising-router-default.md)).
- **Rescoring separates "the agent changed" from "the judge changed".** It also made judge fixes cheap to verify.
- **Online sampling catches drift** that a static golden set can't, and every thumbs-down gets a second opinion.

## What the first runs taught (evaluation is software too)

The first baseline (run `df6da3ff`, 10 cases, fixed models) exposed **evaluation bugs**, fixed before trusting the numbers:

- judges were truncated by RAGAS's default output cap (faithfulness lists every claim) → `JUDGE_MAX_TOKENS = 8192`;
- faithfulness was judged without the member profile and reference facts the assistant is allowed to use → passed as grounding context;
- one judge cache was shared by all judges → one cache per judge;
- follow-ups were judged on their raw wording → judged on the resolved question.

Rescore `50e47e0d` (same answers, fixed jury): faithfulness **0.80**, answer relevancy **0.68**, context recall **1.0**; answer rate, refusal, routing and isolation **1.0**; required content 0.86.

## Consequences

- **Positive:** quality is a number with a gate; isolation and refusal are proven, not assumed.
- **Negative / open:**
  - the gate is **not met** (faithfulness 0.80 < 0.85, relevancy 0.68 < 0.80). The diagnosed cause is answers volunteering unasked detail; a synthesis-prompt fix (`PROMPT_VERSION 2026.10.4`) is in place but **not yet re-measured**;
  - the router default has **not been evaluated** yet (≈$0.30–0.40 for a per-category run);
  - judge cost appears in reports, not in `llm_calls`.

## Interview soundbite

> "I built the golden set from the database, so references are correct by construction, and run every case as its persona through the real chat pipeline. Binary properties like isolation and refusal are checked deterministically and must be 100 %; open-ended quality is scored by a three-family judge jury with a disagreement signal. The first baseline found bugs in my evaluation before it found bugs in the agent, and I'm upfront that faithfulness and relevancy are still below the gate, with a diagnosed cause and a fix waiting to be measured."
