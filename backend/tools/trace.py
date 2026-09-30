"""Structured observability writers (§15).

Three tables: agent_runs (one row per agent invocation, keyed by idempotency_key),
tool_calls (every tool call inside a run), decision_traces (every important decision
with a structured reason_code — the authoritative audit signal, O3/O4).

Every write goes through the service-role client — traces are system-scoped, not
user-scoped, and must persist regardless of RLS.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from supabase import Client

from backend.idempotency.writer import insert_agent_run
from backend.models.enums import HITLStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@dataclass
class RunHandle:
    """Handle returned by the agent_run context manager. Used to log tool calls and
    decisions, and to record final outcome fields before the context exits."""

    client: Client
    run_id: int
    inserted: bool  # False if this idempotency_key already had a row (retry semantics)
    _outcome: dict[str, Any] = field(default_factory=dict)
    _total_tokens: int = 0
    _total_cost_inr: float = 0.0

    def log_tool_call(
        self,
        *,
        tool_name: str,
        input_snapshot: Any = None,
        output_snapshot: Any = None,
        status: str = "ok",
        latency_ms: int | None = None,
        tokens_used: int | None = None,
        cost_inr: float | None = None,
    ) -> None:
        self.client.table("tool_calls").insert(
            {
                "agent_run_id": self.run_id,
                "tool_name": tool_name,
                "input_snapshot": input_snapshot,
                "output_snapshot": output_snapshot,
                "status": status,
                "latency_ms": latency_ms,
                "tokens_used": tokens_used,
                "cost_inr": cost_inr,
                "called_at": _iso(_utcnow()),
            }
        ).execute()
        if tokens_used:
            self._total_tokens += tokens_used
        if cost_inr:
            self._total_cost_inr += cost_inr

    def log_decision(
        self,
        *,
        step_name: str,
        reason_code: str,
        candidates_considered: list[dict[str, Any]] | None = None,
        validation_results: list[dict[str, Any]] | None = None,
        chosen_option: dict[str, Any] | None = None,
        llm_reasoning: str | None = None,
    ) -> None:
        """§15 O3/O4 — reason_code is structured and authoritative; llm_reasoning is
        optional secondary text."""
        if not reason_code:
            raise ValueError("reason_code is required on every decision_trace (§15 O3)")
        self.client.table("decision_traces").insert(
            {
                "agent_run_id": self.run_id,
                "step_name": step_name,
                "candidates_considered": candidates_considered,
                "validation_results": validation_results,
                "chosen_option": chosen_option,
                "reason_code": reason_code,
                "llm_reasoning": llm_reasoning,
                "created_at": _iso(_utcnow()),
            }
        ).execute()

    def set_final_outcome(
        self,
        *,
        confidence: float | None = None,
        confidence_factors: dict[str, Any] | None = None,
        final_outcome: dict[str, Any] | None = None,
        hitl_status: HITLStatus | None = None,
        degraded_mode: bool = False,
    ) -> None:
        if confidence is not None:
            self._outcome["confidence"] = confidence
        if confidence_factors is not None:
            self._outcome["confidence_factors"] = confidence_factors
        if final_outcome is not None:
            self._outcome["final_outcome"] = final_outcome
        if hitl_status is not None:
            self._outcome["hitl_status"] = hitl_status.value
        if degraded_mode:
            self._outcome["degraded_mode"] = True


@contextmanager
def agent_run(
    client: Client,
    *,
    idempotency_key: str,
    agent: str,
    trigger: str,
    user_id: str | None,
    triggering_event_id: str | None = None,
    planner_state_snapshot: dict[str, Any] | None = None,
) -> Iterator[RunHandle]:
    """Wrap an agent invocation in a lifetime-scoped agent_runs row.

    Semantics:
      - INSERT on entry (idempotency-key deduped, §14 I1/I3/I5).
      - If a prior run exists, `inserted=False` on the handle; the caller SHOULD
        short-circuit and return the prior outcome.
      - On exit, UPDATE the row with ended_at, latency_ms, totals, and any fields
        the caller stored via set_final_outcome().
    """
    started = _utcnow()
    row, inserted = insert_agent_run(
        client,
        idempotency_key=idempotency_key,
        payload={
            "user_id": user_id,
            "agent": agent,
            "trigger": trigger,
            "triggering_event_id": triggering_event_id,
            "planner_state_snapshot": planner_state_snapshot,
            "started_at": _iso(started),
        },
    )
    handle = RunHandle(client=client, run_id=row["id"], inserted=inserted)
    try:
        yield handle
    finally:
        if not inserted:
            # Existing run — do not clobber its recorded outcome on retry.
            return
        ended = _utcnow()
        update = {
            "ended_at": _iso(ended),
            "latency_ms": int((ended - started).total_seconds() * 1000),
            "total_tokens": handle._total_tokens or None,
            "total_cost_inr": handle._total_cost_inr or None,
            **handle._outcome,
        }
        client.table("agent_runs").update(update).eq("id", handle.run_id).execute()
