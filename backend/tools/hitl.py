"""HITL request writer + reader (§17 hitl_requests, §14 I6).

Six HITL surfaces from the spec:
  1. Low overall confidence — "we're not sure about this plan, approve?"
  2. Menu uncertain — "no active menu for today, use last known?"
  3. Constraint infeasible — "no plan fits your rules, override?"
  4. Canteen fallback proposal — "mess can't hit your macros, add these canteen items?"
  6. Macro estimation review — admin-surface (M2)
  7. Plan approval — routine "does this plan look good?"

Surface 5 is behavioral memory review (M6).
"""
# NOTE: no `from __future__ import annotations` — Pydantic v2 evaluates
# response_model annotations eagerly.

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Optional

from postgrest.exceptions import APIError
from supabase import Client

from backend.models.enums import HITLStatus


class HITLSurface:
    LOW_CONFIDENCE = "low_confidence"
    MENU_UNCERTAIN = "menu_uncertain"
    CONSTRAINT_INFEASIBLE = "constraint_infeasible"
    FALLBACK_PROPOSAL = "fallback_proposal"
    WEEKLY_REVIEW = "weekly_review"
    MACRO_REVIEW = "macro_review"
    PLAN_APPROVAL = "plan_approval"


def create_hitl_request(
    svc: Client,
    *,
    user_id: str,
    agent: str,
    surface: str,
    question: str,
    options: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert a pending hitl_requests row. Returns the created row.

    We treat surface as part of `question` (prefix) so the schema stays as-is
    and the UI can group by surface without an additional column.
    """
    body = {
        "user_id": user_id,
        "agent": agent,
        "question": f"[{surface}] {question}",
        "options_json": options or [],
        "context_json": context or {},
        "status": HITLStatus.PENDING.value,
    }
    resp = svc.table("hitl_requests").insert(body).execute()
    return resp.data[0]


def list_pending_for_user(svc: Client, *, user_id: str) -> list[dict[str, Any]]:
    resp = (
        svc.table("hitl_requests")
        .select("*")
        .eq("user_id", user_id)
        .eq("status", HITLStatus.PENDING.value)
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data or []


def respond_to_hitl(
    svc: Client,
    *,
    hitl_id: int,
    user_id: str,
    status: HITLStatus,
    response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a user response. Idempotent per (id, response_hash) via §14 I6.

    status must be APPROVED / REJECTED / EDITED / EXPIRED.
    """
    if status is HITLStatus.PENDING:
        raise ValueError("respond_to_hitl cannot set status back to pending")
    payload = response or {}
    resp_hash = sha256(
        _canonicalize(payload).encode("utf-8")
    ).hexdigest()
    updates = {
        "status": status.value,
        "response_json": payload,
        "response_hash": resp_hash,
        "responded_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = (
            svc.table("hitl_requests")
            .update(updates)
            .eq("id", hitl_id)
            .eq("user_id", user_id)
            .execute()
        )
    except APIError:
        raise
    rows = resp.data or []
    if not rows:
        raise LookupError(f"hitl_request {hitl_id} not found for user {user_id}")
    return rows[0]


def _canonicalize(payload: Any) -> str:
    import json
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
