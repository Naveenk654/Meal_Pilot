"""Node implementations for the Planner LangGraph (§6.2).

Each node is a pure `PlannerState -> partial PlannerState` function. LangGraph
merges the partial return into the working state automatically, so nodes
only need to fill in the keys they compute.

The node code is DB-aware but not DB-owning: the loader assembles inputs from
Supabase once at the start of the run, and everything downstream operates on
the loaded snapshot. This keeps the graph testable with mocked DB slices.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from supabase import Client

from backend.config import Settings, get_settings
from backend.models.enums import (
    ActivityLevel,
    Gender,
    Goal,
    HITLStatus,
    MealSlot,
    PlanMode,
    PlanStatus,
    PreferenceAuthority,
    PreferenceKind,
    PreferenceSource,
    PreferenceStatus,
    UserMode,
)
from backend.models.menu import MenuFreshness
from backend.models.nutrition import Macros, MacrosSigned
from backend.models.plan import (
    CandidatePlan,
    ConfidenceBreakdown,
    Plan,
    PlanMealEntry,
    ValidationResult,
)
from backend.models.trace import TraceEvent
from backend.models.user import BehavioralFact, UserPreference, UserProfile
from backend.tools.candidate_gen import (
    CandidateGenError,
    CandidateGenInputs,
    MenuDishForGen,
    generate_candidates,
    generate_revised_candidates,
)
from backend.tools.canteen import (
    augment_with_canteen,
    find_canteen_additions_for_gap,
    load_canteen_for_mixed_mode,
)
from backend.tools.hitl import HITLSurface, create_hitl_request
from backend.tools.memory import load_active_facts
from backend.tools.supplements import compute_daily_supplement_macros
from backend.tools.confidence import compute_confidence
from backend.tools.constraint_engine import (
    MenuDishRef,
    ValidationInputs,
    validate_candidate,
)
from backend.tools.llm_router import DegradedModeSignal
from backend.tools.menu import resolve_daily_menu
from backend.tools.nutrition import (
    macro_delta as calc_macro_delta,
    planning_remaining as calc_planning_remaining,
    sum_macros,
)
from backend.tools.plan_writer import ConcurrentReplanError, commit_plan
from backend.tools.serving_scaler import (
    bump_candidates_to_protein_floor,
    scale_candidates_to_kcal,
)


ALL_MEALS: list[MealSlot] = [MealSlot.BREAKFAST, MealSlot.LUNCH, MealSlot.SNACK, MealSlot.DINNER]


# --- Load state -------------------------------------------------------------


async def load_state(
    svc: Client,
    *,
    user_id: str,
    plan_date: date,
    trigger: str,
    triggering_event_id: str | None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Assemble PlannerState from Supabase. First node in the graph."""
    s = settings or get_settings()

    profile = _load_profile(svc, user_id)
    preferences = _load_preferences(svc, user_id)
    daily_menu = resolve_daily_menu(svc, on_date=plan_date)
    consumed = _load_consumed_macros(svc, user_id=user_id, plan_date=plan_date)
    active_memory = _load_active_memory_as_facts(svc, user_id=user_id)

    target = Macros(
        kcal=profile.target_kcal,
        protein_g=profile.target_protein_g,
        carbs_g=profile.target_carbs_g,
        fats_g=profile.target_fats_g,
    )
    delta = calc_macro_delta(target=target, consumed=consumed)
    remaining = calc_planning_remaining(delta)
    meals_completed = _load_completed_meals(svc, user_id=user_id, plan_date=plan_date)
    meals_remaining = [m for m in ALL_MEALS if m not in meals_completed]

    freshness = None
    todays_menu_snapshot = None
    if daily_menu is not None:
        freshness = MenuFreshness(
            source=daily_menu.source,  # type: ignore[arg-type]
            ingested_at=datetime.now(timezone.utc),
            effective_from=daily_menu.effective_from,
            effective_to=daily_menu.effective_to,
            version=daily_menu.version,
            content_hash=daily_menu.content_hash,
        )
        todays_menu_snapshot = daily_menu

    return {
        "user_id": user_id,
        "date": plan_date,
        "trigger": trigger,
        "triggering_event_id": triggering_event_id,
        "profile": profile,
        "behavioral_memory": active_memory,
        "todays_menu": todays_menu_snapshot,
        "menu_freshness": freshness,
        "veg_today": profile.veg_default,
        "budget_remaining_inr": profile.budget_soft_inr,
        "target_macros": target,
        "consumed_macros": consumed,
        "macro_delta": delta,
        "planning_remaining_macros": remaining,
        "meals_completed": meals_completed,
        "meals_remaining": meals_remaining,
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
        "_preferences": preferences,       # internal — not on PlannerState schema
        "_settings": s,
    }


