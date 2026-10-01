"""Constraint & Validation Engine (§8).

DETERMINISTIC PYTHON. No LLM. Sole authority on plan validity.

Public API:
  * `ValidationInputs` — everything the engine needs to score one candidate.
  * `validate_candidate(candidate, inputs) -> ValidationResult`

Hard constraints (any violation → INVALID; not scored):
  - allergy match against dish ingredients (via name substring — v1 heuristic)
  - dietary restriction (veg-day + non-veg dish; egg restriction; etc.)
  - dish not in today's menu for the meal slot it's being scheduled at
  - hard budget ceiling exceeded
  - meal slot mismatch (e.g. dinner-only dish scheduled at breakfast)
  - practical_max_servings_per_day exceeded across the day for one dish

Soft objectives (scored, never invalidate):
  - macro deviation from target (signed, penalized both directions)
  - budget deviation (soft budget)
  - preference match (likes/dislikes)
  - variety vs recent days (fed in as `recent_dishes`)
  - practicality (fewer total servings-of-same-dish across day = higher score)
  - macro_confidence_penalty when candidate leans on `verified=false` macros

The weighted `total_soft_score` selects between VALID candidates. Weights are
tuneable but the defaults reflect §8 emphasis: macro accuracy dominates,
preference/variety second, cost/practicality tertiary.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.models.enums import (
    HardViolationType,
    MealSlot,
    PreferenceAuthority,
    PreferenceKind,
    PreferenceStatus,
)
from backend.models.nutrition import MacroDeviationSigned, Macros, MacrosSigned
from backend.models.plan import (
    CandidatePlan,
    HardViolation,
    PlanMealEntry,
    ValidationResult,
)
from backend.models.user import BehavioralFact, UserPreference

# --- Tuneable weights for total_soft_score ---------------------------------
# Kept explicit and named so weights end up in decision_traces via the
# ValidationResult snapshot when we serialize the engine's output.


@dataclass(frozen=True)
class SoftWeights:
    macro: float = 0.40          # signed macro deviation (both directions)
    preference: float = 0.18     # likes / dislikes
    variety: float = 0.10
    practicality: float = 0.10
    budget: float = 0.10
    memory: float = 0.07         # advisory-only behavioral facts (§7)
    macro_confidence: float = 0.05    # penalty applied at the end

    def total(self) -> float:
        return (
            self.macro
            + self.preference
            + self.variety
            + self.practicality
            + self.budget
            + self.memory
            + self.macro_confidence
        )


DEFAULT_WEIGHTS = SoftWeights()


# --- Inputs the engine needs -----------------------------------------------


@dataclass(frozen=True)
class MenuDishRef:
    """Slice of a menu_items row the engine needs. Loaded before validation."""

    dish_normalized: str
    meal: MealSlot
    is_veg: bool
    # Populated when the extractor split a "Tea/Coffee/Milk" cell — see
    # 0016_menu_choice_groups.sql. Dishes sharing this id in the same meal
    # are alternatives; plans picking >1 hit `choice_group_violated`.
    choice_group_id: str | None = None


@dataclass(frozen=True)
class ValidationInputs:
    """Everything validate_candidate needs. All deterministic; no live DB access."""

    target_macros: Macros
    planning_remaining: Macros
    veg_today: bool
    hard_budget_inr: float | None
    soft_budget_inr: float
    preferences: list[UserPreference]
    todays_menu: list[MenuDishRef]
    recent_dishes: set[str] = field(default_factory=set)
    behavioral_facts: list[BehavioralFact] = field(default_factory=list)
    # Per-dish practical serving cap per day (keyed by dish_ref). When absent,
    # no cap check runs for that dish. The Planner populates this from
    # macro_db.practical_max_servings_per_day before calling.
    practical_serving_caps: dict[str, float] = field(default_factory=dict)
    weights: SoftWeights = DEFAULT_WEIGHTS
    # Macro sanity thresholds — plans that exceed / undershoot these become
    # INVALID so the revision loop retries. Bends §8 for user-impact reasons.
    # protein floor of 0.40 is a mess-only compromise for M4: LLM currently
    # can't reliably pick protein-dense dishes even with prompt hints. M5
    # canteen fallback (eggs, chicken/paneer rolls) + a deterministic
    # protein-first candidate will let us restore this to 0.70+.
    max_kcal_ratio: float = 1.20
    min_protein_ratio: float = 0.40


# --- Entrypoint -------------------------------------------------------------


def validate_candidate(
    candidate: CandidatePlan, inputs: ValidationInputs
) -> ValidationResult:
    """§8 — validate one candidate. Never mutates inputs."""
    violations: list[HardViolation] = []

    allergy_terms = _pref_terms(inputs.preferences, PreferenceKind.ALLERGY)
    restriction_terms = _pref_terms(inputs.preferences, PreferenceKind.RESTRICTION)
    like_terms = _pref_terms(inputs.preferences, PreferenceKind.LIKE)
    dislike_terms = _pref_terms(inputs.preferences, PreferenceKind.DISLIKE)

    menu_index = _menu_index(inputs.todays_menu)

    # Per-dish servings-across-the-day, checked once at the end.
    servings_by_dish: dict[str, float] = {}
    total_cost = 0.0

    for idx, entry in enumerate(candidate.entries):
        dish_key = entry.dish_ref
        total_cost += entry.price_inr
        servings_by_dish[dish_key] = servings_by_dish.get(dish_key, 0.0) + entry.servings

        # Hard: allergy match — literal substring against dish_ref.
        matched_allergen = _first_term_match(dish_key, allergy_terms)
        if matched_allergen is not None:
            violations.append(
                HardViolation(
                    type=HardViolationType.ALLERGY,
                    detail=f"dish '{dish_key}' matches allergy '{matched_allergen}'",
                    entry_index=idx,
                )
            )

        # Hard: restriction — veg-day + non-veg dish, plus generic term match.
        if inputs.veg_today and _is_non_veg_dish(entry, menu_index):
            violations.append(
                HardViolation(
                    type=HardViolationType.RESTRICTION,
                    detail=f"non-veg dish '{dish_key}' on a veg day",
                    entry_index=idx,
                )
            )
        matched_restriction = _first_term_match(dish_key, restriction_terms)
        if matched_restriction is not None:
            violations.append(
                HardViolation(
                    type=HardViolationType.RESTRICTION,
                    detail=f"dish '{dish_key}' violates restriction '{matched_restriction}'",
                    entry_index=idx,
                )
            )

        # Hard: dish must appear in today's menu for canonical `mess` source.
        # Canteen entries are self-validating — they're picked from
        # canteen_items directly, no mess-menu membership check applies.
        if entry.source == "mess":
            menu_ref = menu_index.get(dish_key)
            if menu_ref is None:
                violations.append(
                    HardViolation(
                        type=HardViolationType.DISH_UNAVAILABLE,
                        detail=f"dish '{dish_key}' not in today's mess menu",
                        entry_index=idx,
                    )
                )
            elif menu_ref.meal != entry.meal:
                violations.append(
                    HardViolation(
                        type=HardViolationType.MEAL_SLOT_MISMATCH,
                        detail=f"dish '{dish_key}' is scheduled at "
                        f"{menu_ref.meal.value} not {entry.meal.value}",
                        entry_index=idx,
                    )
                )
        elif entry.source == "canteen":
            # No mess-menu constraint; canteen tool ensures the dish exists in
            # canteen_items. Practical caps still apply.
            pass

    # Hard budget ceiling — checked once against the plan's total.
    if inputs.hard_budget_inr is not None and total_cost > inputs.hard_budget_inr:
        violations.append(
            HardViolation(
                type=HardViolationType.BUDGET_HARD_CEILING,
                detail=(
                    f"total cost {total_cost:.2f} exceeds hard budget "
                    f"{inputs.hard_budget_inr:.2f}"
                ),
            )
        )

    # Macro sanity against the ACTIVE planning window (remaining = target
    # minus already-consumed) so mid-day replans aren't rejected for being
    # smaller than the full-day target. At start-of-day planning_remaining
    # equals target_macros so behavior is unchanged for the morning run.
    budget_kcal = inputs.planning_remaining.kcal
    budget_protein = inputs.planning_remaining.protein_g
    plan_kcal = candidate.total_macros.kcal
    plan_protein = candidate.total_macros.protein_g
    if budget_kcal > 0 and plan_kcal > inputs.max_kcal_ratio * budget_kcal:
        violations.append(
            HardViolation(
                type=HardViolationType.MACRO_KCAL_CEILING,
                detail=(
                    f"plan kcal {plan_kcal:.0f} exceeds {inputs.max_kcal_ratio:.0%} "
                    f"of remaining {budget_kcal:.0f}"
                ),
            )
        )
    if budget_protein > 0 and plan_protein < inputs.min_protein_ratio * budget_protein:
        violations.append(
            HardViolation(
                type=HardViolationType.MACRO_PROTEIN_FLOOR,
                detail=(
                    f"plan protein {plan_protein:.0f}g below {inputs.min_protein_ratio:.0%} "
                    f"of remaining {budget_protein:.0f}g"
                ),
            )
        )

    # Practical serving cap per dish across the day. Caps arrive via
    # ValidationInputs.practical_serving_caps — the Planner populates them
    # from macro_db.practical_max_servings_per_day.
    for dish_key, total_servings in servings_by_dish.items():
        cap = inputs.practical_serving_caps.get(dish_key)
        if cap is not None and total_servings > cap + 1e-6:
            violations.append(
                HardViolation(
                    type=HardViolationType.PRACTICAL_SERVING_EXCEEDED,
                    detail=(
                        f"dish '{dish_key}' scheduled {total_servings:g} servings; "
                        f"practical_max_servings_per_day={cap:g}"
                    ),
                )
            )

    # Choice group cardinality — at most one dish per (meal, group). The
    # extractor sets `choice_group_id` on menu items when a source cell like
    # "Tea / Coffee / Milk" is split; picking 2+ from the same group means
    # the plan claims the user drinks all three, which is nonsense.
    picked_by_group: dict[tuple[MealSlot, str], list[str]] = {}
    for entry in candidate.entries:
        if entry.source != "mess":
            continue
        ref = menu_index.get(entry.dish_ref)
        gid = getattr(ref, "choice_group_id", None) if ref is not None else None
        if not gid:
            continue
        key = (entry.meal, gid)
        picked_by_group.setdefault(key, []).append(entry.dish_ref)
    for (meal, gid), dishes in picked_by_group.items():
        if len(dishes) > 1:
            violations.append(
                HardViolation(
                    type=HardViolationType.CHOICE_GROUP_VIOLATED,
                    detail=(
                        f"{meal.value}: chose {len(dishes)} alternatives from "
                        f"choice group {gid} ({', '.join(dishes)}); pick one"
                    ),
                )
            )

    is_valid = not violations
    # Score deviation against the active planning window too — mid-day plans
    # should be judged on how well they close the remaining gap, not against
    # the full day's target.
    deviation = _signed_deviation(candidate.total_macros, inputs.planning_remaining)
    budget_deviation = total_cost - inputs.soft_budget_inr

    preference_score = _preference_score(candidate.entries, like_terms, dislike_terms)
    variety_score = _variety_score(candidate.entries, inputs.recent_dishes)
    practicality_score = _practicality_score(servings_by_dish)
    macro_conf_penalty = _macro_confidence_penalty(candidate.entries)
    memory_score = _memory_score(candidate.entries, inputs.behavioral_facts)

    total_soft = _weighted_soft_score(
        weights=inputs.weights,
        deviation=deviation,
        target=inputs.planning_remaining,
        budget_deviation=budget_deviation,
        soft_budget=inputs.soft_budget_inr,
        preference_score=preference_score,
        variety_score=variety_score,
        practicality_score=practicality_score,
        macro_conf_penalty=macro_conf_penalty,
        memory_score=memory_score,
    )

    return ValidationResult(
        is_valid=is_valid,
        hard_violations=violations,
        macro_deviation=deviation,
        budget_deviation=budget_deviation,
        preference_score=preference_score,
        variety_score=variety_score,
        practicality_score=practicality_score,
        macro_confidence_penalty=macro_conf_penalty,
        total_soft_score=total_soft,
    )


# --- Building blocks --------------------------------------------------------


def _pref_terms(prefs: list[UserPreference], kind: PreferenceKind) -> list[str]:
    """Active preferences of a given kind, hard preferences first."""
    out: list[str] = []
    for p in prefs:
        if p.status is not PreferenceStatus.ACTIVE:
            continue
        if p.kind is not kind:
            continue
        # Only hard preferences invalidate (allergies + restrictions ARE hard by
        # onboarding rule §7). Soft likes/dislikes score, they don't invalidate.
        if kind in (PreferenceKind.ALLERGY, PreferenceKind.RESTRICTION):
            if p.authority is PreferenceAuthority.HARD:
                out.append(p.value.strip().lower())
        else:
            out.append(p.value.strip().lower())
    return out


def _first_term_match(dish_ref: str, terms: list[str]) -> str | None:
    """Cheap substring match against normalized dish_ref (`aloo_paratha` form)."""
    if not terms:
        return None
    haystack = dish_ref.replace("_", " ").lower()
    for term in terms:
        if term and term in haystack:
            return term
    return None


def _menu_index(items: list[MenuDishRef]) -> dict[str, MenuDishRef]:
    return {i.dish_normalized: i for i in items}


def _is_non_veg_dish(entry: PlanMealEntry, menu_index: dict[str, MenuDishRef]) -> bool:
    ref = menu_index.get(entry.dish_ref)
    if ref is not None:
        return not ref.is_veg
    return False


def _signed_deviation(total: Macros, target: Macros) -> MacroDeviationSigned:
    """Signed target - total. Positive = under target, negative = over."""
    return MacroDeviationSigned(
        kcal=target.kcal - total.kcal,
        protein_g=target.protein_g - total.protein_g,
        carbs_g=target.carbs_g - total.carbs_g,
        fats_g=target.fats_g - total.fats_g,
    )


def _preference_score(
    entries: list[PlanMealEntry], likes: list[str], dislikes: list[str]
) -> float:
    """Bounded in [0,1]. Starts at 0.5 and moves for each like/dislike hit,
    clamped so a single plan can't drop below 0 or rise above 1."""
    if not entries:
        return 0.5
    score = 0.5
    per_hit = 0.1
    for e in entries:
        if _first_term_match(e.dish_ref, likes) is not None:
            score += per_hit
        if _first_term_match(e.dish_ref, dislikes) is not None:
            score -= per_hit
    return max(0.0, min(1.0, score))


