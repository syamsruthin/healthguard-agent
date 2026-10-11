# ADR-0023: Structure-aware chunking and idempotent, versioned ingestion

- **Status:** Accepted (`a718a96`)
- **Area:** RAG

## Context

The policy corpus is Markdown with a heading hierarchy, prose, and **tables** (SBC benefit tables: service → in-network cost → out-of-network cost). Naive fixed-size chunking splits a table from its header row (so "$40" loses its meaning) and loses which section a paragraph belongs to.

Re-embedding everything on every deploy wastes money, and cached answers built on old documents must not be served after the documents change.

## Decision

**Chunking** ([rag/chunking.py](../../backend/rag/chunking.py)):

- Split by headings and keep a **`section_path`** on every chunk: `Doc title › Section › Subsection`.
- Pack paragraphs up to **380 tokens** (hard max 520) with a **one-paragraph overlap**.
- Keep Markdown tables as single blocks; **split long tables by rows and repeat the header and separator rows** in every piece.
- Embed `section_path + "\n\n" + content`, so the vector knows where the text came from.

**Ingestion** ([rag/ingest.py](../../backend/rag/ingest.py), `hg-ingest`):

- Each document has front matter (`doc_id`, title, category, `plan_id`, effective date) and a **SHA-256 content hash**.
- A document is re-chunked and re-embedded **only when its hash changes** (or with `--force`); removed documents are deleted.
- Chunk ids are deterministic (hash of doc id, index and content).
- Any change bumps **`rag.data_version`**, which tags semantic-cache entries ([ADR-0026](0026-scoped-semantic-answer-cache.md)).

## Why

- **Self-describing chunks retrieve and cite better.** A table piece that repeats "| Service | In-network | Out-of-network |" is understandable on its own; the section path gives both the model and the member context ("Appeals › Level 1 appeal").
- **≈380 tokens** keeps a chunk focused enough to rank well, while six chunks still fit comfortably in the synthesis prompt.
- **Overlap** prevents a rule from being cut in half at a boundary. It is skipped for tables, where the repeated header already provides context.
- **Idempotent ingest** makes `make data` safe to re-run: unchanged documents cost nothing (a full embed of 60 documents is about 2 minutes and under $0.01).
- **Data versioning prevents stale answers.** A cached answer built on the old appeals policy would otherwise be replayed after the policy changed.

## Alternatives considered

| Option | Why not |
|---|---|
| Fixed-size character/token windows | Splits tables from headers and sections from their titles |
| Semantic chunking by embedding similarity | More embedding calls at ingest; Markdown structure already marks the boundaries |
| One chunk per section | Some sections (SBC tables) are too long; others too short |
| Re-embed everything every time | Wasteful and slow; no reason to with content hashes |
| Time-based cache expiry only | Stale answers until TTL expiry after a document change |

## Consequences

- **Positive:** better retrieval on tables; cheap re-ingest; cache invalidation tied to content.
- **Negative:** the chunker is Markdown-specific; PDFs or HTML would need a conversion step first.

## Interview soundbite

> "Chunking follows the document structure: every chunk carries its section path, paragraphs are packed to about 380 tokens with overlap, and long tables are split by rows with the header repeated so each piece stands alone. Ingest is idempotent by content hash, and any change bumps a data version that invalidates cached answers built on the old documents."
