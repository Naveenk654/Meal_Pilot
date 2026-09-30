"""Weekly Learning cron entry (§6.3).

Two ways to invoke:
  * `python -m backend.crons.weekly_learning` — runs for every user with logs.
    Railway cron config points at this.
  * FastAPI POST /learning/run — per-user manual trigger from the UI.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from supabase import Client

from backend.agents.learning import LearningOutcome, run_weekly_learning
from backend.auth.dependencies import AuthUser, get_current_auth_user
from backend.db.supabase_client import get_service_role_client
from backend.tools.memory import approve_fact, reject_fact

router = APIRouter(prefix="/learning", tags=["learning"])
AuthedUser = Annotated[AuthUser, Depends(get_current_auth_user)]


class RunLearningRequest(BaseModel):
    date: Optional[date] = None


class RunLearningResponse(BaseModel):
    inserted: bool
    proposed_count: int
    retired_stale_count: int
    hitl_created: bool
    idempotency_key: str


@router.post("/run", response_model=RunLearningResponse)
async def run_learning_endpoint(
    payload: RunLearningRequest, user: AuthedUser
) -> RunLearningResponse:
    svc = get_service_role_client()
    outcome = await run_weekly_learning(svc, user_id=user.id, on_date=payload.date)
    return RunLearningResponse(**outcome.__dict__)


class MemoryDecision(BaseModel):
    memory_id: int
    decision: str  # 'approve' | 'reject'


@router.post("/memory/decide")
def decide_memory(payload: MemoryDecision, user: AuthedUser) -> dict[str, str]:
    svc = get_service_role_client()
    try:
        if payload.decision == "approve":
            approve_fact(svc, memory_id=payload.memory_id, user_id=user.id)
        elif payload.decision == "reject":
            reject_fact(svc, memory_id=payload.memory_id, user_id=user.id)
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "decision must be approve|reject")
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"memory_id": str(payload.memory_id), "decision": payload.decision}


async def _run_for_all_users(svc: Client, on_date: date | None = None) -> list[LearningOutcome]:
    users = svc.table("user_profile").select("user_id").execute().data or []
    outcomes: list[LearningOutcome] = []
    for u in users:
        try:
            outcomes.append(await run_weekly_learning(svc, user_id=u["user_id"], on_date=on_date))
        except Exception as exc:
            print(f"user {u['user_id']}: {type(exc).__name__}: {exc}")
    return outcomes


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, help="YYYY-MM-DD; defaults to today")
    args = parser.parse_args()
    the_date = date.fromisoformat(args.date) if args.date else None
    svc = get_service_role_client()
    outcomes = asyncio.run(_run_for_all_users(svc, on_date=the_date))
    for o in outcomes:
        print(f"  proposed={o.proposed_count} retired_stale={o.retired_stale_count} hitl={o.hitl_created}")
