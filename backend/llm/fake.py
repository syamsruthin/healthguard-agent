"""
Deterministic, scripted LLM for tests (no network).

Responses are looked up by the call `name` (e.g. "understand", "sql",
"providers", "synthesize", "verify"). A response can be a string, a
Pydantic model / dict (for structured calls), a list (consumed in order
across repeated calls; the last element repeats), a callable
`(messages) -> value`, or an exception instance, which is raised (use it to
simulate provider failures such as `LLMError`).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from pydantic import BaseModel

from backend.llm.client import LLMResult, extract_json
from backend.llm.registry import Role, spec_for
from backend.llm.usage import LLMCallRecord, record


class FakeLLM:
    def __init__(self, script: dict[str, Any] | None = None):
        self.script: dict[str, Any] = dict(script or {})
        self.calls: list[dict[str, Any]] = []

    def _resolve(self, name: str, messages: list[dict[str, Any]]) -> Any:
        key = name if name in self.script else name.split(".")[0]
        if key not in self.script:
            raise KeyError(f"FakeLLM has no scripted response for call '{name}'")
        value = self.script[key]
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if callable(value) and not isinstance(value, type):
            value = value(messages)
        if isinstance(value, BaseException):
            raise value
        return value

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, BaseModel):
            return value.model_dump_json()
        if isinstance(value, dict | list):
            return json.dumps(value)
        return str(value)

    def _record(self, role: Role, name: str, messages: list[dict[str, Any]]) -> None:
        role = Role(role)
        self.calls.append({"role": role.value, "name": name, "messages": messages})
        record(
            LLMCallRecord(
                role=role.value,
                name=name,
                model=spec_for(role).model,
                prompt_tokens=sum(len(str(m.get("content", ""))) // 4 for m in messages),
                completion_tokens=50,
                cached_tokens=0,
            )
        )

    async def complete(self, role: Role, messages: list[dict[str, Any]], *, name: str, **_: Any) -> LLMResult:
        self._record(role, name, messages)
        return LLMResult(text=self._text(self._resolve(name, messages)), model=spec_for(role).model, usage={})

    async def structured(
        self, role: Role, messages: list[dict[str, Any]], schema: type[BaseModel], *, name: str, **_: Any
    ) -> Any:
        self._record(role, name, messages)
        value = self._resolve(name, messages)
        if isinstance(value, schema):
            return value
        if isinstance(value, dict):
            return schema.model_validate(value)
        return schema.model_validate_json(extract_json(str(value)))

    async def stream(
        self, role: Role, messages: list[dict[str, Any]], *, name: str, **_: Any
    ) -> AsyncIterator[str]:
        self._record(role, name, messages)
        text = self._text(self._resolve(name, messages))
        for i in range(0, len(text), 12):
            yield text[i : i + 12]

    def names(self) -> list[str]:
        return [c["name"] for c in self.calls]


Responder = Callable[[list[dict[str, Any]]], Any]