# --- Node: menu freshness ---------------------------------------------------


def check_menu_freshness(state: dict[str, Any]) -> dict[str, Any]:
    """§12 — if no active menu covers today, we do NOT silently fall back to
    yesterday. In M3 we mark infeasible; M5 will invoke canteen fallback."""
    trace: list[TraceEvent] = list(state.get("reasoning_trace") or [])
    if state.get("todays_menu") is None:
        trace.append(
            TraceEvent(step_name="check_menu_freshness", reason_code="menu_uncertain")
        )
        return {"infeasible": True, "reasoning_trace": trace}
    trace.append(TraceEvent(step_name="check_menu_freshness", reason_code="menu_fresh"))
    return {"reasoning_trace": trace}


# --- Node: generate candidates ----------------------------------------------


async def generate_candidates_node(state: dict[str, Any]) -> dict[str, Any]:
    """Ask the LLM for N candidates, deterministically attach macros. On revision
    attempts we pass the last iteration's violations back as feedback."""
    s: Settings = state["_settings"]
    todays_menu = state["todays_menu"]
    if todays_menu is None:
        return {"infeasible": True}

    inputs = _build_candidate_inputs(state, n=s.max_candidates)
    revision = int(state.get("revision_attempts") or 0)

    trace: list[TraceEvent] = list(state.get("reasoning_trace") or [])
    try:
        if revision == 0:
            plans, llm_result = await generate_candidates(inputs, settings=s)
        else:
            feedback = _violations_to_feedback(state.get("validation_results") or [])
            plans, llm_result = await generate_revised_candidates(
                inputs, feedback_lines=feedback, settings=s
            )
    except DegradedModeSignal as exc:
        trace.append(
            TraceEvent(
                step_name="generate_candidates",
                reason_code="llm_degraded",
                llm_reasoning=str(exc),
            )
        )
        return {"infeasible": True, "reasoning_trace": trace}
    except CandidateGenError as exc:
        trace.append(
            TraceEvent(
                step_name="generate_candidates",
                reason_code="llm_output_invalid",
                llm_reasoning=str(exc),
            )
        )
        return {"infeasible": True, "reasoning_trace": trace}

    generated = int(state.get("candidates_generated") or 0) + len(plans)
    trace.append(
        TraceEvent(
            step_name="generate_candidates",
            reason_code="candidates_generated" if plans else "no_candidates",
            candidates_considered=[
                {"candidate_id": p.candidate_id, "entries": len(p.entries)}
                for p in plans
            ],
        )
    )
    # Deterministic post-processing: shrink over-target plans before validation.
    # The LLM is unreliable at holding a kcal ceiling across many items; the
    # scaler enforces it in Python (matches §4 "deterministic for all numeric
    # calc"). Only fires when a plan is >110% of target — under-target plans
    # go straight through to the revision loop.
    target: Macros = state["target_macros"]
    plans, scaled_count = scale_candidates_to_kcal(plans, target)
    if scaled_count > 0:
        trace.append(
            TraceEvent(
                step_name="scale_servings",
                reason_code="candidates_scaled_to_kcal_ceiling",
                chosen_option={"scaled_count": scaled_count, "total": len(plans)},
            )
        )
    # Symmetric to the kcal scaler: if the shrunk (or originally-undershot)
    # plan falls below the protein floor, bump high-protein-density entries
    # back up within the practical caps and a kcal-headroom limit. Handles
    # both the mirror of the kcal overshoot (LLM undershoots protein on cut)
    # and the side-effect of scaling a protein-borderline plan.
    todays_menu = state.get("todays_menu")
    practical_caps: dict[str, float] = {}
    svc: Client | None = state.get("_svc_client")
    if svc is not None and todays_menu is not None:
        mids = [i.macro_id for i in todays_menu.items if i.macro_id]
        if mids:
            rows = svc.table("macro_db").select(
                "dish_name_normalized, practical_max_servings_per_day"
            ).in_("id", mids).execute().data or []
            practical_caps = {
                r["dish_name_normalized"]: float(r["practical_max_servings_per_day"])
                for r in rows
            }
    plans, bumped_count = bump_candidates_to_protein_floor(plans, target, practical_caps)
    if bumped_count > 0:
        trace.append(
            TraceEvent(
                step_name="bump_protein",
                reason_code="candidates_bumped_to_protein_floor",
                chosen_option={"bumped_count": bumped_count, "total": len(plans)},
            )
        )
    return {
        "candidate_plans": plans,
        "candidates_generated": generated,
        "reasoning_trace": trace,
        "_last_llm": {
            "tokens": llm_result.tokens_used,
            "latency_ms": llm_result.latency_ms,
        },
    }


