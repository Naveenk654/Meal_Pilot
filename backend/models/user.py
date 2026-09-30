from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from backend.models.enums import (
    ActivityLevel,
    Gender,
    Goal,
    PlanMode,
    PreferenceAuthority,
    PreferenceKind,
    PreferenceSource,
    PreferenceStatus,
    UserMode,
)


class UserProfile(BaseModel):
    user_id: str
    age: int = Field(ge=10, le=100)
    gender: Gender
    height_cm: float = Field(ge=100.0, le=250.0)
    weight_kg: float = Field(ge=30.0, le=250.0)
    activity_level: ActivityLevel
    goal: Goal
    mode: UserMode
    veg_default: bool = True
    budget_soft_inr: float = Field(ge=0.0)
    budget_hard_inr: float | None = None
    plan_mode: PlanMode = PlanMode.MESS_ONLY
    bmr: float
    tdee: float
    target_kcal: float
    target_protein_g: float
    target_carbs_g: float
    target_fats_g: float
    updated_at: datetime | None = None


class UserPreference(BaseModel):
    id: int | None = None
    user_id: str
    kind: PreferenceKind
    value: str
    source: PreferenceSource
    authority: PreferenceAuthority
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    status: PreferenceStatus = PreferenceStatus.ACTIVE
    superseded_by: int | None = None
    first_seen: datetime | None = None
    last_confirmed_at: datetime | None = None
    retired_at: datetime | None = None


class BehavioralFact(BaseModel):
    id: int | None = None
    user_id: str
    fact: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    status: PreferenceStatus = PreferenceStatus.ACTIVE
    first_seen: datetime | None = None
    last_confirmed_at: datetime | None = None
    retired_at: datetime | None = None
    contradiction_count: int = 0
