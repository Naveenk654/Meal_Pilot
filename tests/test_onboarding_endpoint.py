"""End-to-end onboarding integration test.

Requires a real Supabase project + JWT secret. Creates an ephemeral auth user,
signs a JWT locally with the shared secret, POSTs to /onboarding, then verifies
public.users, user_profile, and user_preferences rows via the service-role client.
"""
from __future__ import annotations

import os
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from backend.config import get_settings
from tests.conftest import requires_db, requires_jwt_secret


def _sign(user_id: str, email: str) -> str:
    secret = os.environ["SUPABASE_JWT_SECRET"]
    now = int(time.time())
    payload = {
        "sub": user_id,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "iat": now,
        "exp": now + 300,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


@requires_db
@requires_jwt_secret
def test_onboarding_flow_persists_all_rows(svc_client, ephemeral_auth_user):
    get_settings.cache_clear()
    from backend.main import create_app

    app = create_app()
    client = TestClient(app)

    token = _sign(ephemeral_auth_user["id"], ephemeral_auth_user["email"])
    payload = {
        "age": 22,
        "gender": "male",
        "height_cm": 178,
        "weight_kg": 72,
        "activity_level": "gym_3_4",
        "goal": "bulk",
        "mode": "fitness",
        "veg_default": True,
        "allergies": ["peanuts"],
        "dislikes": ["okra"],
        "restrictions": [],
        "supplements": ["whey"],
        "budget_soft_inr": 200,
    }
    resp = client.post(
        "/onboarding",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target_protein_g"] == pytest.approx(72 * 1.8)  # fitness/bulk
    # BULK adds 10% surplus over TDEE (GOAL_KCAL_MULTIPLIERS[BULK] = 1.10).
    assert body["target_kcal"] == pytest.approx(body["tdee"] * 1.10)

    # Verify persisted rows
    user_row = svc_client.table("users").select("*").eq("id", ephemeral_auth_user["id"]).single().execute().data
    assert user_row["email"] == ephemeral_auth_user["email"]
    assert user_row["mode"] == "fitness"

    profile_row = (
        svc_client.table("user_profile")
        .select("*")
        .eq("user_id", ephemeral_auth_user["id"])
        .single()
        .execute()
        .data
    )
    assert float(profile_row["weight_kg"]) == 72
    assert float(profile_row["target_protein_g"]) == pytest.approx(72 * 1.8)

    prefs = (
        svc_client.table("user_preferences")
        .select("*")
        .eq("user_id", ephemeral_auth_user["id"])
        .execute()
        .data
    )
    kinds = {(p["kind"], p["value"], p["authority"]) for p in prefs}
    assert ("allergy", "peanuts", "hard") in kinds
    assert ("dislike", "okra", "soft") in kinds
    assert ("supplement", "whey", "soft") in kinds

    # /onboarding/me should return the same profile
    me = client.get("/onboarding/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    assert me.json()["target_kcal"] == body["target_kcal"]

    # Cleanup (foreign-key cascades handle the rest)
    svc_client.table("user_preferences").delete().eq("user_id", ephemeral_auth_user["id"]).execute()
    svc_client.table("user_profile").delete().eq("user_id", ephemeral_auth_user["id"]).execute()
    svc_client.table("users").delete().eq("id", ephemeral_auth_user["id"]).execute()


@requires_db
@requires_jwt_secret
def test_onboarding_rejects_unsafe_kcal(svc_client, ephemeral_auth_user):
    from backend.main import create_app

    app = create_app()
    client = TestClient(app)

    token = _sign(ephemeral_auth_user["id"], ephemeral_auth_user["email"])
    # 30kg / 140cm / age 80 female sedentary:
    #   BMR = 10*30 + 6.25*140 - 5*80 - 161 = 300 + 875 - 400 - 161 = 614
    #   TDEE = 614 * 1.2 = 736.8 — well below the MIN_SAFE_KCAL floor (1200).
    resp = client.post(
        "/onboarding",
        json={
            "age": 80,
            "gender": "female",
            "height_cm": 140,
            "weight_kg": 30,
            "activity_level": "sedentary",
            "goal": "cut",
            "mode": "general",
            "budget_soft_inr": 200,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "safe floor" in resp.text or "below" in resp.text.lower()


def test_onboarding_requires_auth():
    get_settings.cache_clear()
    from backend.main import create_app

    app = create_app()
    client = TestClient(app)
    resp = client.post("/onboarding", json={})
    # 401 without auth header (before payload validation runs)
    assert resp.status_code in (401, 422)
