"""Verify agent_run context manager + tool_call + decision_trace writers."""
from __future__ import annotations

import uuid

from tests.conftest import requires_db


@requires_db
def test_agent_run_context_writes_all_three_tables(svc_client):
    from backend.models.enums import HITLStatus
    from backend.tools.trace import agent_run

    key = f"pytest:trace:{uuid.uuid4()}"
    with agent_run(
        svc_client,
        idempotency_key=key,
        agent="planner",
        trigger="morning_cron",
        user_id=None,
        planner_state_snapshot={"revision_attempts": 0},
    ) as run:
        assert run.inserted is True
        run.log_tool_call(
            tool_name="nutrition_calc",
            input_snapshot={"weight_kg": 70},
            output_snapshot={"bmr": 1650},
            status="ok",
            latency_ms=3,
        )
        run.log_decision(
            step_name="score_and_select",
            reason_code="highest_soft_score",
            candidates_considered=[{"id": "c1"}, {"id": "c2"}],
            chosen_option={"id": "c1"},
        )
        run.set_final_outcome(
            confidence=0.82,
            confidence_factors={"overall": 0.82, "menu_freshness_factor": 1.0},
            final_outcome={"selected_plan_id": "c1"},
            hitl_status=HITLStatus.NOT_NEEDED,
        )

    row = (
        svc_client.table("agent_runs")
        .select("*")
        .eq("idempotency_key", key)
        .single()
        .execute()
        .data
    )
    assert row["ended_at"] is not None
    assert row["confidence"] == 0.82
    assert row["hitl_status"] == "not_needed"

    tool_calls = (
        svc_client.table("tool_calls")
        .select("*")
        .eq("agent_run_id", row["id"])
        .execute()
        .data
    )
    assert any(tc["tool_name"] == "nutrition_calc" for tc in tool_calls)

    traces = (
        svc_client.table("decision_traces")
        .select("*")
        .eq("agent_run_id", row["id"])
        .execute()
        .data
    )
    assert any(t["reason_code"] == "highest_soft_score" for t in traces)

    svc_client.table("agent_runs").delete().eq("id", row["id"]).execute()


@requires_db
def test_agent_run_context_second_entry_is_noop(svc_client):
    from backend.tools.trace import agent_run

    key = f"pytest:trace-dedupe:{uuid.uuid4()}"
    with agent_run(
        svc_client,
        idempotency_key=key,
        agent="planner",
        trigger="meal_log",
        user_id=None,
    ) as run1:
        run1.set_final_outcome(final_outcome={"first": True})

    with agent_run(
        svc_client,
        idempotency_key=key,
        agent="planner",
        trigger="meal_log",
        user_id=None,
    ) as run2:
        assert run2.inserted is False
        # Second run must NOT clobber the first run's outcome — this is the point of I3.
        run2.set_final_outcome(final_outcome={"second": True})

    row = (
        svc_client.table("agent_runs")
        .select("*")
        .eq("idempotency_key", key)
        .single()
        .execute()
        .data
    )
    assert row["final_outcome"] == {"first": True}

    svc_client.table("agent_runs").delete().eq("id", row["id"]).execute()


@requires_db
def test_decision_trace_requires_reason_code(svc_client):
    import pytest as _pytest

    from backend.tools.trace import agent_run

    key = f"pytest:trace-reason:{uuid.uuid4()}"
    with agent_run(
        svc_client,
        idempotency_key=key,
        agent="planner",
        trigger="override",
        user_id=None,
    ) as run:
        with _pytest.raises(ValueError):
            run.log_decision(step_name="x", reason_code="")

    # Cleanup: fetch and delete the run we created
    row = svc_client.table("agent_runs").select("id").eq("idempotency_key", key).single().execute()
    svc_client.table("agent_runs").delete().eq("id", row.data["id"]).execute()
