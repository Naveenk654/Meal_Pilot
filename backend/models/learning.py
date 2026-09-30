"""Pydantic schemas for the Learning Agent (§6.3)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ProposedFact(BaseModel):
    """LLM output — one behavioral pattern the model detected.

    Every proposed fact must cite its evidence (`meal_log` ids) so the HITL
    reviewer can trust or reject it based on the actual log data.
    """

    fact: str = Field(..., description="Natural-language description of the pattern")
    evidence_log_ids: list[int] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class ProposedFactBatch(BaseModel):
    facts: list[ProposedFact] = Field(default_factory=list)
