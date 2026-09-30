"""HITL endpoints (M5). List + respond to pending requests for the caller."""
# NOTE: no `from __future__ import annotations` — Pydantic v2 struggles with
# PEP-604 unions inside FastAPI response models.

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.auth.dependencies import AuthUser, get_current_auth_user
from backend.db.supabase_client import get_service_role_client
from backend.models.enums import HITLStatus
from backend.tools.hitl import list_pending_for_user, respond_to_hitl

router = APIRouter(prefix="/hitl", tags=["hitl"])
AuthedUser = Annotated[AuthUser, Depends(get_current_auth_user)]


class HITLRequestOut(BaseModel):
    id: int
    agent: str
    question: str
    surface: str
    options: list[dict[str, Any]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class HITLResponseIn(BaseModel):
    status: HITLStatus
    response: Optional[dict[str, Any]] = None


@router.get("/pending", response_model=list[HITLRequestOut])
def list_pending(user: AuthedUser) -> list[HITLRequestOut]:
    svc = get_service_role_client()
    rows = list_pending_for_user(svc, user_id=user.id)
    out: list[HITLRequestOut] = []
    for row in rows:
        q = row["question"]
        surface = ""
        if q.startswith("[") and "]" in q:
            surface = q[1:q.index("]")]
            q = q[q.index("]") + 1:].strip()
        out.append(
            HITLRequestOut(
                id=row["id"],
                agent=row["agent"],
                question=q,
                surface=surface,
                options=row.get("options_json") or [],
                context=row.get("context_json") or {},
                created_at=row["created_at"],
            )
        )
    return out


@router.post("/{hitl_id}/respond")
def respond(hitl_id: int, payload: HITLResponseIn, user: AuthedUser) -> dict[str, Any]:
    svc = get_service_role_client()
    try:
        row = respond_to_hitl(
            svc,
            hitl_id=hitl_id,
            user_id=user.id,
            status=payload.status,
            response=payload.response or {},
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"id": row["id"], "status": row["status"]}
