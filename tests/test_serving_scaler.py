"""Serving scaler: deterministic post-LLM kcal enforcement."""
from __future__ import annotations

from backend.models.enums import MealSlot
from backend.models.nutrition import Macros
from backend.models.plan import CandidatePlan, PlanMealEntry
from backend.tools.serving_scaler import (
    bump_candidate_to_protein_floor,
    bump_candidates_to_protein_floor,
    scale_candidate_to_kcal,
    scale_candidates_to_kcal,
)


def _entry(dish: str, meal: MealSlot, servings: float, kcal: float, protein: float) -> PlanMealEntry:
    return PlanMealEntry(
        meal=meal,
        source="mess",
        dish_ref=dish,
        macro_id=None,
        servings=servings,
        macros=Macros(kcal=kcal, protein_g=protein, carbs_g=kcal / 4 * 0.5, fats_g=kcal / 9 * 0.2),
        price_inr=0.0,
        macro_verified=True,
    )


def _plan(entries: list[PlanMealEntry]) -> CandidatePlan:
    total = Macros.zero()
    for e in entries:
        total = total + e.macros
    return CandidatePlan(
        candidate_id="c1",
        entries=entries,
        total_macros=total,
        total_cost_inr=0.0,
        generated_by="llm_plan",
    )


def test_no_op_when_plan_within_ceiling():
    """Plan at exactly target → left alone. No scaling artifacts."""
    p = _plan([_entry("dal_tadka", MealSlot.LUNCH, 2.0, 2600, 60)])
    target = Macros(kcal=2675, protein_g=76, carbs_g=400, fats_g=70)
    out, scaled = scale_candidate_to_kcal(p, target)
    assert scaled is False
    assert out.total_macros.kcal == 2600
    assert out.entries[0].servings == 2.0


def test_no_op_when_slightly_over_but_under_intervention_threshold():
    """108% of target — inside the 110% intervention floor, don't touch it."""
    p = _plan([_entry("dal_tadka", MealSlot.LUNCH, 2.0, 2889, 60)])  # 108%
    target = Macros(kcal=2675, protein_g=76, carbs_g=400, fats_g=70)
    out, scaled = scale_candidate_to_kcal(p, target)
    assert scaled is False
    assert out.total_macros.kcal == 2889


def test_scales_down_a_4280_kcal_plan_to_under_ceiling():
    """Reproduces the user's actual failure: 4280 kcal candidate for a
    2675 kcal cut target. Post-scale must be <=120% of target (the ceiling)
    so the Constraint Engine will accept it."""
    entries = [
        _entry("chapati", MealSlot.LUNCH, 3.0, 330, 10),
        _entry("shahi_paneer", MealSlot.LUNCH, 2.0, 720, 28),
        _entry("dal_moong_tadka", MealSlot.DINNER, 3.0, 540, 33),
        _entry("chapati", MealSlot.DINNER, 3.0, 330, 10),
        _entry("dal_fry", MealSlot.LUNCH, 3.0, 540, 33),
        _entry("dry_chhole", MealSlot.LUNCH, 2.0, 520, 24),
        _entry("sprouts", MealSlot.BREAKFAST, 2.0, 260, 18),
        _entry("boiled_egg", MealSlot.BREAKFAST, 4.0, 312, 25),  # (dropped: not veg in reality, but shape only)
        _entry("tea", MealSlot.SNACK, 1.0, 60, 2),
    ]
    p = _plan(entries)
    assert p.total_macros.kcal > 3500, "test setup: pre-scale should be well over target"
    target = Macros(kcal=2675, protein_g=76, carbs_g=400, fats_g=70)

    out, scaled = scale_candidate_to_kcal(p, target)
    assert scaled is True
    # Must land inside the 120% ceiling that the Constraint Engine enforces.
    assert out.total_macros.kcal <= target.kcal * 1.20, (
        f"post-scale {out.total_macros.kcal} still exceeds 120% ceiling "
        f"({target.kcal * 1.20})"
    )
    # Same set of entries preserved — scaler shrinks, doesn't drop.
    assert len(out.entries) == len(entries)
    # Every entry got smaller (or stayed same at min-serving floor).
    for original, scaled_entry in zip(entries, out.entries):
        assert scaled_entry.servings <= original.servings


