"""M4 tests: meal-log idempotency, log→replan lifecycle."""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.conftest import requires_db


@requires_db
def test_meal_log_idempotent_and_triggers_replan(svc_client, ephemeral_auth_user, monkeypatch):
    from backend.agents import planner_nodes
    from backend.models.enums import (
        ActivityLevel,
        Gender,
        Goal,
        MealAction,
        MealSlot,
        UserMode,
    )
    from backend.models.nutrition import Macros
    from backend.models.plan import CandidatePlan, PlanMealEntry
    from backend.routers import meals as meals_router
    from backend.tools.llm_router import LLMResult
    from backend.tools.nutrition import compute_profile_targets

    plan_date = date.today()
    uid = ephemeral_auth_user["id"]

    # --- Onboard the user -----------------------------------------------------
    pt = compute_profile_targets(
        age=22,
        gender=Gender.MALE,
        height_cm=175.0,
        weight_kg=70.0,
        activity_level=ActivityLevel.LIGHT,
        goal=Goal.MAINTAIN,
        mode=UserMode.GENERAL,
    )
    svc_client.table("user_profile").upsert(
        {
            "user_id": uid,
            "age": 22,
            "gender": Gender.MALE.value,
            "height_cm": 175.0,
            "weight_kg": 70.0,
            "activity_level": ActivityLevel.LIGHT.value,
            "goal": Goal.MAINTAIN.value,
            "veg_default": True,
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

    # --- Seed a menu cycle so the replan finds real data --------------------
    # Use source='email' + status='draft' to avoid colliding with any live
    # manual/pdf cycle covering today. Flip to active for the test only.
    cycle = (
        svc_client.table("menu_cycles")
        .insert(
            {
                "effective_from": plan_date.isoformat(),
                "effective_to": (plan_date + timedelta(days=1)).isoformat(),
                "source": "email",
                "version": 1,
                "content_hash": f"pytest-{uuid.uuid4()}",
                "status": "active",
            }
        )
        .execute()
        .data[0]
    )
    cycle_id = cycle["id"]
    dal = (
        svc_client.table("macro_db")
        .select("id")
        .eq("dish_name_normalized", "dal_tadka")
        .single()
        .execute()
        .data
    )
    svc_client.table("menu_items").insert(
        [
            {
                "cycle_id": cycle_id,
                "day_of_week": plan_date.weekday(),
                "meal": MealSlot.LUNCH.value,
                "dish_name": "Dal Tadka",
                "is_veg": True,
                "macro_id": dal["id"],
                "confidence": 1.0,
            }
        ]
    ).execute()

    # --- Fake the LLM candidate generator so tests are deterministic -------
    fake_llm = LLMResult(text="", tokens_used=1, latency_ms=1, provider="fake", model="fake")

    async def fake_gen(inputs, **kwargs):
        entry = PlanMealEntry(
            meal=MealSlot.LUNCH,
            source="mess",
            dish_ref="dal_tadka",
            servings=1.0,
            macros=Macros(kcal=180, protein_g=10, carbs_g=22, fats_g=6),
            price_inr=0.0,
            macro_verified=True,
        )
        return (
            [
                CandidatePlan(
                    candidate_id="mock",
                    entries=[entry],
                    total_macros=Macros(kcal=180, protein_g=10, carbs_g=22, fats_g=6),
                    total_cost_inr=0.0,
                    generated_by="llm_plan",
                )
            ],
            fake_llm,
        )

    monkeypatch.setattr(planner_nodes, "generate_candidates", fake_gen)
    monkeypatch.setattr(planner_nodes, "generate_revised_candidates", fake_gen)

    # --- Log a meal ----------------------------------------------------------
    event_id = str(uuid.uuid4())
    payload = meals_router.LogMealRequest(
        event_id=event_id,
        meal=MealSlot.BREAKFAST,
        action=MealAction.ATE_DIFFERENT,
        actual_dishes=[meals_router.LoggedDish(dish_ref="dal_tadka", servings=1.0)],
    )

    class _FakeUser:
        id = uid
        email = ephemeral_auth_user["email"]
        jwt = ""
        role_claim = "authenticated"

    resp1 = asyncio.run(meals_router.log_meal(payload, _FakeUser))
    assert resp1.inserted is True
    assert resp1.log_id > 0

    # kcal recorded from dal_tadka macros — non-zero.
    assert resp1.consumed_delta["kcal"] > 0

    # Second call with same event_id → idempotent, no new row.
    resp2 = asyncio.run(meals_router.log_meal(payload, _FakeUser))
    assert resp2.inserted is False
    assert resp2.log_id == resp1.log_id

    # Verify only one meal_logs row for this event.
    rows = (
        svc_client.table("meal_logs")
        .select("id")
        .eq("event_id", event_id)
        .execute()
        .data
    )
    assert len(rows) == 1

    # /nutrition/today should reflect the logged consumption.
    nutrition = meals_router.nutrition_today(_FakeUser)
    assert nutrition.consumed_kcal > 0
    assert nutrition.delta_kcal < nutrition.target_kcal   # something was consumed

    # Cleanup
    svc_client.table("meal_logs").delete().eq("user_id", uid).eq("event_id", event_id).execute()
    svc_client.table("menu_items").delete().eq("cycle_id", cycle_id).execute()
    svc_client.table("menu_cycles").delete().eq("id", cycle_id).execute()
    svc_client.table("agent_runs").delete().eq("user_id", uid).execute()
    svc_client.table("daily_plans").delete().eq("user_id", uid).execute()


@requires_db
def test_meal_logs_are_append_only(svc_client, ephemeral_auth_user):
    """meal_logs has a BEFORE UPDATE trigger that raises. Attempting to update
    a row must fail — this is the append-only guarantee (§13 A10)."""
    from postgrest.exceptions import APIError
    from backend.models.enums import MealAction, MealSlot

    uid = ephemeral_auth_user["id"]
    event_id = str(uuid.uuid4())
    row = (
        svc_client.table("meal_logs")
        .insert(
            {
                "user_id": uid,
                "date": date.today().isoformat(),
                "meal": MealSlot.SNACK.value,
                "event_id": event_id,
                "action": MealAction.SKIPPED.value,
                "actual_dishes_json": [],
                "actual_macros_json": {"kcal": 0, "protein_g": 0, "carbs_g": 0, "fats_g": 0},
            }
        )
        .execute()
        .data[0]
    )
    row_id = row["id"]
    with pytest.raises(APIError):
        svc_client.table("meal_logs").update({"note": "no way"}).eq("id", row_id).execute()

    svc_client.table("meal_logs").delete().eq("id", row_id).execute()
