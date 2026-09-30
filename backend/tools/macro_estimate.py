"""LLM macro estimator for dishes new to macro_db (§6.1 step 3, §9).

Every estimate is stamped `verified=false` at the DB layer. Confidence below
CONFIDENCE_THRESHOLD flags the dish for admin review (`pending_review_json`
on menu_cycles), and the Constraint Engine will later penalize plans that
lean on unverified macros (§8 macro_confidence_penalty).
"""
from __future__ import annotations

from pydantic import ValidationError

from backend.config import Settings, get_settings
from backend.models.menu_extract import EstimatedMacros
from backend.tools.llm_router import LLMResult, call_llm
from backend.tools.menu_extract import MenuExtractionError, _parse_json_object


class MacroEstimateError(RuntimeError):
    """LLM output was not valid JSON matching EstimatedMacros."""


_ESTIMATE_SYSTEM = (
    "You are estimating per-serving macronutrients for a single Indian hostel-mess dish. "
    "Return ONLY a JSON object matching the schema. No prose, no code fence. "
    "Pick a realistic single-serving portion (e.g. '1 piece', '1 bowl (150g)', '2 rotis (80g)'), "
    "and serving_grams must match that portion. Macros are for ONE serving, not per 100g. "
    "practical_max_servings_per_day is the realistic upper bound of servings a student "
    "would eat in one day of the same dish (e.g. 4 for rotis, 2 for a curry bowl, 6 for idli). "
    "confidence in [0,1] reflects how well-known the dish's nutrition profile is."
)

_ESTIMATE_PROMPT_TEMPLATE = """Estimate macros for the dish:
- name: {dish_name}
- is_veg: {is_veg}

Return this exact JSON shape:
{{
  "dish_name_normalized": "aloo_paratha",
  "serving_unit": "1 piece (80g)",
  "serving_grams": 80,
  "kcal": 210,
  "protein_g": 5.2,
  "carbs_g": 28,
  "fats_g": 8.5,
  "is_veg": true,
  "practical_max_servings_per_day": 3,
  "confidence": 0.75
}}
"""


async def estimate_macros_for_dish(
    dish_name: str,
    *,
    is_veg: bool,
    settings: Settings | None = None,
) -> tuple[EstimatedMacros, LLMResult]:
    s = settings or get_settings()
    prompt = _ESTIMATE_PROMPT_TEMPLATE.format(dish_name=dish_name, is_veg=str(is_veg).lower())
    result = await call_llm(
        prompt,
        system=_ESTIMATE_SYSTEM,
        temperature=0.0,
        max_tokens=512,
        settings=s,
    )
    try:
        payload = _parse_json_object(result.text)
    except MenuExtractionError as exc:  # reuse the tolerant JSON parser
        raise MacroEstimateError(str(exc)) from exc
    try:
        return EstimatedMacros.model_validate(payload), result
    except ValidationError as exc:
        raise MacroEstimateError(f"LLM output failed schema validation: {exc}") from exc
