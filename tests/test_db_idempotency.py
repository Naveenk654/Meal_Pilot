"""DB-integration tests for idempotency writers. Skipped when Supabase not configured."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from tests.conftest import requires_db


@requires_db
def test_agent_run_insert_is_idempotent(svc_client):
    from backend.idempotency.writer import insert_agent_run

    key = f"pytest:agent_run:{uuid.uuid4()}"
    payload = {
        "user_id": None,
        "agent": "planner",
        "trigger": "morning_cron",
        "triggering_event_id": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    row1, inserted1 = insert_agent_run(svc_client, idempotency_key=key, payload=payload)
    assert inserted1 is True

    row2, inserted2 = insert_agent_run(svc_client, idempotency_key=key, payload=payload)
    assert inserted2 is False
    assert row2["id"] == row1["id"]

    svc_client.table("agent_runs").delete().eq("id", row1["id"]).execute()


@requires_db
def test_menu_cycle_insert_is_idempotent(svc_client):
    from backend.idempotency.writer import insert_menu_cycle

    content_hash = uuid.uuid4().hex
    effective_from = date(2026, 3, 5).isoformat()
    source = "pdf"
    row1, ins1 = insert_menu_cycle(
        svc_client,
        content_hash=content_hash,
        effective_from=effective_from,
        source=source,
        payload={
            "effective_to": date(2026, 3, 11).isoformat(),
            "version": 1,
        },
    )
    assert ins1 is True

    row2, ins2 = insert_menu_cycle(
        svc_client,
        content_hash=content_hash,
        effective_from=effective_from,
        source=source,
        payload={"effective_to": date(2026, 3, 11).isoformat(), "version": 1},
    )
    assert ins2 is False
    assert row2["id"] == row1["id"]

    svc_client.table("menu_cycles").delete().eq("id", row1["id"]).execute()
