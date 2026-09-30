"""Idempotent seed loader for macro_db + canteen_items.

Reads `data/mess_macros_seed.json` and `data/canteen_seed.json`, upserts every
row into macro_db by dish_name_normalized. Canteen items link back to the
macro_db row by name.

Rules:
  - Seed rows land as `source='manual'`, `verified=true`, `confidence=1.0`.
    These are the ground-truth macros — LLM estimates only add rows for
    dishes not already present.
  - Re-running the loader on top of admin-verified LLM estimates is safe:
    we upsert on `dish_name_normalized`, and admin-touched rows already have
    verified=true so the upsert only tightens their macros to the seed values.
    If that's undesirable, remove the entry from the JSON before re-running.

Run: `python -m backend.db.seed_macros`
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from postgrest.exceptions import APIError

from backend.db.supabase_client import get_service_role_client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_seeds(root: Path | None = None) -> tuple[int, int]:
    root = root or Path(__file__).resolve().parents[2] / "data"
    mess_path = root / "mess_macros_seed.json"
    canteen_path = root / "canteen_seed.json"

    svc = get_service_role_client()
    mess_count = _seed_macros(svc, json.loads(mess_path.read_text(encoding="utf-8")))
    canteen_count = _seed_canteen(svc, json.loads(canteen_path.read_text(encoding="utf-8")))
    return mess_count, canteen_count


def _seed_macros(svc, rows: list[dict]) -> int:
    count = 0
    now = _now_iso()
    for row in rows:
        body = {
            **row,
            "source": "manual",
            "confidence": 1.0,
            "verified": True,
            "verified_at": now,
        }
        svc.table("macro_db").upsert(body, on_conflict="dish_name_normalized").execute()
        count += 1
    return count


def _seed_canteen(svc, rows: list[dict]) -> int:
    count = 0
    now = _now_iso()
    macro_ids = _macro_id_by_name(svc)
    for row in rows:
        macro = row["macro"]
        normalized = macro["dish_name_normalized"]
        if normalized not in macro_ids:
            # Insert the macro first so we can link to it.
            body = {
                **macro,
                "source": "manual",
                "confidence": 1.0,
                "verified": True,
                "verified_at": now,
            }
            try:
                inserted = svc.table("macro_db").insert(body).execute()
                macro_ids[normalized] = inserted.data[0]["id"]
            except APIError as exc:
                if getattr(exc, "code", None) != "23505":
                    raise
                # concurrent insert — refetch id
                macro_ids = _macro_id_by_name(svc)

        canteen_body = {
            "shop_name": row["shop_name"],
            "dish": row["dish"],
            "price_inr": row["price_inr"],
            "is_veg": row["is_veg"],
            "macro_id": macro_ids.get(normalized),
            "available_hours": row["available_hours"],
            "practical_max_servings_per_day": row["practical_max_servings_per_day"],
        }
        # canteen_items has no natural unique key; delete-then-insert per
        # (shop_name, dish) so re-runs stay idempotent.
        svc.table("canteen_items").delete().eq("shop_name", row["shop_name"]).eq(
            "dish", row["dish"]
        ).execute()
        svc.table("canteen_items").insert(canteen_body).execute()
        count += 1
    return count


def _macro_id_by_name(svc) -> dict[str, int]:
    resp = svc.table("macro_db").select("id, dish_name_normalized").execute()
    return {row["dish_name_normalized"]: row["id"] for row in (resp.data or [])}


if __name__ == "__main__":
    mess, canteen = load_seeds()
    print(f"seeded macros={mess}, canteen items={canteen}")
