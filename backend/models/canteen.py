from __future__ import annotations

from pydantic import BaseModel, Field

from backend.models.enums import MealSlot


class CanteenOption(BaseModel):
    """A candidate canteen dish surfaced by the fallback tool. Validated by Constraint Engine."""

    id: int | None = None
    shop_name: str
    dish: str
    price_inr: float = Field(ge=0.0)
    is_veg: bool
    macro_id: int | None = None
    available_hours: str
    practical_max_servings_per_day: float = Field(gt=0.0)
    proposed_for_meal: MealSlot | None = None
    proposed_servings: float = Field(gt=0.0, default=1.0)
