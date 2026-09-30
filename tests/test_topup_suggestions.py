"""M5 addition: end-of-day canteen top-up endpoint.

Fires after dinner is logged and the day's macros still fall short of target.
Advisory only — does not touch the plan lifecycle.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from tests.conftest import requires_db


def _today_utc() -> str:
    """The endpoint reads/writes meal_logs.date in UTC (see meals router).
    Tests must match that or the query will look at the wrong day when it's
    near midnight in UTC."""
    return datetime.now(timezone.utc).date().isoformat()


def _onboard(svc, uid: str, *, veg_default: bool = True) -> None:
    from backend.models.enums import ActivityLevel, Gender, Goal, UserMode
    from backend.tools.nutrition import compute_profile_targets

    pt = compute_profile_targets(
        age=22,
        gender=Gender.MALE,
        height_cm=175.0,
        weight_kg=70.0,
        activity_level=ActivityLevel.GYM_3_4,
        goal=Goal.BULK,
        mode=UserMode.FITNESS,
    )
    svc.table("user_profile").upsert(
        {
            "user_id": uid,
            "age": 22,
            "gender": Gender.MALE.value,
            "height_cm": 175.0,
            "weight_kg": 70.0,
            "activity_level": ActivityLevel.GYM_3_4.value,
            "goal": Goal.BULK.value,
            "veg_default": veg_default,
            "budget_soft_inr": 200.0,
            "budget_hard_inr": None,
            "bmr": pt.bmr,
            "tdee": pt.tdee,
            "target_kcal": pt.targets.kcal,
            "target_protein_g": pt.targets.protein_g,
            "target_carbs_g": pt.targets.carbs_g,
            "target_fats_g": pt.targets.fats_g,
        },
        on_conflict="user_id",
    ).execute()


def _fake_user(uid: str, email: str):
    class _FakeUser:
        id = uid

        def __init__(self):
            self.email = email
            self.jwt = ""
            self.role_claim = "authenticated"

    return _FakeUser()


@requires_db
def test_topup_ineligible_before_dinner_logged(svc_client, ephemeral_auth_user):
    from backend.routers import meals as meals_router

    uid = ephemeral_auth_user["id"]
    _onboard(svc_client, uid)

    # No meal logs at all — pre-dinner, top-up should be silent.
    resp = meals_router.topup_suggestions(_fake_user(uid, ephemeral_auth_user["email"]))
    assert resp.eligible is False
    assert resp.dinner_logged is False
    assert "Dinner not logged" in (resp.reason or "")


@requires_db
def test_topup_ineligible_when_gap_is_small(svc_client, ephemeral_auth_user):
    """User logged dinner AND is nearly at target — nothing to suggest."""
    from backend.models.enums import MealAction, MealSlot
    from backend.routers import meals as meals_router

    uid = ephemeral_auth_user["id"]
    _onboard(svc_client, uid)

    # Read the target so we can log consumption just under it.
    prof = (
        svc_client.table("user_profile")
        .select("target_kcal, target_protein_g")
        .eq("user_id", uid)
        .single()
        .execute()
        .data
    )
    # Consume 99% of target — gap is smaller than the top-up thresholds.
    consumed_kcal = float(prof["target_kcal"]) - 50
    consumed_protein = float(prof["target_protein_g"]) - 5
    svc_client.table("meal_logs").insert(
        {
            "user_id": uid,
            "date": _today_utc(),
            "meal": MealSlot.DINNER.value,
            "event_id": str(uuid.uuid4()),
            "action": MealAction.ATE_PLANNED.value,
            "actual_dishes_json": [],
            "actual_macros_json": {
                "kcal": consumed_kcal,
                "protein_g": consumed_protein,
                "carbs_g": 400.0,
                "fats_g": 80.0,
            },
        }
    ).execute()

    resp = meals_router.topup_suggestions(_fake_user(uid, ephemeral_auth_user["email"]))
    assert resp.dinner_logged is True
    assert resp.eligible is False
    assert "close enough" in (resp.reason or "").lower()


@requires_db
def test_topup_eligible_returns_canteen_suggestions(svc_client, ephemeral_auth_user):
    """The example scenario: dinner logged but consumption is well under target
    → get canteen picks that close the protein/kcal gap."""
    from backend.models.enums import MealAction, MealSlot
    from backend.routers import meals as meals_router

    uid = ephemeral_auth_user["id"]
    _onboard(svc_client, uid)

    # Big shortfall — log dinner with modest consumption so we're WELL below target.
    svc_client.table("meal_logs").insert(
        {
            "user_id": uid,
            "date": _today_utc(),
            "meal": MealSlot.DINNER.value,
            "event_id": str(uuid.uuid4()),
            "action": MealAction.ATE_PLANNED.value,
            "actual_dishes_json": [],
            "actual_macros_json": {
                "kcal": 1200.0,
                "protein_g": 40.0,
                "carbs_g": 150.0,
                "fats_g": 40.0,
            },
        }
    ).execute()

    resp = meals_router.topup_suggestions(_fake_user(uid, ephemeral_auth_user["email"]))
    assert resp.dinner_logged is True
    # canteen_items seed has 29 rows per project memory — at least one should fit.
    assert resp.eligible is True, f"expected suggestions, got reason={resp.reason!r}"
    assert resp.gap_protein_g > 15.0
    assert len(resp.suggestions) >= 1
    assert resp.total_added_protein_g > 0
    assert resp.total_added_kcal <= resp.gap_kcal + 1e-6  # doesn't overshoot the kcal gap
    # Suggestions carry the shop / dish name pretty-print.
    first = resp.suggestions[0]
    assert first.dish_name and first.shop_name
    assert first.price_inr >= 0
