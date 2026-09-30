"""Deterministic serving-size scaler.

The LLM candidate generator is unreliable at holding a total-kcal ceiling
across 8–10 items — especially on cut targets where the mess menu is
calorie-dense. Rather than relying on prompt engineering (which fights the
tool), we run this after `generate_candidates` and before the Constraint
Engine: if a candidate blows past the kcal ceiling, uniformly shrink every
entry's servings so the total lands just under target.

This preserves the LLM's dish selection (its actual strength) while making
serving quantities deterministic (§4 non-negotiable: "Deterministic Python
for all numeric calc"). The scaler never grows a plan — under-target plans
are left for the LLM revision loop, which has real information to add.
"""
from __future__ import annotations

from backend.models.nutrition import Macros
from backend.models.plan import CandidatePlan, PlanMealEntry

# Aim for 105% of target after scaling. Leaves ~5% headroom without slamming
# right into the 120% ceiling — a rounding-to-0.5 step can push a plan back
# over the edge if the aim point is too aggressive.
_TARGET_KCAL_RATIO = 1.05
# Only intervene when we're actually over — a plan already at 110% is inside
# the ceiling and shouldn't be touched.
_INTERVENE_ABOVE_RATIO = 1.10
# Servings quantize to 0.5 to match how the LLM prompt and macro_db think
# about serving discipline. Never drop below 0.5 — a 0.25-serving chapati
# reads as a bug to the user.
_SERVING_QUANTUM = 0.5
_MIN_SERVINGS = 0.5


def scale_candidate_to_kcal(
    candidate: CandidatePlan, target: Macros
) -> tuple[CandidatePlan, bool]:
    """Shrink servings so total kcal ≤ ~105% of target. Returns (new_plan, was_scaled)."""
    if target.kcal <= 0 or candidate.total_macros.kcal <= 0:
        return candidate, False
    ratio = candidate.total_macros.kcal / target.kcal
    if ratio <= _INTERVENE_ABOVE_RATIO:
        return candidate, False

    scale = (target.kcal * _TARGET_KCAL_RATIO) / candidate.total_macros.kcal
    new_entries: list[PlanMealEntry] = []
    for entry in candidate.entries:
        raw = entry.servings * scale
        # Round to the nearest 0.5, clamped to at least one serving-quantum.
        # Rounding (vs. flooring) keeps us close to target — flooring drifts low
        # and would trigger the protein-floor check on already-tight plans.
        rounded = round(raw / _SERVING_QUANTUM) * _SERVING_QUANTUM
        new_servings = max(_MIN_SERVINGS, rounded)
        if new_servings >= entry.servings:
            # Rounding-up would GROW the entry — refuse. Scaler is
            # shrink-only; growing risks re-breaking the kcal ceiling.
            new_servings = entry.servings
        # Macros scale linearly with servings — no DB round-trip needed.
        # macros_per_serving = entry.macros / entry.servings, so:
        # new_macros = macros_per_serving * new_servings = entry.macros * (new/old)
        macro_factor = new_servings / entry.servings if entry.servings > 0 else 0.0
        new_entries.append(
            entry.model_copy(
                update={
                    "servings": new_servings,
                    "macros": entry.macros.scaled(macro_factor),
                    "price_inr": entry.price_inr * macro_factor,
                }
            )
        )

    total = Macros.zero()
    total_cost = 0.0
    for e in new_entries:
        total = total + e.macros
        total_cost += e.price_inr

    return (
        candidate.model_copy(
            update={
                "entries": new_entries,
                "total_macros": total,
                "total_cost_inr": total_cost,
                "candidate_id": f"{candidate.candidate_id}s",
            }
        ),
        True,
    )


def scale_candidates_to_kcal(
    candidates: list[CandidatePlan], target: Macros
) -> tuple[list[CandidatePlan], int]:
    """Batch helper — returns (scaled_list, num_scaled) for tracing."""
    out: list[CandidatePlan] = []
    scaled_count = 0
    for c in candidates:
        new_c, was_scaled = scale_candidate_to_kcal(c, target)
        out.append(new_c)
        if was_scaled:
            scaled_count += 1
    return out, scaled_count


# --- Protein-floor bumper --------------------------------------------------
# Mirror of the kcal scaler. Two situations produce under-protein candidates:
#   (a) LLM undershoots protein on cut targets (the sibling of the kcal
#       overshoot we just fixed).
#   (b) Our own scaler shrinks a plan proportionally — if the LLM's plan was
#       already protein-borderline, the shrunk version dips below the floor.
# Both are cured the same way: greedily bump servings on the highest-protein-
# density dishes until the floor is met, capped by kcal headroom and each
# dish's practical_max serving.

