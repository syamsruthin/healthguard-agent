"""
`hg-ingest` — idempotent policy-corpus ingestion into pgvector.

Documents are re-embedded only when their content hash changes; removed
documents are deleted; any change bumps `rag.data_version`, which
invalidates semantic-cache entries built on older data.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import time
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import delete, select, text

from backend.core.config import settings
from backend.core.logging import configure_logging, get_logger
from backend.data_gen.corpus import parse_front_matter
from backend.db.models import Chunk, Document
from backend.db.session import SessionLocal
from backend.llm.embeddings import get_embedder
from backend.rag.chunking import chunk_markdown

log = get_logger(__name__)


def _load_corpus(root: Path) -> list[dict]:
    docs = []
    for path in sorted(root.rglob("*.md")):
        raw = path.read_text()
        meta, body = parse_front_matter(raw)
        if "doc_id" not in meta:
            continue
        docs.append(
            {
                "doc_id": meta["doc_id"],
                "title": meta.get("title", path.stem),
                "category": meta.get("category", "general"),
                "plan_id": meta.get("plan_id"),
                "effective_date": date.fromisoformat(meta["effective_date"])
                if meta.get("effective_date")
                else None,
                "source_path": str(path.relative_to(root)),
                "body": body,
                "content_hash": hashlib.sha256(raw.encode()).hexdigest(),
            }
        )
    return docs


async def ingest(force: bool = False) -> dict[str, int]:
    t0 = time.monotonic()
    docs = _load_corpus(settings.corpus_dir)
    if not docs:
        raise SystemExit(f"No documents found in {settings.corpus_dir} — run `hg-seed` first.")
    embedder = get_embedder()
    stats = {"documents": len(docs), "updated": 0, "unchanged": 0, "removed": 0, "chunks": 0}
    async with SessionLocal() as session:
        existing = {d.doc_id: d for d in (await session.execute(select(Document))).scalars()}
        for doc in docs:
            current = existing.pop(doc["doc_id"], None)
            if current and current.content_hash == doc["content_hash"] and not force:
                stats["unchanged"] += 1
                continue
            chunks = chunk_markdown(doc["body"], doc["title"])
            texts = [f"{c.section_path}\n\n{c.content}" for c in chunks]
            vectors = await embedder.embed(texts)
            await session.execute(delete(Chunk).where(Chunk.doc_id == doc["doc_id"]))
            if current:
                for key in ("title", "category", "plan_id", "effective_date", "source_path", "content_hash"):
                    setattr(current, key, doc[key])
                current.updated_at = datetime.now(UTC)
            else:
                session.add(Document(**{k: v for k, v in doc.items() if k != "body"}))
            await session.flush()
            for c, vec in zip(chunks, vectors, strict=True):
                cid = hashlib.sha256(f"{doc['doc_id']}:{c.index}:{c.content}".encode()).hexdigest()[:40]
                session.add(
                    Chunk(
                        chunk_id=cid,
                        doc_id=doc["doc_id"],
                        chunk_index=c.index,
                        section_path=c.section_path,
                        content=c.content,
                        token_count=c.token_count,
                        embedding=vec,
                    )
                )
            stats["updated"] += 1
            stats["chunks"] += len(chunks)
        for stale in existing.values():
            await session.execute(delete(Document).where(Document.doc_id == stale.doc_id))
            stats["removed"] += 1
        if stats["updated"] or stats["removed"]:
            await session.execute(
                text(
                    "INSERT INTO rag.data_version (id, version, updated_at) VALUES (1, 1, now()) "
                    "ON CONFLICT (id) DO UPDATE SET version = rag.data_version.version + 1, updated_at = now()"
                )
            )
        await session.commit()
        total = (await session.execute(text("SELECT count(*) FROM rag.chunks"))).scalar_one()
    stats["total_chunks"] = int(total)
    log.info("ingest_complete", seconds=round(time.monotonic() - t0, 1), **stats)
    print(f"✅ Ingest complete in {time.monotonic() - t0:.1f}s — {stats}")
    return stats


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Chunk + embed the policy corpus into pgvector")
    parser.add_argument("--force", action="store_true", help="re-embed every document")
    args = parser.parse_args()
    asyncio.run(ingest(force=args.force))


if __name__ == "__main__":
    main()
