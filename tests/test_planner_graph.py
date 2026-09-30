"""End-to-end planner test with a mocked candidate generator.

Runs against live Supabase (skipped when env is absent). We build a fresh
menu cycle + fresh onboarded user for the test, monkey-patch the candidate
generator to return one plausible plan, and assert:
  1. run_planner commits a `sent` plan
  2. daily_plans row has confidence, validation_result, etc.
  3. re-running with the same trigger short-circuits (agent_runs idempotency)
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta

import pytest

from tests.conftest import requires_db


@requires_db
def test_run_planner_end_to_end(svc_client, ephemeral_auth_user, monkeypatch):
    from backend.agents import planner_nodes
    from backend.models.enums import MealSlot, Trigger
    from backend.models.nutrition import Macros
    from backend.models.plan import CandidatePlan, PlanMealEntry
    from backend.tools.llm_router import LLMResult

    plan_date = date.today() + timedelta(days=400)   # avoid stepping on other tests

    # --- Seed a menu cycle so resolve_daily_menu returns something ----------
    cycle = (
        svc_client.table("menu_cycles")
        .insert(
            {
                "effective_from": plan_date.isoformat(),
                "effective_to": (plan_date + timedelta(days=1)).isoformat(),
                "source": "manual",
                "version": 1,
                "content_hash": f"pytest-{uuid.uuid4()}",
                "status": "active",
            }
        )
        .execute()
        .data[0]
    )
    cycle_id = cycle["id"]
    macro = (
        svc_client.table("macro_db")
        .select("id, dish_name_normalized")
        .eq("dish_name_normalized", "aloo_paratha")
        .single()
        .execute()
        .data
    )
    dal = (
        svc_client.table("macro_db")
        .select("id, dish_name_normalized")
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
                "meal": MealSlot.BREAKFAST.value,
                "dish_name": "Aloo Paratha",
                "is_veg": True,
                "macro_id": macro["id"],
                "confidence": 1.0,
            },
            {
                "cycle_id": cycle_id,
                "day_of_week": plan_date.weekday(),
                "meal": MealSlot.LUNCH.value,
                "dish_name": "Dal Tadka",
                "is_veg": True,
                "macro_id": dal["id"],
                "confidence": 1.0,
            },
        ]
    ).execute()

    # --- Onboard the ephemeral user via the API's compute path -------------
    from backend.models.enums import (
        ActivityLevel,
        Gender,
        Goal,
        UserMode,
    )
    from backend.tools.nutrition import compute_profile_targets

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
            "user_id": ephemeral_auth_user["id"],
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

    # --- Mock the LLM candidate generator -----------------------------------
    fake_llm = LLMResult(text="", tokens_used=10, latency_ms=1, provider="fake", model="fake")

    async def fake_generate(inputs, **kwargs):
        macros = Macros(kcal=500, protein_g=25, carbs_g=60, fats_g=15)
        entry_b = PlanMealEntry(
            meal=MealSlot.BREAKFAST,
            source="mess",
            dish_ref="aloo_paratha",
            servings=1.0,
            macros=macros,
            price_inr=0.0,
            macro_verified=True,
        )
        entry_l = PlanMealEntry(
            meal=MealSlot.LUNCH,
            source="mess",
            dish_ref="dal_tadka",
            servings=1.0,
            macros=macros,
            price_inr=0.0,
            macro_verified=True,
        )
        return (
            [
                CandidatePlan(
                    candidate_id="mock-1",
                    entries=[entry_b, entry_l],
                    total_macros=Macros(kcal=1000, protein_g=50, carbs_g=120, fats_g=30),
                    total_cost_inr=0.0,
                    generated_by="llm_plan",
                )
            ],
            fake_llm,
        )

    monkeypatch.setattr(planner_nodes, "generate_candidates", fake_generate)
    monkeypatch.setattr(planner_nodes, "generate_revised_candidates", fake_generate)

    # --- Run planner --------------------------------------------------------
    from backend.agents.planner_graph import run_planner

    outcome = asyncio.run(
        run_planner(
            svc_client,
            user_id=ephemeral_auth_user["id"],
            plan_date=plan_date,
            trigger=Trigger.MORNING_CRON,
        )
    )
    try:
        assert outcome.inserted is True
        assert outcome.final_plan is not None
        assert outcome.final_plan.id is not None
        assert not outcome.infeasible

        row = (
            svc_client.table("daily_plans")
            .select("*")
            .eq("id", outcome.final_plan.id)
            .single()
            .execute()
            .data
        )
        assert row["status"] == "sent"
        assert row["confidence"] == pytest.approx(outcome.confidence)

        # Re-run: agent_runs idempotency short-circuits.
        outcome2 = asyncio.run(
            run_planner(
                svc_client,
                user_id=ephemeral_auth_user["id"],
                plan_date=plan_date,
                trigger=Trigger.MORNING_CRON,
            )
        )
        assert outcome2.inserted is False
    finally:
        # Cleanup order: menu_items → daily_plans → menu_cycles → agent_runs.
        svc_client.table("daily_plans").delete().eq("id", outcome.final_plan.id).execute()
        svc_client.table("menu_items").delete().eq("cycle_id", cycle_id).execute()
        svc_client.table("menu_cycles").delete().eq("id", cycle_id).execute()
        svc_client.table("agent_runs").delete().eq(
            "idempotency_key", outcome.idempotency_key
        ).execute()
