import math

import pytest

from backend.models.enums import ActivityLevel, Gender, Goal, UserMode
from backend.models.nutrition import Macros, MacrosSigned
from backend.tools.nutrition import (
    ACTIVITY_MULTIPLIERS,
    FAT_KCAL_FRACTION,
    KCAL_PER_G_CARB,
    KCAL_PER_G_FAT,
    KCAL_PER_G_PROTEIN,
    MAX_SAFE_KCAL,
    MIN_SAFE_KCAL,
    UnsafePlanningInputError,
    bmr_mifflin_st_jeor,
    compute_profile_targets,
    macro_delta,
    macro_targets,
    meal_total,
    planning_remaining,
    protein_target_g,
    should_recompute_targets,
    sum_macros,
    tdee,
)


# --- BMR ---------------------------------------------------------------------


def test_bmr_male_reference():
    # 70kg, 175cm, 25y male — Mifflin-St Jeor reference
    # 10*70 + 6.25*175 - 5*25 + 5 = 700 + 1093.75 - 125 + 5 = 1673.75
    assert bmr_mifflin_st_jeor(
        weight_kg=70, height_cm=175, age=25, gender=Gender.MALE
    ) == pytest.approx(1673.75)


def test_bmr_female_reference():
    # 60kg, 165cm, 30y female: 10*60 + 6.25*165 - 5*30 - 161 = 600+1031.25-150-161 = 1320.25
    assert bmr_mifflin_st_jeor(
        weight_kg=60, height_cm=165, age=30, gender=Gender.FEMALE
    ) == pytest.approx(1320.25)


def test_bmr_other_between_male_and_female():
    male = bmr_mifflin_st_jeor(weight_kg=70, height_cm=175, age=25, gender=Gender.MALE)
    female = bmr_mifflin_st_jeor(
        weight_kg=70, height_cm=175, age=25, gender=Gender.FEMALE
    )
    other = bmr_mifflin_st_jeor(
        weight_kg=70, height_cm=175, age=25, gender=Gender.OTHER
    )
    assert female < other < male


# --- TDEE --------------------------------------------------------------------


def test_tdee_uses_locked_multipliers():
    assert ACTIVITY_MULTIPLIERS[ActivityLevel.SEDENTARY] == 1.2
    assert ACTIVITY_MULTIPLIERS[ActivityLevel.LIGHT] == 1.375
    assert ACTIVITY_MULTIPLIERS[ActivityLevel.GYM_3_4] == 1.55
    assert ACTIVITY_MULTIPLIERS[ActivityLevel.GYM_5_6] == 1.725
    assert ACTIVITY_MULTIPLIERS[ActivityLevel.ATHLETE] == 1.9


def test_tdee_multiplies_bmr():
    assert tdee(1600, ActivityLevel.GYM_3_4) == pytest.approx(1600 * 1.55)


# --- Protein -----------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,goal,expected_multiplier",
    [
        (UserMode.GENERAL, Goal.MAINTAIN, 0.8),
        (UserMode.GENERAL, Goal.CUT, 0.8),  # general ignores goal
        (UserMode.GENERAL, Goal.BULK, 0.8),
        (UserMode.FITNESS, Goal.MAINTAIN, 1.3),
        (UserMode.FITNESS, Goal.BULK, 1.8),
        (UserMode.FITNESS, Goal.CUT, 1.7),
    ],
)
def test_protein_target_rules(mode, goal, expected_multiplier):
    assert protein_target_g(weight_kg=70, mode=mode, goal=goal) == pytest.approx(
        70 * expected_multiplier
    )


# --- Macro targets -----------------------------------------------------------


def test_macro_targets_fats_are_25pct_of_kcal():
    t = macro_targets(target_kcal=2000, protein_g=140)
    assert t.fats_g * KCAL_PER_G_FAT == pytest.approx(2000 * FAT_KCAL_FRACTION)


def test_macro_targets_sum_matches_kcal():
    t = macro_targets(target_kcal=2000, protein_g=140)
    reconstructed = (
        t.protein_g * KCAL_PER_G_PROTEIN
        + t.carbs_g * KCAL_PER_G_CARB
        + t.fats_g * KCAL_PER_G_FAT
    )
    assert reconstructed == pytest.approx(2000, abs=0.5)


def test_macro_targets_rejects_below_safe_floor():
    with pytest.raises(UnsafePlanningInputError):
        macro_targets(target_kcal=MIN_SAFE_KCAL - 1, protein_g=100)


def test_macro_targets_rejects_above_safe_ceiling():
    with pytest.raises(UnsafePlanningInputError):
        macro_targets(target_kcal=MAX_SAFE_KCAL + 1, protein_g=100)


def test_macro_targets_rejects_when_protein_exceeds_non_fat_budget():
    # 2000 kcal * 0.75 non-fat kcal = 1500 → 375g protein alone would exhaust it
    with pytest.raises(UnsafePlanningInputError):
        macro_targets(target_kcal=2000, protein_g=400)


def test_macro_targets_rejects_negative_protein():
    with pytest.raises(UnsafePlanningInputError):
        macro_targets(target_kcal=2000, protein_g=-1)


# --- Composite compute_profile_targets --------------------------------------


