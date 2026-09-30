"""Deterministic tests for dish-name normalization + menu diff. No DB, no LLM."""
from __future__ import annotations

from backend.models.enums import MealSlot
from backend.models.menu_extract import (
    ExtractedDish,
    ExtractedMealSlot,
    ExtractedMenu,
)
from backend.tools.menu import diff_extraction, normalize_dish_name


def test_normalize_is_idempotent_and_ascii_folds():
    assert normalize_dish_name("Aloo Paratha") == "aloo_paratha"
    assert normalize_dish_name("Aloo  Paratha") == "aloo_paratha"
    assert normalize_dish_name(" Aloo Paratha (Butter) ") == "aloo_paratha_butter"
    assert normalize_dish_name("Paneer Butter Masala") == "paneer_butter_masala"
    once = normalize_dish_name("Paneer Butter-Masala")
    assert normalize_dish_name(once) == once


def test_normalize_empty_and_symbols():
    assert normalize_dish_name("") == ""
    assert normalize_dish_name("   ") == ""
    assert normalize_dish_name("Ghee-Rice / Jeera-Rice") == "ghee_rice_jeera_rice"


def _extraction(*dishes: tuple[int, MealSlot, str, bool, float]) -> ExtractedMenu:
    slots: dict[tuple[int, MealSlot], ExtractedMealSlot] = {}
    for day, meal, name, is_veg, conf in dishes:
        key = (day, meal)
        slot = slots.setdefault(
            key, ExtractedMealSlot(day_of_week=day, meal=meal, dishes=[])
        )
        slot.dishes.append(ExtractedDish(dish_name=name, is_veg=is_veg, confidence=conf))
    return ExtractedMenu(slots=list(slots.values()))


def test_diff_first_upload_all_added():
    ext = _extraction(
        (0, MealSlot.BREAKFAST, "Aloo Paratha", True, 0.9),
        (0, MealSlot.LUNCH, "Dal Tadka", True, 0.95),
    )
    diff = diff_extraction(ext, current_items=[], known_macro_names=set())
    assert len(diff.added) == 2
    assert diff.unchanged == []
    assert diff.removed == []
    assert set(diff.unknown_dishes) == {"aloo_paratha", "dal_tadka"}


def test_diff_reports_unknown_only_for_missing_macros():
    ext = _extraction(
        (0, MealSlot.BREAKFAST, "Aloo Paratha", True, 0.9),
        (0, MealSlot.LUNCH, "Rare New Dish", True, 0.8),
    )
    diff = diff_extraction(
        ext,
        current_items=[],
        known_macro_names={"aloo_paratha"},
    )
    assert diff.unknown_dishes == ["rare_new_dish"]


def test_diff_detects_added_removed_unchanged():
    ext = _extraction(
        (0, MealSlot.LUNCH, "Dal Tadka", True, 0.9),
        (0, MealSlot.LUNCH, "Paneer Butter Masala", True, 0.9),
    )
    current = [
        {"day_of_week": 0, "meal": "lunch", "dish_name": "Dal Tadka"},
        {"day_of_week": 0, "meal": "lunch", "dish_name": "Bhindi Masala"},
    ]
    diff = diff_extraction(
        ext,
        current_items=current,
        known_macro_names={"dal_tadka", "bhindi_masala", "paneer_butter_masala"},
    )
    added = {(k.day_of_week, k.meal, k.dish_normalized) for k in diff.added}
    removed = {(k.day_of_week, k.meal, k.dish_normalized) for k in diff.removed}
    unchanged = {(k.day_of_week, k.meal, k.dish_normalized) for k in diff.unchanged}
    assert (0, MealSlot.LUNCH, "paneer_butter_masala") in added
    assert (0, MealSlot.LUNCH, "bhindi_masala") in removed
    assert (0, MealSlot.LUNCH, "dal_tadka") in unchanged
    assert diff.unknown_dishes == []


def test_diff_ignores_empty_dish_name():
    ext = _extraction(
        (0, MealSlot.LUNCH, "", True, 0.5),
        (0, MealSlot.LUNCH, "Rajma", True, 0.9),
    )
    diff = diff_extraction(ext, current_items=[], known_macro_names=set())
    assert len(diff.added) == 1
    assert diff.added[0].dish_normalized == "rajma"
