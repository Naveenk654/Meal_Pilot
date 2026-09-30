"""Menu Intelligence Agent (§6.1).

One entrypoint: `ingest_menu_pdf`. It:
  1. Hashes the PDF and derives the idempotency key.
  2. Opens an agent_run — retry of the same PDF short-circuits at the DB layer
     via UNIQUE(content_hash, effective_from, source) on menu_cycles.
  3. pdf_parse → llm_extract → menu_diff → llm macro_estimate for unknowns.
  4. Inserts menu_items into the new draft cycle. Preserves verified macros
     by never overwriting existing macro_db rows (INSERT-if-absent).
  5. Flags dishes with per-dish confidence below the configured threshold
     into `menu_cycles.pending_review_json` so the admin verifies before
     approving.
  6. Emits `menu_updated` to `menu_events` — the Planner will consume this
     when M3 wires up menu-driven replans.

Errors from the PDF/LLM steps surface as `MenuIngestionError`. The router
converts these to a 4xx/5xx. degraded_mode is set on agent_runs when both
LLM providers are unreachable so the audit trail records the reason.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from postgrest.exceptions import APIError
from supabase import Client

from backend.config import Settings, get_settings
from backend.idempotency.keys import content_hash_of, menu_ingestion_key
from backend.idempotency.writer import insert_menu_cycle
from backend.models.enums import HITLStatus, MacroSource, MealSlot
from backend.models.menu_extract import EstimatedMacros, ExtractedMenu
from backend.tools.llm_router import DegradedModeSignal
from backend.tools.macro_estimate import MacroEstimateError, estimate_macros_for_dish
from backend.tools.menu import diff_extraction, normalize_dish_name
from backend.tools.menu_extract import MenuExtractionError, extract_menu_from_snippet
from backend.tools.pdf_parse import (
    PdfParseError,
    parse_pdf_bytes,
    parsed_pdf_to_llm_snippet,
    summarize_parsed_pdf,
)
from backend.tools.trace import agent_run


class MenuIngestionError(RuntimeError):
    """Wraps pdf/LLM failures so the router can respond with a clean 4xx/5xx."""


@dataclass(frozen=True)
class IngestionResult:
    cycle_id: int
    inserted: bool                        # False on idempotent retry
    added: int
    unchanged: int
    unknown_dishes_estimated: int
    flagged_for_review: int
    degraded_mode: bool
    idempotency_key: str


async def ingest_menu_pdf(
    svc_client: Client,
    *,
    pdf_bytes: bytes,
    effective_from: date,
    effective_to: date,
    source: str = "pdf",
    admin_user_id: str | None,
    pdf_url: str | None = None,
    settings: Settings | None = None,
) -> IngestionResult:
    """Main entrypoint — safe to call more than once with the same PDF."""
    s = settings or get_settings()
    if effective_to < effective_from:
        raise MenuIngestionError("effective_to must be >= effective_from")

    content_hash = content_hash_of(pdf_bytes)
    idem_key = menu_ingestion_key(content_hash, effective_from, source)

    with agent_run(
        svc_client,
        idempotency_key=idem_key,
        agent="menu_intel",
        trigger="menu_update",
        user_id=admin_user_id,
    ) as run:
        if not run.inserted:
            existing = _lookup_cycle(svc_client, content_hash, effective_from, source)
            return IngestionResult(
                cycle_id=existing["id"] if existing else -1,
                inserted=False,
                added=0,
                unchanged=0,
                unknown_dishes_estimated=0,
                flagged_for_review=0,
                degraded_mode=bool(run and False),
                idempotency_key=idem_key,
            )

        try:
            parsed = parse_pdf_bytes(pdf_bytes)
        except PdfParseError as exc:
            run.log_decision(
                step_name="pdf_parse",
                reason_code="pdf_parse_error",
                llm_reasoning=str(exc),
            )
            raise MenuIngestionError(f"pdf_parse failed: {exc}") from exc

        run.log_tool_call(
            tool_name="pdf_parse",
            input_snapshot={"bytes": len(pdf_bytes), "content_hash": content_hash},
            output_snapshot=summarize_parsed_pdf(parsed),
        )

        # Step 2 — LLM structured extraction.
        try:
            extraction, llm_result = await extract_menu_from_snippet(
                parsed_pdf_to_llm_snippet(parsed), settings=s
            )
        except DegradedModeSignal as exc:
            run.set_final_outcome(degraded_mode=True)
            run.log_decision(
                step_name="llm_extract_structured",
                reason_code="llm_degraded",
                llm_reasoning=str(exc),
            )
            raise MenuIngestionError("LLM providers unavailable — retry later") from exc
        except MenuExtractionError as exc:
            run.log_decision(
                step_name="llm_extract_structured",
                reason_code="llm_output_invalid",
                llm_reasoning=str(exc),
            )
            raise MenuIngestionError(f"menu extraction failed: {exc}") from exc

        run.log_tool_call(
            tool_name="llm_extract_structured",
            input_snapshot={"content_hash": content_hash},
            output_snapshot={"slot_count": len(extraction.slots)},
            latency_ms=llm_result.latency_ms,
            tokens_used=llm_result.tokens_used,
        )

        # Step 5 — diff against currently-active cycle for this source.
        current_items = _load_current_menu_items(svc_client, source=source, on_date=effective_from)
        known_macro_names = _load_known_macro_names(svc_client)
        diff = diff_extraction(
            extraction,
            current_items=current_items,
            known_macro_names=known_macro_names,
        )

        # Step 3 — macro estimation for dishes we've never seen. Runs the
        # per-dish LLM calls concurrently (bounded parallelism) so a menu
        # with N unknowns takes ~ceil(N/CONCURRENCY) * per-call latency
        # instead of N × per-call latency. Previously sequential → ~85s for
        # ~30 unknowns; parallel at CONCURRENCY=5 → ~15s.
        estimated: dict[str, EstimatedMacros] = {}
        veg_hints = _veg_hints_from_extraction(extraction)

        _MACRO_ESTIMATE_CONCURRENCY = 5
        semaphore = asyncio.Semaphore(_MACRO_ESTIMATE_CONCURRENCY)

        async def _estimate_one(normalized: str) -> tuple[str, str, EstimatedMacros | None, object]:
            is_veg = veg_hints.get(normalized, True)
            display_name = _display_name_hint(extraction, normalized) or normalized
            async with semaphore:
                try:
                    macros, llm_result = await estimate_macros_for_dish(
                        display_name, is_veg=is_veg, settings=s
                    )
                except (DegradedModeSignal, MacroEstimateError) as exc:
                    return normalized, display_name, None, exc
            return normalized, display_name, macros, llm_result

        results = await asyncio.gather(
            *(_estimate_one(n) for n in diff.unknown_dishes),
            return_exceptions=False,   # exceptions come through as the 4th tuple slot
        )

        for normalized, display_name, macros, extra in results:
            if isinstance(extra, DegradedModeSignal):
                # Any degraded signal aborts the whole ingest — we don't want
                # to persist half-estimated macros when the LLM is down.
                run.set_final_outcome(degraded_mode=True)
                run.log_decision(
                    step_name="macro_estimate",
                    reason_code="llm_degraded",
                    llm_reasoning=str(extra),
                )
                raise MenuIngestionError("LLM unavailable during macro estimation") from extra
            if isinstance(extra, MacroEstimateError):
                run.log_decision(
                    step_name="macro_estimate",
                    reason_code="llm_output_invalid",
                    llm_reasoning=f"{display_name}: {extra}",
                )
                continue   # skip this dish; menu_item.macro_id stays NULL, flagged below
            # Happy path — `extra` is the LLMResult, macros is set.
            run.log_tool_call(
                tool_name="macro_estimate",
                input_snapshot={"dish": display_name, "is_veg": veg_hints.get(normalized, True)},
                output_snapshot={
                    "kcal": macros.kcal,
                    "confidence": macros.confidence,
                },
                latency_ms=extra.latency_ms,
                tokens_used=extra.tokens_used,
            )
            estimated[normalized] = macros.model_copy(update={"dish_name_normalized": normalized})

        # Upsert macro_db rows for the estimates. Preserve verified rows (§6.1
        # step 5): only insert if the name is not already present.
        _persist_estimated_macros(svc_client, estimated)

        # Insert the menu_cycles row (idempotent via UNIQUE).
        pending_review, flagged_count = _compute_pending_review(
            extraction=extraction,
            estimated=estimated,
            threshold=s.confidence_threshold,
        )
        cycle_row, _cycle_inserted = insert_menu_cycle(
            svc_client,
            content_hash=content_hash,
            effective_from=effective_from.isoformat(),
            source=source,
            payload={
                "effective_to": effective_to.isoformat(),
                "version": 1,
                "pdf_url": pdf_url,
                "ingested_by": admin_user_id,
                "status": "draft",
                "pending_review_json": pending_review,
                "ingested_at": _utcnow_iso(),
            },
        )
        cycle_id = cycle_row["id"]

        # Insert menu_items for the new (draft) cycle. Rows re-attach to
        # macro_db by normalized name so verified macros stay linked.
        macro_id_by_normalized = _load_macro_id_map(svc_client)
        _insert_menu_items(
            svc_client,
            cycle_id=cycle_id,
            extraction=extraction,
            macro_id_by_normalized=macro_id_by_normalized,
        )

        run.log_decision(
            step_name="menu_diff",
            reason_code="diff_applied",
            candidates_considered=None,
            chosen_option={
                "added": len(diff.added),
                "removed": len(diff.removed),
                "unchanged": len(diff.unchanged),
                "unknown_dishes": len(diff.unknown_dishes),
                "flagged_for_review": flagged_count,
            },
        )

        # Step 7 — emit menu_updated event.
        svc_client.table("menu_events").insert(
            {
                "event_type": "menu_updated",
                "cycle_id": cycle_id,
                "effective_from": effective_from.isoformat(),
                "effective_to": effective_to.isoformat(),
                "payload": {
                    "source": source,
                    "content_hash": content_hash,
                    "status": "draft",
                    "added": len(diff.added),
                    "flagged_for_review": flagged_count,
                },
            }
        ).execute()

        run.set_final_outcome(
            final_outcome={
                "cycle_id": cycle_id,
                "content_hash": content_hash,
                "added": len(diff.added),
                "flagged_for_review": flagged_count,
            },
            hitl_status=HITLStatus.PENDING if flagged_count > 0 else HITLStatus.NOT_NEEDED,
        )

        return IngestionResult(
            cycle_id=cycle_id,
            inserted=True,
            added=len(diff.added),
            unchanged=len(diff.unchanged),
            unknown_dishes_estimated=len(estimated),
            flagged_for_review=flagged_count,
            degraded_mode=False,
            idempotency_key=idem_key,
        )


# --- Internal helpers -------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lookup_cycle(
    client: Client, content_hash: str, effective_from: date, source: str
) -> dict[str, Any] | None:
    resp = (
        client.table("menu_cycles")
        .select("id, status")
        .eq("content_hash", content_hash)
        .eq("effective_from", effective_from.isoformat())
        .eq("source", source)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def _load_current_menu_items(client: Client, *, source: str, on_date: date) -> list[dict]:
    """Items from the currently-active cycle covering `on_date`, if any."""
    cycles = (
        client.table("menu_cycles")
        .select("id")
        .eq("source", source)
        .eq("status", "active")
        .lte("effective_from", on_date.isoformat())
        .gte("effective_to", on_date.isoformat())
        .order("version", desc=True)
        .limit(1)
        .execute()
    )
    rows = cycles.data or []
    if not rows:
        return []
    items = (
        client.table("menu_items")
        .select("day_of_week, meal, dish_name")
        .eq("cycle_id", rows[0]["id"])
        .execute()
    )
    return items.data or []


def _load_known_macro_names(client: Client) -> set[str]:
    resp = client.table("macro_db").select("dish_name_normalized").execute()
    return {row["dish_name_normalized"] for row in (resp.data or [])}


def _load_macro_id_map(client: Client) -> dict[str, int]:
    resp = client.table("macro_db").select("id, dish_name_normalized").execute()
    return {row["dish_name_normalized"]: row["id"] for row in (resp.data or [])}


def _display_name_hint(extraction: ExtractedMenu, normalized: str) -> str | None:
    for _day, _meal, dish in extraction.flatten():
        if normalize_dish_name(dish.dish_name) == normalized:
            return dish.dish_name
    return None


def _veg_hints_from_extraction(extraction: ExtractedMenu) -> dict[str, bool]:
    """First occurrence's is_veg wins. Consistent enough for the estimator."""
    hints: dict[str, bool] = {}
    for _day, _meal, dish in extraction.flatten():
        key = normalize_dish_name(dish.dish_name)
        hints.setdefault(key, dish.is_veg)
    return hints