# --- Node: validate candidates ---------------------------------------------


def validate_candidates_node(state: dict[str, Any]) -> dict[str, Any]:
    """Run the Constraint Engine over every candidate. No LLM.

    Post-fallback (state.fallback_invoked=True) we relax the macro-sanity
    thresholds: fallback is by definition a compromise, so the ceiling and
    floor loosen instead of rejecting the augmented plan for the same margins
    that got us here.
    """
    prefs: list[UserPreference] = state.get("_preferences") or []
    todays_menu = state["todays_menu"]
    profile: UserProfile = state["profile"]
    target: Macros = state["target_macros"]
    planning: Macros = state["planning_remaining_macros"]
    veg = bool(state.get("veg_today"))
    recent_dishes = set(state.get("_recent_dishes") or [])

    menu_refs = [
        MenuDishRef(
            dish_normalized=i.dish_normalized,
            meal=i.meal,
            is_veg=i.is_veg,
            choice_group_id=getattr(i, "choice_group_id", None),
        )
        for i in (todays_menu.items if todays_menu else [])
    ]

    # Load per-dish practical caps so the constraint engine can hard-fail
    # candidates that stack too many servings.
    practical_caps: dict[str, float] = {}
    svc: Client | None = state.get("_svc_client")
    macro_ids = [i.macro_id for i in (todays_menu.items if todays_menu else []) if i.macro_id]
    if svc is not None and macro_ids:
        rows = svc.table("macro_db").select(
            "dish_name_normalized, practical_max_servings_per_day"
        ).in_("id", macro_ids).execute().data or []
        practical_caps = {
            r["dish_name_normalized"]: float(r["practical_max_servings_per_day"])
            for r in rows
        }

    if state.get("fallback_invoked"):
        # Fallback = compromise plan (mess + canteen). Accept looser bounds.
        max_kcal_ratio = 1.35
        min_protein_ratio = 0.30
    else:
        # Strict thresholds — trip the invalid path so canteen fallback kicks
        # in whenever mess-only can't get close to the real protein target.
        max_kcal_ratio = 1.20
        min_protein_ratio = 0.60

    inputs = ValidationInputs(
        target_macros=target,
        planning_remaining=planning,
        veg_today=veg,
        hard_budget_inr=profile.budget_hard_inr,
        soft_budget_inr=profile.budget_soft_inr,
        preferences=prefs,
        todays_menu=menu_refs,
        recent_dishes=recent_dishes,
        behavioral_facts=state.get("behavioral_memory") or [],
        practical_serving_caps=practical_caps,
        max_kcal_ratio=max_kcal_ratio,
        min_protein_ratio=min_protein_ratio,
    )

    results: list[ValidationResult] = []
    for candidate in state.get("candidate_plans") or []:
        results.append(validate_candidate(candidate, inputs))

    trace: list[TraceEvent] = list(state.get("reasoning_trace") or [])
    valid_count = sum(1 for r in results if r.is_valid)
    trace.append(
        TraceEvent(
            step_name="validate_candidates",
            reason_code="validation_complete",
            validation_results=[
                {
                    "is_valid": r.is_valid,
                    "hard_violations": [v.model_dump() for v in r.hard_violations],
                    "total_soft_score": r.total_soft_score,
                }
                for r in results
            ],
            chosen_option={"valid_count": valid_count, "total": len(results)},
        )
    )
    return {"validation_results": results, "reasoning_trace": trace}


# --- Node: classify outcome (revise vs select vs infeasible) ---------------


def classify_outcome(state: dict[str, Any]) -> dict[str, Any]:
    """Pure classifier — sets a routing hint on state. LangGraph edges read it."""
    results: list[ValidationResult] = state.get("validation_results") or []
    any_valid = any(r.is_valid for r in results)
    s: Settings = state["_settings"]
    revisions = int(state.get("revision_attempts") or 0)

    if any_valid:
        return {"_route": "select"}
    if revisions < s.max_revisions:
        return {
            "_route": "revise",
            "revision_attempts": revisions + 1,
        }
    return {"_route": "infeasible", "infeasible": True}


# --- Node: score and select -------------------------------------------------


