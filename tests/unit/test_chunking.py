from __future__ import annotations

from backend.rag.chunking import MAX_TOKENS, chunk_markdown

DOC = """# Claims Guide

Intro paragraph about claims.

## Filing a claim

You must file within 180 days of service.

### Out-of-network claims

Submit an itemized bill.

## Denial codes

| Code | Meaning |
|---|---|
""" + "\n".join(
    f"| D{i:03d} | Reason number {i} explained in some detail for the member |" for i in range(1, 120)
)


def test_section_paths_follow_heading_hierarchy() -> None:
    chunks = chunk_markdown(DOC, "Claims Guide")
    paths = [c.section_path for c in chunks]
    assert paths[0] == "Claims Guide"
    assert "Claims Guide › Filing a claim" in paths
    assert "Claims Guide › Filing a claim › Out-of-network claims" in paths


def test_long_tables_split_with_repeated_header() -> None:
    chunks = [c for c in chunk_markdown(DOC, "Claims Guide") if "Denial codes" in c.section_path]
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.content.startswith("| Code | Meaning |")
        assert chunk.token_count <= MAX_TOKENS


def test_chunk_indexes_are_sequential() -> None:
    chunks = chunk_markdown(DOC, "Claims Guide")
    assert [c.index for c in chunks] == list(range(len(chunks)))
