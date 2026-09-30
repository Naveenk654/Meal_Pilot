"""Retry-safe insert helpers.

The DB is the authority (§14 I7). Every helper attempts INSERT; on UNIQUE violation
(SQLSTATE 23505) it fetches and returns the existing row. Callers see the returned
row plus a boolean saying whether they were the one who created it.
"""
from __future__ import annotations

from typing import Any

from postgrest.exceptions import APIError
from supabase import Client

# PostgreSQL "unique_violation"
PG_UNIQUE_VIOLATION_CODE = "23505"


def _is_unique_violation(exc: APIError) -> bool:
    return getattr(exc, "code", None) == PG_UNIQUE_VIOLATION_CODE


def insert_agent_run(
    client: Client, *, idempotency_key: str, payload: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Insert public.agent_runs row keyed on idempotency_key (§14 I1/I3/I5).

    Returns (row, inserted). inserted=False means a prior run for this key exists;
    the caller should return the existing outcome and NOT re-run the agent.
    """
    body = {**payload, "idempotency_key": idempotency_key}
    try:
        resp = client.table("agent_runs").insert(body).execute()
        return resp.data[0], True
    except APIError as exc:
        if not _is_unique_violation(exc):
            raise
    existing = (
        client.table("agent_runs")
        .select("*")
        .eq("idempotency_key", idempotency_key)
        .single()
        .execute()
    )
    return existing.data, False


def insert_meal_log(
    client: Client, *, event_id: str, payload: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Insert public.meal_logs row keyed on event_id (§14 I2).

    meal_logs is append-only (§13 A10). Duplicate event_id returns the existing row
    unchanged — never an UPDATE (the trigger blocks that anyway).
    """
    body = {**payload, "event_id": event_id}
    try:
        resp = client.table("meal_logs").insert(body).execute()
        return resp.data[0], True
    except APIError as exc:
        if not _is_unique_violation(exc):
            raise
    existing = (
        client.table("meal_logs")
        .select("*")
        .eq("event_id", event_id)
        .single()
        .execute()
    )
    return existing.data, False


def insert_menu_cycle(
    client: Client,
    *,
    content_hash: str,
    effective_from: str,
    source: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Insert public.menu_cycles keyed on (content_hash, effective_from, source) (§14 I4)."""
    body = {
        **payload,
        "content_hash": content_hash,
        "effective_from": effective_from,
        "source": source,
    }
    try:
        resp = client.table("menu_cycles").insert(body).execute()
        return resp.data[0], True
    except APIError as exc:
        if not _is_unique_violation(exc):
            raise
    existing = (
        client.table("menu_cycles")
        .select("*")
        .eq("content_hash", content_hash)
        .eq("effective_from", effective_from)
        .eq("source", source)
        .single()
        .execute()
    )
    return existing.data, False


def update_plan_with_version_guard(
    client: Client,
    *,
    plan_id: int,
    expected_version: int,
    fields: dict[str, Any],
) -> dict[str, Any] | None:
    """§14 TX2 — apply an update to public.daily_plans only if version matches.

    Returns the updated row, or None if the version guard rejected the write.
    """
    body = {**fields, "version": expected_version + 1}
    resp = (
        client.table("daily_plans")
        .update(body)
        .eq("id", plan_id)
        .eq("version", expected_version)
        .execute()
    )
    if not resp.data:
        return None
    return resp.data[0]
