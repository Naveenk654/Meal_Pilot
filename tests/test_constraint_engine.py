"""Unit tests for backend/tools/constraint_engine.py. No DB, no LLM."""
from __future__ import annotations

from backend.models.enums import (
    HardViolationType,
    MealSlot,
    PreferenceAuthority,
    PreferenceKind,
    PreferenceSource,
    PreferenceStatus,
)
from backend.models.nutrition import Macros
from backend.models.plan import CandidatePlan, PlanMealEntry
from backend.models.user import UserPreference
from backend.tools.constraint_engine import (
    MenuDishRef,
    ValidationInputs,
    validate_candidate,
)


def _pref(kind: PreferenceKind, value: str, authority=PreferenceAuthority.HARD) -> UserPreference:
    return UserPreference(
        id=None,
        user_id="u",
        kind=kind,
        value=value,
        source=PreferenceSource.ONBOARDING,
        authority=authority,
        confidence=1.0,
        status=PreferenceStatus.ACTIVE,
    )


def _entry(meal: MealSlot, dish: str, kcal: float = 200, protein: float = 10, source: str = "mess", verified: bool = True) -> PlanMealEntry:
    return PlanMealEntry(
        meal=meal,
        source=source,
        dish_ref=dish,
        servings=1.0,
        macros=Macros(kcal=kcal, protein_g=protein, carbs_g=20, fats_g=6),
        price_inr=0.0,
        macro_verified=verified,
    )


def _candidate(*entries: PlanMealEntry) -> CandidatePlan:
    total = Macros.zero()
    for e in entries:
        total = total + e.macros
    return CandidatePlan(
        candidate_id="c",
        entries=list(entries),
        total_macros=total,
        total_cost_inr=sum(e.price_inr for e in entries),
        generated_by="llm_plan",
    )


def _menu(*items: tuple[str, MealSlot, bool]) -> list[MenuDishRef]:
    return [MenuDishRef(dish_normalized=n, meal=m, is_veg=v) for (n, m, v) in items]


def _inputs(**overrides) -> ValidationInputs:
    # Default tests target the classic hard constraints (allergy, restriction,
    # etc.), so we relax the macro-sanity bounds so tiny candidates don't
    # accidentally trip the new protein floor / kcal ceiling.
    defaults = dict(
        target_macros=Macros(kcal=2000, protein_g=100, carbs_g=250, fats_g=60),
        planning_remaining=Macros(kcal=2000, protein_g=100, carbs_g=250, fats_g=60),
        veg_today=False,
        hard_budget_inr=None,
        soft_budget_inr=200.0,
        preferences=[],
        todays_menu=_menu(
            ("aloo_paratha", MealSlot.BREAKFAST, True),
            ("dal_tadka", MealSlot.LUNCH, True),
            ("chicken_curry", MealSlot.DINNER, False),
        ),
        min_protein_ratio=0.0,
        max_kcal_ratio=10.0,
    )
    defaults.update(overrides)
    return ValidationInputs(**defaults)


def test_valid_plan_has_no_violations():
    cand = _candidate(
        _entry(MealSlot.BREAKFAST, "aloo_paratha"),
        _entry(MealSlot.LUNCH, "dal_tadka"),
    )
    result = validate_candidate(cand, _inputs())
    assert result.is_valid
    assert result.hard_violations == []
    assert 0.0 <= result.total_soft_score <= 1.0


def test_allergy_invalidates():
    cand = _candidate(_entry(MealSlot.BREAKFAST, "aloo_paratha"))
    prefs = [_pref(PreferenceKind.ALLERGY, "aloo")]
    result = validate_candidate(cand, _inputs(preferences=prefs))
    assert not result.is_valid
    assert any(v.type is HardViolationType.ALLERGY for v in result.hard_violations)


def test_soft_allergy_does_not_invalidate():
    """Only hard-authority allergies invalidate — but onboarding always
    marks allergies as HARD, so this is a defensive check."""
    cand = _candidate(_entry(MealSlot.BREAKFAST, "aloo_paratha"))
    prefs = [_pref(PreferenceKind.ALLERGY, "aloo", authority=PreferenceAuthority.SOFT)]
    result = validate_candidate(cand, _inputs(preferences=prefs))
    assert result.is_valid


def test_restriction_veg_day_rejects_nonveg_dish():
    cand = _candidate(_entry(MealSlot.DINNER, "chicken_curry"))
    result = validate_candidate(cand, _inputs(veg_today=True))
    assert not result.is_valid
    assert any(v.type is HardViolationType.RESTRICTION for v in result.hard_violations)


def test_dish_unavailable_when_not_in_menu():
    cand = _candidate(_entry(MealSlot.LUNCH, "mystery_dish"))
    result = validate_candidate(cand, _inputs())
    assert not result.is_valid
    assert any(v.type is HardViolationType.DISH_UNAVAILABLE for v in result.hard_violations)


def test_meal_slot_mismatch():
    cand = _candidate(_entry(MealSlot.DINNER, "aloo_paratha"))
    result = validate_candidate(cand, _inputs())
    assert not result.is_valid
    assert any(v.type is HardViolationType.MEAL_SLOT_MISMATCH for v in result.hard_violations)


