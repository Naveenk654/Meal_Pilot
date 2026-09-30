from datetime import date

from backend.idempotency.keys import (
    content_hash_of,
    hitl_response_hash,
    menu_ingestion_key,
    morning_cron_key,
    planner_run_key,
    weekly_learning_key,
)
from backend.models.enums import Trigger


def test_morning_cron_key_deterministic():
    d = date(2026, 3, 5)
    assert (
        morning_cron_key("u-1", d)
        == morning_cron_key("u-1", d)
        == "morning_cron:u-1:2026-03-05"
    )


def test_morning_cron_key_differs_per_user_and_date():
    d = date(2026, 3, 5)
    assert morning_cron_key("u-1", d) != morning_cron_key("u-2", d)
    assert morning_cron_key("u-1", d) != morning_cron_key("u-1", date(2026, 3, 6))


def test_planner_run_key_includes_all_components():
    d = date(2026, 3, 5)
    k = planner_run_key("u-1", d, Trigger.MEAL_LOG, "evt-42")
    assert "u-1" in k and "2026-03-05" in k and "meal_log" in k and "evt-42" in k


def test_planner_run_key_none_event_id_renders_as_literal():
    d = date(2026, 3, 5)
    assert planner_run_key("u-1", d, Trigger.MORNING_CRON, None).endswith(":none")


def test_planner_run_keys_distinct_across_triggers():
    d = date(2026, 3, 5)
    a = planner_run_key("u-1", d, Trigger.MORNING_CRON, None)
    b = planner_run_key("u-1", d, Trigger.MEAL_LOG, None)
    assert a != b


def test_weekly_learning_key_zero_pads():
    assert weekly_learning_key("u-1", 2026, 3) == "learning:u-1:2026W03"


def test_menu_ingestion_key_includes_all_components():
    k = menu_ingestion_key("abc123", date(2026, 3, 5), "pdf")
    assert k == "menu:pdf:2026-03-05:abc123"


def test_content_hash_stability():
    a = content_hash_of("hello world")
    b = content_hash_of(b"hello world")
    assert a == b
    assert content_hash_of("hello world!") != a


def test_hitl_response_hash_is_canonical():
    a = hitl_response_hash({"a": 1, "b": 2})
    b = hitl_response_hash({"b": 2, "a": 1})  # different key order → same hash
    assert a == b
    assert hitl_response_hash({"a": 1, "b": 3}) != a
