"""
Markdown-aware chunking.

* Splits on the heading hierarchy and keeps a `section_path`
  ("Doc title › Section › Subsection") on every chunk for retrieval and citations.
* Packs paragraphs up to a token budget with one-paragraph overlap.
* Splits long tables by rows while repeating the header row, so every table
  chunk is self-describing (important for SBC benefit tables).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.core.tokens import count_tokens

TARGET_TOKENS = 380
MAX_TOKENS = 520
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")


@dataclass
class Chunk:
    index: int
    section_path: str
    content: str
    token_count: int


def _blocks(text: str) -> list[str]:
    """Split into paragraphs, keeping markdown tables as single blocks."""
    blocks, buf, in_table = [], [], False
    for line in text.splitlines():
        is_table = line.strip().startswith("|")
        if not line.strip():
            if buf and not in_table:
                blocks.append("\n".join(buf))
                buf = []
            elif in_table:
                blocks.append("\n".join(buf))
                buf, in_table = [], False
            continue
        if is_table != in_table and buf:
            blocks.append("\n".join(buf))
            buf = []
        in_table = is_table
        buf.append(line)
    if buf:
        blocks.append("\n".join(buf))
    return [b.strip() for b in blocks if b.strip()]


def _split_table(table: str, budget: int) -> list[str]:
    lines = table.splitlines()
    if len(lines) < 3:
        return [table]
    header, sep, rows = lines[0], lines[1], lines[2:]
    out, cur = [], [header, sep]
    for row in rows:
        if count_tokens("\n".join([*cur, row])) > budget and len(cur) > 2:
            out.append("\n".join(cur))
            cur = [header, sep]
        cur.append(row)
    if len(cur) > 2:
        out.append("\n".join(cur))
    return out


def _sections(markdown: str, title: str) -> list[tuple[str, str]]:
    path: list[str] = []
    sections: list[tuple[str, str]] = []
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            sections.append((" › ".join([title, *path]) if path else title, body))

    for line in markdown.splitlines():
        m = _HEADING.match(line)
        if m:
            flush()
            buf = []
            level = len(m.group(1))
            heading = m.group(2).strip()
            if level == 1:
                path = []
                continue  # document title (already in `title`)
            path = [*path[: level - 2], heading]
        else:
            buf.append(line)
    flush()
    return sections


def chunk_markdown(markdown: str, title: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section_path, body in _sections(markdown, title):
        pieces: list[str] = []
        for block in _blocks(body):
            if block.startswith("|") and count_tokens(block) > TARGET_TOKENS:
                pieces.extend(_split_table(block, TARGET_TOKENS))
            else:
                pieces.append(block)
        cur: list[str] = []
        for piece in pieces:
            candidate = "\n\n".join([*cur, piece])
            if cur and count_tokens(candidate) > TARGET_TOKENS:
                chunks.append(
                    Chunk(len(chunks), section_path, "\n\n".join(cur), count_tokens("\n\n".join(cur)))
                )
                # one-paragraph overlap (skip if it would blow the budget or is a table)
                last = cur[-1]
                overlap_ok = not last.startswith("|") and count_tokens(last + piece) < MAX_TOKENS
                cur = [last, piece] if overlap_ok else [piece]
            else:
                cur.append(piece)
        if cur:
            text = "\n\n".join(cur)
            chunks.append(Chunk(len(chunks), section_path, text, count_tokens(text)))
    return chunks
