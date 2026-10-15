"""
Shared test configuration.

Environment overrides are applied *before* any backend module is imported,
because `backend.core.config.settings` is instantiated at import time and
environment variables take priority over `.env`.
"""

from __future__ import annotations

import os

os.environ["ENV"] = "test"
os.environ["LLM_EXACT_CACHE_ENABLED"] = "false"
os.environ["LOG_JSON"] = "false"
os.environ.setdefault("JWT_SECRET", "test-only-secret-value-that-is-long-enough-123")
if os.environ.get("HG_LIVE") != "1":
    # Unit and integration tests must never reach a real model.
    os.environ["OPENROUTER_API_KEY"] = ""
    os.environ["EMBEDDING_PROVIDER"] = "hash"
    # ...nor export traces; `langfuse_spans` installs an in-memory exporter where a test needs one.
    os.environ["LANGFUSE_ENABLED"] = "false"

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from backend.core import observability
from backend.llm.client import set_llm
from backend.llm.fake import FakeLLM

_TESTS_DIR = Path(__file__).parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        parts = Path(str(item.fspath)).relative_to(_TESTS_DIR).parts
        if parts and parts[0] == "integration":
            item.add_marker(pytest.mark.integration)
        elif parts and parts[0] == "live":
            item.add_marker(pytest.mark.live)


@pytest.fixture
def fake_llm() -> Iterator[Callable[[dict[str, Any]], FakeLLM]]:
    """Install a scripted FakeLLM: `llm = fake_llm({"understand": {...}, ...})`."""

    def install(script: dict[str, Any]) -> FakeLLM:
        llm = FakeLLM(script)
        set_llm(llm)
        return llm

    yield install
    set_llm(None)


@pytest.fixture
def langfuse_spans() -> Iterator[InMemorySpanExporter]:
    """Real Langfuse SDK wired to an in-memory exporter (no network); yields the exporter.
    Call `observability.flush()` before reading `get_finished_spans()`."""
    exporter = InMemorySpanExporter()
    client = observability._build_client(
        public_key=f"pk-lf-test-{uuid.uuid4()}",  # one SDK resource set per test
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:9",
        span_exporter=exporter,
    )
    observability.set_langfuse(client)
    yield exporter
    client.shutdown()
    observability.set_langfuse(None)
