"""Meal logging + macro tracker (M4).

Endpoints:
  * POST /meal-logs         — append-only meal_log with client event_id (idempotent).
                              After a successful insert we fire the Planner with
                              trigger=meal_log so the remainder of the day is
                              replanned against consumed macros.
  * GET  /meal-logs/today   — list today's logs for the caller.
  * GET  /nutrition/today   — target / consumed / signed macro_delta.

The log-then-replan sequence is transactional in the "meal_logs commits first,
then Planner runs" sense (§14 TX1). A Planner failure never rolls back the log.
"""
# NOTE: no `from __future__ import annotations` — Pydantic v2 struggles to
# evaluate PEP-604 unions inside FastAPI response_model.

import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.agents.learning import schedule_weekly_learning_if_due
from backend.agents.planner_graph import run_planner
from backend.auth.dependencies import AuthUser, get_current_auth_user
from backend.db.supabase_client import get_service_role_client
from backend.idempotency.writer import insert_meal_log
from backend.models.enums import (
    MealAction,
    MealSlot,
    PreferenceKind,
    PreferenceStatus,
    Trigger,
)
from backend.models.nutrition import Macros
from backend.tools.canteen import find_canteen_additions_for_gap
from backend.tools.nutrition import (
    macro_delta as calc_macro_delta,
    sum_macros,
)
from backend.tools.plan_writer import fetch_today_plan
from backend.tools.supplements import compute_daily_supplement_macros

# Servings tolerance for the "ate exactly what was planned" short-circuit.
# Streamlit sends floats round-tripped through JSON; 0.05 covers 1.0 vs 1.00001
# without letting a genuine 1.0 → 1.5 swap slip through as a no-op.
_SERVINGS_TOLERANCE = 0.05

# Fill-the-gap thresholds. A shortfall smaller than these isn't worth
# suggesting an out-of-pocket canteen trip for — user is close enough.
TOPUP_MIN_PROTEIN_GAP_G = 15.0
TOPUP_MIN_KCAL_GAP = 200.0

router = APIRouter(tags=["meals"])
AuthedUser = Annotated[AuthUser, Depends(get_current_auth_user)]


class LoggedDish(BaseModel):
    dish_ref: str
    servings: float = Field(gt=0.0, default=1.0)
    macro_id: Optional[int] = None
    # Optional per-dish macro override for `ate_different` when the dish isn't
    # in macro_db and the client wants to record ground-truth macros directly.
    kcal: Optional[float] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fats_g: Optional[float] = None


