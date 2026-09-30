"""Planner endpoints (M3).

* POST /planner/run  — trigger the Planner Agent for the caller.
* GET  /plans/today  — return the current `sent` plan.

Both endpoints authenticate via Supabase JWT. Planner writes go through the
service-role client so RLS doesn't fight the observability trail; the returned
plan is filtered by user_id in the query, so an admin can't accidentally
serve someone else's plan through /plans/today.
"""
# NOTE: no `from __future__ import annotations` — Pydantic v2 struggles to
# evaluate PEP-604 unions inside FastAPI request/response models when the
# whole module is under string-forward-refs.

from datetime import date, datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.agents.planner_graph import run_planner
from backend.auth.dependencies import AuthUser, get_current_auth_user
from backend.db.supabase_client import get_service_role_client
from backend.models.enums import HITLStatus, Trigger
from backend.tools.plan_writer import fetch_today_plan

router = APIRouter(tags=["planner"])
AuthedUser = Annotated[AuthUser, Depends(get_current_auth_user)]


class RunPlannerRequest(BaseModel):
    date: Optional[date] = None
    trigger: Trigger = Trigger.MORNING_CRON
    triggering_event_id: Optional[str] = None
    # When true, delete any prior planner run for this (user, date, trigger)
    # so the idempotency slot is fresh. Fixes the "first run failed → all
    # retries short-circuit" trap for a day with no committed plan.
    force: bool = False


class RunPlannerResponse(BaseModel):
    inserted: bool
    plan_id: Optional[int]
    infeasible: bool
    hitl_status: HITLStatus
    confidence: float
    idempotency_key: str


@router.post("/planner/run", response_model=RunPlannerResponse)
async def run_planner_endpoint(
    payload: RunPlannerRequest, user: AuthedUser
) -> RunPlannerResponse:
    on_date = payload.date or datetime.now(timezone.utc).date()
    svc = get_service_role_client()

    if payload.force:
        # Free the idempotency slot for this (user_id, date, trigger,
        # triggering_event_id) so run_planner does a full pass instead of
        # returning the previous poisoned outcome.
        from backend.idempotency.keys import planner_run_key

        key = planner_run_key(
            user.id, on_date, payload.trigger, payload.triggering_event_id
        )
        svc.table("agent_runs").delete().eq("idempotency_key", key).execute()

    try:
        outcome = await run_planner(
            svc,
            user_id=user.id,
            plan_date=on_date,
            trigger=payload.trigger,
            triggering_event_id=payload.triggering_event_id,
        )
    except Exception as exc:  # explicit surface for debugging M3
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    return RunPlannerResponse(
        inserted=outcome.inserted,
        plan_id=outcome.final_plan.id if outcome.final_plan else None,
        infeasible=outcome.infeasible,
        hitl_status=outcome.hitl_status,
        confidence=outcome.confidence,
        idempotency_key=outcome.idempotency_key,
    )


class OverrideRequest(BaseModel):
    note: Optional[str] = None


@router.post("/planner/override", response_model=RunPlannerResponse)
async def override_endpoint(payload: OverrideRequest, user: AuthedUser) -> RunPlannerResponse:
    """User explicitly rejects the current plan and forces a fresh one with
    trigger=override. Each override gets a fresh idempotency key so retries
    always regenerate. Note lands in agent_runs.triggering_event_id for audit."""
    import uuid as _uuid

    on_date = datetime.now(timezone.utc).date()
    svc = get_service_role_client()
    event_id = f"override:{_uuid.uuid4()}"
    outcome = await run_planner(
        svc,
        user_id=user.id,
        plan_date=on_date,
        trigger=Trigger.OVERRIDE,
        triggering_event_id=event_id,
    )
    return RunPlannerResponse(
        inserted=outcome.inserted,
        plan_id=outcome.final_plan.id if outcome.final_plan else None,
        infeasible=outcome.infeasible,
        hitl_status=outcome.hitl_status,
        confidence=outcome.confidence,
        idempotency_key=outcome.idempotency_key,
    )


class ChoiceGroupAlternative(BaseModel):
    """One of several dishes the user could have picked instead of what the
    plan selected. Same meal slot, same choice_group_id on menu_items."""

    dish_ref: str
    dish_name: str


class PlanEntryOut(BaseModel):
    meal: str
    source: str
    dish_ref: str
    dish_name: Optional[str] = None
    servings: float
    kcal: float
    protein_g: float
    carbs_g: float
    fats_g: float
    price_inr: float
    macro_verified: bool
    # Populated for mess entries whose menu_items row has a choice_group_id.
    # The Streamlit log surface shows these as a radio so the user can log
    # "I actually had Coffee, not Milk" without going through the free-form
    # 'Ate different' form. Learning Agent uses the delta to detect
    # preferences over time.
    alternatives: list[ChoiceGroupAlternative] = Field(default_factory=list)


