"""FastAPI auth dependencies. Local JWT verification against Supabase.

Supabase issues user tokens with two possible signing schemes:
  * Legacy projects: HS256 signed with the shared JWT_SECRET.
  * Current projects: ES256 (asymmetric) with a rotating key set exposed at
    /auth/v1/.well-known/jwks.json.

We inspect the token header to pick the path. The JWKS client caches keys
in-memory so the hot path is a local verify after the first miss. Both
paths fail loudly on invalid tokens (§20 PR4).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Header, HTTPException, status
from jwt import PyJWKClient

from backend.config import get_settings
from backend.models.enums import UserRole


@dataclass(frozen=True)
class AuthUser:
    """Verified caller identity, extracted from a Supabase-issued JWT."""

    id: str
    email: str
    jwt: str
    role_claim: str


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
        )
    return authorization[7:].strip()


@lru_cache
def _jwks_client() -> PyJWKClient:
    s = get_settings()
    if not s.supabase_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SUPABASE_URL is not configured on the server.",
        )
    return PyJWKClient(f"{s.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json")


def _decode(token: str) -> dict:
    """Verify + decode according to the token header's alg. Symmetric HS256
    uses JWT_SECRET; asymmetric algs use JWKS."""
    unverified_header = jwt.get_unverified_header(token)
    alg = unverified_header.get("alg", "")
    if alg == "HS256":
        s = get_settings()
        if not s.supabase_jwt_secret:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="SUPABASE_JWT_SECRET is not configured on the server.",
            )
        return jwt.decode(
            token,
            s.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    if alg in ("ES256", "RS256"):
        signing_key = _jwks_client().get_signing_key_from_jwt(token).key
        return jwt.decode(
            token,
            signing_key,
            algorithms=[alg],
            audience="authenticated",
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Unsupported token alg: {alg}",
    )


def get_current_auth_user(authorization: str | None = Header(default=None)) -> AuthUser:
    """Verify a Supabase JWT and return the caller identity."""
    token = _extract_bearer(authorization)
    try:
        payload = _decode(token)
    except HTTPException:
        raise
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        ) from exc
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject claim",
        )
    return AuthUser(
        id=sub,
        email=payload.get("email") or "",
        jwt=token,
        role_claim=payload.get("role") or "authenticated",
    )


def require_admin_role(user_row_role: str) -> None:
    """Check the public.users.role column. Raises 403 if not admin.

    The JWT role claim is not authoritative — public.users.role is (§17, §20 PR2).
    """
    if user_row_role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
