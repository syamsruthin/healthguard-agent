# ADR-0024: Deterministic synthetic data from a single source of truth

- **Status:** Accepted (`b66687d`)
- **Area:** Data

## Context

A health-insurance assistant can't be built or demonstrated on real member data. But toy data hides the hard problems: an answer like "your MRI was denied because prior authorization was missing; here is how to appeal" only works if the **claim record, the plan rules and the policy documents agree**. If the SQL says denial code D002 and the policy corpus describes D002 differently, the agent looks wrong even when it isn't. Evaluation also needs **stable ground truth**.

## Decision

- **One reference module** ([data_gen/reference.py](../../backend/data_gen/reference.py)) defines geography, networks, service categories, benefit rules, prior-authorization lists, denial codes, the formulary and the as-of date (`AS_OF = 2026-10-05`). Every generator **and** the policy-document renderer read from it.
- **A real claims adjudication engine** ([data_gen/claims_engine.py](../../backend/data_gen/claims_engine.py)) applies the same cost-sharing rules the `plan_benefits` rows describe: copays vs deductible + coinsurance, catastrophic-plan free visits, HDHP rules, out-of-pocket caps, family accumulators, No Surprises Act emergencies, HMO referrals, EPO/HMO out-of-network denials, duplicates, pending and in-review statuses.
- **Seed 42 everywhere**, so `hg-seed` always produces the same 1,504 members, 14.3k claims, 9.6k pharmacy claims, 5,016 providers and 60 documents.
- **Six scripted personas + an admin**, each with a story (Maria's D002 MRI denial, James's Ozempic step therapy, Robert's out-of-network ER and met OOP max, …).
- `--scale small` builds a faster dataset with the same personas, for CI.

## Why

- **Cross-source questions need consistent sources.** The single source of truth is what makes "why was it denied and how do I appeal" answerable from SQL and RAG together.
- **Adjudicating claims, rather than generating amounts randomly,** makes accumulators, member responsibility and denials internally consistent, so arithmetic questions ("how much more until my deductible?") have one right answer ($700 for Maria).
- **Determinism makes evaluation meaningful.** The golden set's numeric references are computed from this data by SQL and pandas ([ADR-0034](0034-evaluation-strategy.md)); a re-seed produces the same answers.
- **Personas make demos and live tests repeatable** and cover the edge cases (family plans, met OOP max, HMO referral denial, HDHP/HSA, pending claims).

## Alternatives considered

| Option | Why not |
|---|---|
| Random Faker data per table | Contradictions between tables and documents; no stable ground truth |
| Public datasets (e.g. CMS synthetic claims) | Don't match a coherent plan design or a policy corpus |
| LLM-generated documents per run | Non-deterministic and could contradict the data |

## Consequences

- **Positive:** realistic, consistent, reproducible; evaluation is stable across runs.
- **Negative:** the generator is a substantial amount of code (`reference.py` alone is ~2.5k lines).
- **Known gaps:** the corpus states a $35 insulin cap that the claims engine does not enforce, and cardiac-rehab sessions count toward the therapy limit. No persona hits either; both are listed as open items.

## Interview soundbite

> "All data is synthetic but adjudicated: one reference module drives the plans, a real cost-sharing engine, and the policy documents, so SQL and RAG never contradict each other. Everything is seeded, so the evaluation ground truth computed from it is stable."