def test_compute_profile_targets_end_to_end():
    pt = compute_profile_targets(
        age=22,
        gender=Gender.MALE,
        height_cm=178,
        weight_kg=72,
        activity_level=ActivityLevel.GYM_3_4,
        goal=Goal.BULK,
        mode=UserMode.FITNESS,
    )
    expected_bmr = 10 * 72 + 6.25 * 178 - 5 * 22 + 5
    assert pt.bmr == pytest.approx(expected_bmr)
    assert pt.tdee == pytest.approx(expected_bmr * 1.55)
    assert pt.targets.protein_g == pytest.approx(72 * 1.8)
    # BULK adds 10% surplus over TDEE.
    assert pt.targets.kcal == pytest.approx(pt.tdee * 1.10)


def test_goal_multiplier_applies_deficit_for_cut():
    from backend.tools.nutrition import GOAL_KCAL_MULTIPLIERS
    assert GOAL_KCAL_MULTIPLIERS[Goal.CUT] < 1.0
    assert GOAL_KCAL_MULTIPLIERS[Goal.MAINTAIN] == 1.0
    assert GOAL_KCAL_MULTIPLIERS[Goal.BULK] > 1.0

    cut = compute_profile_targets(
        age=22, gender=Gender.MALE, height_cm=175, weight_kg=95,
        activity_level=ActivityLevel.GYM_3_4, goal=Goal.CUT, mode=UserMode.FITNESS,
    )
    maintain = compute_profile_targets(
        age=22, gender=Gender.MALE, height_cm=175, weight_kg=95,
        activity_level=ActivityLevel.GYM_3_4, goal=Goal.MAINTAIN, mode=UserMode.FITNESS,
    )
    assert cut.targets.kcal < maintain.targets.kcal
    assert cut.targets.kcal == pytest.approx(cut.tdee * 0.80)


# --- Macros arithmetic ------------------------------------------------------


def test_macros_addition_and_scaling():
    a = Macros(kcal=100, protein_g=10, carbs_g=15, fats_g=3)
    b = Macros(kcal=50, protein_g=5, carbs_g=7, fats_g=2)
    total = a + b
    assert total.kcal == 150 and total.protein_g == 15
    doubled = a.scaled(2)
    assert doubled.kcal == 200 and doubled.fats_g == 6


def test_sum_macros_over_list():
    items = [
        Macros(kcal=100, protein_g=10, carbs_g=15, fats_g=3),
        Macros(kcal=200, protein_g=20, carbs_g=25, fats_g=5),
    ]
    total = sum_macros(items)
    assert total.kcal == 300
    assert total.protein_g == 30


def test_sum_macros_empty_returns_zero():
    total = sum_macros([])
    assert total == Macros.zero()


# --- Signed delta + planning_remaining --------------------------------------


def test_macro_delta_positive_when_under_consumed():
    target = Macros(kcal=2000, protein_g=140, carbs_g=250, fats_g=55)
    consumed = Macros(kcal=1200, protein_g=80, carbs_g=150, fats_g=30)
    d = macro_delta(target=target, consumed=consumed)
    assert d.kcal == 800 and d.protein_g == 60


def test_macro_delta_negative_when_over_consumed():
    target = Macros(kcal=1500, protein_g=100, carbs_g=180, fats_g=40)
    consumed = Macros(kcal=1800, protein_g=90, carbs_g=220, fats_g=50)
    d = macro_delta(target=target, consumed=consumed)
    assert d.kcal == -300
    assert d.protein_g == 10  # still under on protein


def test_planning_remaining_clamps_negatives_to_zero():
    d = MacrosSigned(kcal=-200, protein_g=50, carbs_g=-10, fats_g=5)
    r = planning_remaining(d)
    assert r.kcal == 0.0
    assert r.protein_g == 50
    assert r.carbs_g == 0.0
    assert r.fats_g == 5


# --- Meal totals ------------------------------------------------------------


def test_meal_total_scales_per_serving_macros():
    per_serving = Macros(kcal=250, protein_g=8, carbs_g=45, fats_g=5)
    total = meal_total(per_serving, 2.5)
    assert total.kcal == pytest.approx(625)
    assert total.protein_g == pytest.approx(20)


def test_meal_total_rejects_negative_servings():
    per_serving = Macros(kcal=100, protein_g=5, carbs_g=15, fats_g=2)
    with pytest.raises(ValueError):
        meal_total(per_serving, -1)


# --- Weekly recompute rule --------------------------------------------------


@pytest.mark.parametrize(
    "prev,new,expected",
    [
        (70.0, 71.0, False),   # exactly 1kg — not > 1
        (70.0, 71.5, True),
        (70.0, 68.5, True),    # loss also triggers
        (70.0, 70.5, False),
    ],
)
def test_should_recompute_targets(prev, new, expected):
    assert should_recompute_targets(prev_weight_kg=prev, new_weight_kg=new) is expected


# --- Zero macros identity ---------------------------------------------------


def test_zero_macros_is_additive_identity():
    m = Macros(kcal=500, protein_g=30, carbs_g=60, fats_g=15)
    assert (m + Macros.zero()).kcal == m.kcal
    assert (Macros.zero() + m).protein_g == m.protein_g


# --- No silent NaN / inf ----------------------------------------------------


def test_no_nan_in_normal_flow():
    pt = compute_profile_targets(
        age=25,
        gender=Gender.FEMALE,
        height_cm=160,
        weight_kg=55,
        activity_level=ActivityLevel.LIGHT,
        goal=Goal.MAINTAIN,
        mode=UserMode.GENERAL,
    )
    for v in (pt.bmr, pt.tdee, pt.targets.kcal, pt.targets.protein_g, pt.targets.carbs_g, pt.targets.fats_g):
        assert not math.isnan(v) and not math.isinf(v)
