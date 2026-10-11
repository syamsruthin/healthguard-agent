# ADR-0012: Verify every answer for groundedness and completeness, with bounded repair

- **Status:** Accepted (`5fd5769`, tuned in `9c8bc6d`)
- **Area:** Agent / output guardrail

## Context

A synthesizer that is told "answer only from the sources" still sometimes adds a plausible number, a deadline from general knowledge, or skips half of a two-part question. In health insurance, a wrong deductible or appeal deadline is a real harm. Prompting alone is not a control.

## Decision

After synthesis, a `verify` node ([nodes/verify.py](../../backend/agents/nodes/verify.py)) makes one `FAST` structured call returning:

```python
class Verification(BaseModel):
    grounded: bool                 # every MATERIAL claim is supported
    complete: bool                 # every part answered, or honestly marked unavailable
    unsupported_claims: list[str]  # verbatim
    minor_issues: list[str]        # harmless imprecision; does not fail grounding
    missing: str                   # phrased as a retrieval need
    reason: str
```

Routing after the verdict:

| Verdict | Action | Bound |
|---|---|---|
| Not grounded, first time | `revise`: re-run synthesis with the flagged claims as `revision_notes` ("write it again without these") | `MAX_REVISIONS = 1` |
| Not grounded after the rewrite | Replace the answer with the `insufficient_context` fallback (or only log it when `VERIFY_REPLACE_UNGROUNDED=false`) | — |
| Incomplete, with a `missing` description | `replan`: feed `missing` back to `understand` for one more planning round; the new results are added to the old | `MAX_REPLANS = 1` |
| Verifier unavailable | Skip verification (`skipped: true`), keep the answer, do not cache it | — |
| OK | `finalize` | — |

The verifier sees the **standalone question**, the **same labelled sources** the synthesizer saw, the **member profile**, the last ~1,500 tokens of conversation and the shared **reference facts** (phone numbers, the as-of date).

## Why

- **A second model call with a narrow job is more reliable** than asking the writer to police itself. Verification is classification against provided text, which fast models do well.
- **"Material claims only"** came from a live run: the verifier flagged the Member Services phone number and the as-of date, which were in the system prompt but not the sources. The fix was two-fold:
  - one `REFERENCE_FACTS` block shared by the synthesis and verify prompts ([prompts/common.py](../../backend/llm/prompts/common.py)), so both agree on what is always true;
  - a `minor_issues` field for harmless imprecision, so wording quibbles don't fail an answer.
- **Rewrite before replace.** Often only one sentence is unsupported. Removing it keeps a useful answer; replacing the whole answer is a last resort.
- **Replan only when retrieval was the problem.** `missing` is phrased as a retrieval need ("the appeal deadline from the appeals policy"), so the replan has a concrete target. If the replan finds nothing new, the previous answer is kept.
- **Bounded loops.** One rewrite and one replan keep the worst case to a known cost and latency.
- **Fail open on the verifier, fail closed on the cache.** A verifier outage should not block an answer the member is waiting for, but an unverified answer must never be replayed to others, so `cacheable()` requires a real verdict ([ADR-0026](0026-scoped-semantic-answer-cache.md)).
- **The UI shows the verdict.** Members see "grounded ✓ complete ✓" for each answer.

## Alternatives considered

| Option | Why not |
|---|---|
| No verification, prompt only | Unsupported claims reach members |
| Deterministic checks only (every sentence has a tag) | Catches missing citations, not wrong numbers next to a citation |
| Always replace ungrounded answers | Throws away mostly-good answers; the first live version did this too often |
| NLI / claim-level entailment models | More precise, but another model to host; a fast LLM with a narrow prompt is good enough at this scale |
| Unlimited verify/repair loops | Unbounded cost; diminishing returns after one repair |

## Consequences

- **Positive:** a measured output guardrail; the member sees a verdict; ungrounded answers are rewritten or withheld.
- **Negative:** one extra model call per turn (~$0.0005 on fixed models) and a second synthesis on the rewrite path.
- **Known limitation:** the verifier is itself an LLM and can be wrong in both directions. Offline faithfulness (0.80 on the baseline, gate 0.85) shows it does not catch everything; that is tracked as an open item.

## Interview soundbite

> "Every answer is checked by a separate verifier for groundedness and completeness. An unsupported claim triggers one rewrite without that claim; only if that also fails do we fall back to an honest 'I couldn't verify this'. An incomplete answer triggers one targeted replan. Loops are bounded, verifier outages fail open for the user but closed for the cache, and the member sees the verdict."
