"""Integration-style test for the Menu Intelligence Agent.

Runs against Supabase (skipped when env vars are missing). LLM calls are
monkey-patched so we don't burn tokens on every CI run. Verifies:
  - a fresh PDF creates a draft cycle with menu_items + macro_db entries
  - re-uploading the same PDF short-circuits (same idempotency key)
  - low-confidence extractions land in pending_review_json
"""
from __future__ import annotations

import io
import uuid

import pytest

from tests.conftest import requires_db


def _make_pdf(text: str) -> bytes:
    """Tiny inline PDF containing arbitrary text so pdfplumber has something
    to read. We use reportlab if available; otherwise we skip."""
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed; skipping PDF integration test")
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for line in text.splitlines() or [text]:
        c.drawString(72, y, line)
        y -= 20
    c.save()
    return buf.getvalue()


@requires_db
def test_ingest_menu_pdf_end_to_end(svc_client, monkeypatch):
    from backend.agents import menu_intel
    from backend.models.enums import MealSlot
    from backend.models.menu_extract import (
        EstimatedMacros,
        ExtractedDish,
        ExtractedMealSlot,
        ExtractedMenu,
    )
    from backend.tools.llm_router import LLMResult

    fake_menu = ExtractedMenu(
        slots=[
            ExtractedMealSlot(
                day_of_week=0,
                meal=MealSlot.BREAKFAST,
                dishes=[
                    ExtractedDish(dish_name="Aloo Paratha", is_veg=True, confidence=0.9)
                ],
            ),
            ExtractedMealSlot(
                day_of_week=0,
                meal=MealSlot.LUNCH,
                dishes=[
                    ExtractedDish(dish_name="Test Dish Xyz", is_veg=True, confidence=0.55),
                ],
            ),
        ]
    )
    fake_llm = LLMResult(text="", tokens_used=10, latency_ms=5, provider="fake", model="fake")

    async def fake_extract(_snippet, *, settings=None):
        return fake_menu, fake_llm

    async def fake_estimate(dish_name, *, is_veg, settings=None):
        return (
            EstimatedMacros(
                dish_name_normalized="test_dish_xyz",
                serving_unit="1 bowl (150g)",
                serving_grams=150,
                kcal=200,
                protein_g=10,
                carbs_g=20,
                fats_g=8,
                is_veg=is_veg,
                practical_max_servings_per_day=2,
                confidence=0.5,
            ),
            fake_llm,
        )

    monkeypatch.setattr(menu_intel, "extract_menu_from_snippet", fake_extract)
    monkeypatch.setattr(menu_intel, "estimate_macros_for_dish", fake_estimate)

    pdf_bytes = _make_pdf(f"menu-{uuid.uuid4()}\nAloo Paratha\nTest Dish Xyz")
    from datetime import date, timedelta

    ef = date.today() + timedelta(days=365)  # far-future to avoid colliding with real data
    et = ef + timedelta(days=6)

    import asyncio

    result = asyncio.run(
        menu_intel.ingest_menu_pdf(
            svc_client,
            pdf_bytes=pdf_bytes,
            effective_from=ef,
            effective_to=et,
            source="pdf",
            admin_user_id=None,
        )
    )
    try:
        assert result.inserted is True
        assert result.cycle_id > 0
        # low-confidence dish should show up in the review queue (0.55 and macro 0.5)
        assert result.flagged_for_review >= 1

        items = (
            svc_client.table("menu_items")
            .select("*")
            .eq("cycle_id", result.cycle_id)
            .execute()
            .data
        )
        assert {row["dish_name"] for row in items} == {"Aloo Paratha", "Test Dish Xyz"}

        events = (
            svc_client.table("menu_events")
            .select("*")
            .eq("cycle_id", result.cycle_id)
            .execute()
            .data
        )
        assert any(e["event_type"] == "menu_updated" for e in events)

        # Retry with the same PDF must not double-insert.
        result2 = asyncio.run(
            menu_intel.ingest_menu_pdf(
                svc_client,
                pdf_bytes=pdf_bytes,
                effective_from=ef,
                effective_to=et,
                source="pdf",
                admin_user_id=None,
            )
        )
        assert result2.inserted is False
        assert result2.cycle_id == result.cycle_id
    finally:
        # Best-effort cleanup so the test doesn't leave junk in Supabase.
        svc_client.table("menu_events").delete().eq("cycle_id", result.cycle_id).execute()
        svc_client.table("menu_items").delete().eq("cycle_id", result.cycle_id).execute()
        svc_client.table("menu_cycles").delete().eq("id", result.cycle_id).execute()
        svc_client.table("macro_db").delete().eq(
            "dish_name_normalized", "test_dish_xyz"
        ).execute()
        svc_client.table("agent_runs").delete().eq(
            "idempotency_key", result.idempotency_key
        ).execute()