def _variety_score(entries: list[PlanMealEntry], recent: set[str]) -> float:
    """1.0 = every dish is new this week; 0.0 = every dish is a repeat."""
    if not entries:
        return 1.0
    novel = sum(1 for e in entries if e.dish_ref not in recent)
    return novel / len(entries)


def _practicality_score(servings_by_dish: dict[str, float]) -> float:
    """Fewer distinct servings across the day = higher score. A plan that
    stacks 3 servings of one dish is less realistic than 3 different dishes."""
    if not servings_by_dish:
        return 1.0
    max_of_any = max(servings_by_dish.values())
    # Anything above 3 servings of a single dish already feels forced.
    return max(0.0, min(1.0, 1.0 - (max_of_any - 1.0) / 3.0))


def _macro_confidence_penalty(entries: list[PlanMealEntry]) -> float:
    """Fraction of the plan (by kcal) that leans on unverified macros. §9."""
    total_kcal = sum(e.macros.kcal for e in entries)
    if total_kcal <= 0:
        return 0.0
    unverified_kcal = sum(e.macros.kcal for e in entries if not e.macro_verified)
    return unverified_kcal / total_kcal


def _memory_score(
    entries: list[PlanMealEntry], facts: list[BehavioralFact]
) -> float:
    """Advisory-only bonus/penalty for how well the plan matches active
    behavioral memory (§6.3 point 7, §7 advisory rule).

    Cheap heuristic: for each active fact, if the fact's text mentions a
    dish_ref that appears in the plan, count it as agreement (+); if the
    fact suggests avoiding a dish that appears anyway, count as friction (-).
    Returns a score in [0,1] centered around 0.5 when there's no signal."""
    if not facts or not entries:
        return 0.5
    entry_refs = {e.dish_ref.replace("_", " ").lower() for e in entries}
    score = 0.5
    per_hit = 0.08
    for f in facts:
        text = (f.fact or "").lower()
        for ref in entry_refs:
            if ref and ref in text:
                if any(kw in text for kw in ("avoid", "dislike", "skip", "never")):
                    score -= per_hit
                elif any(kw in text for kw in ("prefer", "like", "usually", "always")):
                    score += per_hit
    return max(0.0, min(1.0, score))