# Only intervene if we're actually below the protein-floor ratio. Above that,
# leave the plan alone — bumping "just to be safe" would push kcal up needlessly.
_PROTEIN_FLOOR_RATIO = 0.90
# When bumping, don't grow kcal past this ratio of target — keeps a margin
# under the 120% kcal ceiling so the scaler's headroom isn't erased.
_BUMP_KCAL_CEILING_RATIO = 1.15
# Default practical cap for dishes we can't look up (should be rare).
_DEFAULT_PRACTICAL_MAX = 4.0


def bump_candidate_to_protein_floor(
    candidate: CandidatePlan,
    target: Macros,
    practical_caps: dict[str, float] | None = None,
) -> tuple[CandidatePlan, bool]:
    """Bump servings on protein-dense entries until total protein ≥ floor.

    practical_caps maps dish_ref → per-day serving cap from macro_db. Entries
    for dishes not in the map get `_DEFAULT_PRACTICAL_MAX`.
    """
    if target.protein_g <= 0 or not candidate.entries:
        return candidate, False
    floor = target.protein_g * _PROTEIN_FLOOR_RATIO
    if candidate.total_macros.protein_g >= floor:
        return candidate, False

    caps = practical_caps or {}
    # Work on mutable copies keyed by index — order matters because we need to
    # reconstruct the entries list at the end.
    entries = list(candidate.entries)
    per_serving_cache: list[Macros] = []
    for e in entries:
        if e.servings <= 0:
            per_serving_cache.append(Macros.zero())
        else:
            per_serving_cache.append(e.macros.scaled(1.0 / e.servings))

    # Rank by protein-per-kcal density; skip zero-protein entries entirely
    # (bumping tea won't help).
    def _density(idx: int) -> float:
        ps = per_serving_cache[idx]
        if ps.kcal <= 0:
            return 0.0
        return ps.protein_g / ps.kcal

    ranked_indices = sorted(
        (i for i, ps in enumerate(per_serving_cache) if ps.protein_g > 0),
        key=_density,
        reverse=True,
    )
    if not ranked_indices:
        return candidate, False

    current_kcal = candidate.total_macros.kcal
    current_protein = candidate.total_macros.protein_g
    kcal_ceiling = target.kcal * _BUMP_KCAL_CEILING_RATIO

    changed = False
    for idx in ranked_indices:
        if current_protein >= floor:
            break
        entry = entries[idx]
        ps = per_serving_cache[idx]
        cap = caps.get(entry.dish_ref, _DEFAULT_PRACTICAL_MAX)
        room_by_cap = cap - entry.servings
        if room_by_cap <= 0:
            continue
        room_by_kcal = (kcal_ceiling - current_kcal) / ps.kcal if ps.kcal > 0 else float("inf")
        if room_by_kcal <= 0:
            break   # no kcal headroom left; can't bump anything
        room_by_protein = (floor - current_protein) / ps.protein_g if ps.protein_g > 0 else 0.0
        additional_raw = min(room_by_cap, room_by_kcal, room_by_protein)
        # Round DOWN to nearest 0.5 so we don't accidentally exceed cap or ceiling.
        additional = (int(additional_raw / _SERVING_QUANTUM)) * _SERVING_QUANTUM
        if additional < _SERVING_QUANTUM:
            continue
        new_servings = entry.servings + additional
        factor = new_servings / entry.servings
        entries[idx] = entry.model_copy(
            update={
                "servings": new_servings,
                "macros": entry.macros.scaled(factor),
                "price_inr": entry.price_inr * factor,
            }
        )
        current_kcal += ps.kcal * additional
        current_protein += ps.protein_g * additional
        changed = True

    if not changed:
        return candidate, False

    total = Macros.zero()
    total_cost = 0.0
    for e in entries:
        total = total + e.macros
        total_cost += e.price_inr
    return (
        candidate.model_copy(
            update={
                "entries": entries,
                "total_macros": total,
                "total_cost_inr": total_cost,
                "candidate_id": f"{candidate.candidate_id}b",
            }
        ),
        True,
    )


def bump_candidates_to_protein_floor(
    candidates: list[CandidatePlan],
    target: Macros,
    practical_caps: dict[str, float] | None = None,
) -> tuple[list[CandidatePlan], int]:
    """Batch helper — returns (bumped_list, num_bumped) for tracing."""
    out: list[CandidatePlan] = []
    bumped_count = 0
    for c in candidates:
        new_c, was_bumped = bump_candidate_to_protein_floor(c, target, practical_caps)
        out.append(new_c)
        if was_bumped:
            bumped_count += 1
    return out, bumped_count
