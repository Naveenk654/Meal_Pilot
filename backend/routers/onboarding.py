"""Onboarding endpoints (§21).

POST /onboarding — accept the seven-step form, run deterministic nutrition math,
mirror the auth.users row into public.users (§17 mirror pattern), write user_profile
and onboarding preferences under RLS.

GET /me/profile — return the caller's stored profile, RLS-enforced.

No LLM here. Nutrition math is authoritative (§10). Bad inputs raise
UnsafePlanningInputError (§20 S5), returned as HTTP 400.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.auth.dependencies import AuthUser, get_current_auth_user
from backend.config import get_settings
from backend.db.supabase_client import get_service_role_client, get_user_client
from backend.models.enums import (
    ActivityLevel,
    Gender,
    Goal,
    PlanMode,
    PreferenceAuthority,
    PreferenceKind,
    PreferenceSource,
    UserMode,
)
from backend.tools.nutrition import (
    UnsafePlanningInputError,
    WEIGHT_RECOMPUTE_THRESHOLD_KG,
    compute_profile_targets,
)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class OnboardingRequest(BaseModel):
    """§21 fields. UI collects; backend computes targets deterministically."""

    age: int = Field(ge=10, le=100)
    gender: Gender
    height_cm: float = Field(ge=100, le=250)
    weight_kg: float = Field(ge=30, le=250)
    activity_level: ActivityLevel
    goal: Goal
    mode: UserMode
    veg_default: bool = True
    allergies: list[str] = Field(default_factory=list)      # §20 S2 fixed list
    dislikes: list[str] = Field(default_factory=list)
    restrictions: list[str] = Field(default_factory=list)   # dietary restrictions
    supplements: list[str] = Field(default_factory=list)
    budget_soft_inr: float = Field(ge=0)
    budget_hard_inr: float | None = Field(default=None, ge=0)


class OnboardingResponse(BaseModel):
    bmr: float
    tdee: float
    target_kcal: float
    target_protein_g: float
    target_carbs_g: float
    target_fats_g: float


class ProfileResponse(OnboardingResponse):
    age: int
    gender: Gender
    height_cm: float
    weight_kg: float
    activity_level: ActivityLevel
    goal: Goal
    mode: UserMode
    veg_default: bool
    budget_soft_inr: float
    budget_hard_inr: float | None


AuthedUser = Annotated[AuthUser, Depends(get_current_auth_user)]


@router.post("", response_model=OnboardingResponse)
def create_onboarding(payload: OnboardingRequest, user: AuthedUser) -> OnboardingResponse:
    """Compute targets, mirror public.users, upsert user_profile + onboarding preferences.

    Returns the computed targets so the UI can render the calibration screen (§21 step 7).
    """
    try:
        pt = compute_profile_targets(
            age=payload.age,
            gender=payload.gender,
            height_cm=payload.height_cm,
            weight_kg=payload.weight_kg,
            activity_level=payload.activity_level,
            goal=payload.goal,
            mode=payload.mode,
        )
    except UnsafePlanningInputError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    settings = get_settings()
    if not user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="JWT missing email claim; cannot mirror user row.",
        )

    # Mirror the auth.users row into public.users (RLS-bypass via service role).
    svc = get_service_role_client()
    svc.table("users").upsert(
        {
            "id": user.id,
            "email": user.email,
            "timezone": settings.default_timezone,
            "mode": payload.mode.value,
        },
        on_conflict="id",
    ).execute()

    # From here on we use the user client so RLS is exercised.
    user_client = get_user_client(user.jwt)

    user_client.table("user_profile").upsert(
        {
            "user_id": user.id,
            "age": payload.age,
            "gender": payload.gender.value,
            "height_cm": payload.height_cm,
            "weight_kg": payload.weight_kg,
            "activity_level": payload.activity_level.value,
            "goal": payload.goal.value,
            "veg_default": payload.veg_default,
            "budget_soft_inr": payload.budget_soft_inr,
            "budget_hard_inr": payload.budget_hard_inr,
            "bmr": pt.bmr,
            "tdee": pt.tdee,
            "target_kcal": pt.targets.kcal,
            "target_protein_g": pt.targets.protein_g,
            "target_carbs_g": pt.targets.carbs_g,
            "target_fats_g": pt.targets.fats_g,
        },
        on_conflict="user_id",
    ).execute()

    _write_onboarding_preferences(user_client, user.id, payload)

    return OnboardingResponse(
        bmr=pt.bmr,
        tdee=pt.tdee,
        target_kcal=pt.targets.kcal,
        target_protein_g=pt.targets.protein_g,
        target_carbs_g=pt.targets.carbs_g,
        target_fats_g=pt.targets.fats_g,
    )


def _write_onboarding_preferences(
    client, user_id: str, payload: OnboardingRequest
) -> None:
    """Replace onboarding-sourced preferences on every submit.

    Users tweak the form and resubmit; we must NOT accumulate duplicate rows
    every time. Only rows where source='onboarding' get wiped — anything the
    Learning Agent proposed (source='learning') survives.
    """
    client.table("user_preferences").delete().eq("user_id", user_id).eq(
        "source", PreferenceSource.ONBOARDING.value
    ).execute()
    rows: list[dict] = []
    for value in payload.allergies:
        rows.append(_pref_row(user_id, PreferenceKind.ALLERGY, value, PreferenceAuthority.HARD))
    for value in payload.restrictions:
        rows.append(_pref_row(user_id, PreferenceKind.RESTRICTION, value, PreferenceAuthority.HARD))
    for value in payload.dislikes:
        rows.append(_pref_row(user_id, PreferenceKind.DISLIKE, value, PreferenceAuthority.SOFT))
    for value in payload.supplements:
        rows.append(_pref_row(user_id, PreferenceKind.SUPPLEMENT, value, PreferenceAuthority.SOFT))
    if rows:
        client.table("user_preferences").insert(rows).execute()


def _pref_row(user_id: str, kind: PreferenceKind, value: str, authority: PreferenceAuthority) -> dict:
    return {
        "user_id": user_id,
        "kind": kind.value,
        "value": value.strip().lower(),
        "source": PreferenceSource.ONBOARDING.value,
        "authority": authority.value,
        "confidence": 1.0,
    }


class PreferencesEditRequest(BaseModel):
    allergies: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    restrictions: list[str] = Field(default_factory=list)
    supplements: list[str] = Field(default_factory=list)
    # Optional profile-level fields. When None, we leave the stored value
    # untouched — the client only sends what the user actually changed.
    # Explicit `Optional[...]`: `X | None` combined with `from __future__
    # import annotations` at the top of this file trips Pydantic v2 into
    # silently dropping the field on validation. Same trap as planner.py.
    plan_mode: Optional[PlanMode] = None
    budget_soft_inr: Optional[float] = Field(default=None, ge=0)


class PreferencesResponse(BaseModel):
    allergies: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    restrictions: list[str] = Field(default_factory=list)
    supplements: list[str] = Field(default_factory=list)
    # Profile-level fields surfaced alongside preferences so the UI can
    # render one editor for everything the user tweaks day-to-day.
    plan_mode: PlanMode = PlanMode.MESS_ONLY
    budget_soft_inr: float = 0.0


@router.get("/me/preferences", response_model=PreferencesResponse)
def get_my_preferences(user: AuthedUser) -> PreferencesResponse:
    user_client = get_user_client(user.jwt)
    rows = (
        user_client.table("user_preferences")
        .select("kind, value, status, source")
        .eq("user_id", user.id)
        .eq("status", "active")
        .execute()
        .data
        or []
    )
    buckets: dict = {"allergies": [], "dislikes": [], "restrictions": [], "supplements": []}
    kind_map = {
        "allergy": "allergies",
        "dislike": "dislikes",
        "restriction": "restrictions",
        "supplement": "supplements",
    }
    for r in rows:
        bucket = kind_map.get(r["kind"])
        if bucket is not None:
            buckets[bucket].append(r["value"])

    # Read plan_mode + budget_soft_inr from user_profile. Absent profile means
    # not-yet-onboarded — fall back to defaults so the endpoint still returns
    # something coherent.
    prof = (
        user_client.table("user_profile")
        .select("plan_mode, budget_soft_inr")
        .eq("user_id", user.id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if prof:
        buckets["plan_mode"] = PlanMode(prof[0].get("plan_mode") or "mess_only")
        buckets["budget_soft_inr"] = float(prof[0].get("budget_soft_inr") or 0.0)
    return PreferencesResponse(**buckets)


@router.put("/me/preferences", response_model=PreferencesResponse)
def edit_my_preferences(
    payload: PreferencesEditRequest, user: AuthedUser
) -> PreferencesResponse:
    """Atomically replace the user's onboarding-source preferences. Learning-
    source (`source='learning'`) rows are preserved so approved behavioral
    facts survive an explicit edit. plan_mode / budget_soft_inr on the profile
    are only touched when the caller explicitly sends new values."""
    user_client = get_user_client(user.jwt)
    # Duck-typing: `_write_onboarding_preferences` only reads the four list
    # attributes off the payload object. Passing `PreferencesEditRequest`
    # directly avoids the fake-OnboardingRequest hack that 500'd on Pydantic
    # v2 due to the `age >= 10` constraint.
    _write_onboarding_preferences(user_client, user.id, payload)

    # Patch plan_mode / budget_soft_inr on user_profile if the caller sent
    # them. Missing keys are a partial update — we do not zero out the row.
    profile_patch: dict = {}
    if payload.plan_mode is not None:
        profile_patch["plan_mode"] = payload.plan_mode.value
    if payload.budget_soft_inr is not None:
        profile_patch["budget_soft_inr"] = payload.budget_soft_inr
    if profile_patch:
        user_client.table("user_profile").update(profile_patch).eq(
            "user_id", user.id
        ).execute()

    return get_my_preferences(user)


class WeightLogRequest(BaseModel):
    weight_kg: float = Field(ge=30, le=250)
    note: str | None = None


class WeightLogResponse(BaseModel):
    log_id: int
    weight_kg: float
    recomputed: bool
    target_kcal: float
    target_protein_g: float


@router.post("/me/weight", response_model=WeightLogResponse)
def log_weight(payload: WeightLogRequest, user: AuthedUser) -> WeightLogResponse:
    """Record a fresh weight; if it moved > 1 kg from the profile's stored
    weight_kg, recompute BMR/TDEE/macro targets and update user_profile.

    Uses the service role for the profile update so the recompute is atomic
    with the log write from the caller's point of view."""
    svc = get_service_role_client()

    prof = (
        svc.table("user_profile").select("*").eq("user_id", user.id).single().execute().data
    )
    if prof is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Onboard first.")

    # Insert the weight_log row.
    log = (
        svc.table("weight_logs")
        .insert({"user_id": user.id, "weight_kg": payload.weight_kg, "note": payload.note})
        .execute()
        .data[0]
    )

    old_weight = float(prof["weight_kg"])
    recomputed = False
    target_kcal = float(prof["target_kcal"])
    target_protein_g = float(prof["target_protein_g"])

    if abs(payload.weight_kg - old_weight) > WEIGHT_RECOMPUTE_THRESHOLD_KG:
        user_row = svc.table("users").select("mode").eq("id", user.id).single().execute().data
        try:
            pt = compute_profile_targets(
                age=int(prof["age"]),
                gender=Gender(prof["gender"]),
                height_cm=float(prof["height_cm"]),
                weight_kg=payload.weight_kg,
                activity_level=ActivityLevel(prof["activity_level"]),
                goal=Goal(prof["goal"]),
                mode=UserMode(user_row["mode"]),
            )
        except UnsafePlanningInputError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        svc.table("user_profile").update(
            {
                "weight_kg": payload.weight_kg,
                "bmr": pt.bmr,
                "tdee": pt.tdee,
                "target_kcal": pt.targets.kcal,
                "target_protein_g": pt.targets.protein_g,
                "target_carbs_g": pt.targets.carbs_g,
                "target_fats_g": pt.targets.fats_g,
            }
        ).eq("user_id", user.id).execute()
        target_kcal = pt.targets.kcal
        target_protein_g = pt.targets.protein_g
        recomputed = True

    return WeightLogResponse(
        log_id=log["id"],
        weight_kg=payload.weight_kg,
        recomputed=recomputed,
        target_kcal=target_kcal,
        target_protein_g=target_protein_g,
    )