def test_min_serving_floor_of_half():
    """Entry that would scale to 0.1 servings gets clamped to 0.5 — a
    1/10 chapati isn't a real thing on the user's plate."""
    p = _plan([
        _entry("chapati", MealSlot.LUNCH, 1.0, 3000, 5),  # tiny per-serving protein, huge kcal
    ])
    target = Macros(kcal=200, protein_g=50, carbs_g=30, fats_g=10)
    out, _scaled = scale_candidate_to_kcal(p, target)
    assert out.entries[0].servings == 0.5


def test_macros_scale_proportionally_with_servings():
    """Macros must track servings exactly — no drift, no double-counting."""
    p = _plan([_entry("paneer", MealSlot.DINNER, 2.0, 720, 28)])
    target = Macros(kcal=360, protein_g=14, carbs_g=20, fats_g=15)  # exactly 50% of the plan
    out, scaled = scale_candidate_to_kcal(p, target)
    assert scaled is True
    e = out.entries[0]
    ratio = e.servings / 2.0
    assert abs(e.macros.kcal - 720 * ratio) < 1e-6
    assert abs(e.macros.protein_g - 28 * ratio) < 1e-6
    # Total macros must equal the sum of entries — no independent drift.
    assert abs(out.total_macros.kcal - e.macros.kcal) < 1e-6


# --- Protein-floor bumper ---------------------------------------------------


def _entry_ps(dish: str, meal: MealSlot, servings: float, kcal_per: float, prot_per: float) -> PlanMealEntry:
    """Build an entry from per-serving macros × servings (so the ratio math is exact)."""
    return PlanMealEntry(
        meal=meal,
        source="mess",
        dish_ref=dish,
        macro_id=None,
        servings=servings,
        macros=Macros(kcal=kcal_per * servings, protein_g=prot_per * servings, carbs_g=0, fats_g=0),
        price_inr=0.0,
        macro_verified=True,
    )


def test_bumper_no_op_when_protein_above_floor():
    p = _plan([_entry_ps("dal", MealSlot.LUNCH, 2.0, 180, 12)])   # 24g P
    target = Macros(kcal=2000, protein_g=25, carbs_g=200, fats_g=50)  # floor = 22.5g, already above
    out, bumped = bump_candidate_to_protein_floor(p, target, {"dal": 5.0})
    assert bumped is False
    assert out.entries[0].servings == 2.0


def test_bumper_raises_protein_by_bumping_densest_entry():
    """Under-protein plan with kcal headroom → bump the highest protein/kcal dish."""
    entries = [
        _entry_ps("dal", MealSlot.LUNCH, 1.0, 180, 12),          # density 0.067
        _entry_ps("chapati", MealSlot.LUNCH, 3.0, 110, 3),       # density 0.027 (worse)
        _entry_ps("egg", MealSlot.BREAKFAST, 2.0, 78, 6.3),      # density 0.081 (best)
    ]
    p = _plan(entries)
    # Plan totals: 180 + 330 + 156 = 666 kcal, 12+9+12.6 = 33.6g P
    target = Macros(kcal=2000, protein_g=76, carbs_g=200, fats_g=50)  # floor = 68.4g
    out, bumped = bump_candidate_to_protein_floor(
        p, target, {"dal": 5.0, "chapati": 6.0, "egg": 6.0}
    )
    assert bumped is True
    # Highest-density entry (egg) grew.
    egg_before = 2.0
    egg_after = out.entries[2].servings
    assert egg_after > egg_before, f"expected egg to grow, got {egg_after}"
    # Kcal must remain under the bumper's own 115% ceiling.
    assert out.total_macros.kcal <= target.kcal * 1.15 + 1e-6


