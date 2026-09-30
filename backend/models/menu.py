from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.models.enums import MacroSource, MealSlot
from backend.models.nutrition import Macros


class MenuItem(BaseModel):
    """A single dish scheduled in a menu cycle."""

    id: int | None = None
    cycle_id: int
    day_of_week: int = Field(ge=0, le=6)
    meal: MealSlot
    dish_name: str
    is_veg: bool
    macro_id: int | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)


class MacroRecord(BaseModel):
    """A row in macro_db (§11)."""

    id: int | None = None
    dish_name_normalized: str
    serving_unit: str
    serving_grams: float = Field(gt=0.0)
    kcal: float = Field(ge=0.0)
    protein_g: float = Field(ge=0.0)
    carbs_g: float = Field(ge=0.0)
    fats_g: float = Field(ge=0.0)
    is_veg: bool
    practical_max_servings_per_day: float = Field(gt=0.0)
    source: MacroSource
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    verified: bool = False
    estimated_at: datetime | None = None
    verified_at: datetime | None = None
    verified_by: str | None = None

    def macros_per_serving(self) -> Macros:
        return Macros(
            kcal=self.kcal,
            protein_g=self.protein_g,
            carbs_g=self.carbs_g,
            fats_g=self.fats_g,
        )


class MenuFreshness(BaseModel):
    """§5, §12 — attached to today's menu snapshot."""

    source: Literal["pdf", "email", "manual"]
    ingested_at: datetime
    effective_from: date
    effective_to: date
    version: int
    content_hash: str


class DailyMenu(BaseModel):
    """A single day's menu resolved from the active cycle. None on state = uncertainty."""

    date: date
    veg_only: bool
    items_by_meal: dict[MealSlot, list[MenuItem]] = Field(default_factory=dict)
    freshness: MenuFreshness
