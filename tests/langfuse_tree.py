"""Read Langfuse observations back from an in-memory OTel exporter (see the `langfuse_spans` fixture)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from backend.core import observability


@dataclass
class Node:
    name: str
    span_id: int
    parent_id: int | None
    trace_id: str
    attrs: dict[str, Any]
    children: list[Node] = field(default_factory=list)

    @property
    def type(self) -> str:
        return str(self.attrs.get("langfuse.observation.type", ""))

    @property
    def level(self) -> str:
        return str(self.attrs.get("langfuse.observation.level", "DEFAULT"))

    def _json(self, key: str) -> Any:
        raw = self.attrs.get(f"langfuse.observation.{key}")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    @property
    def input(self) -> Any:
        return self._json("input")

    @property
    def output(self) -> Any:
        return self._json("output")

    @property
    def usage(self) -> Any:
        return self._json("usage_details")

    @property
    def cost(self) -> Any:
        return self._json("cost_details")

    def meta(self, key: str) -> Any:
        raw = self.attrs.get(f"langfuse.observation.metadata.{key}")
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return raw

    def child_names(self) -> list[str]:
        return [c.name for c in self.children]

    def child(self, name: str) -> Node:
        return next(c for c in self.children if c.name == name)

    def find(self, name: str) -> list[Node]:
        """Every descendant (and self) with this name, depth first."""
        found = [self] if self.name == name else []
        for c in self.children:
            found += c.find(name)
        return found


def collect(exporter: InMemorySpanExporter) -> list[Node]:
    """Root observations (with their subtrees), ordered by start time."""
    observability.flush()
    spans = sorted(exporter.get_finished_spans(), key=lambda s: s.start_time or 0)
    nodes = {
        s.context.span_id: Node(
            name=s.name,
            span_id=s.context.span_id,
            parent_id=s.parent.span_id if s.parent else None,
            trace_id=format(s.context.trace_id, "032x"),
            attrs=dict(s.attributes or {}),
        )
        for s in spans
    }
    roots = []
    for node in nodes.values():
        parent = nodes.get(node.parent_id) if node.parent_id else None
        (parent.children if parent else roots).append(node)
    return roots
