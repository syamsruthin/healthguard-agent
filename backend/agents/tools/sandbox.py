"""
Sandboxed execution of LLM-generated pandas code.

Defence in depth:
1. AST validation — no imports, no dunders, no dangerous builtins, no I/O
   or dynamic-evaluation attributes (read_*/to_csv/eval/query/...).
2. Restricted builtins in the exec namespace.
3. Separate *spawned* worker processes (process pool) with an address-space
   rlimit, a wall-clock timeout (the pool is recycled on timeout), and only
   in-memory copies of the CSV data lake.
4. Result size caps.
"""

from __future__ import annotations

import ast
import asyncio
import math
import multiprocessing as mp
import resource
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

from backend.core.config import settings
from backend.core.logging import get_logger

log = get_logger(__name__)


class SandboxError(ValueError):
    pass


_DENY_NAMES = {
    "eval",
    "exec",
    "compile",
    "open",
    "__import__",
    "getattr",
    "setattr",
    "delattr",
    "globals",
    "locals",
    "vars",
    "input",
    "breakpoint",
    "exit",
    "quit",
    "help",
    "memoryview",
    "dir",
    "type",
    "object",
    "super",
    "classmethod",
    "staticmethod",
    "property",
    "os",
    "sys",
    "subprocess",
    "importlib",
    "builtins",
}
_DENY_ATTRS = {
    "eval",
    "query",
    "pipe",
    "tofile",
    "fromfile",
    "load",
    "loads",
    "save",
    "savez",
    "savetxt",
    "loadtxt",
    "genfromtxt",
    "memmap",
    "system",
    "popen",
    "lib",
    "ctypeslib",
    "io",
    "testing",
    "api",
    "plotting",
    "style",
    "to_clipboard",
    "to_pickle",
    "read_pickle",
    "f2py",
    "distutils",
    "frombuffer",
}
_ALLOWED_TO = {
    "to_dict",
    "to_list",
    "to_numpy",
    "to_string",
    "to_markdown",
    "to_frame",
    "to_datetime",
    "to_numeric",
    "to_timedelta",
    "to_period",
    "to_records",
}
_FORBIDDEN_NODES = (
    ast.Import,
    ast.ImportFrom,
    ast.Global,
    ast.Nonlocal,
    ast.ClassDef,
    ast.AsyncFunctionDef,
    ast.Await,
    ast.Yield,
    ast.YieldFrom,
    ast.With,
    ast.AsyncWith,
    ast.Raise,
    ast.Try,
)


def validate_code(code: str) -> ast.Module:
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise SandboxError(f"syntax error: {exc.msg} (line {exc.lineno})") from exc
    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_NODES):
            raise SandboxError(f"forbidden construct: {type(node).__name__}")
        if isinstance(node, ast.Name) and (node.id in _DENY_NAMES or node.id.startswith("__")):
            raise SandboxError(f"forbidden name: {node.id}")
        if isinstance(node, ast.Attribute):
            attr = node.attr
            if attr.startswith("_"):
                raise SandboxError(f"forbidden attribute: {attr}")
            if attr in _DENY_ATTRS or attr.startswith("read_"):
                raise SandboxError(f"forbidden attribute: {attr}")
            if attr.startswith("to_") and attr not in _ALLOWED_TO:
                raise SandboxError(f"forbidden attribute: {attr}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value:
            raise SandboxError("dunder strings are not allowed")
    if not any(
        isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "result" for t in n.targets)
        for n in ast.walk(tree)
    ):
        raise SandboxError("code must assign the final answer to `result`")
    return tree


# ═══════════════════════════════════════════════════════════════════════════
# Worker process
# ═══════════════════════════════════════════════════════════════════════════
_FRAMES: dict[str, Any] = {}
_SAFE_BUILTINS: dict[str, Any] = {}


def _init_worker(lake_dir: str, memory_mb: int) -> None:  # pragma: no cover - runs in child process
    import builtins

    import numpy as np
    import pandas as pd

    lake = Path(lake_dir)
    bool_cols = {
        "is_pcp",
        "accepting_new_patients",
        "board_certified",
        "telehealth_available",
        "has_emergency_room",
        "open_24_hours",
        "accepts_walk_ins",
        "drive_through",
        "delivery_available",
        "vaccinations_offered",
        "compounding",
    }
    for name, file in (
        ("providers", "providers.csv"),
        ("facilities", "facilities.csv"),
        ("pharmacies", "pharmacies.csv"),
        ("zips", "zip_centroids.csv"),
    ):
        df = pd.read_csv(lake / file, dtype={"zip_code": str, "npi": str})
        for col in bool_cols & set(df.columns):
            df[col] = df[col].astype(str).str.lower().eq("true")
        for col in ("languages", "networks", "hospital_affiliation", "trauma_level"):
            if col in df.columns:
                df[col] = df[col].fillna("").astype(str)
        _FRAMES[name] = df
    allowed = [
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "filter",
        "float",
        "int",
        "isinstance",
        "len",
        "list",
        "map",
        "max",
        "min",
        "range",
        "reversed",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
        "True",
        "False",
        "None",
        "KeyError",
        "ValueError",
        "Exception",
    ]
    _SAFE_BUILTINS.update({k: getattr(builtins, k) for k in allowed if hasattr(builtins, k)})
    _FRAMES["_pd"], _FRAMES["_np"] = pd, np
    try:
        with open("/proc/self/status") as fh:
            vm_kb = next(int(line.split()[1]) for line in fh if line.startswith("VmSize"))
        limit = (vm_kb * 1024) + memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (OSError, StopIteration, ValueError):  # non-Linux: no /proc → rely on the timeout
        return


