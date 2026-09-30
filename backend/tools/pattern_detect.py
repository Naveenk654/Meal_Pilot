"""LLM pattern detection for the weekly Learning Agent (§6.3 step 2).

Takes a compact summary of the last 7 days of meal_logs and asks the LLM to
return a JSON list of ProposedFact objects (fact, evidence_log_ids, confidence).
No behavioral memory ever writes to the DB from here — that's the caller's
job — and every proposed fact carries the meal_log ids it was derived from,
so the HITL reviewer can verify.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from backend.config import Settings, get_settings
from backend.models.learning import ProposedFactBatch
from backend.tools.llm_router import LLMResult, call_llm
from backend.tools.menu_extract import MenuExtractionError, _parse_json_object


class PatternDetectError(RuntimeError):
    """LLM output was not valid JSON matching ProposedFactBatch."""


_SYSTEM = (
    "You analyze one week of meal logs from a hostel mess user and identify "
    "behavioral patterns worth remembering. Return ONLY a JSON object with a "
    "top-level 'facts' array. Each fact must reference the meal_log ids that "
    "support it (evidence_log_ids), be phrased as a short, actionable pattern "
    "('user skips breakfast on weekdays', 'user prefers dal over paneer'), "
    "and carry a confidence between 0 and 1. Never invent facts without "
    "evidence. Return at most 5 facts. "
    "PRIORITY signals to look for: "
    "(a) DISH-LEVEL REJECTIONS — a dish that consistently appears in "
    "`actual_dishes_json` with `action='skipped'`, OR that was on the plan "
    "but missing from `actual_dishes_json` when `action='ate_different'`. "
    "Phrase these as 'user skips <dish> at <meal>'. "
    "(b) DISH-LEVEL PREFERENCES — a dish the user consistently ATE (planned "
    "or added). Phrase as 'user prefers <dish> at <meal>'. "
    "(c) MEAL-SLOT PATTERNS — skipping an entire meal on certain days. "
    "Prefer specific per-dish facts over vague generalizations — 'user "
    "skips peanut butter at breakfast' beats 'user avoids dense breakfasts'."
)


_PROMPT_TEMPLATE = """Meal logs for the last 7 days:

{logs_json}

Return this exact shape:
{{
  "facts": [
    {{
      "fact": "user consistently skips breakfast",
      "evidence_log_ids": [17, 24, 31, 38],
      "confidence": 0.8
    }}
  ]
}}
"""


async def detect_patterns(
    logs: list[dict[str, Any]],
    *,
    settings: Settings | None = None,
) -> tuple[ProposedFactBatch, LLMResult]:
    """Ask the LLM for a small batch of behavioral facts. Bubbles up any
    router / parsing / schema error as PatternDetectError so the caller can
    log it and move on."""
    if not logs:
        return ProposedFactBatch(), LLMResult(
            text="", tokens_used=0, latency_ms=0, provider="none", model="none"
        )
    s = settings or get_settings()
    prompt = _PROMPT_TEMPLATE.format(
        logs_json=json.dumps(logs, default=str, sort_keys=True)
    )
    result = await call_llm(
        prompt, system=_SYSTEM, temperature=0.0, max_tokens=1024, settings=s
    )
    try:
        payload = _parse_json_object(result.text)
    except MenuExtractionError as exc:
        raise PatternDetectError(f"LLM did not return JSON: {exc}") from exc
    try:
        return ProposedFactBatch.model_validate(payload), result
    except ValidationError as exc:
        raise PatternDetectError(f"LLM output failed schema: {exc}") from exc