def _persist_estimated_macros(client: Client, estimated: dict[str, EstimatedMacros]) -> None:
    """Insert-if-absent so `verified=true` rows are never overwritten."""
    for macros in estimated.values():
        body = {
            "dish_name_normalized": macros.dish_name_normalized,
            "serving_unit": macros.serving_unit,
            "serving_grams": macros.serving_grams,
            "kcal": macros.kcal,
            "protein_g": macros.protein_g,
            "carbs_g": macros.carbs_g,
            "fats_g": macros.fats_g,
            "is_veg": macros.is_veg,
            "practical_max_servings_per_day": macros.practical_max_servings_per_day,
            "source": MacroSource.LLM_ESTIMATED.value,
            "confidence": macros.confidence,
            "verified": False,
            "estimated_at": _utcnow_iso(),
        }
        try:
            client.table("macro_db").insert(body).execute()
        except APIError as exc:
            # 23505: someone else beat us to the insert. Treat as "already present"
            # and preserve the existing (potentially verified) row.
            if getattr(exc, "code", None) != "23505":
                raise


def _insert_menu_items(
    client: Client,
    *,
    cycle_id: int,
    extraction: ExtractedMenu,
    macro_id_by_normalized: dict[str, int],
) -> None:
    rows: list[dict] = []
    for day, meal, dish in extraction.flatten():
        normalized = normalize_dish_name(dish.dish_name)
        if not normalized:
            continue
        rows.append(
            {
                "cycle_id": cycle_id,
                "day_of_week": day,
                "meal": meal.value if isinstance(meal, MealSlot) else meal,
                "dish_name": dish.dish_name.strip(),
                "is_veg": dish.is_veg,
                "macro_id": macro_id_by_normalized.get(normalized),
                "confidence": dish.confidence,
                # Extractor sets this only when the dish was split out of a
                # "Tea/Coffee/Milk"-style choice cell; standalone dishes stay
                # NULL. Planner reads it via resolve_daily_menu → constraint
                # engine to enforce "pick exactly one per group per meal".
                "choice_group_id": dish.choice_group_id,
            }
        )
    if rows:
        client.table("menu_items").insert(rows).execute()


def _compute_pending_review(
    *,
    extraction: ExtractedMenu,
    estimated: dict[str, EstimatedMacros],
    threshold: float,
) -> tuple[list[dict[str, Any]], int]:
    """Aggregate low-confidence extraction dishes + low-confidence macros.

    Returned list is what the admin UI shows on the approval screen. `count`
    drives the HITL flag on agent_runs.
    """
    flagged: list[dict[str, Any]] = []
    for day, meal, dish in extraction.flatten():
        if dish.confidence < threshold:
            flagged.append(
                {
                    "reason": "low_extraction_confidence",
                    "day_of_week": day,
                    "meal": meal.value,
                    "dish_name": dish.dish_name,
                    "confidence": dish.confidence,
                }
            )
    for normalized, macros in estimated.items():
        if macros.confidence < threshold:
            flagged.append(
                {
                    "reason": "low_macro_confidence",
                    "dish_name_normalized": normalized,
                    "confidence": macros.confidence,
                }
            )
    return flagged, len(flagged)
