"""Deterministic nutrition math (§10). Pure functions. No LLM. Unit-tested.

Every function here is authoritative — no LLM-generated number ever overrides these.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from backend.models.enums import ActivityLevel, Gender, Goal, UserMode
from backend.models.nutrition import Macros, MacrosSigned

# --- Deterministic constants -------------------------------------------------

MIN_SAFE_KCAL = 1200.0          # §20 S5 — hard floor for adult meal planning
MAX_SAFE_KCAL = 5000.0          # reject implausibly high targets too
KCAL_PER_G_PROTEIN = 4.0
KCAL_PER_G_CARB = 4.0
KCAL_PER_G_FAT = 9.0
FAT_KCAL_FRACTION = 0.25        # §10 — fats = 25% of kcal
WEIGHT_RECOMPUTE_THRESHOLD_KG = 1.0  # §10 — recompute weekly if Δweight > 1kg

ACTIVITY_MULTIPLIERS: dict[ActivityLevel, float] = {
    ActivityLevel.SEDENTARY: 1.2,
    ActivityLevel.LIGHT: 1.375,
    ActivityLevel.GYM_3_4: 1.55,
    ActivityLevel.GYM_5_6: 1.725,
    ActivityLevel.ATHLETE: 1.9,
}

# §10 spec lists TDEE calc + protein rules but doesn't specify the kcal
# adjustment per goal. Standard nutrition practice: cut ≈ TDEE − 20%,
# bulk ≈ TDEE + 10%, maintain = TDEE. Applied after TDEE so the deficit /
# surplus scales with body size and activity.
GOAL_KCAL_MULTIPLIERS: dict[Goal, float] = {
    Goal.CUT: 0.80,
    Goal.MAINTAIN: 1.00,
    Goal.BULK: 1.10,
}


class UnsafePlanningInputError(ValueError):
    """§20 S5 — deterministic guard rejects obviously unsafe planning inputs."""


# --- BMR / TDEE --------------------------------------------------------------


def bmr_mifflin_st_jeor(
    *, weight_kg: float, height_cm: float, age: int, gender: Gender
) -> float:
    """Mifflin-St Jeor BMR (§10)."""
    base = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age
    if gender is Gender.MALE:
        return base + 5.0
    if gender is Gender.FEMALE:
        return base - 161.0
    return base - 78.0  # midpoint of male/female offsets — deterministic


def tdee(bmr: float, activity_level: ActivityLevel) -> float:
    return bmr * ACTIVITY_MULTIPLIERS[activity_level]


# --- Protein target ----------------------------------------------------------


def protein_target_g(*, weight_kg: float, mode: UserMode, goal: Goal) -> float:
    """Protein target per goal — pragmatic mess-feasible values (2026-09-23
    tuning). Spec §10 originally listed 1.6/2.0/2.2 but bodybuilder-tier
    targets are unreachable from mess+canteen. Current defaults:
      general = 0.8 g/kg
      fitness/maintain = 1.3 g/kg
      fitness/bulk = 1.8 g/kg
      fitness/cut = 1.7 g/kg
    """
    if mode is UserMode.GENERAL:
        return 0.8 * weight_kg
    if goal is Goal.MAINTAIN:
        return 1.3 * weight_kg
    if goal is Goal.BULK:
        return 1.8 * weight_kg
    if goal is Goal.CUT:
        return 1.7 * weight_kg
    raise ValueError(f"unknown mode/goal combination: {mode}/{goal}")


# --- Macro targets -----------------------------------------------------------


def macro_targets(*, target_kcal: float, protein_g: float) -> Macros:
    """Given a target kcal and protein floor, derive fats (25% kcal) and carbs (remainder).

    Raises UnsafePlanningInputError on inputs that would produce an unsafe or infeasible plan.
    """
    if target_kcal < MIN_SAFE_KCAL:
        raise UnsafePlanningInputError(
            f"target_kcal {target_kcal:.0f} below safe floor {MIN_SAFE_KCAL:.0f}"
        )
    if target_kcal > MAX_SAFE_KCAL:
        raise UnsafePlanningInputError(
            f"target_kcal {target_kcal:.0f} above safe ceiling {MAX_SAFE_KCAL:.0f}"
        )
    if protein_g < 0:
        raise UnsafePlanningInputError("protein target cannot be negative")

    fats_g = (target_kcal * FAT_KCAL_FRACTION) / KCAL_PER_G_FAT
    protein_kcal = protein_g * KCAL_PER_G_PROTEIN
    fat_kcal = fats_g * KCAL_PER_G_FAT
    carbs_kcal = target_kcal - protein_kcal - fat_kcal
    if carbs_kcal < 0.0:
        raise UnsafePlanningInputError(
            "protein target alone exceeds available (kcal - fats). Lower protein or raise kcal."
        )
    carbs_g = carbs_kcal / KCAL_PER_G_CARB
    return Macros(kcal=target_kcal, protein_g=protein_g, carbs_g=carbs_g, fats_g=fats_g)


# --- Composite -----------------------------------------------------------------


@dataclass(frozen=True)
class ProfileTargets:
    bmr: float
    tdee: float
    targets: Macros


def compute_profile_targets(
    *,
    age: int,
    gender: Gender,
    height_cm: float,
    weight_kg: float,
    activity_level: ActivityLevel,
    goal: Goal,
    mode: UserMode,
) -> ProfileTargets:
    """BMR → TDEE → goal-adjusted target kcal → protein → macro_targets.
    Sole entry point for onboarding calibration."""
    bmr = bmr_mifflin_st_jeor(
        weight_kg=weight_kg, height_cm=height_cm, age=age, gender=gender
    )
    total_energy = tdee(bmr, activity_level)
    goal_adjusted_kcal = total_energy * GOAL_KCAL_MULTIPLIERS[goal]
    protein_g = protein_target_g(weight_kg=weight_kg, mode=mode, goal=goal)
    targets = macro_targets(target_kcal=goal_adjusted_kcal, protein_g=protein_g)
    return ProfileTargets(bmr=bmr, tdee=total_energy, targets=targets)


# --- Consumption arithmetic (over meal_logs) --------------------------------


def sum_macros(items: Iterable[Macros]) -> Macros:
    total = Macros.zero()
    for m in items:
        total = total + m
    return total


def macro_delta(*, target: Macros, consumed: Macros) -> MacrosSigned:
    """§10, N2 — SIGNED delta. Negative values indicate over-consumption."""
    return MacrosSigned(
        kcal=target.kcal - consumed.kcal,
        protein_g=target.protein_g - consumed.protein_g,
        carbs_g=target.carbs_g - consumed.carbs_g,
        fats_g=target.fats_g - consumed.fats_g,
    )


def planning_remaining(delta: MacrosSigned) -> Macros:
    """§5, N3 — used only for what-to-plan-next. Over-consumption is clamped away here."""
    return delta.clamp_nonneg()


def meal_total(macros_per_serving: Macros, servings: float) -> Macros:
    if servings < 0:
        raise ValueError("servings cannot be negative")
    return macros_per_serving.scaled(servings)


# --- Weekly recompute rule -----------------------------------------------------


def should_recompute_targets(*, prev_weight_kg: float, new_weight_kg: float) -> bool:
    """§10 — recompute weekly if weight changes > 1 kg."""
    return abs(new_weight_kg - prev_weight_kg) > WEIGHT_RECOMPUTE_THRESHOLD_KG
