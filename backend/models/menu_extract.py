"""Pydantic schemas for LLM menu-intelligence outputs (§6.1 steps 2-3).

Kept in `models/` so both the tool layer and the agent orchestrator can import
them without a circular dep.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from backend.models.enums import MealSlot


class ExtractedDish(BaseModel):
    """A single dish the LLM identifies from the PDF."""

    dish_name: str
    is_veg: bool
    confidence: float = Field(ge=0.0, le=1.0)
    # Set when this dish was split out of a "Tea/Coffee/Milk"-style choice
    # cell. All dishes derived from the same source cell share the same id,
    # signaling to the Planner that they're mutually exclusive alternatives.
    choice_group_id: str | None = None


class ExtractedMealSlot(BaseModel):
    day_of_week: int = Field(ge=0, le=6)   # 0=Monday, 6=Sunday
    meal: MealSlot
    dishes: list[ExtractedDish] = Field(default_factory=list)


class ExtractedMenu(BaseModel):
    """Full LLM extraction for one PDF."""

    slots: list[ExtractedMealSlot] = Field(default_factory=list)

    def flatten(self) -> list[tuple[int, MealSlot, ExtractedDish]]:
        return [
            (slot.day_of_week, slot.meal, dish)
            for slot in self.slots
            for dish in slot.dishes
        ]


class EstimatedMacros(BaseModel):
    """LLM's per-serving macro estimate for a single dish (§6.1 step 3)."""

    dish_name_normalized: str
    serving_unit: str
    serving_grams: float = Field(gt=0.0)
    kcal: float = Field(ge=0.0)
    protein_g: float = Field(ge=0.0)
    carbs_g: float = Field(ge=0.0)
    fats_g: float = Field(ge=0.0)
    is_veg: bool
    practical_max_servings_per_day: float = Field(gt=0.0)
    confidence: float = Field(ge=0.0, le=1.0)
