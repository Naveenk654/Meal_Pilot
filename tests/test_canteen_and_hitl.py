"""M5 tests: canteen fallback tool + HITL round-trip."""
from __future__ import annotations

import os
import uuid

import pytest

from tests.conftest import requires_db


def test_find_canteen_additions_closes_protein_gap():
    """Uses live macro_db seed if available; else skips."""
    from backend.models.enums import MealSlot
    from backend.tools.canteen import find_canteen_additions_for_gap
    from backend.db.supabase_client import get_service_role_client

    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY")):
        pytest.skip("Supabase not configured")
    svc = get_service_role_client()

    picks = find_canteen_additions_for_gap(
        svc,
        protein_gap_g=40,
        kcal_remaining_ceiling=2000,
        veg_only=False,
        dislikes=[],
        hard_budget_remaining_inr=None,
        meal_slot=MealSlot.SNACK,
    )
    total_p = sum(e.macros.protein_g for e in picks)
    assert total_p > 0
    for e in picks:
        assert e.source == "canteen"
        assert e.dish_ref.startswith("canteen:")


@requires_db
def test_hitl_create_and_respond_roundtrip(svc_client, ephemeral_auth_user):
    from backend.models.enums import HITLStatus
    from backend.tools.hitl import (
        HITLSurface,
        create_hitl_request,
        list_pending_for_user,
        respond_to_hitl,
    )

    uid = ephemeral_auth_user["id"]
    row = create_hitl_request(
        svc_client,
        user_id=uid,
        agent="planner",
        surface=HITLSurface.LOW_CONFIDENCE,
        question="Approve this low-confidence plan?",
        options=[],
        context={"test": True},
    )
    hitl_id = row["id"]
    try:
        pending = list_pending_for_user(svc_client, user_id=uid)
        assert any(r["id"] == hitl_id for r in pending)

        updated = respond_to_hitl(
            svc_client,
            hitl_id=hitl_id,
            user_id=uid,
            status=HITLStatus.APPROVED,
            response={"note": "looks good"},
        )
        assert updated["status"] == "approved"
        assert updated["response_hash"] is not None

        pending2 = list_pending_for_user(svc_client, user_id=uid)
        assert not any(r["id"] == hitl_id for r in pending2)
    finally:
        svc_client.table("hitl_requests").delete().eq("id", hitl_id).execute()
