"""
HTTP client for the HealthGuard API, used by every Streamlit page.

Tokens live in `st.session_state`; a 401 triggers one refresh-token rotation
and a retry. If the refresh fails the session is cleared and the user is
sent back to the login page.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import httpx
import streamlit as st

from frontend.lib.sse import parse_sse

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/")
API = f"{BACKEND_URL}/api/v1"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class SessionExpired(ApiError):
    pass


def _error(resp: httpx.Response) -> ApiError:
    try:
        err = resp.json().get("error", {})
        code, message = err.get("code", "error"), err.get("message", resp.text)
    except ValueError:
        code, message = "error", resp.text or resp.reason_phrase
    cls = SessionExpired if resp.status_code == 401 else ApiError
    return cls(resp.status_code, code, message)


class ApiClient:
    def __init__(self, base: str = API, timeout: float = 30.0):
        self.base = base
        self.http = httpx.Client(timeout=timeout)

    # ── token handling ──────────────────────────────────────────────────
    @property
    def tokens(self) -> dict[str, Any] | None:
        return st.session_state.get("tokens")

    def _store(self, tokens: dict[str, Any]) -> None:
        st.session_state["tokens"] = tokens
        st.session_state["user"] = tokens["user"]

    def _headers(self) -> dict[str, str]:
        tokens = self.tokens
        return {"Authorization": f"Bearer {tokens['access_token']}"} if tokens else {}

    def _refresh(self) -> bool:
        tokens = self.tokens
        if not tokens:
            return False
        resp = self.http.post(f"{self.base}/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        if resp.status_code != 200:
            clear_session()
            return False
        self._store(resp.json())
        return True

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self.http.request(method, f"{self.base}{path}", headers=self._headers(), **kwargs)
        if resp.status_code == 401 and self._refresh():
            resp = self.http.request(method, f"{self.base}{path}", headers=self._headers(), **kwargs)
        if resp.status_code >= 400:
            raise _error(resp)
        return resp.json() if resp.content else None

    # ── auth ────────────────────────────────────────────────────────────
    def login(self, email: str, password: str) -> dict[str, Any]:
        resp = self.http.post(f"{self.base}/auth/login", json={"email": email, "password": password})
        if resp.status_code != 200:
            raise _error(resp)
        self._store(resp.json())
        return resp.json()["user"]

    def register(self, email: str, password: str, full_name: str) -> dict[str, Any]:
        resp = self.http.post(
            f"{self.base}/auth/register", json={"email": email, "password": password, "full_name": full_name}
        )
        if resp.status_code != 201:
            raise _error(resp)
        self._store(resp.json())
        return resp.json()["user"]

    def logout(self) -> None:
        tokens = self.tokens
        if tokens:
            try:
                self.http.post(f"{self.base}/auth/logout", json={"refresh_token": tokens["refresh_token"]})
            except httpx.HTTPError:
                pass
        clear_session()

    # ── conversations & chat ────────────────────────────────────────────
    def conversations(self) -> list[dict[str, Any]]:
        return self.request("GET", "/conversations")

    def create_conversation(self) -> dict[str, Any]:
        return self.request("POST", "/conversations", json={})

    def delete_conversation(self, cid: str) -> None:
        self.request("DELETE", f"/conversations/{cid}")

    def rename_conversation(self, cid: str, title: str) -> dict[str, Any]:
        return self.request("PATCH", f"/conversations/{cid}", json={"title": title})

    def messages(self, cid: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/conversations/{cid}/messages")

    def stream(self, cid: str, message: str) -> Iterator[dict[str, Any]]:
        """Yield SSE events for one turn (refreshing the access token first if needed)."""
        for attempt in range(2):
            with self.http.stream(
                "POST",
                f"{self.base}/conversations/{cid}/messages/stream",
                json={"message": message},
                headers=self._headers(),
                timeout=httpx.Timeout(30.0, read=180.0),
            ) as resp:
                if resp.status_code == 401 and attempt == 0 and self._refresh():
                    continue
                if resp.status_code >= 400:
                    resp.read()
                    raise _error(resp)
                yield from parse_sse(resp.iter_lines())
                return

    def feedback(self, message_id: str, rating: int, comment: str | None = None) -> None:
        self.request("POST", f"/messages/{message_id}/feedback", json={"rating": rating, "comment": comment})

    # ── me ──────────────────────────────────────────────────────────────
    def coverage(self) -> dict[str, Any]:
        return self.request("GET", "/me/coverage")

    def memories(self) -> list[dict[str, Any]]:
        return self.request("GET", "/me/memories")

    def delete_memory(self, memory_id: str) -> None:
        self.request("DELETE", f"/me/memories/{memory_id}")

    def clear_memories(self) -> None:
        self.request("DELETE", "/me/memories")

    def set_memory_enabled(self, enabled: bool) -> dict[str, Any]:
        user = self.request("PATCH", "/me/settings", json={"memory_enabled": enabled})
        st.session_state["user"] = user
        if self.tokens:
            self.tokens["user"] = user
        return user

    # ── admin ───────────────────────────────────────────────────────────
    def metrics(self, days: int) -> dict[str, Any]:
        return self.request("GET", "/admin/metrics/summary", params={"days": days})

    def eval_runs(self) -> list[dict[str, Any]]:
        return self.request("GET", "/admin/evals/runs")

    def start_eval(self, limit: int | None = None) -> dict[str, Any]:
        return self.request("POST", "/admin/evals/run", json={"limit": limit})

    def trace(self, trace_id: str) -> dict[str, Any]:
        return self.request("GET", f"/admin/traces/{trace_id}")


def clear_session() -> None:
    for key in ("tokens", "user", "conversation_id", "messages"):
        st.session_state.pop(key, None)


def client() -> ApiClient:
    if "api" not in st.session_state:
        st.session_state["api"] = ApiClient()
    api: ApiClient = st.session_state["api"]
    return api
