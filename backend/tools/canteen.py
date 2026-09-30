"""Canteen fallback tool (§6.2, §17).

Deterministic. No LLM. Given a mess-only plan and the day's macro shortfall,
proposes a set of canteen items that close the gap. The proposals get run
through the constraint engine again as `source='canteen'` PlanMealEntries.

Two entrypoints:
  * `find_canteen_additions_for_gap` — pick canteen items that fill the
    signed macro delta between the best mess-only plan and the target.
  * `propose_canteen_only` — for the extreme case where there's no valid
    mess-only plan (e.g., allergen wipes the whole menu). Builds a candidate
    from canteen alone.
"""
from __future__ import annotations

from dataclasses import dataclass

from supabase import Client

from backend.models.canteen import CanteenOption
from backend.models.enums import MealSlot
from backend.models.nutrition import Macros
from backend.models.plan import CandidatePlan, PlanMealEntry


@dataclass(frozen=True)
class CanteenCandidate:
    canteen_item_id: int
    macro_id: int
    dish_ref: str                # `canteen:<shop>:<dish_norm>`
    dish_name: str
    shop_name: str
    price_inr: float
    is_veg: bool
    per_serving: Macros
    practical_max: float
    macro_verified: bool


def load_canteen_for_mixed_mode(
    svc: Client,
    *,
    veg_only: bool,
    dislikes: list[str],
    max_price_inr: float | None = None,
) -> list[CanteenCandidate]:
    """Public loader — returns canteen candidates the LLM can include in a
    mixed-mode plan directly.

    Filters:
      * veg only when it's a veg day.
      * drop any dish matching a user dislike (substring in name or ref).
      * drop items that alone exceed `max_price_inr` (the daily canteen
        budget) — the LLM shouldn't see options it can never afford.

    Unlike `find_canteen_additions_for_gap`, this does NOT rank by protein
    density — the LLM decides. Returns the full filtered catalogue.
    """
    candidates = _load_canteen_candidates(svc, veg_only=veg_only)
    dislike_terms = [d.lower() for d in dislikes if d]

    def _dislikes_hit(c: CanteenCandidate) -> bool:
        hay = c.dish_name.lower() + " " + c.dish_ref.lower()
        return any(d in hay for d in dislike_terms)

    filtered: list[CanteenCandidate] = []
    for c in candidates:
        if _dislikes_hit(c):
            continue
        # A single serving already blows the budget — no point offering it.
        if max_price_inr is not None and c.price_inr > max_price_inr + 1e-6:
            continue
        filtered.append(c)
    return filtered


def _load_canteen_candidates(
    svc: Client, *, veg_only: bool
) -> list[CanteenCandidate]:
    rows = svc.table("canteen_items").select("*").execute().data or []
    if not rows:
        return []
    macro_ids = [r["macro_id"] for r in rows if r["macro_id"] is not None]
    macro_by_id: dict[int, dict] = {}
    if macro_ids:
        macros = svc.table("macro_db").select("*").in_("id", macro_ids).execute().data or []
        macro_by_id = {m["id"]: m for m in macros}
    out: list[CanteenCandidate] = []
    for row in rows:
        macro = macro_by_id.get(row["macro_id"]) if row["macro_id"] is not None else None
        if macro is None:
            continue
        if veg_only and not row["is_veg"]:
            continue
        norm = macro["dish_name_normalized"]
        out.append(
            CanteenCandidate(
                canteen_item_id=row["id"],
                macro_id=macro["id"],
                dish_ref=f"canteen:{row['shop_name']}:{norm}",
                dish_name=f"{row['dish']} @ {row['shop_name']}",
                shop_name=row["shop_name"],
                price_inr=float(row["price_inr"]),
                is_veg=row["is_veg"],
                per_serving=Macros(
                    kcal=float(macro["kcal"]),
                    protein_g=float(macro["protein_g"]),
                    carbs_g=float(macro["carbs_g"]),
                    fats_g=float(macro["fats_g"]),
                ),
                practical_max=float(row["practical_max_servings_per_day"]),
                macro_verified=bool(macro["verified"]),
            )
        )
    return out


