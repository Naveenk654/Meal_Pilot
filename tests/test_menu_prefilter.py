"""Pre-filter for allergen/restricted/non-veg dishes before LLM candidate gen.

The Constraint Engine catches these at validation, but if the LLM can *see*
them in its prompt it will keep picking them on every revision pass — an
infinite loop of hard-violations. Filtering at the source stops that.
Dislikes are deliberately still shown to the LLM (soft signal, not hard block).
"""
from __future__ import annotations

from backend.agents.planner_nodes import is_dish_forbidden_for_llm


def test_non_veg_dish_hidden_on_veg_day():
    assert is_dish_forbidden_for_llm(
        "chicken_curry", is_veg=False,
        veg_today=True, allergy_terms=[], restriction_terms=[],
    ) is True


def test_non_veg_dish_allowed_on_non_veg_day():
    assert is_dish_forbidden_for_llm(
        "chicken_curry", is_veg=False,
        veg_today=False, allergy_terms=[], restriction_terms=[],
    ) is False


def test_allergen_substring_hidden():
    """Nut allergy should block anything with 'nut' in the normalized name."""
    assert is_dish_forbidden_for_llm(
        "peanut_chutney", is_veg=True,
        veg_today=True, allergy_terms=["nut"], restriction_terms=[],
    ) is True


def test_restriction_substring_hidden():
    """Restriction terms use the same substring match as allergies."""
    assert is_dish_forbidden_for_llm(
        "paneer_butter_masala", is_veg=True,
        veg_today=True, allergy_terms=[], restriction_terms=["paneer"],
    ) is True


def test_case_insensitive_match():
    assert is_dish_forbidden_for_llm(
        "Paneer_Butter_Masala", is_veg=True,
        veg_today=True, allergy_terms=[], restriction_terms=["PANEER"],
    ) is True


def test_empty_term_never_matches():
    """A stray empty string in the prefs list shouldn't wildcard-block everything."""
    assert is_dish_forbidden_for_llm(
        "dal_tadka", is_veg=True,
        veg_today=True, allergy_terms=[""], restriction_terms=[""],
    ) is False


def test_non_matching_dish_passes_through():
    assert is_dish_forbidden_for_llm(
        "dal_tadka", is_veg=True,
        veg_today=True, allergy_terms=["shellfish"], restriction_terms=["gluten"],
    ) is False


def test_missing_dish_name_is_safe():
    """Defensive: empty dish_normalized shouldn't blow up or match a real term."""
    assert is_dish_forbidden_for_llm(
        "", is_veg=True,
        veg_today=True, allergy_terms=["nut"], restriction_terms=[],
    ) is False
