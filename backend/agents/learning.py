"""Weekly Profile & Learning Agent (§6.3).

One entrypoint: `run_weekly_learning`. It:
  1. Pulls the last 7 days of meal_logs for the user.
  2. Retires any active behavioral facts whose last_confirmed_at is stale.
  3. Asks the LLM to detect patterns (`pattern_detect`).
  4. Writes each proposed fact via `memory.propose_fact` (dedupes by fact text).
  5. Emits an HITL surface #5 (weekly_review) so the user can approve / reject
     each proposal from the app.
  6. Records the whole run under `agent_runs` with idempotency key
     `(user_id, iso_week)` — one learning run per user per week.

Also exposes `schedule_weekly_learning_if_due` — a fire-and-forget scheduler
that lets natural user actions (meal_log, planner_run) trigger the weekly
learning automatically, so no cron is required. Called from meal_log commits.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from supabase import Client

from backend.config import Settings, get_settings
from backend.idempotency.keys import weekly_learning_key
from backend.models.enums import HITLStatus
from backend.tools.hitl import HITLSurface, create_hitl_request
from backend.tools.llm_router import DegradedModeSignal
from backend.tools.memory import propose_fact, retire_stale_facts
from backend.tools.pattern_detect import PatternDetectError, detect_patterns
from backend.tools.trace import agent_run


@dataclass(frozen=True)
class LearningOutcome:
    inserted: bool                    # False on idempotent retry
    proposed_count: int
    retired_stale_count: int
    hitl_created: bool
    idempotency_key: str


async def run_weekly_learning(
    svc: Client,
    *,
    user_id: str,
    on_date: date | None = None,
    settings: Settings | None = None,
) -> LearningOutcome:
    """Sole entrypoint. `on_date` is the day the cron fires — the ISO week of
    that date drives the idempotency key."""
    s = settings or get_settings()
    the_date = on_date or datetime.now(timezone.utc).date()
    iso_year, iso_week, _ = the_date.isocalendar()
    idem_key = weekly_learning_key(user_id, iso_year, iso_week)

    with agent_run(
        svc,
        idempotency_key=idem_key,
        agent="learning",
        trigger="weekly_cron",
        user_id=user_id,
    ) as run:
        if not run.inserted:
            return LearningOutcome(
                inserted=False,
                proposed_count=0,
                retired_stale_count=0,
                hitl_created=False,
                idempotency_key=idem_key,
            )

        # Step 6 — housekeeping: retire stale active facts.
        retired = retire_stale_facts(svc, user_id=user_id)

        # Step 1 — pull the last 7 days of logs.
        start = the_date - timedelta(days=7)
        logs_resp = (
            svc.table("meal_logs")
            .select("id, date, meal, action, actual_dishes_json, actual_macros_json, note")
            .eq("user_id", user_id)
            .gte("date", start.isoformat())
            .lte("date", the_date.isoformat())
            .order("logged_at")
            .execute()
        )
        logs = logs_resp.data or []
        run.log_tool_call(
            tool_name="read_logs",
            input_snapshot={"user_id": user_id, "from": start.isoformat(), "to": the_date.isoformat()},
            output_snapshot={"log_count": len(logs)},
        )

        if not logs:
            run.log_decision(
                step_name="pattern_detect",
                reason_code="no_logs_available",
            )
            run.set_final_outcome(
                final_outcome={"proposed": 0, "retired_stale": retired},
                hitl_status=HITLStatus.NOT_NEEDED,
            )
            return LearningOutcome(
                inserted=True,
                proposed_count=0,
                retired_stale_count=retired,
                hitl_created=False,
                idempotency_key=idem_key,
            )

        # Step 2 — LLM pattern detection.
        try:
            batch, llm_result = await detect_patterns(logs, settings=s)
        except DegradedModeSignal as exc:
            run.set_final_outcome(degraded_mode=True)
            run.log_decision(
                step_name="pattern_detect",
                reason_code="llm_degraded",
                llm_reasoning=str(exc),
            )
            return LearningOutcome(
                inserted=True,
                proposed_count=0,
                retired_stale_count=retired,
                hitl_created=False,
                idempotency_key=idem_key,
            )
        except PatternDetectError as exc:
            run.log_decision(
                step_name="pattern_detect",
                reason_code="llm_output_invalid",
                llm_reasoning=str(exc),
            )
            return LearningOutcome(
                inserted=True,
                proposed_count=0,
                retired_stale_count=retired,
                hitl_created=False,
                idempotency_key=idem_key,
            )

        run.log_tool_call(
            tool_name="pattern_detect",
            input_snapshot={"log_count": len(logs)},
            output_snapshot={"fact_count": len(batch.facts)},
            latency_ms=llm_result.latency_ms,
            tokens_used=llm_result.tokens_used,
        )

        # Step 3-4 — write proposed facts + collect for HITL.
        proposed_summaries: list[dict[str, Any]] = []
        for pf in batch.facts:
            evidence = {"log_ids": pf.evidence_log_ids, "confidence": pf.confidence}
            result = propose_fact(svc, user_id=user_id, fact=pf.fact, evidence=evidence)
            proposed_summaries.append(
                {
                    "memory_id": result.id,
                    "fact": pf.fact,
                    "confidence": pf.confidence,
                    "evidence_log_ids": pf.evidence_log_ids,
                    "new": result.inserted,
                }
            )

        run.log_decision(
            step_name="propose_facts",
            reason_code="facts_proposed" if proposed_summaries else "no_facts",
            candidates_considered=proposed_summaries,
        )

        # Step 4 — HITL surface #5 (weekly review).
        hitl_created = False
        if proposed_summaries:
            try:
                create_hitl_request(
                    svc,
                    user_id=user_id,
                    agent="learning",
                    surface=HITLSurface.WEEKLY_REVIEW,
                    question="Weekly review — approve or reject the patterns we noticed.",
                    options=proposed_summaries,
                    context={"iso_year": iso_year, "iso_week": iso_week},
                )
                hitl_created = True
            except Exception:
                hitl_created = False

        run.set_final_outcome(
            final_outcome={
                "proposed": len(proposed_summaries),
                "retired_stale": retired,
            },
            hitl_status=HITLStatus.PENDING if hitl_created else HITLStatus.NOT_NEEDED,
        )

        return LearningOutcome(
            inserted=True,
            proposed_count=len(proposed_summaries),
            retired_stale_count=retired,
            hitl_created=hitl_created,
            idempotency_key=idem_key,
        )


# --- Auto-trigger --------------------------------------------------------


_LEARNING_INTERVAL_DAYS = 7


def _last_learning_run_at(svc: Client, user_id: str) -> datetime | None:
    """Return the started_at of the user's most recent learning agent_run,
    or None if the agent has never run for this user."""
    resp = (
        svc.table("agent_runs")
        .select("started_at")
        .eq("agent", "learning")
        .eq("user_id", user_id)
        .order("started_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return None
    raw = rows[0].get("started_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _is_learning_due(svc: Client, user_id: str, *, now: datetime | None = None) -> bool:
    """True when the user's last learning run is missing OR older than
    `_LEARNING_INTERVAL_DAYS` days. Cheap DB round-trip; called before we
    kick off the (expensive) LLM pattern detection."""
    now = now or datetime.now(timezone.utc)
    last = _last_learning_run_at(svc, user_id)
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) >= timedelta(days=_LEARNING_INTERVAL_DAYS)


def schedule_weekly_learning_if_due(svc: Client, user_id: str) -> bool:
    """Fire-and-forget the weekly Learning Agent for `user_id` when it's due.

    Called from meal_log commits (and could be called from planner runs) so
    the learning cadence is driven by natural user activity — no cron
    required. Two dedup layers:

      * `_is_learning_due` skips runs younger than 7 days.
      * `run_weekly_learning` itself is idempotent per (user, ISO week);
        even if we somehow fired twice, only one insert wins.

    Returns True when a background task was actually scheduled, False when
    we early-exited because a recent run exists. Never raises: a failed
    schedule must not affect the caller's request.
    """
    try:
        if not _is_learning_due(svc, user_id):
            return False
    except Exception:  # noqa: BLE001 — we deliberately swallow to protect the caller
        return False

    loop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — the caller is a sync context (a cron entry, a
        # script). The task would have nowhere to run; fall back to a
        # synchronous invocation would defeat the fire-and-forget intent.
        # Better to silently skip and let the next event-loop context fire.
        return False

    async def _runner() -> None:
        try:
            await run_weekly_learning(svc, user_id=user_id)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception(
                "background weekly learning failed for user %s", user_id
            )

    loop.create_task(_runner())
    return True
