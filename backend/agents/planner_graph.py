"""Planner agent — LangGraph `StateGraph` implementation (§6.2).

The graph wires node functions from `planner_nodes.py` into a declarative
state machine. `run_planner` opens the `agent_run` context, invokes the
graph, then drains the final state into HITL emissions + `decision_traces`
rows. All DB writes and HITL surfaces stay in the wrapper — the graph is
pure state transitions.

Graph shape:

    START → menu_freshness ─┬─(infeasible)→ END
                            └─(fresh)→ generate
    generate ─┬─(infeasible)→ fallback_pre_select
              └─(ok)→ validate → classify
    classify ─┬─(select)→ score → confidence → maybe_augment
              ├─(revise)→ generate
              └─(infeasible)→ fallback_pre_select

    fallback_pre_select ─┬─(no_candidates)→ END
                         └─(added)→ revalidate_pre
    revalidate_pre ─┬─(any_valid)→ score  (continues down the main spine)
                    └─(none)→ END

    maybe_augment ─┬─(should_augment)→ fallback_augment
                   └─(no)→ commit
    fallback_augment ─┬─(no_candidates)→ commit
                      └─(added)→ revalidate_augment
    revalidate_augment ─┬─(any_valid)→ score_augment → confidence_augment → commit
                        └─(none)→ commit
    commit → END

Node *functions* are shared between the pre-select and augment branches —
LangGraph just registers the same callable under two different node names
so the compiled graph has non-cyclic conditional edges.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from langgraph.graph import END, START, StateGraph
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
from backend.models.planner_state import PlannerState
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


# --- Graph build -----------------------------------------------------------


def _build_planner_graph(svc: Client):
    """Compile the Planner `StateGraph`.

    `svc` is captured in closures for the two nodes that need DB access
    (`invoke_fallback_node`, `commit_plan_node`) — this keeps the node
    signatures in `planner_nodes.py` untouched while still letting the
    graph treat every node as a `state -> partial_state` function.
    """

    def _menu_freshness(state: dict[str, Any]) -> dict[str, Any]:
        return check_menu_freshness(state)

    async def _generate(state: dict[str, Any]) -> dict[str, Any]:
        return await generate_candidates_node(state)

    def _validate(state: dict[str, Any]) -> dict[str, Any]:
        return validate_candidates_node(state)

    def _classify(state: dict[str, Any]) -> dict[str, Any]:
        return classify_outcome(state)

    def _fallback(state: dict[str, Any]) -> dict[str, Any]:
        return invoke_fallback_node(state, svc)

    def _score(state: dict[str, Any]) -> dict[str, Any]:
        return score_and_select_node(state)

    def _confidence(state: dict[str, Any]) -> dict[str, Any]:
        return check_confidence_node(state)

    def _commit(state: dict[str, Any]) -> dict[str, Any]:
        return commit_plan_node(state, svc)

    def _clear_infeasible_if_any_valid(state: dict[str, Any]) -> dict[str, Any]:
        """After fallback+revalidate: clear infeasible if any candidate is now
        valid; otherwise explicitly reaffirm infeasible so the following edge
        routes to END. LangGraph rejects empty-dict returns, so we always
        write at least one key."""
        results = state.get("validation_results") or []
        if any(r.is_valid for r in results):
            return {"infeasible": False}
        return {"infeasible": True}

    # --- Routing functions ------------------------------------------------

    def _route_after_freshness(state: dict[str, Any]) -> str:
        return "end" if state.get("infeasible") else "generate"

    def _route_after_generate(state: dict[str, Any]) -> str:
        return "fallback_pre_select" if state.get("infeasible") else "validate"

    def _route_after_classify(state: dict[str, Any]) -> str:
        route = state.get("_route")
        if route == "select":
            return "score"
        if route == "revise":
            return "generate"
        return "fallback_pre_select"

    def _route_after_fallback_pre(state: dict[str, Any]) -> str:
        return "revalidate_pre" if state.get("candidate_plans") else "end"

    def _route_after_revalidate_pre(state: dict[str, Any]) -> str:
        results = state.get("validation_results") or []
        return "score" if any(r.is_valid for r in results) else "end"

    def _route_after_confidence(state: dict[str, Any]) -> str:
        settings: Settings = state["_settings"]
        return "fallback_augment" if _should_augment_with_canteen(state, settings) else "commit"

    def _route_after_fallback_augment(state: dict[str, Any]) -> str:
        # When fallback found no additions, keep the pre-augment winner.
        return "revalidate_augment" if state.get("candidate_plans") else "commit"

    def _route_after_revalidate_augment(state: dict[str, Any]) -> str:
        results = state.get("validation_results") or []
        return "score_augment" if any(r.is_valid for r in results) else "commit"

    # --- Assemble ---------------------------------------------------------

    graph: StateGraph = StateGraph(PlannerState)

    graph.add_node("check_menu_freshness", _menu_freshness)
    graph.add_node("generate", _generate)
    graph.add_node("validate", _validate)
    graph.add_node("classify", _classify)
    graph.add_node("fallback_pre_select", _fallback)
    graph.add_node("revalidate_pre", _validate)
    graph.add_node("clear_infeasible", _clear_infeasible_if_any_valid)
    graph.add_node("score", _score)
    graph.add_node("check_confidence", _confidence)
    graph.add_node("fallback_augment", _fallback)
    graph.add_node("revalidate_augment", _validate)
    graph.add_node("score_augment", _score)
    graph.add_node("check_confidence_augment", _confidence)
    graph.add_node("commit", _commit)

    graph.add_edge(START, "check_menu_freshness")
    graph.add_conditional_edges(
        "check_menu_freshness", _route_after_freshness, {"generate": "generate", "end": END}
    )
    graph.add_conditional_edges(
        "generate",
        _route_after_generate,
        {"validate": "validate", "fallback_pre_select": "fallback_pre_select"},
    )
    graph.add_edge("validate", "classify")
    graph.add_conditional_edges(
        "classify",
        _route_after_classify,
        {
            "score": "score",
            "generate": "generate",
            "fallback_pre_select": "fallback_pre_select",
        },
    )
    graph.add_conditional_edges(
        "fallback_pre_select",
        _route_after_fallback_pre,
        {"revalidate_pre": "revalidate_pre", "end": END},
    )
    graph.add_edge("revalidate_pre", "clear_infeasible")
    graph.add_conditional_edges(
        "clear_infeasible",
        _route_after_revalidate_pre,
        {"score": "score", "end": END},
    )
    graph.add_edge("score", "check_confidence")
    graph.add_conditional_edges(
        "check_confidence",
        _route_after_confidence,
        {"fallback_augment": "fallback_augment", "commit": "commit"},
    )
    graph.add_conditional_edges(
        "fallback_augment",
        _route_after_fallback_augment,
        {"revalidate_augment": "revalidate_augment", "commit": "commit"},
    )
    graph.add_conditional_edges(
        "revalidate_augment",
        _route_after_revalidate_augment,
        {"score_augment": "score_augment", "commit": "commit"},
    )
    graph.add_edge("score_augment", "check_confidence_augment")
    graph.add_edge("check_confidence_augment", "commit")
    graph.add_edge("commit", END)

    return graph.compile()


# --- Public entrypoint -----------------------------------------------------


async def run_planner(
    svc: Client,
    *,
    user_id: str,
    plan_date: date,
    trigger: Trigger,
    triggering_event_id: str | None = None,
    settings: Settings | None = None,
) -> PlannerOutcome:
    """One-shot invocation. Runs the LangGraph and persists the audit trail."""
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

        app = _build_planner_graph(svc)
        # Recursion limit covers worst-case walk: freshness → (generate → validate
        # → classify) × (max_revisions + 1) → fallback_pre_select → revalidate_pre
        # → clear_infeasible → score → confidence → fallback_augment →
        # revalidate_augment → score_augment → confidence_augment → commit. Add
        # headroom for conditional-edge resolution steps.
        recursion_limit = 10 + (s.max_revisions + 1) * 3 + 15
        final_state = await app.ainvoke(state, {"recursion_limit": recursion_limit})

        # Side effects: HITL emission + decision_trace flush + agent_runs outcome.
        _emit_hitl(svc, user_id=user_id, plan_date=plan_date, state=final_state, settings=s)

        _flush_trace(run, final_state, final=True)
        final_plan: Plan | None = final_state.get("final_plan")
        run.set_final_outcome(
            confidence=final_state.get("confidence"),
            confidence_factors=(
                final_state["confidence_factors"].model_dump()
                if final_state.get("confidence_factors") is not None
                else None
            ),
            final_outcome={
                "plan_id": final_plan.id if final_plan else None,
                "infeasible": bool(final_state.get("infeasible")),
            },
            hitl_status=HITLStatus(final_state.get("hitl_status") or HITLStatus.NOT_NEEDED),
        )
        return _outcome(final_state, idem_key)


def _emit_hitl(
    svc: Client,
    *,
    user_id: str,
    plan_date: date,
    state: dict[str, Any],
    settings: Settings,
) -> None:
    """Fire the appropriate HITL surface based on terminal state.

    Lives outside the graph so a Supabase hiccup here doesn't poison the
    decision trace or the `agent_runs` row.
    """
    # Menu-uncertain exit (fired before anything else got a chance).
    if state.get("infeasible") and state.get("todays_menu") is None:
        _create_hitl(
            svc,
            user_id=user_id,
            surface=HITLSurface.MENU_UNCERTAIN,
            question="No active menu covers today. Confirm to proceed with canteen-only planning.",
            context={"date": plan_date.isoformat()},
        )
        state["hitl_status"] = HITLStatus.PENDING
        return

    # Fallback produced a plan with canteen entries → ask the user to approve.
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
            return

    # Low-confidence commit → confirmation surface.
    if state.get("hitl_status") == HITLStatus.PENDING and state.get("final_plan"):
        _create_hitl(
            svc,
            user_id=user_id,
            surface=HITLSurface.LOW_CONFIDENCE,
            question=f"Plan confidence is {state.get('confidence'):.2f} — below threshold. Approve?",
            context={"plan_id": state["final_plan"].id, "date": plan_date.isoformat()},
        )
        return

    # Routine plan approval (opt-in).
    if settings.hitl_always_approve_plan and state.get("final_plan"):
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
        return

    # Infeasible even after fallback → relax-a-constraint surface.
    if state.get("infeasible"):
        _create_hitl(
            svc,
            user_id=user_id,
            surface=HITLSurface.CONSTRAINT_INFEASIBLE,
            question="No feasible plan today from mess or canteen. Relax a constraint?",
            context={"date": plan_date.isoformat()},
        )
        state["hitl_status"] = HITLStatus.PENDING


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
