"""
Structured logging (structlog) with request/user/conversation correlation.

Context such as `request_id`, `user_id`, `conversation_id` and `trace_id` is
bound with `structlog.contextvars` so every log line emitted while handling a
request carries it automatically. Obvious PII (emails, member IDs, DOBs, SSNs,
phone numbers) is masked before it reaches the sink.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

from backend.core.config import settings

_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "***-**-****"),  # SSN
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\b"), "<phone>"),
    (re.compile(r"\b(19|20)\d{2}-\d{2}-\d{2}\b"), "<date>"),
]

_SKIP_KEYS = {"event", "level", "timestamp", "logger", "request_id", "trace_id", "conversation_id"}


def mask_pii(text: str) -> str:
    for pattern, repl in _PII_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _pii_processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key, value in list(event_dict.items()):
        if key in _SKIP_KEYS:
            continue
        if isinstance(value, str):
            event_dict[key] = mask_pii(value)
    if isinstance(event_dict.get("event"), str):
        event_dict["event"] = mask_pii(event_dict["event"])
    return event_dict


def configure_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _pii_processor,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=level, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
