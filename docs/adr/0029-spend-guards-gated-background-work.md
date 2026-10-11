# ADR-0029: Spend guards and gated background work

- **Status:** Accepted (`f2bc4e2`)
- **Area:** Cost

## Context

The engineering review followed the money and found spend that did no useful work, or had no ceiling:

- memory extraction ran a model call after **every** answered turn, although most turns state no preference;
- the summary job was queued **every** turn, although most conversations never overflow the history window;
- online judging sampled **25 %** of turns and could judge the same answer twice;
- feedback was not rate-limited (each 👎 queues paid judging);
- open registration had only a per-minute limit, so one account could run up an unbounded daily bill;
- two paid evaluation runs could overlap;
- judge spend was invisible.

## Decision

| Guard | Rule | Where |
|---|---|---|
| Daily turn cap | `DAILY_TURN_LIMIT = 150` turns per member per UTC day (admins exempt); `429 daily_limit` with `Retry-After` | [api/deps.py](../../backend/api/deps.py), [services/rate_limit.py](../../backend/services/rate_limit.py) |
| Per-minute limits | chat 20, feedback 30 per user; auth 10 per IP | same |
| Memory extraction gate | Only when the message matches a **preference cue** (`prefer`, `I live`, `speak`, `Spanish`, `telehealth`, `short`, …) | `long_term.worth_remembering` |
| Summary gate | Only when this turn pushes older turns past `SUMMARY_TRIGGER_TOKENS` | `MemoryContext.needs_summary_after` |
| Title | First turn only, idempotent job id | [chat_service.py](../../backend/services/chat_service.py) `_enqueue_followups` |
| Online evaluation | **5 %** of answered turns + every 👎; **each answer judged at most once** | `ONLINE_EVAL_SAMPLE_RATE`, [evals/online.py](../../backend/evals/online.py) |
| One eval run at a time | `409 eval_in_progress`; a run stuck > 3 h no longer blocks | [api/routers/admin.py](../../backend/api/routers/admin.py) |
| Judge spend metering | `SpendMeter` sums OpenRouter's `usage.cost` from every judge response | [evals/ragas_jury.py](../../backend/evals/ragas_jury.py) |
| Pre-run estimate | `hg-eval --dry-run` prints the plan and estimated cost | [evals/run.py](../../backend/evals/run.py) |
| Memory search skip | No embedding call for users with no stored memories | [memory/context.py](../../backend/memory/context.py) |
| Paid CI is manual | `eval.yml` runs only on demand | [.github/workflows/eval.yml](../../.github/workflows/eval.yml) |

## Why

- **Background calls are invisible to users but not to the bill.** Gating them on "is there work to do?" removes most of their cost without losing function.
- **Cheap deterministic gates before expensive calls** is the same pattern as the guardrail prefilter: a regex decides whether a model call is worth making. False negatives cost a missed memory; false positives cost one small call.
- **Every open door needs a ceiling.** Open registration + a model-backed endpoint = an unbounded liability without a daily cap.
- **Idempotent job ids** (`title:{cid}`, `memory:{assistant_id}`, `oe:{assistant_id}`) mean retries and duplicates don't pay twice.
- **Measure what you spend.** Judge cost now appears in every evaluation report next to agent cost.

## Alternatives considered

| Option | Why not |
|---|---|
| Run every background job every turn | Pays for no-ops |
| Classify "worth remembering" with a model | Spends a call to save a call |
| Global budget kill-switch | Useful in production (and easy to add on top of the cost ledger), but per-member caps address the abuse case directly |

## Consequences

- **Positive:** background spend scales with useful work; a single account's daily spend is bounded.
- **Negative:** the preference regex can miss unusual phrasings; the daily cap can annoy heavy demo users (set `DAILY_TURN_LIMIT=0`).
- **Fail-open note:** the daily cap is skipped if Redis is down ([ADR-0006](0006-redis-shared-fast-state.md)).

## Interview soundbite

> "I audited spend like a performance profile. Background model calls now run only when they have work: memory extraction only when the user states a preference, summaries only when history overflows, online judging on a 5 % sample with each answer judged once. And every open door has a ceiling: a daily turn cap per member, rate limits on feedback, and one paid eval run at a time."