@router.get("/me/role")
def read_my_role(user: AuthedUser) -> dict[str, str]:
    """Return the caller's role from public.users. Used by the frontend to
    decide whether to show admin tabs."""
    svc = get_service_role_client()
    row = svc.table("users").select("role").eq("id", user.id).single().execute().data
    return {"role": (row or {}).get("role", "student")}


@router.get("/me", response_model=ProfileResponse)
def read_my_profile(user: AuthedUser) -> ProfileResponse:
    user_client = get_user_client(user.jwt)
    resp = (
        user_client.table("user_profile")
        .select("*")
        .eq("user_id", user.id)
        .single()
        .execute()
    )
    row = resp.data
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    return ProfileResponse(
        bmr=row["bmr"],
        tdee=row["tdee"],
        target_kcal=row["target_kcal"],
        target_protein_g=row["target_protein_g"],
        target_carbs_g=row["target_carbs_g"],
        target_fats_g=row["target_fats_g"],
        age=row["age"],
        gender=Gender(row["gender"]),
        height_cm=float(row["height_cm"]),
        weight_kg=float(row["weight_kg"]),
        activity_level=ActivityLevel(row["activity_level"]),
        goal=Goal(row["goal"]),
        mode=UserMode(_infer_mode_from_users_row(get_service_role_client(), user.id)),
        veg_default=row["veg_default"],
        budget_soft_inr=float(row["budget_soft_inr"]),
        budget_hard_inr=float(row["budget_hard_inr"]) if row["budget_hard_inr"] is not None else None,
    )


def _infer_mode_from_users_row(svc, user_id: str) -> str:
    resp = svc.table("users").select("mode").eq("id", user_id).single().execute()
    return resp.data["mode"]