def _macro_deviation_norm(deviation: MacroDeviationSigned, target: Macros) -> float:
    """Sum of absolute per-macro deviation, normalized by target and clamped."""
    if target.kcal <= 0:
        return 1.0
    kcal_err = abs(deviation.kcal) / max(target.kcal, 1.0)
    protein_err = abs(deviation.protein_g) / max(target.protein_g, 1.0)
    carbs_err = abs(deviation.carbs_g) / max(target.carbs_g, 1.0)
    fats_err = abs(deviation.fats_g) / max(target.fats_g, 1.0)
    # Weighted so kcal + protein dominate.
    err = 0.35 * kcal_err + 0.35 * protein_err + 0.15 * carbs_err + 0.15 * fats_err
    return min(1.0, err)


def _budget_deviation_norm(deviation: float, soft_budget: float) -> float:
    """0 when at/under soft budget; grows with overshoot, clamped at 1."""
    if soft_budget <= 0:
        return 0.0 if deviation <= 0 else 1.0
    if deviation <= 0:
        return 0.0
    return min(1.0, deviation / soft_budget)


def _weighted_soft_score(
    *,
    weights: SoftWeights,
    deviation: MacroDeviationSigned,
    target: Macros,
    budget_deviation: float,
    soft_budget: float,
    preference_score: float,
    variety_score: float,
    practicality_score: float,
    macro_conf_penalty: float,
    memory_score: float,
) -> float:
    """§8 total_soft_score. Higher = better. Bounded in [0,1] by construction."""
    macro_component = 1.0 - _macro_deviation_norm(deviation, target)
    budget_component = 1.0 - _budget_deviation_norm(budget_deviation, soft_budget)
    conf_component = 1.0 - macro_conf_penalty
    score = (
        weights.macro * macro_component
        + weights.preference * preference_score
        + weights.variety * variety_score
        + weights.practicality * practicality_score
        + weights.budget * budget_component
        + weights.memory * memory_score
        + weights.macro_confidence * conf_component
    ) / max(weights.total(), 1e-6)
    return max(0.0, min(1.0, score))


# --- Helpers used by other tools -------------------------------------------


def signed_delta_to_deviation(delta: MacrosSigned) -> MacroDeviationSigned:
    """Convert a PlannerState `macro_delta` (target-consumed) into a
    ValidationResult-style deviation. Same sign convention."""
    return MacroDeviationSigned(
        kcal=delta.kcal,
        protein_g=delta.protein_g,
        carbs_g=delta.carbs_g,
        fats_g=delta.fats_g,
    )
