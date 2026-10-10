"""Token counting (tiktoken cl100k_base) with a character-based fallback."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from backend.core.logging import get_logger

log = get_logger(__name__)


@lru_cache(maxsize=1)
def _encoder() -> Any:
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception as exc:  # offline sandbox: encoding files cannot be downloaded
        log.info("tiktoken_unavailable_using_estimate", error=type(exc).__name__)
        return None


def count_tokens(text: str) -> int:
    enc = _encoder()
    if enc is None:
        return max(1, len(text) // 4)
    return len(enc.encode(text, disallowed_special=()))
