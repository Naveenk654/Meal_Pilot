from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TraceEvent(BaseModel):
    """A single structured entry in PlannerState.reasoning_trace. Persisted to decision_traces.

    reason_code is structured and authoritative (§15, O3). llm_reasoning is optional secondary.
    """

    step_name: str
    reason_code: str
    candidates_considered: list[dict[str, Any]] = Field(default_factory=list)
    validation_results: list[dict[str, Any]] = Field(default_factory=list)
    chosen_option: dict[str, Any] | None = None
    llm_reasoning: str | None = None
    at: datetime | None = None
