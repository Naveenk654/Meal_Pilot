"""Admin endpoints — M2 menu intelligence and macro verification.

Every endpoint requires the caller to have public.users.role='admin' (the JWT
role claim is not authoritative — §17, §20 PR2). The upload endpoint delegates
to the Menu Intelligence Agent so the full audit trail flows through agent_runs
even when it's an admin acting manually.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from backend.agents.menu_intel import IngestionResult, MenuIngestionError, ingest_menu_pdf
from backend.auth.dependencies import AuthUser, get_current_auth_user, require_admin_role
from backend.db.supabase_client import get_service_role_client
from backend.models.enums import MacroSource

router = APIRouter(prefix="/admin", tags=["admin"])

AuthedUser = Annotated[AuthUser, Depends(get_current_auth_user)]


def _require_admin(user: AuthUser) -> None:
    """Fetch public.users.role for the caller and enforce admin."""
    svc = get_service_role_client()
    resp = svc.table("users").select("role").eq("id", user.id).single().execute()
    role = (resp.data or {}).get("role", "student")
    require_admin_role(role)


# --- Menu upload ------------------------------------------------------------


class MenuUploadResponse(BaseModel):
    cycle_id: int
    inserted: bool
    idempotency_key: str
    added: int
    unchanged: int
    unknown_dishes_estimated: int
    flagged_for_review: int
    degraded_mode: bool


@router.post("/menu/upload", response_model=MenuUploadResponse)
async def upload_menu(
    user: AuthedUser,
    file: UploadFile = File(...),
    effective_from: date = Form(...),
    effective_to: date = Form(...),
    source: str = Form("pdf"),
) -> MenuUploadResponse:
    _require_admin(user)
    if file.content_type not in {"application/pdf", "application/octet-stream", None}:
        # Accept unknown octet-stream (some clients don't set the right type)
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "expected application/pdf")
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty upload")
    if source not in {"pdf", "email", "manual"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "source must be pdf|email|manual")
    try:
        result: IngestionResult = await ingest_menu_pdf(
            get_service_role_client(),
            pdf_bytes=pdf_bytes,
            effective_from=effective_from,
            effective_to=effective_to,
            source=source,
            admin_user_id=user.id,
        )
    except MenuIngestionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return MenuUploadResponse(**result.__dict__)


# --- Cycle approval ---------------------------------------------------------


class ApproveCycleResponse(BaseModel):
    cycle_id: int
    status: str
    superseded_cycle_ids: list[int] = Field(default_factory=list)


@router.post("/menu/cycles/{cycle_id}/approve", response_model=ApproveCycleResponse)
def approve_cycle(cycle_id: int, user: AuthedUser) -> ApproveCycleResponse:
    """Transition draft → active. Any currently-active cycle for the same
    `source` is set to `superseded`, regardless of the effective_from date.

    A mess has one live menu at a time per source — the previous PDF stops
    being served the moment a new one goes live, no matter what dates the
    admin typed on either upload. The earlier filter also required
    `effective_from` to match, which let two active cycles co-exist when the
    dates differed and caused `resolve_daily_menu` to pick arbitrarily.

    Concurrent approves are still caught by the partial unique index
    `uq_menu_cycles_one_active` — a 23505 surfaces here as HTTP 409.
    """
    _require_admin(user)
    svc = get_service_role_client()
    row = _get_cycle(svc, cycle_id)
    if row["status"] != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, f"cycle status is {row['status']}, expected draft")

    # Supersede every prior active cycle for this source. Exclude the row
    # we're about to activate — belt-and-suspenders in case a caller passes
    # a cycle whose status somehow already reads 'active'.
    prior = (
        svc.table("menu_cycles")
        .select("id")
        .eq("source", row["source"])
        .eq("status", "active")
        .neq("id", cycle_id)
        .execute()
    )
    superseded_ids: list[int] = [r["id"] for r in (prior.data or [])]
    if superseded_ids:
        svc.table("menu_cycles").update({"status": "superseded"}).in_(
            "id", superseded_ids
        ).execute()

    from datetime import datetime, timezone

    svc.table("menu_cycles").update(
        {
            "status": "active",
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "approved_by": user.id,
        }
    ).eq("id", cycle_id).execute()

    # Emit a second `menu_updated` marking the go-live so the Planner knows to
    # replan any downstream users the next time it runs.
    svc.table("menu_events").insert(
        {
            "event_type": "menu_updated",
            "cycle_id": cycle_id,
            "effective_from": row["effective_from"],
            "effective_to": row["effective_to"],
            "payload": {"status": "active", "approved_by": user.id},
        }
    ).execute()
    return ApproveCycleResponse(
        cycle_id=cycle_id, status="active", superseded_cycle_ids=superseded_ids
    )


@router.post("/menu/cycles/{cycle_id}/reject")
def reject_cycle(cycle_id: int, user: AuthedUser) -> dict[str, Any]:
    """Mark a draft cycle as superseded without going live. Its menu_items
    remain queryable for audit; no menu_events event is emitted."""
    _require_admin(user)
    svc = get_service_role_client()
    row = _get_cycle(svc, cycle_id)
    if row["status"] != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, f"cannot reject a {row['status']} cycle")
    svc.table("menu_cycles").update({"status": "superseded"}).eq("id", cycle_id).execute()
    return {"cycle_id": cycle_id, "status": "superseded"}


# --- Cycle listing ----------------------------------------------------------


class CycleSummary(BaseModel):
    id: int
    status: str
    source: str
    effective_from: str
    effective_to: str
    version: int
    content_hash: str
    ingested_at: str
    approved_at: str | None
    pending_review_count: int


@router.get("/menu/pending", response_model=list[CycleSummary])
def list_pending_cycles(user: AuthedUser) -> list[CycleSummary]:
    _require_admin(user)
    return _list_cycles_by_status(status="draft")


@router.get("/menu/active", response_model=list[CycleSummary])
def list_active_cycles(user: AuthedUser) -> list[CycleSummary]:
    _require_admin(user)
    return _list_cycles_by_status(status="active")


def _list_cycles_by_status(*, status: str) -> list[CycleSummary]:
    svc = get_service_role_client()
    resp = (
        svc.table("menu_cycles")
        .select("*")
        .eq("status", status)
        .order("ingested_at", desc=True)
        .execute()
    )
    return [
        CycleSummary(
            id=row["id"],
            status=row["status"],
            source=row["source"],
            effective_from=row["effective_from"],
            effective_to=row["effective_to"],
            version=row["version"],
            content_hash=row["content_hash"],
            ingested_at=row["ingested_at"],
            approved_at=row.get("approved_at"),
            pending_review_count=len(row.get("pending_review_json") or []),
        )
        for row in (resp.data or [])
    ]


class CycleDetail(CycleSummary):
    pending_review_json: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/menu/cycles/{cycle_id}", response_model=CycleDetail)
def get_cycle_detail(cycle_id: int, user: AuthedUser) -> CycleDetail:
    _require_admin(user)
    svc = get_service_role_client()
    row = _get_cycle(svc, cycle_id)
    return CycleDetail(
        id=row["id"],
        status=row["status"],
        source=row["source"],
        effective_from=row["effective_from"],
        effective_to=row["effective_to"],
        version=row["version"],
        content_hash=row["content_hash"],
        ingested_at=row["ingested_at"],
        approved_at=row.get("approved_at"),
        pending_review_count=len(row.get("pending_review_json") or []),
        pending_review_json=row.get("pending_review_json") or [],
    )


# --- Macro verification -----------------------------------------------------


class MacroRow(BaseModel):
    id: int
    dish_name_normalized: str
    serving_unit: str
    serving_grams: float
    kcal: float
    protein_g: float
    carbs_g: float
    fats_g: float
    is_veg: bool
    practical_max_servings_per_day: float
    source: str
    confidence: float
    verified: bool


class MacroVerifyPayload(BaseModel):
    kcal: float | None = Field(default=None, ge=0)
    protein_g: float | None = Field(default=None, ge=0)
    carbs_g: float | None = Field(default=None, ge=0)
    fats_g: float | None = Field(default=None, ge=0)
    serving_unit: str | None = None
    serving_grams: float | None = Field(default=None, gt=0)
    practical_max_servings_per_day: float | None = Field(default=None, gt=0)
    source: MacroSource = MacroSource.MANUAL


@router.get("/macro-db/unverified", response_model=list[MacroRow])
def list_unverified_macros(user: AuthedUser, limit: int = 200) -> list[MacroRow]:
    _require_admin(user)
    svc = get_service_role_client()
    resp = (
        svc.table("macro_db")
        .select("*")
        .eq("verified", False)
        .order("confidence")
        .limit(limit)
        .execute()
    )
    return [MacroRow(**row) for row in (resp.data or [])]


@router.post("/macro-db/{macro_id}/verify", response_model=MacroRow)
def verify_macro(
    macro_id: int, payload: MacroVerifyPayload, user: AuthedUser
) -> MacroRow:
    """Flip verified=true. Admin may override any of the numeric fields at the
    same time; source shifts to manual/ifct/nutritionix to reflect provenance."""
    _require_admin(user)
    svc = get_service_role_client()

    from datetime import datetime, timezone

    updates: dict[str, Any] = {
        "verified": True,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "verified_by": user.id,
        "source": payload.source.value,
        "confidence": 1.0,
    }
    for field_name in (
        "kcal",
        "protein_g",
        "carbs_g",
        "fats_g",
        "serving_unit",
        "serving_grams",
        "practical_max_servings_per_day",
    ):
        val = getattr(payload, field_name)
        if val is not None:
            updates[field_name] = val

    resp = (
        svc.table("macro_db")
        .update(updates)
        .eq("id", macro_id)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "macro_db row not found")
    return MacroRow(**rows[0])


# --- helpers ----------------------------------------------------------------


def _get_cycle(svc, cycle_id: int) -> dict[str, Any]:
    resp = svc.table("menu_cycles").select("*").eq("id", cycle_id).single().execute()
    if not resp.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cycle not found")
    return resp.data
