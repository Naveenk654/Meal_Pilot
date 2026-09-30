"""LLM structured extractor for mess-menu PDFs (§6.1 step 2).

Given the deterministic text/table snippet from `pdf_parse`, ask the LLM to
return a JSON object describing every {day, meal, dish, is_veg, confidence}
tuple it can identify. We validate against `ExtractedMenu`; any structural
failure raises `MenuExtractionError` so the caller can HITL rather than
silently accept garbage.
"""
from __future__ import annotations

import json
import re

from pydantic import ValidationError

from backend.config import Settings, get_settings
from backend.models.menu_extract import ExtractedMenu
from backend.tools.llm_router import LLMResult, call_llm


class MenuExtractionError(RuntimeError):
    """LLM output was not valid JSON matching ExtractedMenu."""


_EXTRACTION_SYSTEM = (
    "You are extracting a hostel mess menu from PDF text into JSON. "
    "Return ONLY a JSON object matching the schema. No prose, no code fence. "
    "day_of_week uses 0=Monday through 6=Sunday. "
    "meal is one of: breakfast, lunch, snack, dinner. "
    "CRITICAL: emit one slot for EVERY (day, meal) pair the PDF contains. "
    "A typical weekly mess menu has 4 meals per day across 7 days (28 slots). "
    "Do not stop after breakfast. Do not omit lunch, snack, or dinner. "
    "For every dish, set is_veg (true unless the dish clearly contains meat/egg/fish) "
    "and confidence in [0,1] reflecting how sure you are the dish is on that day+meal. "
    "\n\n"
    "DISH SPLITTING RULES — these are the biggest source of bugs, follow them exactly: "
    "1) When a menu cell lists multiple items separated by '/', 'or', ',', or ' & ', "
    "emit each as a SEPARATE dish object in the same slot's `dishes` array. "
    "Example: a cell containing 'Tea / Coffee / Milk' becomes THREE dish "
    "entries: {\"dish_name\": \"Tea\"}, {\"dish_name\": \"Coffee\"}, {\"dish_name\": \"Milk\"}. "
    "NEVER emit a combined name like 'Tea/Coffee/Milk' or 'tea_coffee_milk'. "
    "2) Exception: an ampersand-joined dish that names a real single item — "
    "'Plain & Butter Roti', 'Salt & Pepper Chicken' — stays as ONE dish. Rule "
    "of thumb: if replacing '&' with 'or' still reads like a single recipe, "
    "keep it together. If it reads like a list of choices, split. "
    "3) Every `dish_name` must be a real dish, not a concatenation. Reject "
    "any output where a dish_name has more than 4 words OR contains "
    "underscores. If you catch yourself writing 'bread_butter_jam_tea', that "
    "is wrong — split it into 4 dishes. "
    "4) Normalize obvious typos to their canonical form: 'Fruyms' → 'Fryums', "
    "'Chappati' → 'Chapati'. Prefer the more common spelling. "
    "5) Skip pure condiments and sub-1g-protein garnishes when they clutter "
    "the plan (Sugar, Salt, Ketchup listed alone). Keep them only if they "
    "are the sole item in a slot."
)

_EXTRACTION_PROMPT_TEMPLATE = """Extract the mess menu from the following content.

Return this exact JSON shape (no other keys, no prose). Include every day and every meal you can identify — breakfast, lunch, snack, AND dinner.

Note the second breakfast row: a source cell of 'Tea/Coffee/Milk' becomes THREE separate dish entries, one per option — never a single combined dish. Same for any 'A/B', 'A or B', 'A, B' listing.

{{
  "slots": [
    {{"day_of_week": 0, "meal": "breakfast", "dishes": [
      {{"dish_name": "Aloo Paratha", "is_veg": true, "confidence": 0.9}},
      {{"dish_name": "Tea",          "is_veg": true, "confidence": 0.9}},
      {{"dish_name": "Coffee",       "is_veg": true, "confidence": 0.9}},
      {{"dish_name": "Milk",         "is_veg": true, "confidence": 0.9}}
    ]}},
    {{"day_of_week": 0, "meal": "lunch",  "dishes": [{{"dish_name": "Rajma Chawal", "is_veg": true, "confidence": 0.9}}]}},
    {{"day_of_week": 0, "meal": "snack",  "dishes": [{{"dish_name": "Samosa",       "is_veg": true, "confidence": 0.9}}]}},
    {{"day_of_week": 0, "meal": "dinner", "dishes": [{{"dish_name": "Paneer Curry", "is_veg": true, "confidence": 0.9}}]}}
  ]
}}

Content:
```
{content}
```
"""