def score_and_select_node(state: dict[str, Any]) -> dict[str, Any]:
    """Pick the highest total_soft_score among valid candidates. Returns
    empty dict (no state update) when nothing is valid — the previous
    selected_plan (if any) survives."""
    candidates: list[CandidatePlan] = state.get("candidate_plans") or []
    results: list[ValidationResult] = state.get("validation_results") or []
    scored = [
        (c, r) for c, r in zip(candidates, results, strict=False) if r.is_valid
    ]
    if not scored:
        trace: list[TraceEvent] = list(state.get("reasoning_trace") or [])
        trace.append(
            TraceEvent(
                step_name="score_and_select",
                reason_code="no_valid_candidates",
                chosen_option={"total_seen": len(candidates)},
            )
        )
        return {"reasoning_trace": trace}
    scored.sort(key=lambda pair: pair[1].total_soft_score, reverse=True)
    chosen_candidate, chosen_result = scored[0]

    trace: list[TraceEvent] = list(state.get("reasoning_trace") or [])
    trace.append(
        TraceEvent(
            step_name="score_and_select",
            reason_code="highest_soft_score",
            chosen_option={
                "candidate_id": chosen_candidate.candidate_id,
                "total_soft_score": chosen_result.total_soft_score,
            },
        )
    )

    plan = Plan(
        user_id=state["user_id"],
        date=state["date"],
        entries=chosen_candidate.entries,
        target_macros=state["target_macros"],
        validation_result=chosen_result,
        confidence=0.0,
        confidence_factors=_empty_confidence(),
        status=PlanStatus.VALIDATED,
    )
    return {"selected_plan": plan, "reasoning_trace": trace}


# --- Node: confidence -------------------------------------------------------


def check_confidence_node(state: dict[str, Any]) -> dict[str, Any]:
    plan: Plan | None = state.get("selected_plan")
    if plan is None:
        # No plan to score; return a no-op write so LangGraph accepts the step
        # (empty-dict returns raise InvalidUpdateError).
        return {"reasoning_trace": list(state.get("reasoning_trace") or [])}
    s: Settings = state["_settings"]
    results: list[ValidationResult] = state.get("validation_results") or []
    valid_scores = [r.total_soft_score for r in results if r.is_valid]

    breakdown = compute_confidence(
        freshness=state.get("menu_freshness"),
        on_date=state["date"],
        validation=plan.validation_result,
        infeasible=False,
        all_valid_scores=valid_scores,
        chosen_score=plan.validation_result.total_soft_score,
        fallback_invoked=bool(state.get("fallback_invoked")),
        tool_errors=int(state.get("_tool_errors") or 0),
        tool_calls=int(state.get("_tool_calls") or 0),
    )
    plan = plan.model_copy(
        update={"confidence": breakdown.overall, "confidence_factors": breakdown}
    )

    trace: list[TraceEvent] = list(state.get("reasoning_trace") or [])
    below = breakdown.overall < s.confidence_threshold
    trace.append(
        TraceEvent(
            step_name="check_confidence",
            reason_code="confidence_below_threshold" if below else "confidence_ok",
            chosen_option={
                "overall": breakdown.overall,
                "threshold": s.confidence_threshold,
            },
        )
    )
    # M3: below-threshold path has no fallback/HITL — surface as HITL PENDING and
    # DO NOT commit. M5 will wire canteen fallback here.
    if below:
        return {
            "selected_plan": plan,
            "confidence": breakdown.overall,
            "confidence_factors": breakdown,
            "hitl_status": HITLStatus.PENDING,
            "reasoning_trace": trace,
        }
    return {
        "selected_plan": plan,
        "confidence": breakdown.overall,
        "confidence_factors": breakdown,
        "reasoning_trace": trace,
    }


# --- Node: invoke fallback (M5) --------------------------------------------


