"""Unit tests for `resolve_daily_menu` — the "latest cycle with items for
today's day-of-week" semantics. No DB, no LLM; `Client` is mocked.

Pins the resolver behavior we moved to on 2026-10-01:
- `effective_to` is intentionally ignored (menus stay in force until a newer
  one is uploaded).
- Empty cycles (broken ingestion, leaked test rows) are silently skipped so
  the next-newest cycle gets a chance.
- Return `None` only if NO active cycle has items for today's day-of-week.
"""
from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock

from backend.tools.menu import resolve_daily_menu


class _FakeChain:
    """Chainable Supabase stub. Records (table, filters) and returns pre-set
    rows based on which table is being queried."""

    def __init__(self, table_name: str, store: dict[str, list[dict]]):
        self._table = table_name
        self._store = store
        self._filters: list[tuple[str, str, Any]] = []
        self._order: list[tuple[str, bool]] = []

    def select(self, *_, **__) -> "_FakeChain":
        return self

    def eq(self, col: str, val: Any) -> "_FakeChain":
        self._filters.append(("eq", col, val))
        return self

    def lte(self, col: str, val: Any) -> "_FakeChain":
        self._filters.append(("lte", col, val))
        return self

    def gte(self, col: str, val: Any) -> "_FakeChain":
        self._filters.append(("gte", col, val))
        return self

    def order(self, col: str, desc: bool = False) -> "_FakeChain":
        self._order.append((col, desc))
        return self

    def limit(self, _n: int) -> "_FakeChain":
        return self

    def execute(self) -> Any:
        rows = list(self._store.get(self._table, []))
        for op, col, val in self._filters:
            if op == "eq":
                rows = [r for r in rows if r.get(col) == val]
            elif op == "lte":
                rows = [r for r in rows if r.get(col) <= val]
            elif op == "gte":
                rows = [r for r in rows if r.get(col) >= val]
        for col, desc in reversed(self._order):
            rows.sort(key=lambda r: r.get(col) or "", reverse=desc)
        resp = MagicMock()
        resp.data = rows
        return resp


def _client(cycles: list[dict], items: list[dict]) -> Any:
    store = {"menu_cycles": cycles, "menu_items": items}
    svc = MagicMock(name="svc")
    svc.table.side_effect = lambda name: _FakeChain(name, store)
    return svc


# --- helpers ---------------------------------------------------------------


def _cycle(cid: int, eff_from: str, eff_to: str, version: int = 1, source: str = "pdf", status: str = "active") -> dict:
    return {
        "id": cid,
        "source": source,
        "version": version,
        "status": status,
        "effective_from": eff_from,
        "effective_to": eff_to,
        "content_hash": f"h-{cid}",
    }


def _item(cycle_id: int, dow: int, meal: str, dish: str = "aloo_paratha", veg: bool = True) -> dict:
    return {
        "cycle_id": cycle_id,
        "day_of_week": dow,
        "meal": meal,
        "dish_name": dish,
        "is_veg": veg,
        "macro_id": 1,
        "confidence": 1.0,
        "choice_group_id": None,
    }


# --- tests ------------------------------------------------------------------


def test_resolver_skips_empty_newer_cycle_and_picks_older_with_items():
    """Newer cycle (effective_from=today) has no items for today's dow.
    Older cycle (effective_from = a week before) has items. Resolver must
    skip the empty newer cycle and return the older one — this is the exact
    bug the leaked `cycle 160` caused in production."""
    today = date(2026, 10, 1)   # Thursday, dow=3
    cycles = [
        _cycle(200, "2026-10-01", "2026-10-02", version=1, source="email"),   # newer, empty
        _cycle(131, "2026-09-25", "2026-10-01", version=1, source="pdf"),     # older, real
    ]
    items = [
        _item(131, dow=3, meal="breakfast", dish="Sprouts"),
        _item(131, dow=3, meal="lunch", dish="Dal"),
    ]
    svc = _client(cycles, items)

    menu = resolve_daily_menu(svc, on_date=today)

    assert menu is not None
    assert menu.cycle_id == 131, "should skip empty newer cycle and fall through to real PDF"
    assert menu.source == "pdf"
    assert len(menu.items) == 2


def test_resolver_picks_latest_effective_from_when_both_have_items():
    """Two cycles both have items for today. Resolver picks the one with
    the latest effective_from — the 'newer PDF overrides older' rule."""
    today = date(2026, 10, 1)
    cycles = [
        _cycle(300, "2026-09-30", "2026-10-07", version=1, source="pdf"),
        _cycle(131, "2026-09-25", "2026-10-01", version=1, source="pdf"),
    ]
    items = [
        _item(300, dow=3, meal="lunch", dish="New Dish"),
        _item(131, dow=3, meal="lunch", dish="Old Dish"),
    ]
    svc = _client(cycles, items)

    menu = resolve_daily_menu(svc, on_date=today)

    assert menu is not None
    assert menu.cycle_id == 300, "newer effective_from should win"


def test_resolver_ignores_effective_to_so_old_menus_stay_valid():
    """Admin uploaded a PDF a month ago with effective_to in the past. No
    newer PDF has been uploaded. Resolver should still return the old menu
    (NOT None) — menus stay in force until superseded by a newer upload."""
    today = date(2026, 10, 15)
    cycles = [
        _cycle(131, "2026-09-25", "2026-10-01", version=1, source="pdf"),
    ]
    items = [
        _item(131, dow=today.weekday(), meal="breakfast", dish="Oats"),
    ]
    svc = _client(cycles, items)

    menu = resolve_daily_menu(svc, on_date=today)

    assert menu is not None, "effective_to is informational; menu stays valid until a newer cycle supersedes it"
    assert menu.cycle_id == 131


def test_resolver_returns_none_when_no_active_cycle_has_items_for_today():
    """Dow=3 (Thursday). Only cycle in the DB has items for dow=1 (Tuesday).
    Resolver returns None → planner treats as menu-uncertain."""
    today = date(2026, 10, 1)   # Thursday
    cycles = [
        _cycle(131, "2026-09-25", "2026-10-07", version=1, source="pdf"),
    ]
    items = [
        _item(131, dow=1, meal="lunch", dish="Tuesday-only Dish"),
    ]
    svc = _client(cycles, items)

    menu = resolve_daily_menu(svc, on_date=today)

    assert menu is None


def test_resolver_returns_none_when_no_active_cycles_exist():
    today = date(2026, 10, 1)
    svc = _client(cycles=[], items=[])

    menu = resolve_daily_menu(svc, on_date=today)

    assert menu is None


def test_resolver_ignores_superseded_cycles():
    """A superseded cycle with newer effective_from must not win over an
    active cycle. This is how admin force-expires a bad menu."""
    today = date(2026, 10, 1)
    cycles = [
        _cycle(200, "2026-09-28", "2026-10-07", version=1, source="pdf", status="superseded"),
        _cycle(131, "2026-09-25", "2026-10-07", version=1, source="pdf", status="active"),
    ]
    items = [
        _item(200, dow=3, meal="dinner", dish="Should Not Win"),
        _item(131, dow=3, meal="dinner", dish="Should Win"),
    ]
    svc = _client(cycles, items)

    menu = resolve_daily_menu(svc, on_date=today)

    assert menu is not None
    assert menu.cycle_id == 131
