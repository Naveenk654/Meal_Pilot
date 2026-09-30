"""Test config. DB-hitting tests skip cleanly when Supabase env vars are missing."""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Iterator

import pytest
from dotenv import load_dotenv

# Load .env so DB-hitting tests can find SUPABASE_* when run outside CI.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _supabase_configured() -> bool:
    return bool(
        os.environ.get("SUPABASE_URL")
        and os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    )


requires_db = pytest.mark.skipif(
    not _supabase_configured(),
    reason="SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY not set — integration tests skipped.",
)

requires_jwt_secret = pytest.mark.skipif(
    not os.environ.get("SUPABASE_JWT_SECRET"),
    reason="SUPABASE_JWT_SECRET not set — auth tests skipped.",
)


@pytest.fixture(scope="session")
def svc_client():
    if not _supabase_configured():
        pytest.skip("Supabase not configured")
    from backend.db.supabase_client import get_service_role_client

    return get_service_role_client()


@pytest.fixture
def ephemeral_auth_user(svc_client) -> Iterator[dict]:
    """Create a Supabase auth user for the test, delete it in teardown.

    Returns { 'id': uuid, 'email': str }. The user is confirmed and can be signed
    a JWT via the shared JWT secret for RLS-scoped tests.
    """
    email = f"m1test+{uuid.uuid4().hex[:12]}@example.com"
    created = svc_client.auth.admin.create_user(
        {"email": email, "password": uuid.uuid4().hex, "email_confirm": True}
    )
    # supabase-py returns an object with .user; sometimes a dict. Normalize.
    user = getattr(created, "user", None) or created["user"]
    user_id = str(user.id if hasattr(user, "id") else user["id"])

    # Mirror into public.users so FK-dependent tables (daily_plans, meal_logs,
    # user_profile) accept the id. This matches the onboarding endpoint's behavior.
    svc_client.table("users").upsert(
        {"id": user_id, "email": email, "mode": "general"}, on_conflict="id"
    ).execute()

    try:
        yield {"id": user_id, "email": email}
    finally:
        try:
            # public.users has ON DELETE CASCADE on the auth.users FK, so deleting
            # the auth user tears down the mirror row and everything downstream.
            svc_client.auth.admin.delete_user(user_id)
        except Exception:
            pass
