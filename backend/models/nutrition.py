from __future__ import annotations

from pydantic import BaseModel, Field


class Macros(BaseModel):
    """Non-negative macronutrient bundle. Used for targets, consumed, dish totals."""

    kcal: float = Field(ge=0.0)
    protein_g: float = Field(ge=0.0)
    carbs_g: float = Field(ge=0.0)
    fats_g: float = Field(ge=0.0)

    def __add__(self, other: "Macros") -> "Macros":
        return Macros(
            kcal=self.kcal + other.kcal,
            protein_g=self.protein_g + other.protein_g,
            carbs_g=self.carbs_g + other.carbs_g,
            fats_g=self.fats_g + other.fats_g,
        )

    def scaled(self, multiplier: float) -> "Macros":
        return Macros(
            kcal=self.kcal * multiplier,
            protein_g=self.protein_g * multiplier,
            carbs_g=self.carbs_g * multiplier,
            fats_g=self.fats_g * multiplier,
        )

    @classmethod
    def zero(cls) -> "Macros":
        return cls(kcal=0.0, protein_g=0.0, carbs_g=0.0, fats_g=0.0)


class MacrosSigned(BaseModel):
    """Signed macro bundle. Preserves over-consumption information (§5, N2)."""

    kcal: float
    protein_g: float
    carbs_g: float
    fats_g: float

    def clamp_nonneg(self) -> Macros:
        """planning_remaining_macros = max(macro_delta, 0) component-wise (§5, N3)."""
        return Macros(
            kcal=max(self.kcal, 0.0),
            protein_g=max(self.protein_g, 0.0),
            carbs_g=max(self.carbs_g, 0.0),
            fats_g=max(self.fats_g, 0.0),
        )


class MacroDeviationSigned(BaseModel):
    """ValidationResult.macro_deviation. Signed: negatives = over, positives = under."""

    kcal: float
    protein_g: float
    carbs_g: float
    fats_g: float
