"""LLM-backed candidate generator (§6.2).

The LLM's job is *dish selection + serving sizes*, nothing more. Macros come
from `macro_db` — we look them up by normalized dish name and attach them
deterministically. This preserves §10's invariant: LLM-generated numbers
never override deterministic nutrition math.

Public API:
  * `CandidateGenInputs` — everything the generator needs.
  * `generate_candidates(inputs, ...) -> list[CandidatePlan]`
  * `generate_revised_candidates(inputs, feedback, ...)` — revision path
     that re-prompts with hard_violations from a prior attempt.

Fails loudly on:
  * LLM output not matching schema → raises CandidateGenError.
  * LLM references a dish that is not in the supplied menu → the entry is
    dropped and logged; if every entry drops, we return no candidates.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError

from backend.config import Settings, get_settings
from backend.models.enums import MealSlot
from backend.models.nutrition import Macros
from backend.models.plan import CandidatePlan, PlanMealEntry
from backend.tools.llm_router import LLMResult, call_llm
from backend.tools.menu_extract import MenuExtractionError, _parse_json_object


class CandidateGenError(RuntimeError):
    """LLM output was structurally invalid."""


# --- LLM output schema ------------------------------------------------------


class ProposedEntry(BaseModel):
    meal: MealSlot
    dish_normalized: str
    servings: float = Field(gt=0.0)


class ProposedCandidate(BaseModel):
    label: str = ""
    entries: list[ProposedEntry] = Field(default_factory=list)


class ProposedCandidates(BaseModel):
    candidates: list[ProposedCandidate] = Field(default_factory=list)


# --- Inputs -----------------------------------------------------------------


@dataclass(frozen=True)
class MenuDishForGen:
    """Slice of a menu_items row the generator hands to the LLM. Macros are
    pre-attached so the generator knows what the choice implies."""

    dish_normalized: str
    dish_name: str
    meal: MealSlot
    is_veg: bool
    macros_per_serving: Macros
    macro_id: int | None
    macro_verified: bool
    practical_max_servings_per_day: float
    # When set, this dish is one of several alternatives in its meal slot
    # (e.g. Tea/Coffee/Milk). LLM must pick exactly one per group; constraint
    # engine enforces it as a hard violation.
    choice_group_id: str | None = None
    # 'mess' (free) vs 'canteen' (paid). Only populated in mixed mode; in
    # mess-only mode the todays_menu list contains only mess dishes so this
    # is always 'mess'. Used by the LLM to reason about budget and by the
    # planner to emit entries with the correct source tag.
    source: str = "mess"
    price_inr: float = 0.0
    # For canteen dishes: which shop (Nescafe, Amul, Fruit Cart …). Empty
    # for mess dishes. Purely informational for the plan card UI.
    shop_name: str = ""
    dish_ref: str | None = None   # canteen items ship pre-built dish_refs


@dataclass(frozen=True)
class CandidateGenInputs:
    meals_remaining: list[MealSlot]
    planning_remaining: Macros
    todays_menu: list[MenuDishForGen]
    veg_today: bool
    dislikes: list[str] = field(default_factory=list)
    likes: list[str] = field(default_factory=list)
    # Approved behavioral facts from the Learning Agent — free-form sentences
    # like "user skips peanut butter at breakfast" or "user prefers milk over
    # tea". Advisory to the LLM: it should honor them but they are not hard
    # constraints (constraint_engine also uses them as a small soft signal).
    behavioral_facts: list[str] = field(default_factory=list)
    # When mixed mode is active, this is the daily ₹ budget for canteen
    # picks. 0.0 in mess-only mode. LLM must keep sum(canteen prices) below
    # this. Constraint engine also enforces hard/soft budgets separately.
    canteen_budget_inr: float = 0.0
    n_candidates: int = 3


# --- Prompt building --------------------------------------------------------


_SYSTEM = (
    "You are planning a hostel mess meal day. PROTEIN IS THE FIRST PRIORITY — "
    "if the user has a high protein target, prefer protein-dense dishes (paneer, "
    "dal, chana, rajma, chicken curry, eggs, milk, curd) and pick sensible "
    "servings so protein reaches AT LEAST 80% of target. Total kcal should be "
    "close to target and MUST NOT exceed target by more than 15%. Return ONLY "
    "a JSON object, no prose. "
    "HARD RULES: "
    "1) Protein target is the anchor — hit it before adding carbs/fats. "
    "2) Total kcal ≤ 115% of target kcal; being 10% under is fine. "
    "3) At most 2-3 dishes per meal. Do not stack the same dish above 2 servings "
    "unless it is a small item (roti/idli/chapati where 3-4 is acceptable). "
    "4) Use only dishes from the supplied menu; respect meal slots. "
    "5) Never exceed practical_max_servings_per_day for any single dish. "
    "6) Avoid dishes in the user's dislikes; prefer likes when they fit the budget. "
    "7) Spread across breakfast + lunch + snack + dinner — do not pile into one meal. "
    "8) Respect the entries under `Behavioral notes` — these describe how the "
    "user actually eats over time. If a note says the user skips or dislikes a "
    "dish, do NOT include that dish. If a note says they prefer one option "
    "over another, pick the preferred one. These are approved learned "
    "preferences, not guesses. "
    "9) Prefer classic macro-balanced breakfast combos when the menu offers "
    "the pieces: (a) Bread + Peanut Butter, (b) Cornflakes/Oats + Milk, "
    "(c) Boiled Eggs + Bread. Don't just pick the single densest item — "
    "variety and pairing matter for adherence. "
    "10) CHOICE GROUPS — when multiple menu dishes share the same "
    "`choice_group_id` in the same meal slot, they are MUTUALLY EXCLUSIVE "
    "alternatives (e.g. Tea / Coffee / Milk). Pick EXACTLY ONE dish from each "
    "group per meal; never include two dishes from the same group. Prefer "
    "the option that best serves macros (milk > coffee > tea for a protein "
    "target). "
    "11) MIXED MODE (mess + canteen) — dishes with `source: canteen` are "
    "PAID shop items. Include them in the plan to add variety and macro "
    "density that mess alone can't provide (peanut butter, milkshake, "
    "protein bars, boiled eggs, etc.). Sum of `price_inr` across all "
    "canteen entries in the plan MUST NOT exceed `canteen_budget_inr` from "
    "the prompt. If `canteen_budget_inr` is 0, do not include any "
    "`source: canteen` dishes — the user is in mess-only mode. When mixing, "
    "prefer canteen picks that beat mess for protein density or variety "
    "(e.g. bread + peanut butter beats extra tea; milkshake beats a third "
    "serving of dal). "
    "If mostly-carb dishes are available and protein target is high, add extra "
    "servings of dal/paneer/eggs/chana to close the protein gap."
)

_PROMPT_TEMPLATE = """Remaining macros to cover today:
- kcal: {kcal:.0f}
- protein_g: {protein:.0f}
- carbs_g: {carbs:.0f}
- fats_g: {fats:.0f}

