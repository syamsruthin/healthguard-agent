from __future__ import annotations

import uuid

import jwt
import pytest

from backend.core.config import settings
from backend.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    hash_token,
    new_refresh_token,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    h = hash_password("correct horse battery")
    assert h.startswith("$argon2id$")
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", "not-a-hash")


def test_access_token_roundtrip() -> None:
    uid = uuid.uuid4()
    token, ttl = create_access_token(user_id=uid, role="member")
    claims = decode_access_token(token)
    assert claims["sub"] == str(uid)
    assert claims["role"] == "member"
    assert not {"email", "mid"} & set(claims)  # bearer tokens carry no PII
    assert ttl == settings.access_token_ttl_minutes * 60


def test_rejects_wrong_type_and_tampering() -> None:
    forged = jwt.encode(
        {"sub": "x", "iss": settings.jwt_issuer, "exp": 9_999_999_999, "typ": "refresh"},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(forged)
    token, _ = create_access_token(user_id=uuid.uuid4(), role="admin")
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1])


def test_expired_token_rejected() -> None:
    expired = jwt.encode(
        {"sub": "x", "iss": settings.jwt_issuer, "exp": 1, "typ": "access"},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(expired)


def test_refresh_tokens_are_random_and_hashed() -> None:
    raw1, h1 = new_refresh_token()
    raw2, _ = new_refresh_token()
    assert raw1 != raw2
    assert h1 == hash_token(raw1) and len(h1) == 64
