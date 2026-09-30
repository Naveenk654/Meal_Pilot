"""Deterministic confidence calculator (§16).

The LLM may report an uncertainty signal — it feeds in as an advisory factor
only. `overall` is a weighted product-average of the deterministic factors,
which makes any single 0-factor dominant (e.g. infeasibility zeros out
validation_factor and pulls overall towards zero).

Inputs come from the Planner's runtime state — every factor is a pure
function of information already in PlannerState + the validation results.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from backend.models.menu import MenuFreshness
from backend.models.nutrition import Macros
from backend.models.plan import ConfidenceBreakdown, ValidationResult


@dataclass(frozen=True)
class ConfidenceWeights:
    """Factor weights. Plan quality (how close the total macros land to target)
    dominates so a huge overshoot can't sneak past the HITL gate."""

    menu_freshness: float = 0.15
    macro_verification: float = 0.10
    validation: float = 0.20
    plan_quality: float = 0.25       # NEW — reflects total_soft_score of the winner
    candidate_agreement: float = 0.10
    fallback: float = 0.10
    tool_health: float = 0.05
    llm_signal: float = 0.05

    def total(self) -> float:
        return (
            self.menu_freshness
            + self.macro_verification
            + self.validation
            + self.plan_quality
            + self.candidate_agreement
            + self.fallback
            + self.tool_health
            + self.llm_signal
        )


DEFAULT_WEIGHTS = ConfidenceWeights()


# --- Per-factor calculators -------------------------------------------------


def menu_freshness_factor(freshness: MenuFreshness | None, on_date: date) -> float:
    """1.0 when today is inside the active cycle window; 0 when no cycle covers
    today (i.e. menu is uncertain)."""
    if freshness is None:
        return 0.0
    if freshness.effective_from <= on_date <= freshness.effective_to:
        return 1.0
    return 0.0


def macro_verification_factor(validation: ValidationResult) -> float:
    """1.0 when the plan uses only verified macros; 0 when everything is LLM-estimated.

    We reuse the constraint engine's `macro_confidence_penalty` (unverified
    kcal fraction) since it already answers this question."""
    return max(0.0, 1.0 - validation.macro_confidence_penalty)


def validation_factor(validation: ValidationResult, *, infeasible: bool) -> float:
    """Infeasibility zeros mess-only confidence (§16). An invalid but non-
    infeasible outcome is unusual (we shouldn't select invalid plans) — treat
    it as 0.2 to keep the pipeline honest if it slips through."""
    if infeasible:
        return 0.0
    if not validation.is_valid:
        return 0.2
    return 1.0


def candidate_agreement_factor(
    all_valid_scores: list[float], *, chosen_score: float
) -> float:
    """How close were the top valid candidates to the winner?

    High agreement (small spread near chosen_score) → high confidence. When
    only one valid candidate exists, we return 0.7 — reasonable but no
    corroboration."""
    if not all_valid_scores:
        return 0.0
    if len(all_valid_scores) == 1:
        return 0.7
    top = sorted(all_valid_scores, reverse=True)[: min(3, len(all_valid_scores))]
    if top[0] <= 0:
        return 0.5
    spread = (top[0] - top[-1]) / top[0]
    # spread=0 → agreement=1; spread>=0.5 → agreement=0
    return max(0.0, min(1.0, 1.0 - 2.0 * spread))


def fallback_factor(*, fallback_invoked: bool, soft_budget_overshoot_ratio: float) -> float:
    """1.0 when we didn't need to fall back; drops when fallback was used AND
    the mess-only plan blew the soft budget. Mess-only M3 always returns 1.0."""
    if not fallback_invoked:
        return 1.0
    return max(0.0, min(1.0, 1.0 - soft_budget_overshoot_ratio))


def tool_health_factor(*, tool_errors: int, tool_calls: int) -> float:
    """Ratio of successful tool calls in this run. No calls → 1.0 by default
    (nothing has proven the pipeline degraded)."""
    if tool_calls <= 0:
        return 1.0
    return max(0.0, min(1.0, 1.0 - tool_errors / tool_calls))


# --- Composite --------------------------------------------------------------


def compute_confidence(
    *,
    freshness: MenuFreshness | None,
    on_date: date,
    validation: ValidationResult,
    infeasible: bool,
    all_valid_scores: list[float],
    chosen_score: float,
    fallback_invoked: bool = False,
    soft_budget_overshoot_ratio: float = 0.0,
    tool_errors: int = 0,
    tool_calls: int = 0,
    llm_uncertainty_signal: float | None = None,
    weights: ConfidenceWeights = DEFAULT_WEIGHTS,
) -> ConfidenceBreakdown:
    """§16 — the single entrypoint. Returns the full breakdown.

    `chosen_score` (0..1, the winner's total_soft_score) doubles as the
    plan_quality factor — a plan that overshoots target by 40% shouldn't
    claim 0.99 confidence just because it validated.
    """
    menu = menu_freshness_factor(freshness, on_date)
    macro = macro_verification_factor(validation)
    valid = validation_factor(validation, infeasible=infeasible)
    agree = candidate_agreement_factor(all_valid_scores, chosen_score=chosen_score)
    quality = _clamp01(chosen_score)
    fb = fallback_factor(
        fallback_invoked=fallback_invoked,
        soft_budget_overshoot_ratio=soft_budget_overshoot_ratio,
    )
    tools = tool_health_factor(tool_errors=tool_errors, tool_calls=tool_calls)
    llm = _clamp01(llm_uncertainty_signal) if llm_uncertainty_signal is not None else None

    numerator = (
        weights.menu_freshness * menu
        + weights.macro_verification * macro
        + weights.validation * valid
        + weights.plan_quality * quality
        + weights.candidate_agreement * agree
        + weights.fallback * fb
        + weights.tool_health * tools
    )
    denominator = weights.total() - weights.llm_signal
    if llm is not None:
        numerator += weights.llm_signal * llm
        denominator = weights.total()
    overall = numerator / max(denominator, 1e-6)
    # An infeasible run should never land above 0.2 no matter how well the other
    # factors score — protects the confidence gate from a stale valid plan.
    if infeasible:
        overall = min(overall, 0.2)
    return ConfidenceBreakdown(
        overall=_clamp01(overall),
        menu_freshness_factor=menu,
        macro_verification_factor=macro,
        validation_factor=valid,
        candidate_agreement_factor=agree,
        fallback_factor=fb,
        tool_health_factor=tools,
        llm_uncertainty_signal=llm,
    )


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))