Meals still to plan today: {meals}
Veg only today: {veg}
User dislikes: {dislikes}
User likes: {likes}
Canteen budget for today (₹): {canteen_budget:.0f}   (0 = mess-only, do not include any source=canteen dishes)

Behavioral notes (approved learned preferences — respect these):
{behavioral_notes}

Menu (JSON):
{menu_json}

Generate {n} distinct candidate plans. Return this exact JSON shape:
{{
  "candidates": [
    {{
      "label": "high-protein",
      "entries": [
        {{"meal": "lunch", "dish_normalized": "dal_tadka", "servings": 1.5}}
      ]
    }}
  ]
}}
"""


def _menu_payload(menu: list[MenuDishForGen]) -> str:
    return json.dumps(
        [
            {
                "dish_normalized": d.dish_normalized,
                "dish_name": d.dish_name,
                "meal": d.meal.value,
                "is_veg": d.is_veg,
                "kcal": d.macros_per_serving.kcal,
                "protein_g": d.macros_per_serving.protein_g,
                "carbs_g": d.macros_per_serving.carbs_g,
                "fats_g": d.macros_per_serving.fats_g,
                "practical_max_servings_per_day": d.practical_max_servings_per_day,
                # source + price only surface for canteen items — mess is
                # implicit and free. Keeps the LLM prompt readable.
                **(
                    {"source": "canteen", "price_inr": d.price_inr, "shop": d.shop_name}
                    if d.source == "canteen"
                    else {}
                ),
                # Only emit when set — keeps the JSON small and makes the
                # "these are choices, pick one" signal impossible to miss.
                **(
                    {"choice_group_id": d.choice_group_id}
                    if d.choice_group_id
                    else {}
                ),
            }
            for d in menu
        ]
    )


def _build_prompt(inputs: CandidateGenInputs, *, feedback: str | None = None) -> str:
    behavioral_notes = (
        "\n".join(f"- {f}" for f in inputs.behavioral_facts)
        if inputs.behavioral_facts
        else "(none — user has no learned patterns yet)"
    )
    body = _PROMPT_TEMPLATE.format(
        kcal=inputs.planning_remaining.kcal,
        protein=inputs.planning_remaining.protein_g,
        carbs=inputs.planning_remaining.carbs_g,
        fats=inputs.planning_remaining.fats_g,
        meals=", ".join(m.value for m in inputs.meals_remaining),
        veg="yes" if inputs.veg_today else "no",
        dislikes=", ".join(inputs.dislikes) or "(none)",
        likes=", ".join(inputs.likes) or "(none)",
        canteen_budget=float(inputs.canteen_budget_inr),
        behavioral_notes=behavioral_notes,
        menu_json=_menu_payload(inputs.todays_menu),
        n=inputs.n_candidates,
    )
    if feedback:
        body += "\n\nPrevious attempts had these issues — avoid them this time:\n" + feedback
    return body


# --- Entrypoints ------------------------------------------------------------


async def generate_candidates(
    inputs: CandidateGenInputs,
    *,
    settings: Settings | None = None,
    feedback: str | None = None,
) -> tuple[list[CandidatePlan], LLMResult]:
    """Ask the LLM for N proposals, attach macros deterministically, return
    a list of CandidatePlan objects. Empty list is a legitimate outcome — the
    caller should treat it as revision-worthy."""
    s = settings or get_settings()
    prompt = _build_prompt(inputs, feedback=feedback)
    # Higher temperature on revision so the LLM actually explores a different
    # solution space instead of re-emitting the same candidates.
    temp = 0.4 if feedback is None else 0.8
    # Reasoning models (Groq gpt-oss-*) burn tokens thinking before emitting
    # the JSON answer. Give them room; empty content forces our reasoning-field
    # fallback which won't have valid JSON.
    result = await call_llm(
        prompt,
        system=_SYSTEM,
        temperature=temp,
        max_tokens=8192,
        settings=s,
        response_format="json",
    )
    try:
        payload = _parse_json_object(result.text)
    except MenuExtractionError as exc:
        # LLM returned garbage / empty — treat as no candidates so the graph
        # can go straight to the revision / fallback path.
        raise CandidateGenError(f"LLM did not return JSON: {exc}") from exc
    try:
        proposed = ProposedCandidates.model_validate(payload)
    except ValidationError as exc:
        raise CandidateGenError(f"LLM output failed schema: {exc}") from exc

    menu_index = {d.dish_normalized: d for d in inputs.todays_menu}
    plans: list[CandidatePlan] = []
    for prop in proposed.candidates:
        entries, total_macros, total_cost = _attach_macros(prop.entries, menu_index)
        if not entries:
            continue
        plans.append(
            CandidatePlan(
                candidate_id=uuid.uuid4().hex,
                entries=entries,
                total_macros=total_macros,
                total_cost_inr=total_cost,
                generated_by="llm_plan",
            )
        )
    # Enforce MAX_CANDIDATES on the actual list; prompt-level `n` is advisory.
    return plans[: inputs.n_candidates], result


async def generate_revised_candidates(
    inputs: CandidateGenInputs,
    *,
    feedback_lines: list[str],
    settings: Settings | None = None,
) -> tuple[list[CandidatePlan], LLMResult]:
    """Revision pass — re-prompt with the specific violations we hit last time."""
    feedback = "\n".join(f"- {line}" for line in feedback_lines) if feedback_lines else None
    return await generate_candidates(inputs, settings=settings, feedback=feedback)


# --- Helpers ----------------------------------------------------------------


def _attach_macros(
    proposed_entries: list[ProposedEntry],
    menu_index: dict[str, MenuDishForGen],
) -> tuple[list[PlanMealEntry], Macros, float]:
    """Look up macros from the menu; drop unknown dishes silently. Total macros
    and cost come out of this function — the LLM never sets them.

    Practical serving caps are DAY-cumulative: if the LLM proposes chapati at
    both lunch and dinner, the sum across meals must not exceed the per-day
    cap. Extra servings in later entries get clipped to whatever budget
    remains for that dish. Entries clipped to zero are dropped."""
    plan_entries: list[PlanMealEntry] = []
    total = Macros.zero()
    total_cost = 0.0
    servings_used_by_dish: dict[str, float] = {}
    for entry in proposed_entries:
        menu_dish = menu_index.get(entry.dish_normalized)
        if menu_dish is None:
            continue
        key = menu_dish.dish_normalized
        used = servings_used_by_dish.get(key, 0.0)
        remaining = max(0.0, menu_dish.practical_max_servings_per_day - used)
        servings = min(entry.servings, remaining)
        if servings <= 0:
            continue
        servings_used_by_dish[key] = used + servings
        macros_for_entry = menu_dish.macros_per_serving.scaled(servings)
        is_canteen = menu_dish.source == "canteen"
        entry_price = float(menu_dish.price_inr) * servings if is_canteen else 0.0
        plan_entries.append(
            PlanMealEntry(
                meal=menu_dish.meal,
                source="canteen" if is_canteen else "mess",
                # canteen items keep their pre-built `canteen:<shop>:<dish>`
                # dish_ref so downstream (meal_log, macro attach, top-up
                # card) can route them back to canteen_items rows.
                dish_ref=menu_dish.dish_ref or menu_dish.dish_normalized,
                macro_id=menu_dish.macro_id,
                servings=servings,
                macros=macros_for_entry,
                price_inr=entry_price,
                macro_verified=menu_dish.macro_verified,
            )
        )
        total = total + macros_for_entry
        total_cost += entry_price
    return plan_entries, total, total_cost
