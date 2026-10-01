from __future__ import annotations

from datetime import date
from typing import Any, TypedDict

from supabase import Client

from backend.config import Settings
from backend.models.canteen import CanteenOption
from backend.models.enums import HITLStatus, MealSlot, Trigger
from backend.models.menu import DailyMenu, MenuFreshness
from backend.models.nutrition import Macros, MacrosSigned
from backend.models.plan import CandidatePlan, ConfidenceBreakdown, Plan, ValidationResult
from backend.models.trace import TraceEvent
from backend.models.user import BehavioralFact, UserPreference, UserProfile


class PlannerState(TypedDict, total=False):
    """The LangGraph state contract (§5). TypedDict because LangGraph merges
    nodes' partial returns. Underscore-prefixed keys are runtime scratch —
    declared here so LangGraph's channel inference preserves them across
    nodes, NOT part of the §5 public contract."""

    # Identity
    user_id: str
    date: date
    trigger: Trigger
    triggering_event_id: str | None

    # Context
    profile: UserProfile
    behavioral_memory: list[BehavioralFact]
    todays_menu: DailyMenu | None
    menu_freshness: MenuFreshness | None
    veg_today: bool
    budget_remaining_inr: float

    # Nutrition state (deterministic)
    target_macros: Macros
    consumed_macros: Macros
    macro_delta: MacrosSigned
    planning_remaining_macros: Macros
    meals_completed: list[MealSlot]
    meals_remaining: list[MealSlot]

    # Planning workspace
    candidate_plans: list[CandidatePlan]
    validation_results: list[ValidationResult]
    revision_attempts: int
    candidates_generated: int
    fallback_invoked: bool
    fallback_results: list[CanteenOption]
    infeasible: bool

    # Decision surface
    selected_plan: Plan | None
    confidence: float
    confidence_factors: ConfidenceBreakdown | None
    hitl_status: HITLStatus
    hitl_request_id: str | None

    # Output
    final_plan: Plan | None
    reasoning_trace: list[TraceEvent]

    # Runtime scratch (not part of §5)
    _svc_client: Client
    _settings: Settings
    _preferences: list[UserPreference]
    _pushed_trace_count: int
    _route: str
    _last_llm: dict[str, Any]
    _tool_errors: int
    _tool_calls: int
    _recent_dishes: list[str]