def test_hard_budget_ceiling():
    entry = PlanMealEntry(
        meal=MealSlot.LUNCH,
        source="mess",
        dish_ref="dal_tadka",
        servings=1.0,
        macros=Macros(kcal=200, protein_g=10, carbs_g=20, fats_g=5),
        price_inr=250.0,
        macro_verified=True,
    )
    cand = _candidate(entry)
    result = validate_candidate(cand, _inputs(hard_budget_inr=200.0))
    assert not result.is_valid
    assert any(v.type is HardViolationType.BUDGET_HARD_CEILING for v in result.hard_violations)


def test_scoring_bounded_and_prefers_lower_macro_error():
    on_target = _candidate(
        _entry(MealSlot.BREAKFAST, "aloo_paratha", kcal=1000, protein=50),
        _entry(MealSlot.LUNCH, "dal_tadka", kcal=1000, protein=50),
    )
    off_target = _candidate(
        _entry(MealSlot.BREAKFAST, "aloo_paratha", kcal=300, protein=10),
    )
    r_on = validate_candidate(on_target, _inputs())
    r_off = validate_candidate(off_target, _inputs())
    assert r_on.total_soft_score > r_off.total_soft_score


def test_macro_confidence_penalty_reduces_score():
    plain = _candidate(_entry(MealSlot.BREAKFAST, "aloo_paratha", verified=True))
    unverified = _candidate(_entry(MealSlot.BREAKFAST, "aloo_paratha", verified=False))
    r_plain = validate_candidate(plain, _inputs())
    r_unver = validate_candidate(unverified, _inputs())
    assert r_plain.macro_confidence_penalty == 0.0
    assert r_unver.macro_confidence_penalty == 1.0
    assert r_plain.total_soft_score >= r_unver.total_soft_score


def test_practical_serving_cap_invalidates():
    """Stack 5 servings of dal_tadka when practical cap is 3 → INVALID."""
    entry = PlanMealEntry(
        meal=MealSlot.LUNCH,
        source="mess",
        dish_ref="dal_tadka",
        servings=5.0,
        macros=Macros(kcal=500, protein_g=25, carbs_g=60, fats_g=15),
        macro_verified=True,
    )
    result = validate_candidate(
        _candidate(entry),
        _inputs(practical_serving_caps={"dal_tadka": 3.0}),
    )
    assert not result.is_valid
    assert any(v.type is HardViolationType.PRACTICAL_SERVING_EXCEEDED for v in result.hard_violations)


def test_macro_kcal_ceiling_invalidates_overshoot():
    """Plan at 2500 kcal vs 2000 target = 1.25 ratio, above default 1.20 cap."""
    huge = _candidate(
        _entry(MealSlot.BREAKFAST, "aloo_paratha", kcal=1250, protein=50),
        _entry(MealSlot.LUNCH, "dal_tadka", kcal=1250, protein=50),
    )
    result = validate_candidate(huge, _inputs(max_kcal_ratio=1.20))
    assert not result.is_valid
    assert any(v.type is HardViolationType.MACRO_KCAL_CEILING for v in result.hard_violations)


def test_macro_protein_floor_invalidates_underscore():
    """Plan with 30g protein vs 100g target = 0.30 ratio, below 0.70 floor."""
    low_p = _candidate(
        _entry(MealSlot.BREAKFAST, "aloo_paratha", kcal=800, protein=15),
        _entry(MealSlot.LUNCH, "dal_tadka", kcal=800, protein=15),
    )
    result = validate_candidate(low_p, _inputs(min_protein_ratio=0.70))
    assert not result.is_valid
    assert any(v.type is HardViolationType.MACRO_PROTEIN_FLOOR for v in result.hard_violations)


def test_mid_day_replan_uses_planning_remaining_for_protein_floor():
    """Dinner-only candidate: full-day target 120g protein, 80g already eaten
    so planning_remaining=40g. Plan has 35g protein, min_protein_ratio=0.60.
    The floor must be 0.60 * 40 = 24g (not 72g), so the plan is VALID."""
    cand = _candidate(
        _entry(MealSlot.DINNER, "chicken_curry", kcal=600, protein=35, source="mess"),
    )
    inputs = _inputs(
        target_macros=Macros(kcal=2300, protein_g=120, carbs_g=250, fats_g=70),
        planning_remaining=Macros(kcal=750, protein_g=40, carbs_g=80, fats_g=25),
        min_protein_ratio=0.60,
        max_kcal_ratio=1.20,
        veg_today=False,
        todays_menu=_menu(("chicken_curry", MealSlot.DINNER, False)),
    )
    result = validate_candidate(cand, inputs)
    assert result.is_valid, (
        f"mid-day replan wrongly rejected: {[v.model_dump() for v in result.hard_violations]}"
    )


def test_preferences_move_soft_score():
    baseline = validate_candidate(
        _candidate(_entry(MealSlot.BREAKFAST, "aloo_paratha")),
        _inputs(),
    )
    liked = validate_candidate(
        _candidate(_entry(MealSlot.BREAKFAST, "aloo_paratha")),
        _inputs(preferences=[_pref(PreferenceKind.LIKE, "aloo", authority=PreferenceAuthority.SOFT)]),
    )
    disliked = validate_candidate(
        _candidate(_entry(MealSlot.BREAKFAST, "aloo_paratha")),
        _inputs(preferences=[_pref(PreferenceKind.DISLIKE, "aloo", authority=PreferenceAuthority.SOFT)]),
    )
    assert liked.preference_score > baseline.preference_score
    assert disliked.preference_score < baseline.preference_score
