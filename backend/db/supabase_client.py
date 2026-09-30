"""Supabase clients. Two flavors:

- service-role client: bypasses RLS. Use for observability writes, admin ops, mirror-row
  creation on onboarding. NEVER use to serve user data reads that must be RLS-filtered.
- user client: anon key + user JWT. RLS enforced. Use for anything a user does on their
  own data. Created per-request so JWT-scoped state is not shared across requests.

Fails loudly if required env vars are missing (§20 PR4). No silent fallbacks.
"""
from __future__ import annotations

from functools import lru_cache

from supabase import Client, create_client

from backend.config import get_settings


class SupabaseConfigError(RuntimeError):
    """Required Supabase environment variable is missing."""


def _require(name: str, value: str) -> str:
    if not value:
        raise SupabaseConfigError(
            f"{name} is not set. Configure it in .env before starting the backend."
        )
    return value


@lru_cache
def get_service_role_client() -> Client:
    """Singleton service-role client. Bypasses RLS. Use with care."""
    s = get_settings()
    url = _require("SUPABASE_URL", s.supabase_url)
    key = _require("SUPABASE_SERVICE_ROLE_KEY", s.supabase_service_role_key)
    return create_client(url, key)


def get_user_client(jwt: str) -> Client:
    """Per-request client bound to the caller's JWT. RLS enforced."""
    if not jwt:
        raise SupabaseConfigError("JWT is required to create a user-scoped client.")
    s = get_settings()
    url = _require("SUPABASE_URL", s.supabase_url)
    key = _require("SUPABASE_ANON_KEY", s.supabase_anon_key)
    client = create_client(url, key)
    # Bind the user's JWT so PostgREST sends it as the Authorization header,
    # which causes auth.uid() in RLS policies to resolve to this user.
    client.postgrest.auth(jwt)
    return client
