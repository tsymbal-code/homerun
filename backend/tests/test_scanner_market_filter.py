"""Unit tests for the tag-whitelist filter on the scanner ingest path.

Plan: 0005.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.scanner import ArbitrageScanner


def _market(
    market_id: str,
    *,
    tags: list[str] | None = None,
    event_slug: str = "",
    accepting_orders: bool | None = True,
    volume: float = 100.0,
    condition_id: str = "0xabc",
    clob_token_ids: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=market_id,
        tags=list(tags or []),
        event_slug=event_slug,
        accepting_orders=accepting_orders,
        volume=volume,
        condition_id=condition_id,
        clob_token_ids=clob_token_ids or ["t-yes", "t-no"],
        platform="polymarket",
        closed=False,
        resolved=False,
        archived=False,
        active=True,
        status="open",
    )


def _event(
    slug: str,
    *,
    tags: list[str] | None = None,
    markets: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"evt-{slug}",
        slug=slug,
        tags=list(tags or []),
        markets=list(markets or []),
    )


def test_empty_whitelist_returns_inputs_unchanged():
    m1 = _market("m1", tags=["crypto"], event_slug="ev1")
    m2 = _market("m2", tags=["politics"], event_slug="ev2")
    e1 = _event("ev1", markets=[m1])
    e2 = _event("ev2", markets=[m2])

    events, markets = ArbitrageScanner._apply_market_tag_whitelist(
        [e1, e2], [m1, m2], frozenset()
    )
    assert events == [e1, e2]
    assert markets == [m1, m2]


def test_whitelist_keeps_markets_with_matching_market_tag():
    m1 = _market("m1", tags=["crypto"], event_slug="ev1")
    m2 = _market("m2", tags=["politics"], event_slug="ev2")
    e1 = _event("ev1", markets=[m1])
    e2 = _event("ev2", markets=[m2])

    events, markets = ArbitrageScanner._apply_market_tag_whitelist(
        [e1, e2], [m1, m2], frozenset({"crypto"})
    )
    assert markets == [m1]
    assert events == [e1]
    assert e1.markets == [m1]


def test_whitelist_keeps_markets_via_event_tag_union():
    """Markets without their own tag still pass if their event has it."""
    m1 = _market("m1", tags=[], event_slug="sports-1")
    e1 = _event("sports-1", tags=["sports", "nba"], markets=[m1])

    events, markets = ArbitrageScanner._apply_market_tag_whitelist(
        [e1], [m1], frozenset({"nba"})
    )
    assert markets == [m1]
    assert events == [e1]


def test_whitelist_drops_markets_with_no_intersection():
    m1 = _market("m1", tags=["sports"], event_slug="sports-1")
    e1 = _event("sports-1", tags=["nba"], markets=[m1])

    events, markets = ArbitrageScanner._apply_market_tag_whitelist(
        [e1], [m1], frozenset({"crypto"})
    )
    assert markets == []
    assert events == []


def test_whitelist_is_case_insensitive():
    m1 = _market("m1", tags=["Crypto"], event_slug="ev1")
    e1 = _event("ev1", markets=[m1])

    events, markets = ArbitrageScanner._apply_market_tag_whitelist(
        [e1], [m1], frozenset({"crypto"})
    )
    assert markets == [m1]
    assert events == [e1]


def test_whitelist_drops_event_when_all_children_filtered_out():
    m1 = _market("m1", tags=["crypto"], event_slug="multi")
    m2 = _market("m2", tags=["politics"], event_slug="multi")
    e1 = _event("multi", markets=[m1, m2])

    events, markets = ArbitrageScanner._apply_market_tag_whitelist(
        [e1], [m1, m2], frozenset({"sports"})
    )
    assert markets == []
    assert events == []


def test_whitelist_keeps_partial_event_children():
    m1 = _market("m1", tags=["crypto"], event_slug="multi")
    m2 = _market("m2", tags=["politics"], event_slug="multi")
    e1 = _event("multi", markets=[m1, m2])

    events, markets = ArbitrageScanner._apply_market_tag_whitelist(
        [e1], [m1, m2], frozenset({"crypto"})
    )
    assert markets == [m1]
    assert events == [e1]
    assert e1.markets == [m1]


def test_whitelist_or_logic_multiple_tags():
    m1 = _market("m1", tags=["crypto"], event_slug="ev1")
    m2 = _market("m2", tags=["politics"], event_slug="ev2")
    m3 = _market("m3", tags=["sports"], event_slug="ev3")
    e1 = _event("ev1", markets=[m1])
    e2 = _event("ev2", markets=[m2])
    e3 = _event("ev3", markets=[m3])

    events, markets = ArbitrageScanner._apply_market_tag_whitelist(
        [e1, e2, e3], [m1, m2, m3], frozenset({"crypto", "politics"})
    )
    assert {m.id for m in markets} == {"m1", "m2"}
    assert {e.slug for e in events} == {"ev1", "ev2"}
