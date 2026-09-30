"""Local JWT verification. Does not require Supabase — uses a local secret."""
from __future__ import annotations

import os
import time
import uuid

import jwt
import pytest
from fastapi import HTTPException

from backend.auth.dependencies import _extract_bearer, get_current_auth_user
from backend.config import get_settings


TEST_SECRET = "test-jwt-secret-for-unit-tests-do-not-use-in-prod"


@pytest.fixture(autouse=True)
def _set_test_secret(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", TEST_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_jwt(**overrides) -> str:
    now = int(time.time())
    payload = {
        "sub": str(uuid.uuid4()),
        "email": "student@lnmiit.example",
        "aud": "authenticated",
        "role": "authenticated",
        "iat": now,
        "exp": now + 300,
    }
    payload.update(overrides)
    return jwt.encode(payload, TEST_SECRET, algorithm="HS256")


def test_get_current_auth_user_accepts_valid_token():
    token = _make_jwt()
    user = get_current_auth_user(authorization=f"Bearer {token}")
    assert user.email == "student@lnmiit.example"
    assert user.jwt == token
    assert user.role_claim == "authenticated"


def test_missing_authorization_header_returns_401():
    with pytest.raises(HTTPException) as exc:
        get_current_auth_user(authorization=None)
    assert exc.value.status_code == 401


def test_non_bearer_header_returns_401():
    with pytest.raises(HTTPException) as exc:
        get_current_auth_user(authorization="Basic abc123")
    assert exc.value.status_code == 401


def test_tampered_token_returns_401():
    token = _make_jwt()
    tampered = token[:-4] + "AAAA"
    with pytest.raises(HTTPException) as exc:
        get_current_auth_user(authorization=f"Bearer {tampered}")
    assert exc.value.status_code == 401


def test_expired_token_returns_401():
    now = int(time.time())
    token = _make_jwt(iat=now - 3600, exp=now - 60)
    with pytest.raises(HTTPException) as exc:
        get_current_auth_user(authorization=f"Bearer {token}")
    assert exc.value.status_code == 401


def test_wrong_audience_returns_401():
    token = _make_jwt(aud="anon")
    with pytest.raises(HTTPException) as exc:
        get_current_auth_user(authorization=f"Bearer {token}")
    assert exc.value.status_code == 401


def test_missing_jwt_secret_returns_500(monkeypatch, tmp_path):
    # Chdir to a directory with no .env so pydantic-settings can't rehydrate the secret
    # from disk after we clear it from the process env.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    get_settings.cache_clear()
    token = _make_jwt()
    with pytest.raises(HTTPException) as exc:
        get_current_auth_user(authorization=f"Bearer {token}")
    assert exc.value.status_code == 500


def test_extract_bearer_strips_prefix_case_insensitively():
    assert _extract_bearer("bearer abc") == "abc"
    assert _extract_bearer("BEARER def") == "def"
