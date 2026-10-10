"""Minimal Server-Sent Events parser (pure; unit-tested)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any


def parse_sse(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Yield `{"type": event, **data}` for each event in a stream of SSE lines.

    Comment lines (`: ping`) are keep-alives and are skipped; a blank line ends an event;
    multiple `data:` lines are joined with newlines, as the SSE spec requires.
    """
    event: str | None = None
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if event is not None or data:
                yield _build(event, data)
            event, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if event is not None or data:
        yield _build(event, data)


def _build(event: str | None, data: list[str]) -> dict[str, Any]:
    body = "\n".join(data)
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload = {"raw": body}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return {"type": event or "message", **payload}
