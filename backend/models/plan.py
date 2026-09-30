from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from backend.models.enums import HardViolationType, MealSlot, PlanStatus
from backend.models.nutrition import MacroDeviationSigned, Macros


class PlanMealEntry(BaseModel):
    """A single planned meal within a Plan or CandidatePlan."""

    meal: MealSlot
    source: str  # "mess" | "canteen" | "supplement"
    dish_ref: str  # normalized dish name or canteen dish id string
    macro_id: int | None = None
    servings: float = Field(gt=0.0)
    macros: Macros
    price_inr: float = Field(ge=0.0, default=0.0)
    macro_verified: bool = True


class CandidatePlan(BaseModel):
    """A plan proposal awaiting validation. Rejected candidates are still logged in traces."""

    candidate_id: str
    entries: list[PlanMealEntry]
    total_macros: Macros
    total_cost_inr: float = Field(ge=0.0)
    generated_by: str  # "llm_plan" | "fallback" | "degraded" | "hitl"


class HardViolation(BaseModel):
    type: HardViolationType
    detail: str
    entry_index: int | None = None


class ValidationResult(BaseModel):
    """§8. Sole authority on whether a candidate is admissible."""

    is_valid: bool
    hard_violations: list[HardViolation] = Field(default_factory=list)
    macro_deviation: MacroDeviationSigned
    budget_deviation: float
    preference_score: float = Field(ge=0.0, le=1.0)
    variety_score: float = Field(ge=0.0, le=1.0)
    practicality_score: float = Field(ge=0.0, le=1.0)
    macro_confidence_penalty: float = Field(ge=0.0, le=1.0)
    total_soft_score: float


class ConfidenceBreakdown(BaseModel):
    """§16. Deterministic Python computes this; LLM signal is advisory only."""

    overall: float = Field(ge=0.0, le=1.0)
    menu_freshness_factor: float = Field(ge=0.0, le=1.0)
    macro_verification_factor: float = Field(ge=0.0, le=1.0)
    validation_factor: float = Field(ge=0.0, le=1.0)
    candidate_agreement_factor: float = Field(ge=0.0, le=1.0)
    fallback_factor: float = Field(ge=0.0, le=1.0)
    tool_health_factor: float = Field(ge=0.0, le=1.0)
    llm_uncertainty_signal: float | None = None


class Plan(BaseModel):
    """The persisted plan. Lifecycle per §13."""

    id: int | None = None
    user_id: str
    date: date
    entries: list[PlanMealEntry]
    target_macros: Macros
    validation_result: ValidationResult
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_factors: ConfidenceBreakdown
    degraded_mode: bool = False
    status: PlanStatus = PlanStatus.DRAFT
    supersedes_id: int | None = None
    cycle_id_used: int | None = None
    created_at: datetime | None = None
    sent_at: datetime | None = None
    completed_at: datetime | None = None
    superseded_at: datetime | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