class TodayPlanResponse(BaseModel):
    plan_id: int
    date: str
    status: str
    confidence: float
    target_kcal: float
    target_protein_g: float
    target_carbs_g: float
    target_fats_g: float
    total_kcal: float
    total_protein_g: float
    total_carbs_g: float
    total_fats_g: float
    total_cost_inr: float
    budget_soft_inr: Optional[float] = None
    budget_hard_inr: Optional[float] = None
    menu_stale: bool = False
    active_cycle_id: Optional[int] = None
    plan_cycle_id: Optional[int] = None
    plan_cycle_ingested_at: Optional[str] = None
    plan_cycle_source: Optional[str] = None
    entries: list[PlanEntryOut]


class HistoryPlan(BaseModel):
    plan_id: int
    date: str
    status: str
    confidence: float
    total_kcal: float
    total_protein_g: float
    total_cost_inr: float


@router.get("/plans/history", response_model=list[HistoryPlan])
def list_plan_history(user: AuthedUser, days: int = 7) -> list[HistoryPlan]:
    svc = get_service_role_client()
    from datetime import timedelta

    start = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    resp = (
        svc.table("daily_plans")
        .select("id, date, status, confidence, plan_json")
        .eq("user_id", user.id)
        .gte("date", start)
        .order("date", desc=True)
        .execute()
    )
    out: list[HistoryPlan] = []
    for row in resp.data or []:
        entries = row["plan_json"] or []
        tot_k = sum(float((e.get("macros") or {}).get("kcal", 0.0)) for e in entries)
        tot_p = sum(float((e.get("macros") or {}).get("protein_g", 0.0)) for e in entries)
        tot_c = sum(float(e.get("price_inr", 0.0) or 0.0) for e in entries)
        out.append(
            HistoryPlan(
                plan_id=row["id"],
                date=row["date"],
                status=row["status"],
                confidence=float(row["confidence"]),
                total_kcal=tot_k,
                total_protein_g=tot_p,
                total_cost_inr=tot_c,
            )
        )
    return out


