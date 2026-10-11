# ADR-0026: A scoped semantic answer cache with a cache-first lookup

- **Status:** Accepted (`5fd5769`); key and position changed in `9c8bc6d` and `f2bc4e2`
- **Area:** Caching / cost / privacy

## Context

A full turn costs $0.01–0.04 and 5–25 s. Many members ask the same general questions. Serving a previous **verified** answer would be nearly free and instant, but:

1. **Isolation:** "what's my deductible?" for Maria must never be served to James.
2. **Correctness:** a cached unverified or partial answer would be replayed to many people.
3. **Staleness:** answers built on an old document version or an old prompt must expire.
4. **The key:** the first design keyed on the planner's standalone rewrite. Live runs showed the rewrite **varies from run to run**: cosine distance 0.18–0.30 between rewrites of the same question, against a 0.08 threshold. The cache never hit. And looking up after planning meant every hit still paid for the planning call.

## Decision

**Storage:** RedisVL `AsyncSearchIndex` on Redis 8: a hash per entry with `scope`, `data_version` and `prompt_version` **tag** fields and a **FLAT** cosine vector field ([cache/semantic.py](../../backend/cache/semantic.py)). RedisVL's own `SemanticCache` needs a synchronous vectorizer, so the module drives the index directly.

**Two lookup points** ([nodes/cache_lookup.py](../../backend/agents/nodes/cache_lookup.py)):

| Node | When | Key | Model calls on a hit |
|---|---|---|---|
| `cache_first` | **Opening question** (no history, no summary) | Normalised message (lower-cased, whitespace-collapsed) | **None** |
| `cache_lookup` | **Follow-up** (needs history to resolve) | The planner's standalone rewrite | One (planning) |

**Scopes:**

| Caller / question | Reads from | Writes to |
|---|---|---|
| Admin | `role:admin` | `role:admin` |
| Member, general question | `member:{id}` first, then `public` | `public`, **unless** the answer or rewrite mentions the member's name, email, member id, dependents or plan → `member:{id}` |
| Member, own-data question | `member:{id}` | `member:{id}` |
| Unlinked account, own-data question | — (no cache) | — |

**Eligibility to store** (`cacheable`): outcome `answered`, not itself a hit, verification **ran** and is **grounded and complete**, at least one tool ran, and **every** tool step succeeded.

**Freshness:** filters on `data_version` and `PROMPT_VERSION`; TTL 24 h public, 1 h member. **Threshold:** cosine distance ≤ 0.08.

Other rules: messages the prefilter would block skip `cache_first`; evaluation runs disable the cache (a replayed answer would hide regressions); lookups and stores never raise.

## Why

- **Normalised text is a stable key; a model's rewrite is not.** An opening question is self-contained, so it needs no rewrite to be looked up. This turned the cache from "never hits" into "hits before any model call".
- **Follow-ups genuinely need the rewrite** ("what about the second one?" means nothing alone), so they are looked up after planning.
- **Scopes make leakage structurally impossible.** A lookup filter can only match the caller's own scope or `public`. The identity check on store catches the subtle case: a "general" question whose answer was personalised ("Maria, your Choice PPO plan covers…") must not become public.
- **Only cache what you'd defend.** Requiring a real, positive verification and all-successful tools means a hit is never a partial or unverified answer.
- **Tight threshold (0.08)** favours precision over recall: a wrong cached answer is worse than a miss. The trade-off is that paraphrases miss (they measure ≥ 0.18).
- **FLAT (exact) search** is fine for a cache of this size and avoids approximate-search misses on a threshold decision.
- **Member TTL is shorter** (1 h) because member data (claims, accumulators) changes more often than policy text.

## Alternatives considered

| Option | Why not |
|---|---|
| Key on the LLM rewrite always | Measured: rewrites drift 0.18–0.30 apart; no hits; still pays for planning |
| Exact-string cache only | Misses trivial variations in case and whitespace |
| Looser threshold to catch paraphrases | Risk of serving an answer to a different question ("deductible" vs "out-of-pocket max" are close in embedding space) |
| One global cache | Cross-member leakage |
| RedisVL `SemanticCache` class | Requires a sync vectorizer; the app is fully async |

## Consequences

- **Positive:** an opening question that was already answered costs one embedding and returns instantly; member data never crosses scopes.
- **Negative:** paraphrases miss (documented limitation). Member-scoped entries help only that member.
- **Tests:** scoping and identity-based routing are unit-tested; the Redis 8 index is integration-tested and reports the scope of a hit (`3acb639`).

## Interview soundbite

> "The semantic cache is scoped per member, public or admin, and only stores answers that passed verification with every tool succeeding. My first version keyed on the planner's rewrite and never hit, because rewrites of the same question drifted further apart than my threshold. So opening questions are now keyed on their normalised text and looked up before any model call, and only follow-ups use the rewrite. A 'public' answer that mentions the member's name or plan is stored privately instead."