async def extract_menu_from_snippet(
    snippet: str,
    *,
    settings: Settings | None = None,
) -> tuple[ExtractedMenu, LLMResult]:
    """Call the LLM router and validate the returned JSON. Bubbles up router errors."""
    s = settings or get_settings()
    prompt = _EXTRACTION_PROMPT_TEMPLATE.format(content=snippet)
    result = await call_llm(
        prompt,
        system=_EXTRACTION_SYSTEM,
        temperature=0.0,   # extraction is not creative
        max_tokens=8192,
        settings=s,
    )
    payload = _parse_json_object(result.text)
    try:
        parsed = ExtractedMenu.model_validate(payload)
    except ValidationError as exc:
        raise MenuExtractionError(f"LLM output failed schema validation: {exc}") from exc
    return _sanitize_extracted_menu(parsed), result


# Regex for splitting a dish_name that lists multiple options. Matches ' / ',
# '/', ' or ', ' , ', and ' & ' when the pieces on each side look like real
# dishes (letters only, ≤3 words). Deliberately conservative — real compound
# names like 'Plain & Butter Roti' shouldn't split.
_SPLIT_RE = re.compile(r"\s*(?:/|(?:\bor\b))\s*", re.IGNORECASE)

# Typo normalization — deterministic, applied after split. Add pairs here as
# they surface in the wild. Keys are lowercase.
_TYPO_FIXES: dict[str, str] = {
    "fruyms": "Fryums",
    "chappati": "Chapati",
    "channa": "Chana",
}


def _sanitize_extracted_menu(menu: ExtractedMenu) -> ExtractedMenu:
    """Defense-in-depth cleanup of LLM output. Even with the prompt telling
    the model to split slash-separated dishes, we've seen it emit 'Tea/Coffee/
    Milk' or worse. This step:

      * Splits slash / 'or' separated dish_names into multiple ExtractedDish
        entries in the same slot, assigning them a shared `choice_group_id`
        so the Planner knows they're mutually exclusive.
      * Drops dish_names longer than 5 words or containing an underscore
        (those are almost always concat bugs like 'bread_butter_jam_tea...').
      * Applies typo fixes.

    Returns a fresh ExtractedMenu — the original is not mutated.
    """
    from backend.models.menu_extract import ExtractedDish, ExtractedMealSlot

    new_slots: list[ExtractedMealSlot] = []
    for slot in menu.slots:
        new_dishes: list[ExtractedDish] = []
        for dish in slot.dishes:
            names = _clean_dish_name(dish.dish_name)
            # Only assign a choice_group_id when a split actually happened.
            # Standalone dishes stay with choice_group_id=None so the Planner
            # doesn't spuriously group them.
            group_id = (
                _choice_group_id(dish.dish_name, slot.day_of_week, slot.meal)
                if len(names) > 1
                else None
            )
            for name in names:
                new_dishes.append(
                    ExtractedDish(
                        dish_name=name,
                        is_veg=dish.is_veg,
                        confidence=dish.confidence,
                        choice_group_id=group_id,
                    )
                )
        new_slots.append(
            ExtractedMealSlot(day_of_week=slot.day_of_week, meal=slot.meal, dishes=new_dishes)
        )
    return ExtractedMenu(slots=new_slots)


def _choice_group_id(source_name: str, day_of_week: int, meal) -> str:
    """Deterministic id shared by all dishes split out of the same source
    cell. Re-uploading the same PDF produces the same ids so downstream
    diff / idempotency logic stays stable."""
    import hashlib

    meal_str = meal.value if hasattr(meal, "value") else str(meal)
    key = f"{source_name.strip().lower()}|{day_of_week}|{meal_str}"
    return "cg_" + hashlib.md5(key.encode("utf-8")).hexdigest()[:10]


def _clean_dish_name(raw: str) -> list[str]:
    """Return the list of dish_names this string should become. Empty list
    means 'drop this entirely'."""
    name = (raw or "").strip()
    if not name:
        return []
    # Reject obvious concat bugs — LLM sometimes emits underscore_names in
    # dish_name even though the schema is title-cased.
    if "_" in name:
        return []
    # Reject absurdly long names (>5 words) — those are always concat bugs
    # like 'bread butter jam tea coffee milk bournvita sugar fruit'.
    word_count = len(name.split())
    if word_count > 5:
        return []
    # Split slash / 'or' — but preserve ' & ' since that's a real compound
    # marker (e.g. 'Plain & Butter Roti', 'Salt & Pepper Chicken').
    parts = [p.strip() for p in _SPLIT_RE.split(name) if p.strip()]
    if not parts:
        return []
    # Apply typo fixes case-insensitively while preserving output casing.
    fixed: list[str] = []
    for part in parts:
        lower = part.lower()
        fixed.append(_TYPO_FIXES.get(lower, part))
    return fixed


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _parse_json_object(text: str) -> dict:
    """Try direct JSON; strip code fences on failure; final resort raises."""
    text = text.strip()
    for candidate in _json_candidates(text):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise MenuExtractionError(f"LLM output was not JSON: {text[:200]}")


def _json_candidates(text: str):
    yield text
    match = _JSON_FENCE_RE.search(text)
    if match:
        yield match.group(1).strip()
    # Last resort: substring between first '{' and last '}'
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        yield text[start : end + 1]
