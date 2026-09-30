"""Verify the version guard on daily_plans (§14 TX2)."""
from __future__ import annotations

from datetime import date

from tests.conftest import requires_db


@requires_db
def test_version_guard_rejects_stale_update(svc_client, ephemeral_auth_user):
    from backend.idempotency.writer import update_plan_with_version_guard

    resp = svc_client.table("daily_plans").insert(
        {
            "user_id": ephemeral_auth_user["id"],
            "date": date(2026, 3, 5).isoformat(),
            "plan_json": {"entries": []},
            "target_macros_json": {"kcal": 2000},
            "validation_result_json": {"is_valid": True},
            "confidence": 0.9,
            "confidence_factors": {"overall": 0.9},
            "status": "draft",
            "version": 1,
        }
    ).execute()
    plan_id = resp.data[0]["id"]
    try:
        # First writer: expects version=1 → succeeds and bumps to 2
        first = update_plan_with_version_guard(
            svc_client,
            plan_id=plan_id,
            expected_version=1,
            fields={"status": "validated"},
        )
        assert first is not None
        assert first["version"] == 2

        # Second writer: still thinks it's version 1 → must be rejected
        second = update_plan_with_version_guard(
            svc_client,
            plan_id=plan_id,
            expected_version=1,
            fields={"status": "sent"},
        )
        assert second is None, "stale-version update was not rejected (§14 TX2 violated)"
    finally:
        svc_client.table("daily_plans").delete().eq("id", plan_id).execute()
