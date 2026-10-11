# ADR-0021: Data minimisation and PII handling

- **Status:** Accepted (`92680bd`, `9a1eb6d`, `f2c1515`)
- **Area:** Privacy

## Context

Health data leaks in unglamorous places: log lines, token payloads, debugging views, caches and "helpful" memory features. Each of those is a copy of data that needs protecting. The question for each surface is: does it need the data at all?

## Decision

Minimise per surface:

| Surface | Rule | Where |
|---|---|---|
| **Access tokens** | Only `sub` and `role` (plus standard claims) | [core/security.py](../../backend/core/security.py) |
| **Logs** | A structlog processor masks SSNs, emails, phone numbers, ISO dates and member IDs, **including values nested in lists and dicts** (up to depth 4); correlation ids are left intact | [core/logging.py](../../backend/core/logging.py) |
| **Member vs admin view** | Members receive the plan, steps, verification verdict and citations. The **full trace and the generated SQL / pandas code are admin-only** (`/admin/traces/{id}`). Streamed citations lose their code `detail` for members. | [services/chat_service.py](../../backend/services/chat_service.py) (`member_tools`, `member_citations`, `_member_sink`) |
| **Agent state / checkpoints** | Raw tool rows are dropped; only the formatted table needed for synthesis is kept | [nodes/execute.py](../../backend/agents/nodes/execute.py) |
| **Tool prompts** | Code models get only the member id, plan, network, zip and dependents, not the conversation | `UserContext.tool_context()` |
| **Semantic cache** | Answers that mention the user's identity or plan are stored in the member's private scope, never public | [ADR-0026](0026-scoped-semantic-answer-cache.md) |
| **Long-term memory** | Only non-clinical preferences. A deterministic filter rejects ids, amounts, dates, contact details and clinical terms, even if the extraction model proposes them. Users can list, delete, clear and disable memories. | [memory/long_term.py](../../backend/memory/long_term.py), [ADR-0033](0033-three-tier-memory.md) |
| **Accounts** | Self-registered accounts are never linked to member records | [ADR-0020](0020-jwt-and-rotating-refresh-tokens.md) |

## Why

- **Every copy of PII is a liability.** The cheapest data to protect is data you never stored.
- **Logs are the most common leak.** They are shipped to third-party tools and kept for months. Nested masking was added after the review found member IDs inside logged lists of claims.
- **Generated SQL reveals schema and logic.** It is useful for engineers and auditors, not for members, and showing it invites probing. Transparency for members is kept at the level that helps them (plan, steps, verdict, sources).
- **Memory is the feature most likely to accumulate sensitive data silently.** "Remember that I'm pregnant" is clinical; "I prefer Spanish-speaking doctors" is a preference. An LLM's judgement is backed by a deterministic filter because prompts are not controls.

## Alternatives considered

| Option | Why not |
|---|---|
| Show everything to everyone ("full transparency") | Exposes schema and code to members; larger PII surface in responses |
| Rely on the extraction prompt alone for memory privacy | Models occasionally ignore rules; a regex backstop is cheap |
| Disable logging of user content entirely | Hurts debugging; masking keeps logs useful |

## Consequences

- **Positive:** smaller blast radius for every surface; members still see why an answer was given.
- **Negative:** regex masking can over-mask (any ISO date) or under-mask (free-text names are not masked). Names are a known gap.
- **Out of scope for the demo:** encryption at rest, a HIPAA BAA with the model provider, audit logging of admin trace access. These are the next steps for real PHI (see the [interview guide](../interview/INTERVIEW_GUIDE.md)).

## Interview soundbite

> "I minimised PII per surface: tokens carry only id and role, logs mask identifiers even in nested structures, members see the plan and verdict but not the generated SQL, raw rows never enter the checkpoint, and long-term memory only keeps non-clinical preferences, enforced by a deterministic filter, not just the prompt."
