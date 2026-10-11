# ADR-0033: Three-tier memory: window, rolling summary, long-term preferences

- **Status:** Accepted (`92680bd`, gating in `f2bc4e2`)
- **Area:** Memory

## Context

Conversations need context ("what about the second one?"), but sending the full history every turn grows cost linearly and eventually overflows the context window. Members also benefit from durable preferences across conversations ("I prefer Spanish-speaking doctors", "I live in 78704"). In health care, memory is also the feature most likely to accumulate sensitive data without anyone noticing.

## Decision

Three tiers ([backend/memory/](../../backend/memory/)):

| Tier | What | How | Budget |
|---|---|---|---|
| **Window** | Recent turns, verbatim | Newest messages after `summary_upto` that fit `HISTORY_TOKEN_BUDGET`; assistant replies truncated to 1,200 chars; the current message never duplicated | 2,500 tokens |
| **Rolling summary** | Older turns of this conversation | When turns **outside** the window exceed `SUMMARY_TRIGGER_TOKENS`, the worker folds them into `conversations.summary` (previous summary + overflow → new summary) and advances `summary_upto` | trigger at 3,000 tokens |
| **Long-term memory** | Durable, non-clinical preferences across conversations | FAST model extracts candidates after a turn **that states a preference**; deterministic privacy filter; embedding de-duplication; cap per user; retrieved by similarity | top 5 above similarity 0.35; ≤ 50 per user |

Long-term memory rules:

- **Categories:** location, language, provider preference, plan interest, communication style, other.
- **Privacy filter** (`is_storable`): rejects member/claim/PA ids, dollar amounts, dates, long digit runs (phones, NPIs), emails, phone numbers, and clinical terms (diagnoses, pregnancy, medications, procedures, mental health…). A home zip code is explicitly allowed.
- **Duplicates:** skipped above cosine 0.9 against existing memories.
- **Use:** memories go into prompts as `<user_preferences>` with the instruction "use for tone, language and defaults only; never as facts about coverage, claims or costs".
- **User control:** list, delete one, clear all, disable (`memory_enabled`), via the Memory page or `/me/memories`.
- **Cost:** no lookup for users with no memories; the lookup reuses the cache key's embedding.

## Why

- **Window + summary bounds cost** while keeping recent wording exact (the planner needs exact words to resolve references) and older context compressed.
- **`summary_upto` guarantees no overlap and no gap** between the summary and the window.
- **Summaries run in the worker,** only when there is overflow, so they never delay a turn.
- **Preferences, not facts.** Coverage facts must come from tools every time; a remembered deductible would go stale and bypass verification. Memory is restricted to things that shape tone and defaults.
- **A deterministic filter backs up the prompt** because "don't store medical information" is a request a model may not honour every time.
- **User control is a privacy requirement**, not a nice-to-have.

## Alternatives considered

| Option | Why not |
|---|---|
| Send the whole history | Cost grows without bound; context overflow |
| Fixed last-N messages | Ignores message length; long answers crowd out context |
| Vector-retrieve old turns instead of summarising | Loses the conversation's narrative; retrieval may miss the referent |
| Store facts in long-term memory | Stale, unverifiable, and potentially clinical |
| LLM-only privacy rules for memory | Not a control |

## Consequences

- **Positive:** bounded context cost; useful personalisation; a privacy-safe memory with user control.
- **Negative:** a summary can drop a detail the user later refers to; the preference-cue regex can miss unusual phrasing; regex filters can over-reject harmless memories.

## Interview soundbite

> "Memory has three tiers: a token-budgeted verbatim window, a rolling summary that a worker updates only when history overflows, and long-term memories restricted to non-clinical preferences. A deterministic filter rejects ids, amounts, dates, contacts and clinical terms even if the extraction model proposes them, memories are used for tone and defaults only, never as facts, and users can see, delete or disable them."