def invoke_fallback_node(state: dict[str, Any], svc: Client) -> dict[str, Any]:
    """§6.2 fallback — pick canteen items that close the macro shortfall
    against the best mess-only candidate (or against target if no mess plan).

    Preconditions for entry:
      * no valid mess-only candidate (infeasible), OR
      * best mess-only candidate below the confidence threshold.

    Produces a new CandidatePlan `mess+canteen`, appends it to
    `candidate_plans`, and clears `infeasible` so the next validate cycle runs.
    """
    trace: list[TraceEvent] = list(state.get("reasoning_trace") or [])
    profile: UserProfile = state["profile"]
    target: Macros = state["target_macros"]

    # Base for augmentation. Prefer the best VALID mess candidate; failing
    # that, use the mess candidate closest to target macros as base. Only fall
    # back to an empty plan if there are literally no mess candidates.
    candidates: list[CandidatePlan] = state.get("candidate_plans") or []
    results: list[ValidationResult] = state.get("validation_results") or []
    from backend.models.plan import CandidatePlan as _CP

    def _distance_to_target(c: CandidatePlan) -> float:
        # Weighted normalized distance from target kcal + target protein.
        kcal_err = abs(c.total_macros.kcal - target.kcal) / max(target.kcal, 1.0)
        protein_err = abs(c.total_macros.protein_g - target.protein_g) / max(target.protein_g, 1.0)
        return 0.5 * kcal_err + 0.5 * protein_err

    valid_pairs = [
        (c, r) for c, r in zip(candidates, results, strict=False) if r.is_valid
    ]
    if valid_pairs:
        valid_pairs.sort(key=lambda p: p[1].total_soft_score, reverse=True)
        base = valid_pairs[0][0]
    elif candidates:
        # No mess-only candidate validated (all failed protein floor or kcal
        # ceiling). Pick the mess candidate that's closest to hitting target —
        # canteen additions will close the remaining gap.
        candidates_by_distance = sorted(candidates, key=_distance_to_target)
        base = candidates_by_distance[0]
    else:
        base = _CP(
            candidate_id="empty",
            entries=[],
            total_macros=Macros.zero(),
            total_cost_inr=0.0,
            generated_by="fallback",
        )

    # Compute the gap. Use the RELAXED kcal ceiling (1.35) — fallback runs
    # precisely because the mess-only base blew the strict 1.20 cap, so we
    # need real room here or `find_canteen_additions_for_gap` will get 0.
    protein_gap = max(0.0, target.protein_g - base.total_macros.protein_g)
    kcal_ceiling = max(0.0, target.kcal * 1.35 - base.total_macros.kcal)

    # Canteen spend ceiling. Prefer the user's soft budget — that's what
    # they set as "how much I'm willing to spend at shops today." The hard
    # budget (if set) still wins as an absolute wall. Note: mess costs are
    # already baked into base.total_cost_inr for mess+canteen plans, though
    # for pure mess (₹0) the subtraction is a no-op.
    canteen_budget_ceiling: float | None = None
    soft = getattr(profile, "budget_soft_inr", 0.0) or 0.0
    if soft > 0.0:
        canteen_budget_ceiling = soft - base.total_cost_inr
    if profile.budget_hard_inr is not None:
        hard_remaining = profile.budget_hard_inr - base.total_cost_inr
        canteen_budget_ceiling = (
            min(canteen_budget_ceiling, hard_remaining)
            if canteen_budget_ceiling is not None
            else hard_remaining
        )
    hard_budget_remaining = canteen_budget_ceiling

    prefs: list[UserPreference] = state.get("_preferences") or []
    dislikes = [
        p.value for p in prefs
        if p.kind is PreferenceKind.DISLIKE and p.status is PreferenceStatus.ACTIVE
    ]

    additions = find_canteen_additions_for_gap(
        svc,
        protein_gap_g=protein_gap,
        kcal_remaining_ceiling=kcal_ceiling,
        veg_only=bool(state.get("veg_today")),
        dislikes=dislikes,
        hard_budget_remaining_inr=hard_budget_remaining,
        meal_slot=MealSlot.SNACK,
    )

    if not additions:
        trace.append(
            TraceEvent(
                step_name="invoke_fallback",
                reason_code="no_canteen_options",
                chosen_option={"protein_gap": protein_gap, "kcal_ceiling": kcal_ceiling},
            )
        )
        return {
            "fallback_invoked": True,
            "reasoning_trace": trace,
        }

    fallback_candidate = augment_with_canteen(base, additions, label="mess+canteen")
    trace.append(
        TraceEvent(
            step_name="invoke_fallback",
            reason_code="canteen_additions_proposed",
            candidates_considered=[
                {"dish_ref": e.dish_ref, "servings": e.servings, "kcal": e.macros.kcal}
                for e in additions
            ],
            chosen_option={
                "protein_gap_closed": sum(e.macros.protein_g for e in additions),
                "cost_added_inr": sum(e.price_inr for e in additions),
                "base_candidate_id": base.candidate_id,
                "base_mess_entries": len(base.entries),
            },
        )
    )
    # Keep the original mess candidates in the pool — under the relaxed
    # post-fallback thresholds many of them may now validate, and picking
    # a validating mess-only plan is often preferable to spending money on
    # canteen. score_and_select then picks whichever has the highest soft score.
    return {
        "fallback_invoked": True,
        "candidate_plans": candidates + [fallback_candidate],
        "validation_results": [],
        "infeasible": False,
        "reasoning_trace": trace,
    }


