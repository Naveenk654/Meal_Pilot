"""M6 tests: memory lifecycle + Learning Agent with mocked LLM."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from tests.conftest import requires_db


@requires_db
def test_memory_propose_dedupe_and_approve_reject(svc_client, ephemeral_auth_user):
    """propose_fact dedupes identical fact text; approve promotes to active,
    reject retires the proposal."""
    from backend.tools.memory import (
        approve_fact,
        propose_fact,
        reject_fact,
        note_contradiction,
    )

    uid = ephemeral_auth_user["id"]
    r1 = propose_fact(svc_client, user_id=uid, fact="skips breakfast", evidence={"log_ids": [1]})
    assert r1.inserted is True
    r2 = propose_fact(svc_client, user_id=uid, fact="skips breakfast", evidence={"log_ids": [2]})
    assert r2.inserted is False   # deduped
    assert r2.id == r1.id

    r3 = propose_fact(svc_client, user_id=uid, fact="prefers dal", evidence={"log_ids": [3]})
    assert r3.inserted is True

    # Approve the second proposal.
    approved = approve_fact(svc_client, memory_id=r3.id, user_id=uid)
    assert approved["status"] == "active"

    # Reject the first.
    rejected = reject_fact(svc_client, memory_id=r1.id, user_id=uid)
    assert rejected["status"] == "retired"

    # note_contradiction bumps count on active memory; 2 hits → retire.
    contra1 = note_contradiction(svc_client, memory_id=r3.id)
    assert contra1["contradiction_count"] == 1
    assert contra1["status"] == "active"
    contra2 = note_contradiction(svc_client, memory_id=r3.id)
    assert contra2["status"] == "retired"

    # Cleanup
    svc_client.table("user_memory_behavioral").delete().eq("user_id", uid).execute()


@requires_db
def test_learning_agent_end_to_end(svc_client, ephemeral_auth_user, monkeypatch):
    """Mock the LLM, drop 3 meal logs, verify the learning run proposes facts,
    fires an HITL request, and short-circuits on the second call (idempotency)."""
    import asyncio

    from backend.agents import learning as learning_agent
    from backend.models.enums import MealAction, MealSlot
    from backend.models.learning import ProposedFact, ProposedFactBatch
    from backend.tools.llm_router import LLMResult

    uid = ephemeral_auth_user["id"]
    plan_date = date.today()

    # Seed 3 meal_logs
    log_rows: list[int] = []
    for i in range(3):
        row = (
            svc_client.table("meal_logs")
            .insert(
                {
                    "user_id": uid,
                    "date": (plan_date - timedelta(days=i)).isoformat(),
                    "meal": MealSlot.BREAKFAST.value,
                    "event_id": str(uuid.uuid4()),
                    "action": MealAction.SKIPPED.value,
                    "actual_dishes_json": [],
                    "actual_macros_json": {"kcal": 0, "protein_g": 0, "carbs_g": 0, "fats_g": 0},
                }
            )
            .execute()
            .data[0]
        )
        log_rows.append(row["id"])

    fake_llm = LLMResult(text="", tokens_used=1, latency_ms=1, provider="fake", model="fake")

    async def fake_detect(logs, settings=None):
        return (
            ProposedFactBatch(
                facts=[
                    ProposedFact(
                        fact="skips breakfast repeatedly",
                        evidence_log_ids=log_rows,
                        confidence=0.85,
                    )
                ]
            ),
            fake_llm,
        )

    monkeypatch.setattr(learning_agent, "detect_patterns", fake_detect)

    o1 = asyncio.run(learning_agent.run_weekly_learning(svc_client, user_id=uid, on_date=plan_date))
    try:
        assert o1.inserted is True
        assert o1.proposed_count == 1
        assert o1.hitl_created is True

        # Idempotent retry
        o2 = asyncio.run(learning_agent.run_weekly_learning(svc_client, user_id=uid, on_date=plan_date))
        assert o2.inserted is False

        # Facts recorded proposed
        facts = (
            svc_client.table("user_memory_behavioral")
            .select("id, status, fact")
            .eq("user_id", uid)
            .execute()
            .data
        )
        assert any(f["fact"] == "skips breakfast repeatedly" and f["status"] == "proposed" for f in facts)
    finally:
        svc_client.table("user_memory_behavioral").delete().eq("user_id", uid).execute()
        svc_client.table("meal_logs").delete().eq("user_id", uid).execute()
        svc_client.table("hitl_requests").delete().eq("user_id", uid).execute()
        svc_client.table("agent_runs").delete().eq("idempotency_key", o1.idempotency_key).execute()
