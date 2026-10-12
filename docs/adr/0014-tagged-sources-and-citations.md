# ADR-0014: Tagged sources and citations (`[S#]` data, `[D#]` documents)

- **Status:** Accepted (`5fd5769`)
- **Area:** Agent / grounding

## Context

Members need to know **where** a fact came from: their claim record or a policy document. The verifier needs to check claims **against specific evidence**. And the UI needs something to render under each answer.

## Decision

`build_sources` ([agents/sources.py](../../backend/agents/sources.py)) turns tool results into:

1. a **labelled `<sources>` block** for the synthesizer and verifier:
   - each data result becomes `[S1]`, `[S2]` … with its tool label ("Insurance database", "Provider directory") and task, followed by a GitHub-style table capped at 7,000 characters;
   - each retrieved passage becomes `[D1]`, `[D2]` … with its section path (`Doc title › Section › Subsection`), de-duplicated by chunk id across steps;
   - failed steps are listed as `FAILED … treat this data as unavailable`;
2. **structured citations** (`tag`, `kind`, `title`, section, doc and chunk ids, a 400-character snippet for documents, row count for data).

The synthesis prompt requires a tag on every factual sentence. After streaming, `cited()` keeps only citations whose tag appears in the answer (all of them if the model cited none).

## Why

- **Two tag families make provenance obvious** to the member: `[S#]` means "from your records / the directory", `[D#]` means "from a policy document". The prompt also states which is authoritative for what: data tables for plan, claim, provider and drug facts; documents for rules and procedures.
- **The verifier needs the same evidence the writer had.** Both prompts receive the identical labelled block, so the verifier can check "[S1] says D002" literally.
- **Section paths make passages self-describing**, which helps both the model and the human reading a citation.
- **Showing only cited sources** keeps the UI honest: the source list reflects what the answer actually relied on.
- **Caps keep prompts bounded:** at most 7,000 characters per table and 50 displayed rows per SQL result.

## Alternatives considered

| Option | Why not |
|---|---|
| No citations | Members can't check facts; verification becomes vague |
| Provider-native citation features | Not portable across the many models behind the router |
| Footnotes generated after the fact (attribution model) | Another model call; less reliable than citing while writing |

## Consequences

- **Positive:** every answer is checkable by member, verifier and evaluator alike.
- **Negative:** tags add a little noise to the prose; very large result sets are truncated (and marked as truncated).
- **Privacy note:** for members, a data citation's `detail` (the generated SQL or pandas) is removed before it is sent ([ADR-0021](0021-data-minimisation-and-pii.md)).

## Interview soundbite

> "Tool outputs become a labelled evidence block: S-tags for data, D-tags for policy passages. The writer must cite a tag on every factual sentence, the verifier checks against the same block, and the UI shows only the sources the answer actually cited."
