"""Persistence + lifecycle for daily_plans (§13, §14 TX2).

`commit_plan` is the sole entrypoint the Planner uses to write a final plan.
Semantics:
  1. Fetch the currently `sent` plan for (user_id, date), if any.
  2. Mark it `superseded` via an `if_version` guard — this is the transaction
     boundary that stops two concurrent replans from stomping each other.
  3. Insert the new plan directly in `sent` state (a draft that was already
     validated by the Constraint Engine has nothing more to gate — the
     draft→validated→sent chain collapses in one insert as long as we record
     the states in `extra` for the trace).

If the version guard fails, we raise `ConcurrentReplanError`. The Planner
treats that as a soft failure — a newer plan wrote first, keep it, drop ours.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from supabase import Client

from backend.idempotency.writer import update_plan_with_version_guard
from backend.models.enums import PlanStatus
from backend.models.plan import ConfidenceBreakdown, Plan, ValidationResult
from backend.models.nutrition import Macros


class ConcurrentReplanError(RuntimeError):
    """A newer plan wrote first — this replan lost the race and must abort."""


@dataclass(frozen=True)
class CommitResult:
    plan_id: int
    superseded_plan_id: int | None
    plan_row: dict[str, Any]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_current_sent(client: Client, user_id: str, plan_date: date) -> dict | None:
    resp = (
        client.table("daily_plans")
        .select("*")
        .eq("user_id", user_id)
        .eq("date", plan_date.isoformat())
        .eq("status", PlanStatus.SENT.value)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def commit_plan(
    client: Client,
    *,
    user_id: str,
    plan_date: date,
    entries_json: list[dict[str, Any]],
    target_macros: Macros,
    validation_result: ValidationResult,
    confidence: float,
    confidence_factors: ConfidenceBreakdown,
    degraded_mode: bool = False,
    cycle_id_used: int | None = None,
) -> CommitResult:
    """Transactional commit — supersede prior sent plan, insert the new one."""
    prior = _fetch_current_sent(client, user_id, plan_date)
    superseded_id: int | None = None
    now = _utcnow_iso()

    if prior is not None:
        updated = update_plan_with_version_guard(
            client,
            plan_id=prior["id"],
            expected_version=int(prior["version"]),
            fields={
                "status": PlanStatus.SUPERSEDED.value,
                "superseded_at": now,
            },
        )
        if updated is None:
            raise ConcurrentReplanError(
                f"prior plan {prior['id']} version guard failed — another replan won"
            )
        superseded_id = prior["id"]

    body = {
        "user_id": user_id,
        "date": plan_date.isoformat(),
        "plan_json": entries_json,
        "target_macros_json": target_macros.model_dump(),
        "validation_result_json": validation_result.model_dump(),
        "confidence": confidence,
        "confidence_factors": confidence_factors.model_dump(),
        "degraded_mode": degraded_mode,
        "status": PlanStatus.SENT.value,
        "supersedes_id": superseded_id,
        "cycle_id_used": cycle_id_used,
        "sent_at": now,
        "version": 1,
    }
    inserted = client.table("daily_plans").insert(body).execute()
    row = inserted.data[0]
    return CommitResult(plan_id=row["id"], superseded_plan_id=superseded_id, plan_row=row)


def fetch_today_plan(client: Client, *, user_id: str, plan_date: date) -> Plan | None:
    """Convenience for the router. Returns the current `sent` plan or None."""
    row = _fetch_current_sent(client, user_id, plan_date)
    if row is None:
        return None
    return Plan(
        id=row["id"],
        user_id=row["user_id"],
        date=date.fromisoformat(row["date"]),
        entries=row["plan_json"],
        target_macros=row["target_macros_json"],
        validation_result=row["validation_result_json"],
        confidence=float(row["confidence"]),
        confidence_factors=row["confidence_factors"],
        degraded_mode=bool(row["degraded_mode"]),
        status=PlanStatus(row["status"]),
        supersedes_id=row.get("supersedes_id"),
        cycle_id_used=row.get("cycle_id_used"),
        created_at=row.get("created_at"),
        sent_at=row.get("sent_at"),
    )