def test_bumper_respects_practical_cap():
    """Never bump a dish past its per-day practical_max."""
    entries = [_entry_ps("egg", MealSlot.BREAKFAST, 3.0, 78, 6.3)]
    p = _plan(entries)   # 234 kcal, 18.9g P
    target = Macros(kcal=1000, protein_g=100, carbs_g=100, fats_g=30)   # huge floor of 90g
    caps = {"egg": 4.0}  # only 1 more serving of egg allowed
    out, bumped = bump_candidate_to_protein_floor(p, target, caps)
    assert bumped is True
    assert out.entries[0].servings <= 4.0
    # Bumper can't reach the huge floor — that's fine, it does what it can and
    # leaves the residual to the Constraint Engine's soft score.
    assert out.total_macros.protein_g < target.protein_g * 0.90


def test_bumper_respects_kcal_ceiling():
    """No bumping if we'd blow the kcal ceiling doing it."""
    # Plan is already at 110% of target kcal but under protein floor. Bumper
    # should either bump minimally (using the ≤115% headroom) or bail.
    entries = [_entry_ps("dal", MealSlot.LUNCH, 5.0, 180, 6)]  # 900 kcal, 30g P
    p = _plan(entries)
    target = Macros(kcal=820, protein_g=50, carbs_g=200, fats_g=30)  # floor 45, ceiling 943
    caps = {"dal": 10.0}
    out, bumped = bump_candidate_to_protein_floor(p, target, caps)
    # Room = 943 - 900 = 43 kcal / 180 per serving = 0.24 serving → below 0.5 quantum
    # so bumper should bail (nothing to do).
    assert bumped is False
    assert out.entries[0].servings == 5.0


def test_bumper_skips_zero_protein_entries():
    """Bumping tea 5x won't help anyone. Ranker filters zero-protein entries."""
    entries = [
        _entry_ps("tea", MealSlot.SNACK, 1.0, 60, 0),
        _entry_ps("dal", MealSlot.LUNCH, 1.0, 180, 12),
    ]
    p = _plan(entries)
    target = Macros(kcal=2000, protein_g=50, carbs_g=200, fats_g=30)   # floor 45
    caps = {"tea": 5.0, "dal": 5.0}
    out, bumped = bump_candidate_to_protein_floor(p, target, caps)
    assert bumped is True
    assert out.entries[0].servings == 1.0   # tea untouched
    assert out.entries[1].servings > 1.0    # dal grew


def test_batch_reports_bumped_count():
    """Batch entry point counts only plans that actually got bumped."""
    under = _plan([_entry_ps("egg", MealSlot.BREAKFAST, 1.0, 78, 6.3)])   # 6.3g P
    fine = _plan([_entry_ps("dal", MealSlot.LUNCH, 5.0, 180, 12)])         # 60g P (above 45 floor)
    target = Macros(kcal=2000, protein_g=50, carbs_g=200, fats_g=30)
    caps = {"egg": 6.0, "dal": 6.0}
    out, count = bump_candidates_to_protein_floor([under, fine, under], target, caps)
    assert count == 2
    assert len(out) == 3


def test_batch_reports_scaled_count():
    """Batch entry point: counts only the plans that actually got shrunk."""
    over = _plan([_entry("d", MealSlot.LUNCH, 3.0, 4000, 40)])
    under = _plan([_entry("d", MealSlot.LUNCH, 1.0, 500, 20)])
    target = Macros(kcal=2000, protein_g=76, carbs_g=200, fats_g=50)
    out, count = scale_candidates_to_kcal([over, under, over], target)
    assert count == 2
    assert len(out) == 3
    # The under plan passes through unchanged (same object semantics).
    assert out[1].total_macros.kcal == 500
