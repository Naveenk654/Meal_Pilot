-- The `hitl_status` enum was created in 0002 with values matching
-- §17 hitl_requests.status (pending/approved/rejected/expired/edited),
-- but the same enum is also used for agent_runs.hitl_status which mirrors
-- PlannerState.hitl_status from §5 — that needs 'not_needed' too.
-- Add the missing value. Postgres 9.6+ supports ADD VALUE IF NOT EXISTS.

alter type hitl_status add value if not exists 'not_needed';
