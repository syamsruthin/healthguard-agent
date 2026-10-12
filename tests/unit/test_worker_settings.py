from __future__ import annotations

import re
from pathlib import Path

from backend.workers.settings import WorkerSettings

BACKEND = Path(__file__).resolve().parents[2] / "backend"


def test_every_enqueued_job_has_a_worker_function() -> None:
    registered = {getattr(f, "name", None) or f.__name__ for f in WorkerSettings.functions}
    enqueued: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        enqueued |= set(re.findall(r"\.enqueue\(\s*\"(\w+)\"", path.read_text()))
    assert enqueued, "no enqueue calls found"
    assert enqueued <= registered, f"jobs without a worker function: {enqueued - registered}"