class LogMealRequest(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    meal: MealSlot
    action: MealAction
    # For ate_planned / ate_different: what actually got eaten.
    actual_dishes: list[LoggedDish] = Field(default_factory=list)
    note: Optional[str] = None


class LogMealResponse(BaseModel):
    log_id: int
    event_id: str
    inserted: bool
    consumed_delta: dict[str, float]
    replan_triggered: bool
    replan_plan_id: Optional[int] = None
    warnings: list[str] = Field(default_factory=list)


@router.post("/meal-logs", response_model=LogMealResponse)
async def log_meal(payload: LogMealRequest, user: AuthedUser) -> LogMealResponse:
    """Append-only meal log. Duplicate event_id returns the existing row.

    On a genuinely new log, fire a mid-day replan so `meals_remaining` shrinks
    and the plan card refreshes with the updated recommendations.
    """
    svc = get_service_role_client()
    plan_date = datetime.now(timezone.utc).date()

    macros_for_log, warnings = _resolve_dish_macros(svc, payload)
    body = {
        "user_id": user.id,
        "date": plan_date.isoformat(),
        "meal": payload.meal.value,
        "action": payload.action.value,
        "actual_dishes_json": [d.model_dump() for d in payload.actual_dishes],
        "actual_macros_json": macros_for_log.model_dump(),
        "note": payload.note,
    }
    row, inserted = insert_meal_log(svc, event_id=payload.event_id, payload=body)

    replan_plan_id: Optional[int] = None
    replan_triggered = False
    # A skip is semantically different from an "I ate this instead" — the
    # planner treats them as distinct triggers so decision_traces can
    # distinguish which behavioral pattern drove the replan.
    replan_trigger = (
        Trigger.SKIP if payload.action is MealAction.SKIPPED else Trigger.MEAL_LOG
    )
    if inserted:
        # Golden-path short-circuit: if the user ate exactly what was planned
        # for this slot (same dish_refs + servings), the remaining plan doesn't
        # need to change — `meals_remaining` shrinks, `planning_remaining`
        # shifts by the planned macros, and any LLM-driven replan would just
        # shuffle un-logged meals for no reason (non-determinism in candidate
        # gen). Skipping saves 3–8 s per click and keeps the plan card stable.
        current_plan = fetch_today_plan(svc, user_id=user.id, plan_date=plan_date)
        if payload.action is MealAction.ATE_PLANNED and _matches_planned_slot(
            current_plan, payload.meal, payload.actual_dishes
        ):
            schedule_weekly_learning_if_due(svc, user.id)
            return LogMealResponse(
                log_id=row["id"],
                event_id=payload.event_id,
                inserted=inserted,
                consumed_delta=macros_for_log.model_dump(),
                replan_triggered=False,
                replan_plan_id=None,
                warnings=warnings,
            )
        try:
            outcome = await run_planner(
                svc,
                user_id=user.id,
                plan_date=plan_date,
                trigger=replan_trigger,
                triggering_event_id=payload.event_id,
            )
            replan_triggered = True
            if outcome.final_plan is not None:
                replan_plan_id = outcome.final_plan.id
            elif outcome.infeasible:
                warnings.append(
                    "No new plan generated — all meals logged for today, nothing left to plan."
                )
        except Exception as exc:
            # Log-then-replan is TX-separate: a failed Planner run must not
            # roll back the log. Surface the actual error so the UI can help.
            replan_triggered = False
            warnings.append(f"Replan failed: {type(exc).__name__}: {exc}")
            import logging
            logging.getLogger(__name__).exception("replan failed for event %s", payload.event_id)

        # Fire-and-forget: if the user hasn't had a Learning Agent run in
        # the last 7 days, schedule one now. No cron; the cadence is driven
        # by natural user activity. Idempotency inside run_weekly_learning
        # dedups per ISO week — safe to call from every meal_log.
        schedule_weekly_learning_if_due(svc, user.id)

    return LogMealResponse(
        log_id=row["id"],
        event_id=payload.event_id,
        inserted=inserted,
        consumed_delta=macros_for_log.model_dump(),
        replan_triggered=replan_triggered,
        replan_plan_id=replan_plan_id,
        warnings=warnings,
    )


class LoggedMealOut(BaseModel):
    id: int
    meal: str
    action: str
    dishes: list[dict[str, Any]]
    macros: dict[str, float]
    logged_at: str
    note: Optional[str] = None


@router.get("/meal-logs/week", response_model=list[LoggedMealOut])
def list_weeks_logs(user: AuthedUser, days: int = 7) -> list[LoggedMealOut]:
    svc = get_service_role_client()
    from datetime import timedelta

    start = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    resp = (
        svc.table("meal_logs")
        .select("*")
        .eq("user_id", user.id)
        .gte("date", start)
        .order("logged_at", desc=True)
        .execute()
    )
    return [
        LoggedMealOut(
            id=row["id"],
            meal=row["meal"],
            action=row["action"],
            dishes=row.get("actual_dishes_json") or [],
            macros=row.get("actual_macros_json") or {},
            logged_at=row["logged_at"],
            note=row.get("note"),
        )
        for row in (resp.data or [])
    ]


@router.get("/meal-logs/today", response_model=list[LoggedMealOut])
def list_todays_logs(user: AuthedUser) -> list[LoggedMealOut]:
    svc = get_service_role_client()
    plan_date = datetime.now(timezone.utc).date()
    resp = (
        svc.table("meal_logs")
        .select("*")
        .eq("user_id", user.id)
        .eq("date", plan_date.isoformat())
        .order("logged_at")
        .execute()
    )
    return [
        LoggedMealOut(
            id=row["id"],
            meal=row["meal"],
            action=row["action"],
            dishes=row.get("actual_dishes_json") or [],
            macros=row.get("actual_macros_json") or {},
            logged_at=row["logged_at"],
            note=row.get("note"),
        )
        for row in (resp.data or [])
    ]


class NutritionTodayResponse(BaseModel):
    target_kcal: float
    target_protein_g: float
    target_carbs_g: float
    target_fats_g: float
    consumed_kcal: float
    consumed_protein_g: float
    consumed_carbs_g: float
    consumed_fats_g: float
    # Signed: positive = under target, negative = over.
    delta_kcal: float
    delta_protein_g: float
    delta_carbs_g: float
    delta_fats_g: float
    supplement_breakdown: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/nutrition/today", response_model=NutritionTodayResponse)
def nutrition_today(user: AuthedUser) -> NutritionTodayResponse:
    svc = get_service_role_client()
    profile = (
        svc.table("user_profile")
        .select("target_kcal, target_protein_g, target_carbs_g, target_fats_g")
        .eq("user_id", user.id)
        .single()
        .execute()
        .data
    )
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Profile not found — onboard first.")

    target = Macros(
        kcal=float(profile["target_kcal"]),
        protein_g=float(profile["target_protein_g"]),
        carbs_g=float(profile["target_carbs_g"]),
        fats_g=float(profile["target_fats_g"]),
    )
    plan_date = datetime.now(timezone.utc).date()
    logs = (
        svc.table("meal_logs")
        .select("actual_macros_json")
        .eq("user_id", user.id)
        .eq("date", plan_date.isoformat())
        .execute()
        .data
        or []
    )
    consumed_items = [
        Macros(
            kcal=float((l.get("actual_macros_json") or {}).get("kcal", 0.0)),
            protein_g=float((l.get("actual_macros_json") or {}).get("protein_g", 0.0)),
            carbs_g=float((l.get("actual_macros_json") or {}).get("carbs_g", 0.0)),
            fats_g=float((l.get("actual_macros_json") or {}).get("fats_g", 0.0)),
        )
        for l in logs
    ]
    consumed_from_meals = sum_macros(consumed_items)
    supplement_macros, supplement_breakdown = compute_daily_supplement_macros(
        svc, user_id=user.id
    )
    consumed = consumed_from_meals + supplement_macros
    delta = calc_macro_delta(target=target, consumed=consumed)
    return NutritionTodayResponse(
        target_kcal=target.kcal,
        target_protein_g=target.protein_g,
        target_carbs_g=target.carbs_g,
        target_fats_g=target.fats_g,
        consumed_kcal=consumed.kcal,
        consumed_protein_g=consumed.protein_g,
        consumed_carbs_g=consumed.carbs_g,
        consumed_fats_g=consumed.fats_g,
        delta_kcal=delta.kcal,
        delta_protein_g=delta.protein_g,
        delta_carbs_g=delta.carbs_g,
        delta_fats_g=delta.fats_g,
        supplement_breakdown=supplement_breakdown,
    )


# --- Helpers ----------------------------------------------------------------


def _matches_planned_slot(
    current_plan, meal: MealSlot, logged: list[LoggedDish]
) -> bool:
    """True when `logged` exactly matches the plan's entries for `meal`.

    Match = same set of dish_refs with servings equal up to `_SERVINGS_TOLERANCE`.
    Order-insensitive; supports the (currently unused) case of multiple entries
    per slot with the same dish_ref by summing servings per dish_ref before
    comparing. Empty logged list never matches (an "ate planned" click without
    dishes is degenerate — fall through to the replan).
    """
    if current_plan is None or not logged:
        return False
    planned_by_ref: dict[str, float] = {}
    for entry in current_plan.entries or []:
        entry_dict = entry if isinstance(entry, dict) else entry.model_dump()
        if entry_dict.get("meal") != meal.value:
            continue
        ref = entry_dict.get("dish_ref")
        if not ref:
            return False
        planned_by_ref[ref] = planned_by_ref.get(ref, 0.0) + float(
            entry_dict.get("servings", 0.0)
        )
    if not planned_by_ref:
        return False
    logged_by_ref: dict[str, float] = {}
    for d in logged:
        logged_by_ref[d.dish_ref] = logged_by_ref.get(d.dish_ref, 0.0) + float(
            d.servings
        )
    if set(planned_by_ref) != set(logged_by_ref):
        return False
    return all(
        abs(planned_by_ref[ref] - logged_by_ref[ref]) <= _SERVINGS_TOLERANCE
        for ref in planned_by_ref
    )


def _resolve_dish_macros(svc, payload: LogMealRequest) -> tuple[Macros, list[str]]:
    """Compute total macros for the logged dishes + surface any lookup misses.

    Resolution order per dish:
      1. macro_id → macro_db row
      2. dish_ref → macro_db.dish_name_normalized
      3. per-dish macro override (client-supplied kcal/protein_g/etc.)
      4. give up, add a warning so the client can prompt the user

    Skipped meals return zero macros. The tracker uses this to shift the
    remaining-macro target upward, so skips DO shrink meals_remaining but keep
    planning_remaining full — which is what pushes bigger portions into later
    meals on the next replan.
    """
    warnings: list[str] = []
    if payload.action is MealAction.SKIPPED or not payload.actual_dishes:
        return Macros.zero(), warnings

    ids = [d.macro_id for d in payload.actual_dishes if d.macro_id is not None]
    names = [d.dish_ref for d in payload.actual_dishes if d.macro_id is None]

    by_id: dict[int, dict] = {}
    if ids:
        resp = svc.table("macro_db").select("*").in_("id", ids).execute()
        by_id = {row["id"]: row for row in (resp.data or [])}
    by_name: dict[str, dict] = {}
    if names:
        resp = (
            svc.table("macro_db")
            .select("*")
            .in_("dish_name_normalized", names)
            .execute()
        )
        by_name = {row["dish_name_normalized"]: row for row in (resp.data or [])}

    total = Macros.zero()
    for d in payload.actual_dishes:
        macro_row = by_id.get(d.macro_id) if d.macro_id is not None else by_name.get(d.dish_ref)
        if macro_row is not None:
            per = Macros(
                kcal=float(macro_row["kcal"]),
                protein_g=float(macro_row["protein_g"]),
                carbs_g=float(macro_row["carbs_g"]),
                fats_g=float(macro_row["fats_g"]),
            )
        elif d.kcal is not None:
            # Client-supplied macros — take at face value (dev-time / user
            # knows what they ate, dish isn't in macro_db).
            per = Macros(
                kcal=float(d.kcal),
                protein_g=float(d.protein_g or 0.0),
                carbs_g=float(d.carbs_g or 0.0),
                fats_g=float(d.fats_g or 0.0),
            )
        else:
            warnings.append(
                f"'{d.dish_ref}' not found in macro_db and no macros supplied — logged with 0 kcal"
            )
            continue
        total = total + per.scaled(d.servings)
    return total, warnings


# --- Macro-db lookup for the client-side dropdown --------------------------


class MacroSearchRow(BaseModel):
    id: int
    dish_name_normalized: str
    dish_name: str
    kcal: float
    protein_g: float
    is_veg: bool


# --- End-of-day canteen top-up (M5 addition) -------------------------------


class TopupSuggestion(BaseModel):
    canteen_item_id: Optional[int] = None
    macro_id: Optional[int] = None
    dish_ref: str
    dish_name: str
    shop_name: str
    servings: float
    price_inr: float
    # kcal/protein/carbs/fats are TOTALS for `servings` servings (not per-serving),
    # matching how PlanMealEntry.macros is populated in find_canteen_additions_for_gap.
    kcal: float
    protein_g: float
    carbs_g: float
    fats_g: float
    macro_verified: bool


class TopupResponse(BaseModel):
    eligible: bool
    # When eligible=False, `reason` explains why (dinner not logged, gap too
    # small, no viable canteen picks). When True, `suggestions` is populated.
    reason: Optional[str] = None
    dinner_logged: bool
    gap_kcal: float
    gap_protein_g: float
    gap_carbs_g: float
    gap_fats_g: float
    suggestions: list[TopupSuggestion] = Field(default_factory=list)
    total_added_kcal: float = 0.0
    total_added_protein_g: float = 0.0
    total_cost_inr: float = 0.0


@router.get("/nutrition/topup-suggestions", response_model=TopupResponse)
def topup_suggestions(user: AuthedUser) -> TopupResponse:
    """Advisory canteen add-ons to close the day's macro gap.

    Fires once the dinner slot has been logged (any action counts — planned,
    different, or skipped) and there's still a meaningful shortfall vs target.
    Does NOT mutate the committed plan; the Streamlit card renders these as
    hints, not as a new lifecycle plan.
    """
    svc = get_service_role_client()
    plan_date = datetime.now(timezone.utc).date()

    profile = (
        svc.table("user_profile")
        .select(
            "target_kcal, target_protein_g, target_carbs_g, target_fats_g, "
            "veg_default, budget_hard_inr"
        )
        .eq("user_id", user.id)
        .single()
        .execute()
        .data
    )
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Profile not found — onboard first.")

    logs = (
        svc.table("meal_logs")
        .select("meal, actual_macros_json")
        .eq("user_id", user.id)
        .eq("date", plan_date.isoformat())
        .execute()
        .data
        or []
    )
    dinner_logged = any(row.get("meal") == MealSlot.DINNER.value for row in logs)

    consumed_from_meals = sum_macros(
        [
            Macros(
                kcal=float((l.get("actual_macros_json") or {}).get("kcal", 0.0)),
                protein_g=float((l.get("actual_macros_json") or {}).get("protein_g", 0.0)),
                carbs_g=float((l.get("actual_macros_json") or {}).get("carbs_g", 0.0)),
                fats_g=float((l.get("actual_macros_json") or {}).get("fats_g", 0.0)),
            )
            for l in logs
        ]
    )
    supplement_macros, _ = compute_daily_supplement_macros(svc, user_id=user.id)
    consumed = consumed_from_meals + supplement_macros
    target = Macros(
        kcal=float(profile["target_kcal"]),
        protein_g=float(profile["target_protein_g"]),
        carbs_g=float(profile["target_carbs_g"]),
        fats_g=float(profile["target_fats_g"]),
    )
    delta = calc_macro_delta(target=target, consumed=consumed)
    # delta is signed: positive = under target. Clamp to non-negative for the
    # gap-fill logic — overshoots don't get filled.
    gap = Macros(
        kcal=max(0.0, delta.kcal),
        protein_g=max(0.0, delta.protein_g),
        carbs_g=max(0.0, delta.carbs_g),
        fats_g=max(0.0, delta.fats_g),
    )

    base_response = TopupResponse(
        eligible=False,
        dinner_logged=dinner_logged,
        gap_kcal=gap.kcal,
        gap_protein_g=gap.protein_g,
        gap_carbs_g=gap.carbs_g,
        gap_fats_g=gap.fats_g,
    )
    if not dinner_logged:
        base_response.reason = "Dinner not logged yet — waiting until dinner is done to suggest top-ups."
        return base_response
    if gap.protein_g < TOPUP_MIN_PROTEIN_GAP_G and gap.kcal < TOPUP_MIN_KCAL_GAP:
        base_response.reason = "You're close enough to target — no top-up needed."
        return base_response

    prefs = (
        svc.table("user_preferences")
        .select("kind, value, status")
        .eq("user_id", user.id)
        .eq("status", PreferenceStatus.ACTIVE.value)
        .execute()
        .data
        or []
    )
    dislikes = [
        (p.get("value") or "").lower()
        for p in prefs
        if p.get("kind") == PreferenceKind.DISLIKE.value and p.get("value")
    ]

    picks = find_canteen_additions_for_gap(
        svc,
        protein_gap_g=gap.protein_g,
        # Use the kcal gap as the ceiling so picks don't push consumed over
        # target. This is a stricter cap than the planner uses — advisory
        # suggestions should undershoot, not chase the number.
        kcal_remaining_ceiling=gap.kcal if gap.kcal > 0 else TOPUP_MIN_KCAL_GAP,
        veg_only=bool(profile["veg_default"]),
        dislikes=dislikes,
        hard_budget_remaining_inr=(
            float(profile["budget_hard_inr"])
            if profile["budget_hard_inr"] is not None
            else None
        ),
        meal_slot=MealSlot.SNACK,
        max_items=3,
    )
    if not picks:
        base_response.reason = (
            "No canteen options fit your preferences and remaining budget for the gap."
        )
        return base_response

    suggestions: list[TopupSuggestion] = []
    total_kcal = 0.0
    total_protein = 0.0
    total_cost = 0.0
    for entry in picks:
        parts = entry.dish_ref.split(":")
        shop = parts[1] if len(parts) > 1 else ""
        dish = parts[2].replace("_", " ").title() if len(parts) > 2 else entry.dish_ref
        suggestions.append(
            TopupSuggestion(
                macro_id=entry.macro_id,
                dish_ref=entry.dish_ref,
                dish_name=dish,
                shop_name=shop,
                servings=entry.servings,
                price_inr=entry.price_inr,
                kcal=entry.macros.kcal,
                protein_g=entry.macros.protein_g,
                carbs_g=entry.macros.carbs_g,
                fats_g=entry.macros.fats_g,
                macro_verified=entry.macro_verified,
            )
        )
        total_kcal += entry.macros.kcal
        total_protein += entry.macros.protein_g
        total_cost += entry.price_inr

    return TopupResponse(
        eligible=True,
        dinner_logged=True,
        gap_kcal=gap.kcal,
        gap_protein_g=gap.protein_g,
        gap_carbs_g=gap.carbs_g,
        gap_fats_g=gap.fats_g,
        suggestions=suggestions,
        total_added_kcal=total_kcal,
        total_added_protein_g=total_protein,
        total_cost_inr=total_cost,
    )


@router.get("/macro-db/search", response_model=list[MacroSearchRow])
def search_macros(user: AuthedUser, q: str = "", limit: int = 50) -> list[MacroSearchRow]:
    """Case-insensitive substring search over dish_name_normalized. Used by
    the Streamlit `Ate different` picker."""
    svc = get_service_role_client()
    query = svc.table("macro_db").select(
        "id, dish_name_normalized, kcal, protein_g, is_veg"
    )
    q = (q or "").strip().lower()
    if q:
        query = query.ilike("dish_name_normalized", f"%{q}%")
    resp = query.order("dish_name_normalized").limit(limit).execute()
    return [
        MacroSearchRow(
            id=row["id"],
            dish_name_normalized=row["dish_name_normalized"],
            dish_name=row["dish_name_normalized"].replace("_", " ").title(),
            kcal=float(row["kcal"]),
            protein_g=float(row["protein_g"]),
            is_veg=row["is_veg"],
        )
        for row in (resp.data or [])
    ]
