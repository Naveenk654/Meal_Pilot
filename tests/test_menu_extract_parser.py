"""Deterministic tests for the LLM output parser. No network — we feed
literal strings through the JSON extractor and confirm we can survive the
common failure modes (code fences, leading prose, malformed JSON)."""
from __future__ import annotations

import pytest

from backend.models.menu_extract import ExtractedDish, ExtractedMealSlot, ExtractedMenu
from backend.tools.menu_extract import (
    MenuExtractionError,
    _clean_dish_name,
    _parse_json_object,
    _sanitize_extracted_menu,
)


def test_parses_raw_json():
    payload = _parse_json_object('{"slots": []}')
    assert payload == {"slots": []}


def test_parses_code_fenced_json():
    text = "```json\n{\n  \"slots\": [] \n}\n```"
    payload = _parse_json_object(text)
    ExtractedMenu.model_validate(payload)


def test_parses_json_with_leading_prose():
    text = "Sure, here you go:\n{\"slots\": []}"
    payload = _parse_json_object(text)
    assert payload == {"slots": []}


def test_raises_on_non_json():
    with pytest.raises(MenuExtractionError):
        _parse_json_object("no json here")


def test_sanitize_splits_slash_separated_dish():
    """LLM sometimes emits 'Tea/Coffee/Milk' as one dish despite the prompt.
    The sanitizer must break it into three separate ExtractedDish entries in
    the same slot."""
    menu = ExtractedMenu(
        slots=[
            ExtractedMealSlot(
                day_of_week=0, meal="breakfast",
                dishes=[
                    ExtractedDish(dish_name="Tea/Coffee/Milk", is_veg=True, confidence=0.9),
                    ExtractedDish(dish_name="Bread", is_veg=True, confidence=0.9),
                ],
            )
        ]
    )
    cleaned = _sanitize_extracted_menu(menu)
    names = [d.dish_name for d in cleaned.slots[0].dishes]
    assert names == ["Tea", "Coffee", "Milk", "Bread"]


def test_sanitize_drops_underscored_concat_bugs():
    """Names like 'bread_butter_jam_tea_coffee_milk_bournvita_sugar_fruit' are
    LLM concat bugs — the sanitizer must drop them entirely."""
    assert _clean_dish_name("bread_butter_jam_tea") == []


def test_sanitize_drops_overlong_concat_bugs():
    """Any dish_name longer than 5 words is almost certainly a bug."""
    assert _clean_dish_name("bread butter jam tea coffee milk fruit sugar") == []


def test_sanitize_keeps_ampersand_compound_dishes():
    """'Plain & Butter Roti' is a real single dish (butter-topped roti),
    not a list of choices. Must not split on '&'."""
    assert _clean_dish_name("Plain & Butter Roti") == ["Plain & Butter Roti"]


def test_sanitize_fixes_known_typos():
    """'Fruyms' is a common LLM typo of 'Fryums' — sanitizer normalizes it."""
    assert _clean_dish_name("Fruyms") == ["Fryums"]


def test_sanitize_splits_on_or_keyword():
    """Some menus use 'Tea or Coffee' — treat like a slash."""
    assert _clean_dish_name("Tea or Coffee") == ["Tea", "Coffee"]


def test_validates_extracted_menu_schema():
    payload = {
        "slots": [
            {
                "day_of_week": 0,
                "meal": "breakfast",
                "dishes": [
                    {"dish_name": "Aloo Paratha", "is_veg": True, "confidence": 0.9}
                ],
            }
        ]
    }
    ext = ExtractedMenu.model_validate(payload)
    flat = ext.flatten()
    assert len(flat) == 1
    assert flat[0][2].dish_name == "Aloo Paratha"
