"""
Turn tool results into (a) the labelled <sources> block the synthesizer cites
and (b) structured citations the UI renders.

Data results are tagged [S1], [S2]…; retrieved policy passages [D1], [D2]…
"""

from __future__ import annotations

from typing import Any

_TOOL_LABEL = {
    "sql": "Insurance database",
    "providers": "Provider directory",
    "policy_search": "Policy documents",
}
_MAX_TABLE_CHARS = 7000


def build_sources(results: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    blocks: list[str] = []
    citations: list[dict[str, Any]] = []
    s_idx = d_idx = 0
    seen_chunks: set[str] = set()
    for r in results:
        if r["tool"] == "policy_search":
            if not r.get("ok"):
                blocks.append(f"(Policy search failed for: {r['task']})")
                continue
            for chunk in r.get("chunks", []):
                if chunk["chunk_id"] in seen_chunks:
                    continue
                seen_chunks.add(chunk["chunk_id"])
                d_idx += 1
                tag = f"D{d_idx}"
                blocks.append(f"[{tag}] Policy document — {chunk['section_path']}\n{chunk['content']}")
                citations.append(
                    {
                        "tag": tag,
                        "kind": "document",
                        "title": chunk["title"],
                        "section": chunk["section_path"],
                        "doc_id": chunk["doc_id"],
                        "chunk_id": chunk["chunk_id"],
                        "snippet": chunk["content"][:400],
                    }
                )
            continue
        s_idx += 1
        tag = f"S{s_idx}"
        label = _TOOL_LABEL.get(r["tool"], r["tool"])
        if not r.get("ok"):
            blocks.append(
                f"[{tag}] {label} — lookup FAILED for: {r['task']} (treat this data as unavailable)"
            )
            continue
        table = r.get("formatted") or "(no rows)"
        if len(table) > _MAX_TABLE_CHARS:
            table = table[:_MAX_TABLE_CHARS] + "\n… (truncated)"
        blocks.append(f"[{tag}] {label} — {r['task']}\n{table}")
        citations.append(
            {
                "tag": tag,
                "kind": "data",
                "tool": r["tool"],
                "title": f"{label}: {r['task']}",
                "detail": r.get("code", ""),
                "row_count": r.get("row_count", 0),
            }
        )
    return "\n\n".join(blocks), citations


def prior_context(results: list[dict[str, Any]], step_ids: list[str]) -> str:
    """Outputs of dependency steps, handed to a dependent step's tool."""
    parts = []
    for r in results:
        if r["step_id"] in step_ids and r.get("ok"):
            body = r.get("formatted", "")[:3000]
            parts.append(f"From step {r['step_id']} ({r['tool']}: {r['task']}):\n{body}")
    return "\n\n".join(parts)
