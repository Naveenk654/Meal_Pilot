"""Behavioral memory lifecycle (§7 retirement policy).

user_memory_behavioral rows go through proposed → active → retired. Only the
Learning Agent proposes; only the HITL surface promotes; the Planner reads
`active` rows and folds them into candidate scoring as an ADVISORY signal.

Invariants:
  * Hard constraints (allergies, restrictions, veg toggle) NEVER yield to
    behavioral memory. Callers are responsible for keeping memory advisory
    only — this module doesn't enforce that.
  * Auto-retirement triggers: contradiction_count >= 2, OR
    last_confirmed_at older than MEMORY_STALE_DAYS.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from supabase import Client

MEMORY_STALE_DAYS = 30


@dataclass(frozen=True)
class MemoryWriteResult:
    id: int
    status: str
    inserted: bool


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def propose_fact(
    svc: Client,
    *,
    user_id: str,
    fact: str,
    evidence: dict[str, Any],
) -> MemoryWriteResult:
    """Insert a proposed fact. Duplicates (same fact text for the same user)
    are treated as re-confirmations — bumps last_confirmed_at instead of
    inserting a second row."""
    existing = (
        svc.table("user_memory_behavioral")
        .select("id, status")
        .eq("user_id", user_id)
        .eq("fact", fact)
        .limit(1)
        .execute()
        .data
    )
    if existing:
        row = existing[0]
        svc.table("user_memory_behavioral").update(
            {"last_confirmed_at": _utcnow_iso(), "evidence": evidence}
        ).eq("id", row["id"]).execute()
        return MemoryWriteResult(id=row["id"], status=row["status"], inserted=False)

    resp = (
        svc.table("user_memory_behavioral")
        .insert(
            {
                "user_id": user_id,
                "fact": fact,
                "evidence": evidence,
                "status": "proposed",
            }
        )
        .execute()
    )
    row = resp.data[0]
    return MemoryWriteResult(id=row["id"], status=row["status"], inserted=True)


def approve_fact(svc: Client, *, memory_id: int, user_id: str) -> dict[str, Any]:
    """proposed → active."""
    resp = (
        svc.table("user_memory_behavioral")
        .update({"status": "active", "last_confirmed_at": _utcnow_iso()})
        .eq("id", memory_id)
        .eq("user_id", user_id)
        .eq("status", "proposed")
        .execute()
    )
    rows = resp.data or []
    if not rows:
        raise LookupError(f"proposed memory {memory_id} not found for user {user_id}")
    return rows[0]


def reject_fact(svc: Client, *, memory_id: int, user_id: str) -> dict[str, Any]:
    """proposed → retired (rejected before ever going active)."""
    resp = (
        svc.table("user_memory_behavioral")
        .update({"status": "retired", "retired_at": _utcnow_iso()})
        .eq("id", memory_id)
        .eq("user_id", user_id)
        .eq("status", "proposed")
        .execute()
    )
    rows = resp.data or []
    if not rows:
        raise LookupError(f"proposed memory {memory_id} not found for user {user_id}")
    return rows[0]


def note_contradiction(svc: Client, *, memory_id: int) -> dict[str, Any] | None:
    """Increment contradiction_count. If it reaches 2, auto-retire.
    Returns the updated row or None if not found."""
    row = (
        svc.table("user_memory_behavioral")
        .select("*")
        .eq("id", memory_id)
        .single()
        .execute()
        .data
    )
    if row is None or row["status"] != "active":
        return row
    new_count = int(row["contradiction_count"]) + 1
    updates: dict[str, Any] = {"contradiction_count": new_count}
    if new_count >= 2:
        updates["status"] = "retired"
        updates["retired_at"] = _utcnow_iso()
    resp = (
        svc.table("user_memory_behavioral")
        .update(updates)
        .eq("id", memory_id)
        .execute()
    )
    return (resp.data or [None])[0]


def retire_stale_facts(svc: Client, *, user_id: str) -> int:
    """Retire any active facts whose last_confirmed_at is older than
    MEMORY_STALE_DAYS. Returns the count retired.

    Called from the weekly Learning Agent so retirement stays deterministic
    and periodic — the Planner never mutates memory."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=MEMORY_STALE_DAYS)
    ).isoformat()
    resp = (
        svc.table("user_memory_behavioral")
        .update({"status": "retired", "retired_at": _utcnow_iso()})
        .eq("user_id", user_id)
        .eq("status", "active")
        .lt("last_confirmed_at", cutoff)
        .execute()
    )
    return len(resp.data or [])


def load_active_facts(svc: Client, *, user_id: str) -> list[dict[str, Any]]:
    resp = (
        svc.table("user_memory_behavioral")
        .select("id, fact, evidence, first_seen, last_confirmed_at")
        .eq("user_id", user_id)
        .eq("status", "active")
        .execute()
    )
    return resp.data or []
