# ADR-0017: Row-Level Security as the member-isolation boundary

- **Status:** Accepted (`0dd227d`)
- **Area:** Security

## Context

The core privacy promise is that **one member can never see another member's records**. Members ask natural-language questions that become arbitrary SQL. Isolation must hold even when the generated SQL omits a `WHERE member_id = …`, joins tables in unexpected ways, or was written by a manipulated model.

Family plans add a twist: a subscriber should see their dependents' claims; a spouse who is not the subscriber should see only their own.

## Decision

Enable **Postgres Row-Level Security** on every member-scoped table in `insurance` (members, claims, pharmacy claims, prior authorizations, accumulators), with one policy per table that applies `FOR SELECT TO hg_agent_ro`:

```sql
CREATE POLICY <table>_member_isolation ON insurance.<table>
FOR SELECT TO hg_agent_ro
USING (
    current_setting('app.role', true) = 'admin'
    OR member_id     = current_setting('app.member_id', true)
    OR subscriber_id = current_setting('app.member_id', true)
);
```

The identity comes from the **server**, never the model: `execute_scoped` sets `SET LOCAL app.role` and `SET LOCAL app.member_id` inside the read-only transaction from the authenticated `UserContext` ([ADR-0016](0016-defence-in-depth-for-generated-sql.md)).

Resulting visibility:

| Caller | Sees |
|---|---|
| Member who is the subscriber | Own rows + dependents' rows (`subscriber_id` matches) |
| Spouse/dependent who is not the subscriber | Own rows only |
| Admin | All rows |
| Self-registered account with no member link | No member rows (`app.member_id = ''` matches nothing) |
| Public tables (plans, benefits, formulary, codes) | Everyone (no RLS) |

## Why

- **Isolation by construction.** RLS is applied by the database to **every reference** to the table: subqueries, CTEs, joins, unions. No query rewriting is needed and none can be forgotten.
- **The model is outside the trust boundary.** The planner and code model never choose the identity; they don't even need to get the `WHERE` clause right.
- **Fail-closed defaults.** `current_setting(…, true)` returns NULL if unset, and an empty member id matches nothing, so a missing context yields **no rows**, not all rows.
- **Policies are scoped to the agent role** (`TO hg_agent_ro`), so application code paths (seeding, admin services) are unaffected.
- **Family logic lives in one place**: the `subscriber_id` predicate.

## Alternatives considered

| Option | Why not |
|---|---|
| Add `WHERE member_id = …` in the prompt or by rewriting SQL | Prompt instructions are not controls; rewriting arbitrary SQL is error-prone |
| Application-side post-filtering of rows | The data already left the database; aggregates (`SUM`, `COUNT`) can't be post-filtered |
| One schema or database per member | Operationally absurd at 1.5k (let alone 1.5M) members |
| Views with `security_barrier` | Possible, but RLS is the purpose-built mechanism |

## Consequences

- **Positive:** a cross-member data leak through the agent would need a Postgres RLS bypass, not just a clever prompt.
- **Negative:** RLS adds a predicate to every scan; with indexes on `member_id` / `subscriber_id` this is cheap at this scale.
- **Documented behaviour:** a spouse who is not the subscriber sees only their own records. This is intended and listed in the known limitations.
- **Tests:** an RLS matrix (member, subscriber, spouse, admin, unlinked) and database-level block tests run in CI against real Postgres ([tests/integration/test_rls.py](../../tests/integration/test_rls.py)).

## Interview soundbite

> "Member isolation is enforced by Postgres Row-Level Security, not by the prompt. The server sets the caller's member id with SET LOCAL inside a read-only transaction, and every member table has a policy keyed on it, so even SQL that forgets the WHERE clause, or was written by a manipulated model, only sees the caller's rows. A missing context returns nothing, never everything."
