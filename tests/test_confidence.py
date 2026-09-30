"""Unit tests for backend/tools/confidence.py. No DB, no LLM."""
from __future__ import annotations

from datetime import date, datetime, timezone

from backend.models.menu import MenuFreshness
from backend.models.nutrition import MacroDeviationSigned
from backend.models.plan import ValidationResult
from backend.tools.confidence import (
    candidate_agreement_factor,
    compute_confidence,
    menu_freshness_factor,
    tool_health_factor,
    validation_factor,
)


def _freshness(day: date) -> MenuFreshness:
    return MenuFreshness(
        source="pdf",
        ingested_at=datetime.now(timezone.utc),
        effective_from=day,
        effective_to=day,
        version=1,
        content_hash="abc",
    )


def _valid() -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        hard_violations=[],
        macro_deviation=MacroDeviationSigned(kcal=0, protein_g=0, carbs_g=0, fats_g=0),
        budget_deviation=0.0,
        preference_score=0.5,
        variety_score=1.0,
        practicality_score=1.0,
        macro_confidence_penalty=0.0,
        total_soft_score=0.8,
    )


def _invalid() -> ValidationResult:
    r = _valid()
    return r.model_copy(update={"is_valid": False})


def test_menu_freshness_factor_inside_and_outside_window():
    today = date(2026, 3, 5)
    assert menu_freshness_factor(_freshness(today), today) == 1.0
    assert menu_freshness_factor(_freshness(today), date(2026, 3, 6)) == 0.0
    assert menu_freshness_factor(None, today) == 0.0


def test_validation_factor_zeros_on_infeasible():
    assert validation_factor(_valid(), infeasible=True) == 0.0
    assert validation_factor(_valid(), infeasible=False) == 1.0
    assert validation_factor(_invalid(), infeasible=False) == 0.2


def test_candidate_agreement_ranges():
    # Single valid → 0.7
    assert candidate_agreement_factor([0.8], chosen_score=0.8) == 0.7
    # Multiple with same score → 1.0
    assert candidate_agreement_factor([0.8, 0.8, 0.8], chosen_score=0.8) == 1.0
    # Wide spread → close to 0
    assert candidate_agreement_factor([0.9, 0.2], chosen_score=0.9) < 0.5


def test_tool_health_factor():
    assert tool_health_factor(tool_errors=0, tool_calls=10) == 1.0
    assert tool_health_factor(tool_errors=5, tool_calls=10) == 0.5
    assert tool_health_factor(tool_errors=0, tool_calls=0) == 1.0


def test_compute_confidence_all_signals_positive():
    today = date(2026, 3, 5)
    breakdown = compute_confidence(
        freshness=_freshness(today),
        on_date=today,
        validation=_valid(),
        infeasible=False,
        all_valid_scores=[0.8, 0.79, 0.78],
        chosen_score=0.8,
        tool_calls=5,
        tool_errors=0,
    )
    assert breakdown.overall > 0.8
    assert breakdown.menu_freshness_factor == 1.0
    assert breakdown.validation_factor == 1.0


def test_compute_confidence_infeasible_capped_low():
    today = date(2026, 3, 5)
    breakdown = compute_confidence(
        freshness=_freshness(today),
        on_date=today,
        validation=_valid(),
        infeasible=True,
        all_valid_scores=[],
        chosen_score=0.0,
    )
    assert breakdown.overall <= 0.2
    assert breakdown.validation_factor == 0.0


def test_llm_signal_folded_in_when_provided():
    today = date(2026, 3, 5)
    without = compute_confidence(
        freshness=_freshness(today),
        on_date=today,
        validation=_valid(),
        infeasible=False,
        all_valid_scores=[0.8, 0.8],
        chosen_score=0.8,
    )
    with_low_llm = compute_confidence(
        freshness=_freshness(today),
        on_date=today,
        validation=_valid(),
        infeasible=False,
        all_valid_scores=[0.8, 0.8],
        chosen_score=0.8,
        llm_uncertainty_signal=0.1,
    )
    assert with_low_llm.overall <= without.overall
