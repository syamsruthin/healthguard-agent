# ADR-0013: Error-coded, honest fallbacks

- **Status:** Accepted (`5fd5769`, `9a1eb6d`)
- **Area:** Agent / reliability

## Context

Many things can fail in a turn: the model gateway, one tool, every tool, the verifier, the whole turn's time budget. A generic "something went wrong" hides which one, and an answer built on partial failure can mislead. The prototype's single "no data" message was shown for both "the AI service is down" and "you have no claims", which confuses members and operators alike.

## Decision

- Every `ToolResult` carries an **`error_code`**: `llm_unavailable`, `guard_rejected`, `db_error`, `sandbox_error`, `retrieval_error`, `timeout`, `dependency_failed`, `tool_error` ([tools/base.py](../../backend/agents/tools/base.py)).
- The graph maps outcomes to **fixed, pre-written fallback messages** ([guardrails.py](../../backend/agents/guardrails.py)):

| Key | When |
|---|---|
| `out_of_scope`, `prompt_injection`, `other_member_data`, `medical_advice`, `harmful` | Input guardrails ([ADR-0019](0019-layered-input-guardrails.md)) |
| `llm_unavailable` | The planner or synthesizer is unreachable, or every step failed and at least one failure was a model outage |
| `no_data` | Every step failed for non-model reasons |
| `insufficient_context` | The answer failed verification twice |
| `timeout` | The turn exceeded `TURN_TIMEOUT_SECONDS` (150 s) |

- Each turn ends with an **`outcome`**: `answered`, `smalltalk`, `blocked`, `cache_hit`, `insufficient` or `error`, plus the `fallback` key used.
- Partial failure is handed to synthesis honestly: a failed step appears in the sources as `[S2] Insurance database — lookup FAILED … (treat this data as unavailable)`.
- **Degradation rules:**
  - the planner fails on a **replan** → keep the answer already written;
  - synthesis fails on a **rewrite or replan** → restore the previous answer (`answer_replaced` event);
  - the verifier fails → skip verification;
  - the semantic cache, memory search or follow-up jobs fail → log and continue.

## Why

- **Members get the right next step.** "The AI service is unavailable, try again" and "I couldn't retrieve that information" call for different actions. Every fallback includes a Member Services number.
- **Operators get the right signal.** Error codes become metrics (`hg_tool_events_total{outcome}`, `hg_guardrail_blocks_total{reason}`) and dashboard outcome counts.
- **Fixed text is safe text.** Fallbacks are reviewed copy, not model output; the self-harm fallback always includes 988 and the Behavioral Health Line.
- **Never lose a good answer to a late failure.** A failed improvement (replan, rewrite) must not destroy the answer the member already saw streaming.
- **Partial data is labelled, not hidden,** so the synthesizer can say "I found your claim, but couldn't load the appeal policy right now" instead of guessing.

## Alternatives considered

| Option | Why not |
|---|---|
| Let the model write error messages | Inconsistent, can hallucinate reasons, can omit crisis resources |
| One generic error | Hides outages from users and operators |
| Fail the whole turn on any tool error | Throws away answerable parts of multi-part questions |

## Consequences

- **Positive:** predictable behaviour under failure; outages are distinguishable in metrics.
- **Negative:** fallback copy must be maintained alongside the domain (phone numbers live in two places: fallbacks and `REFERENCE_FACTS`).

## Interview soundbite

> "Failures carry error codes all the way up, so the system can tell 'the model is down' from 'there's no data' from 'I couldn't verify this', and show a fixed, reviewed message for each. A failed improvement never destroys an answer the member already has, and partial failures are labelled in the sources so the answer is honest about what's missing."