# --- Node: commit -----------------------------------------------------------


def commit_plan_node(state: dict[str, Any], svc: Client) -> dict[str, Any]:
    """Transactional commit. On concurrent-replan loss we abort softly.

    M4 policy: commit even when confidence < threshold. The plan still shows
    to the user (better than seeing nothing after a replan) with hitl_status=
    PENDING preserved on agent_runs. M5 will replace this with a real HITL
    surface + canteen fallback, at which point we can re-gate commits.
    """
    plan: Plan | None = state.get("selected_plan")
    if plan is None:
        # Nothing to commit; no-op write so LangGraph accepts the step.
        return {"reasoning_trace": list(state.get("reasoning_trace") or [])}
    todays_menu = state.get("todays_menu")
    cycle_id_used = getattr(todays_menu, "cycle_id", None) if todays_menu else None
    try:
        result = commit_plan(
            svc,
            user_id=plan.user_id,
            plan_date=plan.date,
            entries_json=[e.model_dump() for e in plan.entries],
            target_macros=plan.target_macros,
            validation_result=plan.validation_result,
            confidence=plan.confidence,
            confidence_factors=plan.confidence_factors,
            cycle_id_used=cycle_id_used,
        )
    except ConcurrentReplanError:
        trace = list(state.get("reasoning_trace") or [])
        trace.append(
            TraceEvent(step_name="commit_plan", reason_code="concurrent_replan_lost")
        )
        return {"reasoning_trace": trace}

    trace = list(state.get("reasoning_trace") or [])
    trace.append(
        TraceEvent(
            step_name="commit_plan",
            reason_code="plan_committed",
            chosen_option={
                "plan_id": result.plan_id,
                "superseded_plan_id": result.superseded_plan_id,
            },
        )
    )
    committed = plan.model_copy(update={"id": result.plan_id, "status": PlanStatus.SENT})
    return {"final_plan": committed, "reasoning_trace": trace}


# --- Loaders ----------------------------------------------------------------


def _load_profile(svc: Client, user_id: str) -> UserProfile:
    prof = (
        svc.table("user_profile").select("*").eq("user_id", user_id).single().execute().data
    )
    user_row = svc.table("users").select("mode").eq("id", user_id).single().execute().data
    return UserProfile(
        user_id=user_id,
        age=prof["age"],
        gender=Gender(prof["gender"]),
        height_cm=float(prof["height_cm"]),
        weight_kg=float(prof["weight_kg"]),
        activity_level=ActivityLevel(prof["activity_level"]),
        goal=Goal(prof["goal"]),
        mode=UserMode(user_row["mode"]),
        veg_default=prof["veg_default"],
        budget_soft_inr=float(prof["budget_soft_inr"]),
        budget_hard_inr=float(prof["budget_hard_inr"]) if prof["budget_hard_inr"] is not None else None,
        # plan_mode is nullable in older rows (pre-0015); default to mess_only.
        plan_mode=PlanMode(prof.get("plan_mode") or "mess_only"),
        bmr=float(prof["bmr"]),
        tdee=float(prof["tdee"]),
        target_kcal=float(prof["target_kcal"]),
        target_protein_g=float(prof["target_protein_g"]),
        target_carbs_g=float(prof["target_carbs_g"]),
        target_fats_g=float(prof["target_fats_g"]),
    )


def _load_preferences(svc: Client, user_id: str) -> list[UserPreference]:
    resp = (
        svc.table("user_preferences")
        .select("*")
        .eq("user_id", user_id)
        .eq("status", PreferenceStatus.ACTIVE.value)
        .execute()
    )
    out: list[UserPreference] = []
    for row in resp.data or []:
        out.append(
            UserPreference(
                id=row["id"],
                user_id=user_id,
                kind=PreferenceKind(row["kind"]),
                value=row["value"],
                source=PreferenceSource(row["source"]),
                authority=PreferenceAuthority(row["authority"]),
                confidence=float(row["confidence"]),
                status=PreferenceStatus(row["status"]),
            )
        )
    return out


