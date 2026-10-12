from __future__ import annotations

import pytest

from backend.core.config import settings
from tests.integration.api.conftest import API, auth, login, register


async def test_register_login_me(client) -> None:
    tokens = await register(client)
    assert tokens["user"]["role"] == "member" and tokens["user"]["member_id"] is None
    me = await client.get(f"{API}/auth/me", headers=auth(tokens))
    assert me.status_code == 200 and me.json()["email"] == tokens["user"]["email"]


async def test_duplicate_email_and_weak_password(client) -> None:
    tokens = await register(client)
    dup = await client.post(
        f"{API}/auth/register",
        json={"email": tokens["user"]["email"], "password": "another-long-password", "full_name": "X"},
    )
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "email_taken"
    weak = await client.post(
        f"{API}/auth/register", json={"email": "w@example.com", "password": "short", "full_name": "W"}
    )
    assert weak.status_code == 400 and weak.json()["error"]["code"] == "weak_password"


async def test_bad_credentials_and_tokens(client) -> None:
    bad = await client.post(
        f"{API}/auth/login", json={"email": "maria.garcia@demo.healthguard.dev", "password": "nope"}
    )
    unknown = await client.post(f"{API}/auth/login", json={"email": "ghost@example.com", "password": "nope"})
    assert bad.status_code == unknown.status_code == 401
    assert bad.json()["error"]["code"] == unknown.json()["error"]["code"] == "invalid_credentials"
    assert (await client.get(f"{API}/auth/me")).status_code == 401
    assert (await client.get(f"{API}/auth/me", headers={"Authorization": "Bearer junk"})).status_code == 401


async def test_refresh_rotation_and_reuse_detection(client) -> None:
    first = await login(client, "maria.garcia@demo.healthguard.dev")
    second = (await client.post(f"{API}/auth/refresh", json={"refresh_token": first["refresh_token"]})).json()
    assert second["refresh_token"] != first["refresh_token"]
    # Replaying the already-rotated token revokes the whole family …
    replay = await client.post(f"{API}/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "refresh_token_reused"
    # … so the legitimate successor stops working too.
    after = await client.post(f"{API}/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert after.status_code == 401


async def test_logout_revokes_refresh_token(client) -> None:
    tokens = await login(client, "james.chen@demo.healthguard.dev")
    assert (
        await client.post(f"{API}/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    ).status_code == 204
    again = await client.post(f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert again.status_code == 401


async def test_login_rate_limit(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_auth_per_minute", 2)
    codes = [
        (
            await client.post(f"{API}/auth/login", json={"email": "x@example.com", "password": "nope"})
        ).status_code
        for _ in range(3)
    ]
    assert codes == [401, 401, 429]


async def test_health_and_security_headers(client) -> None:
    ready = await client.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["checks"] == {"postgres": "ok", "redis": "ok"}
    live = await client.get("/health/live", headers={"X-Request-ID": "abc-123"})
    assert live.headers["x-request-id"] == "abc-123"
    assert live.headers["x-content-type-options"] == "nosniff"


async def test_body_size_limit(client) -> None:
    resp = await client.post(
        f"{API}/auth/login", content=b"x" * (70 * 1024), headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 413
