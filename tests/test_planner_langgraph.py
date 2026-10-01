"""No-DB, no-LLM graph tests. Invoke the compiled Planner StateGraph via
astream(stream_mode='updates') and assert the node-visit sequence for the
four canonical scenarios.

The Supabase client is replaced with a MagicMock that returns empty rows
for every chained call — enough for the nodes that read macro_db or
canteen_items to return `[]` without erroring. The LLM entry points
`generate_candidates` and `generate_revised_candidates` are patched on
backend.agents.planner_nodes.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from backend.agents.planner_graph import _build_planner_graph
from backend.config import get_settings
from backend.models.enums import (
    ActivityLevel,
    Gender,
    Goal,
    HITLStatus,
    MealSlot,
    PlanMode,
    UserMode,
)
from backend.models.menu import MenuFreshness
from backend.models.nutrition import Macros, MacrosSigned
from backend.models.plan import CandidatePlan, PlanMealEntry
from backend.models.user import UserProfile
from backend.tools.llm_router import DegradedModeSignal, LLMResult
from backend.tools.menu import ResolvedDailyMenu, ResolvedMenuItem


# --- Fixtures ---------------------------------------------------------------


def _fake_svc():
    """Supabase stub: every chained call ends in `.execute()` with `.data=[]`."""
    svc = MagicMock(name="fake_svc")
    tbl = MagicMock(name="table")
    for attr in ("select", "in_", "eq", "insert", "update", "delete", "limit",
                 "gte", "lte", "order", "single", "upsert"):
        getattr(tbl, attr).return_value = tbl
    exec_ret = MagicMock()
    exec_ret.data = []
    tbl.execute.return_value = exec_ret
    svc.table.return_value = tbl
    return svc


def _profile() -> UserProfile:
    return UserProfile(
        user_id="u1", age=22, gender=Gender.MALE, height_cm=175.0, weight_kg=70.0,
        activity_level=ActivityLevel.LIGHT, goal=Goal.MAINTAIN, mode=UserMode.GENERAL,
        veg_default=True, budget_soft_inr=200.0, budget_hard_inr=None,
        plan_mode=PlanMode.MESS_ONLY, bmr=1700, tdee=2300,
        target_kcal=2300, target_protein_g=120, target_carbs_g=250, target_fats_g=70,
    )


def _menu() -> ResolvedDailyMenu:
    dow = date.today().weekday()
    return ResolvedDailyMenu(
        cycle_id=1, effective_from=date.today(), effective_to=date.today(),
        version=1, content_hash="h", source="manual",
        items=[
            ResolvedMenuItem(
                day_of_week=dow, meal=MealSlot.BREAKFAST,
                dish_name="Aloo Paratha", dish_normalized="aloo_paratha",
                is_veg=True, macro_id=1, confidence=1.0,
            ),
            ResolvedMenuItem(
                day_of_week=dow, meal=MealSlot.LUNCH,
                dish_name="Dal Tadka", dish_normalized="dal_tadka",
                is_veg=True, macro_id=2, confidence=1.0,
            ),
        ],
    )


def _state(with_menu: bool = True):
    s = get_settings()
    target = Macros(kcal=2300, protein_g=120, carbs_g=250, fats_g=70)
    return {
        "user_id": "u1",
        "date": date.today(),
        "trigger": "morning_cron",
        "triggering_event_id": None,
        "profile": _profile(),
        "behavioral_memory": [],
        "todays_menu": _menu() if with_menu else None,
        "menu_freshness": MenuFreshness(
            source="manual",
            ingested_at=datetime.now(timezone.utc),
            effective_from=date.today(),
            effective_to=date.today(),
            version=1, content_hash="h",
        ) if with_menu else None,
        "veg_today": True,
        "budget_remaining_inr": 200.0,
        "target_macros": target,
        "consumed_macros": Macros.zero(),
        "macro_delta": MacrosSigned(kcal=2300, protein_g=120, carbs_g=250, fats_g=70),
        "planning_remaining_macros": target,
        "meals_completed": [],
        "meals_remaining": [MealSlot.BREAKFAST, MealSlot.LUNCH, MealSlot.SNACK, MealSlot.DINNER],
        "candidate_plans": [],
        "validation_results": [],
        "revision_attempts": 0,
        "candidates_generated": 0,
        "fallback_invoked": False,
        "fallback_results": [],
        "infeasible": False,
        "selected_plan": None,
        "confidence": 0.0,
        "confidence_factors": None,
        "hitl_status": HITLStatus.NOT_NEEDED,
        "hitl_request_id": None,
        "final_plan": None,
        "reasoning_trace": [],
        "_preferences": [],
        "_settings": s,
        "_svc_client": _fake_svc(),
    }


def _good_candidate() -> CandidatePlan:
    macros = Macros(kcal=1150, protein_g=60, carbs_g=130, fats_g=35)
    entries = [
        PlanMealEntry(
            meal=MealSlot.BREAKFAST, source="mess", dish_ref="aloo_paratha",
            servings=1.0, macros=macros, price_inr=0.0, macro_verified=True,
        ),
        PlanMealEntry(
            meal=MealSlot.LUNCH, source="mess", dish_ref="dal_tadka",
            servings=1.0, macros=macros, price_inr=0.0, macro_verified=True,
        ),
    ]
    return CandidatePlan(
        candidate_id="good", entries=entries,
        total_macros=Macros(kcal=2300, protein_g=120, carbs_g=260, fats_g=70),
        total_cost_inr=0.0, generated_by="llm_plan",
    )


def _bad_candidate() -> CandidatePlan:
    """Fails the kcal ceiling AND the protein floor so classify always says
    revise/infeasible."""
    macros = Macros(kcal=100, protein_g=2, carbs_g=20, fats_g=1)
    entry = PlanMealEntry(
        meal=MealSlot.BREAKFAST, source="mess", dish_ref="aloo_paratha",
        servings=1.0, macros=macros, price_inr=0.0, macro_verified=True,
    )
    return CandidatePlan(
        candidate_id="bad", entries=[entry],
        total_macros=Macros(kcal=100, protein_g=2, carbs_g=20, fats_g=1),
        total_cost_inr=0.0, generated_by="llm_plan",
    )


_FAKE_LLM = LLMResult(text="", tokens_used=10, latency_ms=1, provider="fake", model="fake")


def _fake_commit(client, *, user_id, plan_date, entries_json, target_macros,
                 validation_result, confidence, confidence_factors, cycle_id_used):
    """Stub commit — returns a plausible CommitResult without touching Supabase."""
    from backend.tools.plan_writer import CommitResult
    return CommitResult(plan_id=1, superseded_plan_id=None, plan_row={"id": 1})


async def _collect_nodes(state_dict, gen_fn) -> list[str]:
    """Run the graph with gen_fn patched at both LLM entrypoints AND
    commit_plan stubbed, so no node reaches live Supabase. Returns the
    ordered list of nodes visited (from stream_mode='updates')."""
    app = _build_planner_graph(state_dict["_svc_client"])
    nodes_visited: list[str] = []
    with patch("backend.agents.planner_nodes.generate_candidates", gen_fn), \
         patch("backend.agents.planner_nodes.generate_revised_candidates", gen_fn), \
         patch("backend.agents.planner_nodes.commit_plan", _fake_commit):
        async for chunk in app.astream(state_dict, {"recursion_limit": 60}, stream_mode="updates"):
            for node_name in chunk.keys():
                nodes_visited.append(node_name)
    return nodes_visited


# --- Tests ------------------------------------------------------------------


def test_graph_valid_first_candidate_reaches_commit():
    async def gen(inputs, **kwargs):
        return ([_good_candidate()], _FAKE_LLM)

    nodes = asyncio.run(_collect_nodes(_state(), gen))
    print(f"\n[a] SEQUENCE: {' -> '.join(nodes)}")
    assert nodes == [
        "check_menu_freshness", "generate", "validate", "classify",
        "score", "check_confidence", "commit",
    ]


def test_graph_always_invalid_bounded_then_clean_infeasible_exit():
    """Every candidate fails validation. Expect exactly 1 + MAX_REVISIONS
    `generate` passes, then fallback, then a clean no-exception exit."""
    s = get_settings()

    async def gen(inputs, **kwargs):
        return ([_bad_candidate()], _FAKE_LLM)

    nodes = asyncio.run(_collect_nodes(_state(), gen))
    print(f"\n[b] SEQUENCE: {' -> '.join(nodes)}")

    # Exactly 1 + MAX_REVISIONS generate visits.
    assert nodes.count("generate") == 1 + s.max_revisions, nodes
    # One classify visit after each generate+validate.
    assert nodes.count("classify") == 1 + s.max_revisions, nodes
    # Fallback was invoked before the exit.
    assert "fallback_pre_select" in nodes, nodes
    # No commit happened — nothing was ever valid.
    assert "commit" not in nodes, nodes
    # The clean-exit nodes we introduced must appear with the fix.
    assert "revalidate_pre" in nodes, nodes
    assert "clear_infeasible" in nodes, nodes


def test_graph_degraded_llm_routes_to_fallback():
    async def gen(inputs, **kwargs):
        raise DegradedModeSignal("both providers down")

    nodes = asyncio.run(_collect_nodes(_state(), gen))
    print(f"\n[c] SEQUENCE: {' -> '.join(nodes)}")
    assert nodes[:3] == ["check_menu_freshness", "generate", "fallback_pre_select"], nodes
    assert "commit" not in nodes, nodes


def test_graph_no_active_menu_exits_immediately():
    async def gen(inputs, **kwargs):
        return ([_good_candidate()], _FAKE_LLM)

    nodes = asyncio.run(_collect_nodes(_state(with_menu=False), gen))
    print(f"\n[d] SEQUENCE: {' -> '.join(nodes)}")
    assert nodes == ["check_menu_freshness"], nodes


def test_graph_mid_day_replan_sizes_against_planning_remaining():
    """Regression for the bug where the serving scaler, protein bumper, and
    canteen-fallback gap used the full-day target_macros to size candidates
    even when consumed_macros was non-trivial.

    Repro: user logged breakfast + lunch (1400 kcal / 80g protein consumed).
    planning_remaining = 900 kcal / 40g protein. Mock LLM returns a
    dinner-only plan at 790 kcal / 38g protein — a reasonable dinner that
    fits what's left.

    Pre-fix: bumper saw target_macros.protein_g=120, floor=0.9*120=108,
    inflated the dinner entry until it hit the kcal headroom, then the
    validator (now on planning_remaining) rejected it for exceeding
    1.20 * 900 kcal = 1080 kcal. No plan committed.

    Post-fix: scaler/bumper/fallback all size against planning_remaining,
    so the 790/38 plan passes untouched and commits.
    """
    dow = date.today().weekday()
    # Build a menu that includes a DINNER item (default _menu has only
    # breakfast + lunch, which would trip MEAL_SLOT_MISMATCH on a dinner entry).
    menu = ResolvedDailyMenu(
        cycle_id=1, effective_from=date.today(), effective_to=date.today(),
        version=1, content_hash="h", source="manual",
        items=[
            ResolvedMenuItem(
                day_of_week=dow, meal=MealSlot.DINNER,
                dish_name="Paneer Bhurji", dish_normalized="paneer_bhurji",
                is_veg=True, macro_id=10, confidence=1.0,
            ),
        ],
    )

    state_dict = _state()
    state_dict["todays_menu"] = menu
    state_dict["consumed_macros"] = Macros(kcal=1400, protein_g=80, carbs_g=180, fats_g=40)
    state_dict["macro_delta"] = MacrosSigned(kcal=900, protein_g=40, carbs_g=70, fats_g=30)
    state_dict["planning_remaining_macros"] = Macros(kcal=900, protein_g=40, carbs_g=70, fats_g=30)
    state_dict["meals_completed"] = [MealSlot.BREAKFAST, MealSlot.LUNCH]
    state_dict["meals_remaining"] = [MealSlot.SNACK, MealSlot.DINNER]

    dinner_macros = Macros(kcal=790, protein_g=38, carbs_g=55, fats_g=42)
    dinner_entry = PlanMealEntry(
        meal=MealSlot.DINNER, source="mess", dish_ref="paneer_bhurji",
        servings=1.0, macros=dinner_macros, price_inr=0.0, macro_verified=True,
    )
    dinner_only = CandidatePlan(
        candidate_id="dinner-only", entries=[dinner_entry],
        total_macros=dinner_macros, total_cost_inr=0.0, generated_by="llm_plan",
    )

    async def gen(inputs, **kwargs):
        return ([dinner_only], _FAKE_LLM)

    nodes = asyncio.run(_collect_nodes(state_dict, gen))
    print(f"\n[e] SEQUENCE: {' -> '.join(nodes)}")

    # Plan must survive scaler + bumper + validator and reach commit.
    assert "commit" in nodes, (
        "mid-day plan rejected before commit — scaler/bumper likely still "
        f"sizing against full-day target. Visited: {nodes}"
    )
    # And must NOT loop into revision / fallback — those indicate the
    # validator rejected the plan (which is what the bug produced).
    assert nodes.count("generate") == 1, (
        f"revision loop fired — scaler probably inflated the plan then "
        f"validator rejected. Visited: {nodes}"
    )
    assert "fallback_pre_select" not in nodes, nodes