def _load_consumed_macros(svc: Client, *, user_id: str, plan_date: date) -> Macros:
    resp = (
        svc.table("meal_logs")
        .select("actual_macros_json")
        .eq("user_id", user_id)
        .eq("date", plan_date.isoformat())
        .execute()
    )
    per_meal: list[Macros] = []
    for row in resp.data or []:
        data = row.get("actual_macros_json") or {}
        per_meal.append(
            Macros(
                kcal=float(data.get("kcal", 0.0)),
                protein_g=float(data.get("protein_g", 0.0)),
                carbs_g=float(data.get("carbs_g", 0.0)),
                fats_g=float(data.get("fats_g", 0.0)),
            )
        )
    meals_total = sum_macros(per_meal)
    # Auto-add supplement contribution so planning_remaining shrinks by whatever
    # whey/creatine/etc. the user has in their profile.
    sup_macros, _ = compute_daily_supplement_macros(svc, user_id=user_id)
    return meals_total + sup_macros


def _load_active_memory_as_facts(svc: Client, *, user_id: str) -> list[BehavioralFact]:
    """Read active behavioral memory rows and materialize BehavioralFact models
    for the planner state. Advisory-only per §7 — the constraint engine uses
    these to tilt soft score, never to invalidate a plan."""
    rows = load_active_facts(svc, user_id=user_id)
    out: list[BehavioralFact] = []
    for r in rows:
        try:
            out.append(
                BehavioralFact(
                    id=r["id"],
                    user_id=user_id,
                    fact=r["fact"],
                    evidence=r.get("evidence") or {},
                )
            )
        except Exception:
            continue
    return out


def _load_completed_meals(svc: Client, *, user_id: str, plan_date: date) -> list[MealSlot]:
    resp = (
        svc.table("meal_logs")
        .select("meal")
        .eq("user_id", user_id)
        .eq("date", plan_date.isoformat())
        .execute()
    )
    seen: set[MealSlot] = set()
    for row in resp.data or []:
        try:
            seen.add(MealSlot(row["meal"]))
        except ValueError:
            continue
    return list(seen)


# --- Prompt building helpers -----------------------------------------------


def is_dish_forbidden_for_llm(
    dish_normalized: str,
    is_veg: bool,
    *,
    veg_today: bool,
    allergy_terms: list[str],
    restriction_terms: list[str],
) -> bool:
    """True if this menu dish must be hidden from the LLM candidate generator.

    Pre-filtering hard-block dishes prevents the failure mode where the LLM
    keeps picking the same allergen/restricted dish across revision passes,
    burning tokens without ever producing a valid plan. Dislikes are
    deliberately NOT here — those are soft signal, kept in the prompt so the
    LLM can weigh them against everything else.
    """
    if veg_today and not is_veg:
        return True
    hay = (dish_normalized or "").lower()
    for term in allergy_terms:
        t = (term or "").lower()
        if t and t in hay:
            return True
    for term in restriction_terms:
        t = (term or "").lower()
        if t and t in hay:
            return True
    return False


