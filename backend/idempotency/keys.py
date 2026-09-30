"""Idempotency key generators (§14 I1-I6). Pure functions. Deterministic.

The DB UNIQUE constraints (see 0006/0007/0008 migrations, §14 I7) are the last line of
defense. These generators just produce the strings the constraints test against.
"""
from __future__ import annotations

import json
from datetime import date
from hashlib import sha256
from typing import Any

from backend.models.enums import Trigger


def morning_cron_key(user_id: str, plan_date: date) -> str:
    """I1: (user_id, date, trigger='morning_cron')."""
    return f"morning_cron:{user_id}:{plan_date.isoformat()}"


def planner_run_key(
    user_id: str,
    plan_date: date,
    trigger: Trigger,
    triggering_event_id: str | None,
) -> str:
    """I3: (user_id, date, trigger, triggering_event_id).

    Missing triggering_event_id is rendered as literal 'none' so the key is total.
    """
    tev = triggering_event_id or "none"
    return f"planner:{user_id}:{plan_date.isoformat()}:{trigger.value}:{tev}"


def weekly_learning_key(user_id: str, iso_year: int, iso_week: int) -> str:
    """I5: (user_id, iso_week)."""
    return f"learning:{user_id}:{iso_year:04d}W{iso_week:02d}"


def menu_ingestion_key(content_hash: str, effective_from: date, source: str) -> str:
    """I4: (content_hash, effective_from, source). Enforced directly by the composite UNIQUE
    on public.menu_cycles — this string form is only for the agent_run key."""
    return f"menu:{source}:{effective_from.isoformat()}:{content_hash}"


def _canonicalize_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def content_hash_of(payload: bytes | str) -> str:
    """Content hash for menu PDFs (§6.1) and other blob-idempotency inputs."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return sha256(payload).hexdigest()


def hitl_response_hash(response_json: Any) -> str:
    """I6: hash a canonical JSON representation of the HITL response."""
    return sha256(_canonicalize_json(response_json).encode("utf-8")).hexdigest()