@router.get("/plans/today", response_model=Optional[TodayPlanResponse])
def get_today_plan(user: AuthedUser) -> Optional[TodayPlanResponse]:
    svc = get_service_role_client()
    plan = fetch_today_plan(
        svc, user_id=user.id, plan_date=datetime.now(timezone.utc).date()
    )
    if plan is None:
        return None

    # Preload menu_items for the plan's cycle so we can enrich each mess
    # entry with (a) a nicer display name and (b) its choice-group siblings.
    # One round-trip beats one-per-entry.
    svc2 = get_service_role_client()
    menu_items_by_norm: dict[tuple[str, str], dict] = {}
    choice_group_members: dict[tuple[str, str], list[dict]] = {}
    if plan.cycle_id_used is not None:
        day_of_week = plan.date.weekday()
        mi_rows = (
            svc2.table("menu_items")
            .select("dish_name, meal, choice_group_id")
            .eq("cycle_id", plan.cycle_id_used)
            .eq("day_of_week", day_of_week)
            .execute()
            .data
            or []
        )
        # Local helper — mirrors normalize_dish_name without importing to
        # keep this router import graph tight.
        import re as _re
        import unicodedata as _u
        _NORM_RE = _re.compile(r"[^a-z0-9]+")

        def _norm(name: str) -> str:
            folded = _u.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
            return _NORM_RE.sub("_", folded.lower().strip()).strip("_")

        for row in mi_rows:
            key = (_norm(row["dish_name"]), row["meal"])
            menu_items_by_norm[key] = row
            gid = row.get("choice_group_id")
            if gid:
                choice_group_members.setdefault((gid, row["meal"]), []).append(row)

    entries: list[PlanEntryOut] = []
    total = {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fats_g": 0.0}
    total_cost = 0.0
    for e in plan.entries:
        entry_dict: Any = e if isinstance(e, dict) else e.model_dump()
        macros = entry_dict.get("macros", {})
        for k in total:
            total[k] += float(macros.get(k, 0.0))
        price = float(entry_dict.get("price_inr", 0.0) or 0.0)
        total_cost += price

        # For mess entries: look up alternatives from menu_items sharing the
        # same choice_group_id in the same meal slot. Canteen entries never
        # have alternatives (dish_ref uniquely identifies the shop item).
        dish_ref_val: str = entry_dict["dish_ref"]
        source_val = str(entry_dict.get("source", "mess"))
        alternatives: list[ChoiceGroupAlternative] = []
        display_name: Optional[str] = None
        if source_val == "mess" and menu_items_by_norm:
            mi_row = menu_items_by_norm.get((dish_ref_val, entry_dict["meal"]))
            if mi_row is not None:
                display_name = mi_row["dish_name"]
                gid = mi_row.get("choice_group_id")
                if gid:
                    for sibling in choice_group_members.get((gid, entry_dict["meal"]), []):
                        sib_norm = _norm(sibling["dish_name"])
                        if sib_norm == dish_ref_val:
                            continue   # skip the selected dish itself
                        alternatives.append(
                            ChoiceGroupAlternative(
                                dish_ref=sib_norm, dish_name=sibling["dish_name"]
                            )
                        )

        entries.append(
            PlanEntryOut(
                meal=entry_dict["meal"],
                source=source_val,
                dish_ref=dish_ref_val,
                dish_name=display_name,
                servings=float(entry_dict["servings"]),
                kcal=float(macros.get("kcal", 0.0)),
                protein_g=float(macros.get("protein_g", 0.0)),
                carbs_g=float(macros.get("carbs_g", 0.0)),
                fats_g=float(macros.get("fats_g", 0.0)),
                price_inr=price,
                macro_verified=bool(entry_dict.get("macro_verified", True)),
                alternatives=alternatives,
            )
        )
    target: Any = plan.target_macros if isinstance(plan.target_macros, dict) else plan.target_macros.model_dump()

    # Pull the user's stored budgets so the UI can show budget usage.
    # svc2 is already the service-role singleton created above for the
    # menu_items lookup.
    prof = (
        svc2.table("user_profile")
        .select("budget_soft_inr, budget_hard_inr")
        .eq("user_id", user.id)
        .single()
        .execute()
        .data
        or {}
    )

    # Detect a stale menu — the plan carries `cycle_id_used` from when it was
    # committed (0014 migration). Compare against today's currently-active
    # cycle. No name-based inference — that broke whenever the same dish
    # appeared in multiple cycles and made the "menu updated" banner stick.
    active_cycle_id = None
    plan_cycle_id = plan.cycle_id_used
    active_rows = (
        svc2.table("menu_cycles")
        .select("id")
        .eq("status", "active")
        .lte("effective_from", plan.date.isoformat())
        .gte("effective_to", plan.date.isoformat())
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    if active_rows:
        active_cycle_id = active_rows[0]["id"]
    menu_stale = bool(
        active_cycle_id and plan_cycle_id and active_cycle_id != plan_cycle_id
    )

    # Enrich response with the plan's source-cycle metadata so the UI can show
    # a persistent "based on menu uploaded at <ts>" line — gives the user a
    # trust signal that their plan reflects the current menu.
    plan_cycle_ingested_at: str | None = None
    plan_cycle_source: str | None = None
    if plan_cycle_id:
        cyc_meta = (
            svc2.table("menu_cycles")
            .select("ingested_at, source")
            .eq("id", plan_cycle_id)
            .single()
            .execute()
            .data
        )
        if cyc_meta:
            ingested_raw = cyc_meta.get("ingested_at")
            plan_cycle_ingested_at = str(ingested_raw) if ingested_raw is not None else None
            plan_cycle_source = cyc_meta.get("source")

    return TodayPlanResponse(
        plan_id=plan.id,   # type: ignore[arg-type]
        date=plan.date.isoformat(),
        status=plan.status.value if hasattr(plan.status, "value") else str(plan.status),
        confidence=plan.confidence,
        target_kcal=float(target["kcal"]),
        target_protein_g=float(target["protein_g"]),
        target_carbs_g=float(target["carbs_g"]),
        target_fats_g=float(target["fats_g"]),
        total_kcal=total["kcal"],
        total_protein_g=total["protein_g"],
        total_carbs_g=total["carbs_g"],
        total_fats_g=total["fats_g"],
        total_cost_inr=total_cost,
        budget_soft_inr=float(prof["budget_soft_inr"]) if prof.get("budget_soft_inr") is not None else None,
        budget_hard_inr=float(prof["budget_hard_inr"]) if prof.get("budget_hard_inr") is not None else None,
        menu_stale=menu_stale,
        active_cycle_id=active_cycle_id,
        plan_cycle_id=plan_cycle_id,
        plan_cycle_ingested_at=plan_cycle_ingested_at,
        plan_cycle_source=plan_cycle_source,
        entries=entries,
    )
