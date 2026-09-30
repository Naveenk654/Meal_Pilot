"""LangGraph wiring for the Planner (§6.2).

We keep the graph definition small: nodes come from `planner_nodes`, and the
router `run_planner` opens the `agent_run` context, invokes the graph, and
returns the final state. Idempotency of retries is handled at the
`agent_runs.idempotency_key` layer — the graph itself is stateless.

M3 scope reminder:
  * Mess-only, no canteen fallback, no HITL loops. `invoke_fallback` and
    `request_hitl` nodes are stubs that just set flags; the graph edges
    route straight to terminal on infeasibility.
  * MAX_CANDIDATES and MAX_REVISIONS are read from Settings.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from supabase import Client

from backend.agents.planner_nodes import (
    check_confidence_node,
    check_menu_freshness,
    classify_outcome,
    commit_plan_node,
    generate_candidates_node,
    invoke_fallback_node,
    load_state,
    score_and_select_node,
    validate_candidates_node,
)
from backend.config import Settings, get_settings
from backend.idempotency.keys import planner_run_key
from backend.models.enums import HITLStatus, PlanMode, Trigger
from backend.models.plan import Plan
from backend.models.trace import TraceEvent
from backend.tools.hitl import HITLSurface, create_hitl_request
from backend.tools.trace import agent_run


@dataclass(frozen=True)
class PlannerOutcome:
    inserted: bool
    final_plan: Plan | None
    infeasible: bool
    hitl_status: HITLStatus
    confidence: float
    idempotency_key: str
    trace: list[TraceEvent]


async def run_planner(
    svc: Client,
    *,
    user_id: str,
    plan_date: date,
    trigger: Trigger,
    triggering_event_id: str | None = None,
    settings: Settings | None = None,
) -> PlannerOutcome:
    """One-shot invocation. Runs the graph and persists the audit trail."""
    s = settings or get_settings()
    idem_key = planner_run_key(user_id, plan_date, trigger, triggering_event_id)

    # Un-poison: if a prior run for this key produced no committed plan and no
    # HITL surface (i.e. it errored or hit menu-uncertain and left the user
    # with nothing), delete it so this attempt can actually try. Successful
    # runs (final_plan present OR hitl_status set) are preserved — those are
    # the legitimate idempotency short-circuits.
    prior = (
        svc.table("agent_runs")
        .select("id, final_outcome, hitl_status")
        .eq("idempotency_key", idem_key)
        .limit(1)
        .execute()
        .data
    )
    if prior:
        p = prior[0]
        outcome = p.get("final_outcome") or {}
        has_plan = bool(outcome.get("plan_id"))
        has_hitl = bool(p.get("hitl_status"))
        if not has_plan and not has_hitl:
            svc.table("agent_runs").delete().eq("id", p["id"]).execute()

    with agent_run(
        svc,
        idempotency_key=idem_key,
        agent="planner",
        trigger=trigger.value,
        user_id=user_id,
        triggering_event_id=triggering_event_id,
    ) as run:
        if not run.inserted:
            # Idempotent short-circuit: the caller must accept the earlier outcome.
            return PlannerOutcome(
                inserted=False,
                final_plan=None,
                infeasible=False,
                hitl_status=HITLStatus.NOT_NEEDED,
                confidence=0.0,
                idempotency_key=idem_key,
                trace=[],
            )

        state: dict[str, Any] = await load_state(
            svc,
            user_id=user_id,
            plan_date=plan_date,
            trigger=trigger.value,
            triggering_event_id=triggering_event_id,
            settings=s,
        )
        state["_svc_client"] = svc

        # Menu uncertainty is a hard exit + HITL surface #2.
        state.update(check_menu_freshness(state))
        if state.get("infeasible"):
            _create_hitl(
                svc,
                user_id=user_id,
                surface=HITLSurface.MENU_UNCERTAIN,
                question="No active menu covers today. Confirm to proceed with canteen-only planning.",
                context={"date": plan_date.isoformat()},
            )
            state["hitl_status"] = HITLStatus.PENDING
            _flush_trace(run, state, final=True)
            return _outcome(state, idem_key)

        # Bounded generate → validate → revise loop (mess-only).
        while True:
            state.update(await generate_candidates_node(state))
            if state.get("infeasible"):
                break
            state.update(validate_candidates_node(state))
            classify = classify_outcome(state)
            state.update(classify)
            route = classify.get("_route")
            if route == "select":
                break
            if route == "infeasible":
                break

        # M5 fallback: mess-only infeasible → invoke canteen fallback, then
        # re-validate the augmented plan. Also invoked when mess-only produced
        # a valid winner but confidence is low (checked below after scoring).
        if state.get("infeasible"):
            state.update(invoke_fallback_node(state, svc))
            if state.get("candidate_plans"):
                state.update(validate_candidates_node(state))
                results = state.get("validation_results") or []
                if any(r.is_valid for r in results):
                    state["infeasible"] = False

        if not state.get("infeasible"):
            state.update(score_and_select_node(state))
            state.update(check_confidence_node(state))
            # Augment with canteen items when the mess-only winner isn't good
            # enough. Three triggers, in priority order:
            #   1. plan_mode='mixed' — user explicitly opted into a mess+shops
            #      plan; augment every time up to budget_soft_inr.
            #   2. Winning candidate's soft score below fallback_quality_
            #      threshold — technically valid but silly (e.g. "4 servings
            #      of tea to hit protein"). Catches practicality/variety
            #      failures the hard-constraint layer misses.
            #   3. hitl_status=PENDING from check_confidence_node — the
            #      pre-existing M5 low-confidence safety net.
            # All three are gated on `not fallback_invoked` so we only try
            # augmentation once per run.
            if _should_augment_with_canteen(state, s):
                state.update(invoke_fallback_node(state, svc))
                if state.get("candidate_plans"):
                    state.update(validate_candidates_node(state))
                    results = state.get("validation_results") or []
                    if any(r.is_valid for r in results):
                        state.update(score_and_select_node(state))
                        state.update(check_confidence_node(state))
            state.update(commit_plan_node(state, svc))

            # Surface HITL #4 (fallback proposal) once, or #1 (low confidence).
            if state.get("fallback_invoked") and state.get("final_plan"):
                canteen_entries = [
                    e for e in state["final_plan"].entries if getattr(e, "source", "mess") == "canteen"
                ]
                if canteen_entries:
                    _create_hitl(
                        svc,
                        user_id=user_id,
                        surface=HITLSurface.FALLBACK_PROPOSAL,
                        question="Mess menu can't hit your macro targets alone. Approve these canteen additions?",
                        options=[
                            {
                                "dish_ref": e.dish_ref,
                                "servings": e.servings,
                                "kcal": e.macros.kcal,
                                "protein_g": e.macros.protein_g,
                                "price_inr": e.price_inr,
                            }
                            for e in canteen_entries
                        ],
                        context={"plan_id": state["final_plan"].id, "date": plan_date.isoformat()},
                    )
                    state["hitl_status"] = HITLStatus.PENDING
            elif state.get("hitl_status") == HITLStatus.PENDING and state.get("final_plan"):
                _create_hitl(
                    svc,
                    user_id=user_id,
                    surface=HITLSurface.LOW_CONFIDENCE,
                    question=f"Plan confidence is {state.get('confidence'):.2f} — below threshold. Approve?",
                    context={"plan_id": state["final_plan"].id, "date": plan_date.isoformat()},
                )
            elif s.hitl_always_approve_plan and state.get("final_plan"):
                # Surface #7 — routine plan approval. Only when explicitly on.
                _create_hitl(
                    svc,
                    user_id=user_id,
                    surface=HITLSurface.PLAN_APPROVAL,
                    question="Does today's plan look good?",
                    context={
                        "plan_id": state["final_plan"].id,
                        "date": plan_date.isoformat(),
                        "confidence": state.get("confidence"),
                    },
                )
        else:
            # Infeasible even after fallback → surface #3.
            _create_hitl(
                svc,
                user_id=user_id,
                surface=HITLSurface.CONSTRAINT_INFEASIBLE,
                question="No feasible plan today from mess or canteen. Relax a constraint?",
                context={"date": plan_date.isoformat()},
            )
            state["hitl_status"] = HITLStatus.PENDING

        _flush_trace(run, state, final=True)
        final_plan: Plan | None = state.get("final_plan")
        run.set_final_outcome(
            confidence=state.get("confidence"),
            confidence_factors=(
                state["confidence_factors"].model_dump()
                if state.get("confidence_factors") is not None
                else None
            ),
            final_outcome={
                "plan_id": final_plan.id if final_plan else None,
                "infeasible": bool(state.get("infeasible")),
            },
            hitl_status=HITLStatus(state.get("hitl_status") or HITLStatus.NOT_NEEDED),
        )
        return _outcome(state, idem_key)


def _should_augment_with_canteen(state: dict[str, Any], settings: Settings) -> bool:
    """Decide whether to run `invoke_fallback_node` on a plan that already
    validated at least one mess-only candidate.

    Mixed mode DOES NOT trigger the post-hoc fallback: `_build_candidate_
    inputs` already loads canteen items into the LLM's pool, so the winning
    candidate can (and should) include canteen entries directly. Running
    fallback again would duplicate additions and blow past the soft budget.

    Returns True iff any of these hold AND fallback hasn't already run:
      * winning candidate's soft score is below fallback_quality_threshold —
        catches valid-but-impractical plans (repetition, silly serving counts).
      * confidence check emitted hitl_status=PENDING — the low-confidence
        safety net path we've had since M5.
    """
    if state.get("fallback_invoked"):
        return False
    plan = state.get("selected_plan")
    if plan is None:
        return False

    profile = state.get("profile")
    # Mixed mode: candidates are already mess+canteen — no post-hoc fallback.
    if profile is not None and getattr(profile, "plan_mode", None) is PlanMode.MIXED:
        return False

    try:
        soft_score = float(plan.validation_result.total_soft_score)
    except (AttributeError, TypeError, ValueError):
        soft_score = 1.0
    if soft_score < settings.fallback_quality_threshold:
        return True

    if state.get("hitl_status") == HITLStatus.PENDING:
        return True

    return False


def _outcome(state: dict[str, Any], idem_key: str) -> PlannerOutcome:
    return PlannerOutcome(
        inserted=True,
        final_plan=state.get("final_plan"),
        infeasible=bool(state.get("infeasible")),
        hitl_status=HITLStatus(state.get("hitl_status") or HITLStatus.NOT_NEEDED),
        confidence=float(state.get("confidence") or 0.0),
        idempotency_key=idem_key,
        trace=list(state.get("reasoning_trace") or []),
    )


def _create_hitl(
    svc: Client,
    *,
    user_id: str,
    surface: str,
    question: str,
    options: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """Best-effort HITL create — a Supabase error must not kill the planner run."""
    try:
        create_hitl_request(
            svc,
            user_id=user_id,
            agent="planner",
            surface=surface,
            question=question,
            options=options,
            context=context,
        )
    except Exception:
        pass


def _flush_trace(run, state: dict[str, Any], *, final: bool) -> None:
    """Persist every TraceEvent recorded since the last flush.

    We push one `decision_traces` row per event so the M8 dashboard has fine-
    grained data. `run.log_decision` requires a non-empty reason_code.
    """
    events: list[TraceEvent] = list(state.get("reasoning_trace") or [])
    pushed = int(state.get("_pushed_trace_count") or 0)
    for ev in events[pushed:]:
        run.log_decision(
            step_name=ev.step_name,
            reason_code=ev.reason_code,
            candidates_considered=ev.candidates_considered or None,
            validation_results=ev.validation_results or None,
            chosen_option=ev.chosen_option,
            llm_reasoning=ev.llm_reasoning,
        )
    state["_pushed_trace_count"] = len(events)
