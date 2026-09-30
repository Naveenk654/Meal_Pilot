"""Supplement auto-counting.

Design: onboarding stores each supplement as a `user_preferences` row with
kind='supplement', value=<normalized name>. When we need consumed_macros for
today, we ADD the daily supplement macros to whatever the meal_logs sum to.
This means user doesn't have to log 'took whey today' every day — it's
assumed. If they didn't take it, they can log a 'skip' event later (M6+).

Idempotent: keyed by lookup, not by insert. We compute at read time.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from supabase import Client

from backend.models.enums import PreferenceKind, PreferenceStatus
from backend.models.nutrition import Macros


_SEED_PATH = Path(__file__).resolve().parents[2] / "data" / "supplements_seed.json"


@lru_cache
def _load_seed() -> dict[str, dict[str, Any]]:
    if not _SEED_PATH.exists():
        return {}
    return json.loads(_SEED_PATH.read_text(encoding="utf-8"))


def _normalize(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def match_supplement(user_value: str) -> tuple[str, dict[str, Any]] | None:
    """Best-effort match: exact key, then substring. Returns (key, spec) or None."""
    seed = _load_seed()
    key = _normalize(user_value)
    if key in seed:
        return key, seed[key]
    for k in seed:
        if k in key or key in k:
            return k, seed[k]
    return None


def compute_daily_supplement_macros(
    svc: Client, *, user_id: str
) -> tuple[Macros, list[dict[str, Any]]]:
    """Sum daily macros contributed by the user's active supplement prefs.

    Returns (macros, breakdown) — breakdown is a list of {name, servings, kcal, protein_g}
    so the UI can show what got counted."""
    resp = (
        svc.table("user_preferences")
        .select("value, status")
        .eq("user_id", user_id)
        .eq("kind", PreferenceKind.SUPPLEMENT.value)
        .eq("status", PreferenceStatus.ACTIVE.value)
        .execute()
    )
    # Dedupe by matched key — user_preferences can hold duplicate rows if
    # onboarding is resubmitted (that endpoint inserts, doesn't upsert).
    seen_keys: set[str] = set()
    total = Macros.zero()
    breakdown: list[dict[str, Any]] = []
    for row in resp.data or []:
        matched = match_supplement(row["value"])
        if not matched:
            continue
        key, spec = matched
        if key in seen_keys:
            continue
        seen_keys.add(key)
        servings = float(spec["servings_per_day"])
        macros = Macros(
            kcal=float(spec["kcal_per_serving"]) * servings,
            protein_g=float(spec["protein_g_per_serving"]) * servings,
            carbs_g=float(spec["carbs_g_per_serving"]) * servings,
            fats_g=float(spec["fats_g_per_serving"]) * servings,
        )
        total = total + macros
        breakdown.append(
            {
                "supplement": key,
                "display_name": spec["display_name"],
                "servings_per_day": servings,
                "kcal": macros.kcal,
                "protein_g": macros.protein_g,
                "carbs_g": macros.carbs_g,
                "fats_g": macros.fats_g,
            }
        )
    return total, breakdown