def _distance_miles(lat: float, lon: float, lats: Any, lons: Any) -> Any:  # pragma: no cover - child
    np = _FRAMES["_np"]
    lat1, lon1 = math.radians(float(lat)), math.radians(float(lon))
    lat2, lon2 = np.radians(lats.astype(float)), np.radians(lons.astype(float))
    a = np.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * np.arcsin(np.sqrt(a))


def _zip_location(zip_code: Any) -> tuple[float, float]:  # pragma: no cover - child
    zips = _FRAMES["zips"]
    row = zips[zips["zip_code"] == str(zip_code).zfill(5)]
    if row.empty:
        raise KeyError(f"unknown zip code {zip_code}")
    return float(row.iloc[0]["latitude"]), float(row.iloc[0]["longitude"])


def _serialise(value: Any, max_rows: int) -> dict[str, Any]:  # pragma: no cover - child
    pd = _FRAMES["_pd"]
    if isinstance(value, pd.Series):
        value = (
            value.reset_index()
            if value.index.name or not value.index.equals(pd.RangeIndex(len(value)))
            else value.to_frame()
        )
    if isinstance(value, pd.DataFrame):
        total = len(value)
        df = value.head(max_rows).copy()
        for col in df.columns:
            if str(df[col].dtype).startswith(("float", "Float")):
                df[col] = df[col].round(3)
        df = df.astype(object).where(df.notna(), None)
        return {
            "type": "table",
            "columns": [str(c) for c in df.columns],
            "rows": [{str(k): _plain(v) for k, v in r.items()} for r in df.to_dict("records")],
            "total_rows": total,
        }
    if isinstance(value, dict):
        return {"type": "dict", "value": {str(k): _plain(v) for k, v in list(value.items())[:max_rows]}}
    if isinstance(value, list | tuple):
        return {"type": "list", "value": [_plain(v) for v in list(value)[:max_rows]]}
    return {"type": "scalar", "value": _plain(value)}


def _plain(v: Any) -> Any:  # pragma: no cover - child
    if hasattr(v, "item") and callable(v.item):
        try:
            return v.item()
        except Exception:
            return str(v)
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, int | float | str | bool) or v is None:
        return v
    return str(v)


def _execute(code: str, max_rows: int) -> dict[str, Any]:  # pragma: no cover - child
    validate_code(code)
    namespace: dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        "pd": _FRAMES["_pd"],
        "np": _FRAMES["_np"],
        "distance_miles": _distance_miles,
        "zip_location": _zip_location,
    }
    for name in ("providers", "facilities", "pharmacies", "zips"):
        namespace[name] = _FRAMES[name].copy()
    exec(compile(code, "<agent-code>", "exec"), namespace)  # noqa: S102 - validated + isolated process
    if "result" not in namespace:
        raise SandboxError("code did not assign `result`")
    return _serialise(namespace["result"], max_rows)


# ═══════════════════════════════════════════════════════════════════════════
# Parent-side pool management
# ═══════════════════════════════════════════════════════════════════════════
_pool: ProcessPoolExecutor | None = None


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(
            max_workers=settings.sandbox_workers,
            mp_context=mp.get_context("spawn"),
            initializer=_init_worker,
            initargs=(str(settings.lake_dir), settings.sandbox_memory_mb),
        )
    return _pool


def _reset_pool() -> None:
    global _pool
    if _pool is not None:
        for proc in list(getattr(_pool, "_processes", {}).values()):
            proc.kill()
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


async def run_code(code: str) -> dict[str, Any]:
    validate_code(code)  # fail fast in the parent
    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(_get_pool(), _execute, code, settings.sandbox_max_rows)
        return await asyncio.wait_for(future, timeout=settings.sandbox_timeout_seconds)
    except TimeoutError as exc:
        _reset_pool()
        raise SandboxError(f"code exceeded the {settings.sandbox_timeout_seconds:.0f}s time limit") from exc
    except BrokenProcessPool as exc:
        _reset_pool()
        raise SandboxError("sandbox worker crashed (likely memory limit exceeded)") from exc
    except MemoryError as exc:
        raise SandboxError("code exceeded the memory limit") from exc


def shutdown() -> None:
    _reset_pool()