def _build_candidate_inputs(state: dict[str, Any], *, n: int) -> CandidateGenInputs:
    todays_menu = state["todays_menu"]
    profile: UserProfile = state["profile"]
    prefs: list[UserPreference] = state.get("_preferences") or []
    likes = [p.value for p in prefs if p.kind is PreferenceKind.LIKE and p.status is PreferenceStatus.ACTIVE]
    dislikes = [p.value for p in prefs if p.kind is PreferenceKind.DISLIKE and p.status is PreferenceStatus.ACTIVE]
    allergy_terms = [
        (p.value or "").lower()
        for p in prefs
        if p.kind is PreferenceKind.ALLERGY and p.status is PreferenceStatus.ACTIVE
    ]
    restriction_terms = [
        (p.value or "").lower()
        for p in prefs
        if p.kind is PreferenceKind.RESTRICTION and p.status is PreferenceStatus.ACTIVE
    ]
    veg_today = bool(state.get("veg_today"))

    # Load macros per menu item for the LLM prompt. The menu resolver already
    # returned macro_id — look them up in one batched call.
    macro_ids = [i.macro_id for i in todays_menu.items if i.macro_id is not None]
    macro_by_id: dict[int, dict] = {}
    if macro_ids:
        # Fetch from macro_db via the caller's client — this is a small
        # convenience; a cleaner refactor would pass the map in. Kept here to
        # keep load_state single-responsibility.
        svc: Client | None = state.get("_svc_client")
        if svc is not None:
            resp = svc.table("macro_db").select("*").in_("id", macro_ids).execute()
            macro_by_id = {row["id"]: row for row in (resp.data or [])}

    menu_for_gen: list[MenuDishForGen] = []
    for item in todays_menu.items:
        macro = macro_by_id.get(item.macro_id) if item.macro_id is not None else None
        if macro is None:
            # Menu item without a macro row — skip; validator would reject anyway.
            continue
        if is_dish_forbidden_for_llm(
            item.dish_normalized,
            item.is_veg,
            veg_today=veg_today,
            allergy_terms=allergy_terms,
            restriction_terms=restriction_terms,
        ):
            # Filtered at the source — the LLM never sees allergen /
            # restricted / non-veg-on-veg-day dishes, so it can't pick them.
            continue
        menu_for_gen.append(
            MenuDishForGen(
                dish_normalized=item.dish_normalized,
                dish_name=item.dish_name,
                meal=item.meal,
                is_veg=item.is_veg,
                macros_per_serving=Macros(
                    kcal=float(macro["kcal"]),
                    protein_g=float(macro["protein_g"]),
                    carbs_g=float(macro["carbs_g"]),
                    fats_g=float(macro["fats_g"]),
                ),
                macro_id=macro["id"],
                macro_verified=bool(macro["verified"]),
                practical_max_servings_per_day=float(macro["practical_max_servings_per_day"]),
                choice_group_id=getattr(item, "choice_group_id", None),
            )
        )

    # Thread approved behavioral facts through as plain sentences so the LLM
    # can honor them ("user skips peanut butter at breakfast"). Constraint
    # engine already uses these as a small soft score; without wiring them
    # into the candidate prompt too, the LLM keeps proposing dishes the user
    # has explicitly rejected — the feedback loop never closes.
    active_facts = state.get("behavioral_memory") or []
    behavioral_facts = [
        (f.fact if hasattr(f, "fact") else str(f))
        for f in active_facts
    ]

    # Mixed-mode: append canteen items to the LLM's dish pool so it can
    # build mess+canteen plans directly instead of relying on the post-hoc
    # fallback. Only fires when the user opted in via plan_mode='mixed' AND
    # set a positive soft budget — mixed mode with ₹0 budget is nonsensical
    # (Preferences UI warns about this too).
    canteen_budget_inr = 0.0
    if profile.plan_mode is PlanMode.MIXED and profile.budget_soft_inr > 0:
        canteen_budget_inr = float(profile.budget_soft_inr)
        svc: Client | None = state.get("_svc_client")
        if svc is not None:
            canteen_cands = load_canteen_for_mixed_mode(
                svc,
                veg_only=veg_today,
                dislikes=dislikes,
                max_price_inr=canteen_budget_inr,
            )
            # Canteen items are slotted at SNACK by default — matches how
            # LNMIIT students actually grab them (evening tea-time, late
            # night). If the LLM decides a canteen item pairs better at
            # breakfast (e.g. milkshake), it can still express that via
            # the entry's meal field; constraint engine only checks that
            # canteen entries have valid meal slots, not that canteen
            # items appear at a specific slot on the menu.
            for c in canteen_cands:
                menu_for_gen.append(
                    MenuDishForGen(
                        dish_normalized=c.dish_ref.split(":")[-1],
                        dish_name=c.dish_name,
                        meal=MealSlot.SNACK,
                        is_veg=c.is_veg,
                        macros_per_serving=c.per_serving,
                        macro_id=c.macro_id,
                        macro_verified=c.macro_verified,
                        practical_max_servings_per_day=c.practical_max,
                        choice_group_id=None,
                        source="canteen",
                        price_inr=c.price_inr,
                        shop_name=c.shop_name,
                        dish_ref=c.dish_ref,
                    )
                )

    return CandidateGenInputs(
        meals_remaining=state["meals_remaining"],
        planning_remaining=state["planning_remaining_macros"],
        todays_menu=menu_for_gen,
        veg_today=bool(state.get("veg_today")),
        dislikes=dislikes,
        likes=likes,
        behavioral_facts=behavioral_facts,
        canteen_budget_inr=canteen_budget_inr,
        n_candidates=n,
    )


def _violations_to_feedback(results: list[ValidationResult]) -> list[str]:
    lines: list[str] = []
    for r in results:
        for v in r.hard_violations:
            lines.append(f"{v.type.value}: {v.detail}")
    # De-dupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out[:10]   # cap to avoid ballooning the prompt


def _empty_confidence() -> ConfidenceBreakdown:
    return ConfidenceBreakdown(
        overall=0.0,
        menu_freshness_factor=0.0,
        macro_verification_factor=0.0,
        validation_factor=0.0,
        candidate_agreement_factor=0.0,
        fallback_factor=1.0,
        tool_health_factor=1.0,
        llm_uncertainty_signal=None,
    )
