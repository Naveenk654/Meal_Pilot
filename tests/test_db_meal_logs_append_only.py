"""Verify the append-only trigger on meal_logs (§13 A10)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from tests.conftest import requires_db


@requires_db
def test_meal_log_insert_dedupes_on_event_id(svc_client, ephemeral_auth_user):
    from backend.idempotency.writer import insert_meal_log

    event_id = str(uuid.uuid4())
    payload = {
        "user_id": ephemeral_auth_user["id"],
        "date": date(2026, 3, 5).isoformat(),
        "meal": "breakfast",
        "action": "ate_planned",
        "actual_dishes_json": [{"name": "aloo_paratha"}],
        "actual_macros_json": {"kcal": 500, "protein_g": 12},
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    row1, ins1 = insert_meal_log(svc_client, event_id=event_id, payload=payload)
    assert ins1 is True
    row2, ins2 = insert_meal_log(svc_client, event_id=event_id, payload=payload)
    assert ins2 is False
    assert row2["id"] == row1["id"]
    svc_client.table("meal_logs").delete().eq("id", row1["id"]).execute()


@requires_db
def test_meal_log_update_is_blocked_by_trigger(svc_client, ephemeral_auth_user):
    from postgrest.exceptions import APIError

    event_id = str(uuid.uuid4())
    resp = svc_client.table("meal_logs").insert(
        {
            "user_id": ephemeral_auth_user["id"],
            "date": date(2026, 3, 5).isoformat(),
            "meal": "lunch",
            "event_id": event_id,
            "action": "ate_planned",
            "actual_dishes_json": [],
            "actual_macros_json": {"kcal": 0},
        }
    ).execute()
    log_id = resp.data[0]["id"]
    try:
        raised = False
        try:
            svc_client.table("meal_logs").update({"note": "should not work"}).eq(
                "id", log_id
            ).execute()
        except APIError:
            raised = True
        assert raised, "trigger did not block meal_logs UPDATE (§13 A10 violated)"
    finally:
        svc_client.table("meal_logs").delete().eq("id", log_id).execute()
