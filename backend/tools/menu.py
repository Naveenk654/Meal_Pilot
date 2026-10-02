"""Menu utilities: dish-name normalization, diff against current cycle,
and today's-menu resolver.

Deterministic. No LLM. The Menu Intelligence Agent uses `diff_extraction`
to write only genuinely-new menu_items, preserving verified macros on
dishes that survive from cycle to cycle. The Planner (M3) uses
`resolve_daily_menu` to get today's menu attached to freshness info.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from supabase import Client

from backend.models.enums import MealSlot
from backend.models.menu_extract import ExtractedMenu

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_dish_name(name: str) -> str:
    """Canonical form used as macro_db.dish_name_normalized.

    "Aloo Paratha (Butter)" → "aloo_paratha_butter"
    "Paneer Butter Masala"  → "paneer_butter_masala"
    Idempotent: normalize(normalize(x)) == normalize(x).
    """
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    lowered = folded.lower().strip()
    collapsed = _NON_ALNUM_RE.sub("_", lowered).strip("_")
    return collapsed


@dataclass(frozen=True)
class DiffKey:
    day_of_week: int
    meal: MealSlot
    dish_normalized: str


@dataclass
class MenuDiff:
    added: list[DiffKey] = field(default_factory=list)
    removed: list[DiffKey] = field(default_factory=list)
    unchanged: list[DiffKey] = field(default_factory=list)
    # Dishes that appear in the extraction but are not yet in macro_db.
    unknown_dishes: list[str] = field(default_factory=list)


def diff_extraction(
    extraction: ExtractedMenu,
    *,
    current_items: list[dict],
    known_macro_names: set[str],
) -> MenuDiff:
    """Compare a new extraction to the currently-active cycle's items.

    Args:
      extraction: LLM output for the new PDF.
      current_items: rows from menu_items for the active cycle (may be empty
        on the very first upload). Each row contains day_of_week, meal,
        dish_name.
      known_macro_names: set of macro_db.dish_name_normalized already stored.

    Returns:
      MenuDiff — the caller writes only `added`, keeps `unchanged`, and
      surfaces `unknown_dishes` to the macro estimator + admin HITL.
    """
    current: set[DiffKey] = {
        DiffKey(
            day_of_week=int(row["day_of_week"]),
            meal=MealSlot(row["meal"]),
            dish_normalized=normalize_dish_name(row["dish_name"]),
        )
        for row in current_items
    }

    new: list[DiffKey] = []
    unknown: set[str] = set()
    for day, meal, dish in extraction.flatten():
        normalized = normalize_dish_name(dish.dish_name)
        if not normalized:
            continue
        new.append(DiffKey(day, meal, normalized))
        if normalized not in known_macro_names:
            unknown.add(normalized)

    new_set = set(new)
    return MenuDiff(
        added=sorted(new_set - current, key=_diff_key_sort),
        removed=sorted(current - new_set, key=_diff_key_sort),
        unchanged=sorted(new_set & current, key=_diff_key_sort),
        unknown_dishes=sorted(unknown),
    )


def _diff_key_sort(k: DiffKey) -> tuple[int, str, str]:
    return (k.day_of_week, k.meal.value, k.dish_normalized)


# --- Today's menu resolver (§12) --------------------------------------------


@dataclass(frozen=True)
class ResolvedMenuItem:
    day_of_week: int
    meal: MealSlot
    dish_name: str
    dish_normalized: str
    is_veg: bool
    macro_id: int | None
    confidence: float
    # Set when the extractor split a "Tea/Coffee/Milk"-style cell. Items
    # sharing this id in the same (day, meal) are alternatives — the
    # Planner picks exactly one per group.
    choice_group_id: str | None = None


@dataclass(frozen=True)
class ResolvedDailyMenu:
    """Result of `resolve_daily_menu`. Planner treats None as menu-uncertain."""

    cycle_id: int
    effective_from: date
    effective_to: date
    version: int
    content_hash: str
    source: str
    items: list[ResolvedMenuItem]


def resolve_daily_menu(client: Client, *, on_date: date) -> ResolvedDailyMenu | None:
    """Return today's items from the latest active cycle that has items for
    today's day-of-week, or None.

    Semantics: pick the active cycle with the LATEST `effective_from <= today`
    that actually has `menu_items` for `on_date.weekday()`. `effective_to` is
    intentionally ignored — mess menus stay in force until a newer one is
    uploaded (admin manually sets status='superseded' to force-expire). An
    empty cycle (e.g. a broken/partial ingestion, or a leaked test row) is
    silently skipped so the next-newest cycle gets a chance.

    None means "no cycle at all has items for today" — the Planner treats
    that as menu-uncertain (§12 HITL / canteen fallback).
    """
    resp = (
        client.table("menu_cycles")
        .select("*")
        .lte("effective_from", on_date.isoformat())
        .eq("status", "active")
        .order("effective_from", desc=True)
        .order("version", desc=True)
        .execute()
    )
    cycles = resp.data or []
    if not cycles:
        return None
    day_of_week = on_date.weekday()   # Monday=0

    for cycle in cycles:
        items_resp = (
            client.table("menu_items")
            .select("*")
            .eq("cycle_id", cycle["id"])
            .eq("day_of_week", day_of_week)
            .execute()
        )
        rows = items_resp.data or []
        if not rows:
            # Empty cycle for this dow — skip and try the next-newest cycle.
            # Covers broken ingestions and leaked test rows that would
            # otherwise shadow a real menu.
            continue
        items = [
            ResolvedMenuItem(
                day_of_week=day_of_week,
                meal=MealSlot(row["meal"]),
                dish_name=row["dish_name"],
                dish_normalized=normalize_dish_name(row["dish_name"]),
                is_veg=row["is_veg"],
                macro_id=row["macro_id"],
                confidence=float(row["confidence"]),
                choice_group_id=row.get("choice_group_id"),
            )
            for row in rows
        ]
        return ResolvedDailyMenu(
            cycle_id=cycle["id"],
            effective_from=date.fromisoformat(cycle["effective_from"]),
            effective_to=date.fromisoformat(cycle["effective_to"]),
            version=int(cycle["version"]),
            content_hash=cycle["content_hash"],
            source=cycle["source"],
            items=items,
        )
    # No active cycle had any menu_items for today's day_of_week.
    return None