def find_canteen_additions_for_gap(
    svc: Client,
    *,
    protein_gap_g: float,
    kcal_remaining_ceiling: float,
    veg_only: bool,
    dislikes: list[str],
    hard_budget_remaining_inr: float | None,
    meal_slot: MealSlot = MealSlot.SNACK,
    max_items: int = 3,
) -> list[PlanMealEntry]:
    """Greedy protein-density selection.

    - Sort candidates by protein_g / kcal ratio (best protein-per-kcal first).
    - Skip dislike-matches and non-veg on veg days.
    - Add items until protein_gap_g is closed OR kcal_remaining_ceiling is
      exceeded OR max_items reached.
    - Each item is attached to the given meal_slot (default: SNACK — the
      easiest place to slot a canteen add-on).

    Returns a list of PlanMealEntry with source='canteen'. Empty list is a
    legitimate outcome (no canteen options fit).
    """
    candidates = _load_canteen_candidates(svc, veg_only=veg_only)
    if not candidates:
        return []

    def _score(c: CanteenCandidate) -> float:
        if c.per_serving.kcal <= 0:
            return 0.0
        return c.per_serving.protein_g / c.per_serving.kcal

    def _dislikes_hit(c: CanteenCandidate) -> bool:
        hay = c.dish_name.lower() + " " + c.dish_ref.lower()
        return any(d in hay for d in dislikes if d)

    ordered = sorted(
        (c for c in candidates if not _dislikes_hit(c)),
        key=_score,
        reverse=True,
    )

    picks: list[PlanMealEntry] = []
    protein_added = 0.0
    kcal_added = 0.0
    cost_added = 0.0
    for cand in ordered:
        if len(picks) >= max_items:
            break
        if protein_added >= protein_gap_g:
            break
        # How many servings would close the remaining gap without blowing kcal?
        remaining_protein_gap = protein_gap_g - protein_added
        remaining_kcal = kcal_remaining_ceiling - kcal_added
        if remaining_kcal <= 0:
            break
        needed_servings = min(
            cand.practical_max,
            max(1.0, remaining_protein_gap / max(cand.per_serving.protein_g, 1e-6)),
        )
        # Clamp so we don't blow the kcal ceiling on this pick.
        max_servings_by_kcal = remaining_kcal / max(cand.per_serving.kcal, 1e-6)
        servings = max(0.5, min(needed_servings, max_servings_by_kcal))
        if servings <= 0:
            continue
        add_cost = cand.price_inr * servings
        if hard_budget_remaining_inr is not None and cost_added + add_cost > hard_budget_remaining_inr:
            continue
        add_macros = cand.per_serving.scaled(servings)
        picks.append(
            PlanMealEntry(
                meal=meal_slot,
                source="canteen",
                dish_ref=cand.dish_ref,
                macro_id=cand.macro_id,
                servings=servings,
                macros=add_macros,
                price_inr=add_cost,
                macro_verified=cand.macro_verified,
            )
        )
        protein_added += add_macros.protein_g
        kcal_added += add_macros.kcal
        cost_added += add_cost
    return picks


def augment_with_canteen(
    base: CandidatePlan,
    additions: list[PlanMealEntry],
    *,
    label: str = "mess+canteen",
) -> CandidatePlan:
    """Build a new CandidatePlan by merging canteen picks into a mess-only base."""
    combined = list(base.entries) + list(additions)
    total = Macros.zero()
    total_cost = 0.0
    for e in combined:
        total = total + e.macros
        total_cost += e.price_inr
    return CandidatePlan(
        candidate_id=f"{base.candidate_id}+cnt",
        entries=combined,
        total_macros=total,
        total_cost_inr=total_cost,
        generated_by="fallback",
    )


def canteen_options_for_meal(
    svc: Client, *, meal_slot: MealSlot, veg_only: bool
) -> list[CanteenOption]:
    """Loader for the HITL surface — returns full CanteenOption models for
    the UI when the user is asked to approve a proposed fallback."""
    cands = _load_canteen_candidates(svc, veg_only=veg_only)
    return [
        CanteenOption(
            id=c.canteen_item_id,
            shop_name=c.shop_name,
            dish=c.dish_name.split(" @ ")[0],
            price_inr=c.price_inr,
            is_veg=c.is_veg,
            macro_id=c.macro_id,
            available_hours="",   # populated only when we start honoring hours
            practical_max_servings_per_day=c.practical_max,
            proposed_for_meal=meal_slot,
            proposed_servings=1.0,
        )
        for c in cands
    ]
