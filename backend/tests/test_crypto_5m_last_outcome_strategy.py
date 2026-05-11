"""Decision-gate tests for the crypto_5m_last_outcome strategy.

The strategy is intentionally simple: each new 5-minute Polymarket
up-or-down cycle opens a position on the side that won the
immediately preceding cycle, with the previous outcome reconstructed
from the change in ``price_to_beat`` across the cycle rollover.

Tests seed the WS-fed PriceCache directly with synthetic order books
(same pattern as ``test_crypto_5m_midcycle_strategy.py``) so that
``StrategySDK.get_order_book_depth`` returns deterministic results
without any real Polymarket connectivity.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from services.data_events import DataEvent
from services.optimization.vwap import OrderBookLevel
from services.strategies.crypto_5m_last_outcome import (
    Crypto5mLastOutcomeStrategy,
    crypto_5m_last_outcome_config_schema,
)
from services.ws_feeds import FeedManager, get_feed_manager


# A 5-minute cycle ending at this fixed UTC timestamp (epoch millis).
END_MS = 2_000_000_000_000
CYCLE_MS = 300_000
START_MS = END_MS - CYCLE_MS
# Default entry milestone in the strategy is 30 s into the cycle.
ENTRY_MS = START_MS + 30_000

YES_TOKEN_A = "0x" + "a" * 60
NO_TOKEN_A = "0x" + "b" * 60
YES_TOKEN_B = "0x" + "c" * 60
NO_TOKEN_B = "0x" + "d" * 60


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_cache():
    FeedManager.reset_instance()
    yield get_feed_manager().cache
    FeedManager.reset_instance()


@pytest.fixture
def strategy():
    s = Crypto5mLastOutcomeStrategy()
    s.configure({})
    return s


def _seed_book(cache, token_id: str, *, ask_price: float, ask_size: float = 1000.0) -> None:
    cache.update(
        token_id,
        bids=[OrderBookLevel(price=max(0.0, ask_price - 0.005), size=1000.0)],
        asks=[OrderBookLevel(price=ask_price, size=ask_size)],
    )


def _market(
    *,
    condition_id: str = "0xmarket_a",
    asset: str = "BTC",
    timeframe: str = "5min",
    end_ms: int = END_MS,
    reference: float = 80_000.0,
    yes_token: str = YES_TOKEN_A,
    no_token: str = NO_TOKEN_A,
) -> dict:
    end_iso = datetime.fromtimestamp(end_ms / 1000.0, tz=timezone.utc).isoformat()
    return {
        "condition_id": condition_id,
        "id": condition_id,
        "slug": f"{asset.lower()}-up-or-down-{timeframe}",
        "question": f"{asset} up or down?",
        "asset": asset,
        "timeframe": timeframe,
        "up_price": 0.55,
        "down_price": 0.45,
        "liquidity": 5000.0,
        "clob_token_ids": [yes_token, no_token],
        "end_time": end_iso,
        "price_to_beat": reference,
    }


def _observe(strategy, market: dict, *, now_ms: int) -> None:
    """Run a single eval pass purely for side-effects on rollover state
    (i.e. populate ``_last_outcome``)."""
    strategy._evaluate_market(market, now_ms=now_ms)


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


def test_config_schema_exposes_all_user_knobs():
    schema = crypto_5m_last_outcome_config_schema()
    keys = {f["key"] for f in schema["param_fields"]}
    assert {
        "enabled",
        "assets",
        "max_entry_price",
        "min_entry_price",
        "bet_size_usd",
        "entry_seconds_after_start",
    } <= keys


def test_default_config_ships_btc_only():
    s = Crypto5mLastOutcomeStrategy()
    s.configure({})
    assert s.config["assets"] == ["BTC"]


def test_configure_normalizes_assets_to_canonical_names():
    s = Crypto5mLastOutcomeStrategy()
    s.configure({"assets": "  btc , XBT , eth ,unknown_coin"})
    # XBT collapses to BTC, dedup; unknown filtered.
    assert s.config["assets"] == ["BTC", "ETH"]


# ---------------------------------------------------------------------------
# First-cycle behaviour: no previous outcome → no emit
# ---------------------------------------------------------------------------


def test_first_cycle_does_not_emit(strategy, fresh_cache):
    """Cold-boot first observation cannot follow anything."""
    _seed_book(fresh_cache, YES_TOKEN_A, ask_price=0.55)
    market = _market()
    # Even after the entry-milestone has fired, the previous outcome
    # is unknown so the strategy must not emit.
    assert strategy._evaluate_market(market, now_ms=ENTRY_MS) is None


# ---------------------------------------------------------------------------
# Rollover-driven side selection
# ---------------------------------------------------------------------------


def test_rollover_with_higher_reference_emits_yes(strategy, fresh_cache):
    """price_to_beat ↑ across rollover → previous winner was YES."""
    # Observe cycle A so the strategy records its strike.
    market_a = _market(condition_id="0xa", reference=80_000.0, end_ms=END_MS)
    _observe(strategy, market_a, now_ms=START_MS + 5_000)
    # New cycle B has a higher strike, meaning the chainlink rose
    # across the rollover — YES won cycle A.
    _seed_book(fresh_cache, YES_TOKEN_B, ask_price=0.55)
    market_b = _market(
        condition_id="0xb",
        reference=80_050.0,
        end_ms=END_MS + CYCLE_MS,
        yes_token=YES_TOKEN_B,
        no_token=NO_TOKEN_B,
    )
    opp = strategy._evaluate_market(
        market_b, now_ms=END_MS + 30_000,  # 30 s into new cycle
    )
    assert opp is not None
    assert opp.strategy_context["side"] == "YES"
    assert opp.strategy_context["previous_outcome"] == "YES"


def test_rollover_with_lower_reference_emits_no(strategy, fresh_cache):
    """price_to_beat ↓ across rollover → previous winner was NO."""
    market_a = _market(condition_id="0xa", reference=80_000.0, end_ms=END_MS)
    _observe(strategy, market_a, now_ms=START_MS + 5_000)
    _seed_book(fresh_cache, NO_TOKEN_B, ask_price=0.55)
    market_b = _market(
        condition_id="0xb",
        reference=79_950.0,
        end_ms=END_MS + CYCLE_MS,
        yes_token=YES_TOKEN_B,
        no_token=NO_TOKEN_B,
    )
    opp = strategy._evaluate_market(
        market_b, now_ms=END_MS + 30_000,
    )
    assert opp is not None
    assert opp.strategy_context["side"] == "NO"
    assert opp.strategy_context["previous_outcome"] == "NO"


def test_rollover_with_equal_reference_does_not_emit(strategy, fresh_cache):
    """Tie → outcome ambiguous, treated as unknown."""
    market_a = _market(condition_id="0xa", reference=80_000.0, end_ms=END_MS)
    _observe(strategy, market_a, now_ms=START_MS + 5_000)
    _seed_book(fresh_cache, YES_TOKEN_B, ask_price=0.55)
    market_b = _market(
        condition_id="0xb",
        reference=80_000.0,  # exactly equal
        end_ms=END_MS + CYCLE_MS,
        yes_token=YES_TOKEN_B,
        no_token=NO_TOKEN_B,
    )
    assert strategy._evaluate_market(
        market_b, now_ms=END_MS + 30_000,
    ) is None


# ---------------------------------------------------------------------------
# Filters: timeframe, asset
# ---------------------------------------------------------------------------


def test_skipped_when_timeframe_is_not_5m(strategy, fresh_cache):
    _seed_book(fresh_cache, YES_TOKEN_A, ask_price=0.55)
    market = _market(timeframe="15min")
    assert strategy._evaluate_market(market, now_ms=ENTRY_MS) is None


def test_skipped_when_asset_not_in_enabled_list(strategy, fresh_cache):
    """Default ships BTC only — SOL is rejected."""
    _seed_book(fresh_cache, YES_TOKEN_A, ask_price=0.55)
    market = _market(asset="SOL")
    assert strategy._evaluate_market(market, now_ms=ENTRY_MS) is None


def test_user_extended_asset_list_accepts_sol(fresh_cache):
    s = Crypto5mLastOutcomeStrategy()
    s.configure({"assets": ["BTC", "SOL"]})
    # Seed previous-cycle state so SOL has an outcome to follow.
    sol_a = _market(condition_id="0xa", asset="SOL", reference=150.0)
    _observe(s, sol_a, now_ms=START_MS + 5_000)
    _seed_book(fresh_cache, YES_TOKEN_B, ask_price=0.55)
    sol_b = _market(
        condition_id="0xb",
        asset="SOL",
        reference=151.0,
        end_ms=END_MS + CYCLE_MS,
        yes_token=YES_TOKEN_B,
        no_token=NO_TOKEN_B,
    )
    opp = s._evaluate_market(sol_b, now_ms=END_MS + 30_000)
    assert opp is not None
    assert opp.strategy_context["asset"] == "SOL"


# ---------------------------------------------------------------------------
# Entry-milestone behaviour
# ---------------------------------------------------------------------------


def test_skipped_before_entry_milestone(strategy, fresh_cache):
    # First populate previous-outcome state by simulating a rollover.
    _observe(
        strategy,
        _market(condition_id="0xa", reference=80_000.0, end_ms=END_MS),
        now_ms=START_MS + 5_000,
    )
    _observe(
        strategy,
        _market(
            condition_id="0xb", reference=80_050.0, end_ms=END_MS + CYCLE_MS,
        ),
        now_ms=END_MS + 1_000,  # very early in new cycle — milestone not crossed
    )
    _seed_book(fresh_cache, YES_TOKEN_B, ask_price=0.55)
    # 5 s into new cycle — milestone is at 30 s.
    assert (
        strategy._evaluate_market(
            _market(
                condition_id="0xb",
                reference=80_050.0,
                end_ms=END_MS + CYCLE_MS,
                yes_token=YES_TOKEN_B,
                no_token=NO_TOKEN_B,
            ),
            now_ms=END_MS + 5_000,
        )
        is None
    )


def test_idempotent_within_same_cycle(strategy, fresh_cache):
    """One emit per cycle — second call inside the same cycle returns None."""
    _observe(
        strategy,
        _market(condition_id="0xa", reference=80_000.0, end_ms=END_MS),
        now_ms=START_MS + 5_000,
    )
    _seed_book(fresh_cache, YES_TOKEN_B, ask_price=0.55)
    new_market = _market(
        condition_id="0xb",
        reference=80_050.0,
        end_ms=END_MS + CYCLE_MS,
        yes_token=YES_TOKEN_B,
        no_token=NO_TOKEN_B,
    )
    first = strategy._evaluate_market(new_market, now_ms=END_MS + 30_000)
    second = strategy._evaluate_market(new_market, now_ms=END_MS + 60_000)
    assert first is not None
    assert second is None


# ---------------------------------------------------------------------------
# VWAP entry band
# ---------------------------------------------------------------------------


def test_skipped_when_vwap_above_max_entry_price(strategy, fresh_cache):
    _observe(
        strategy,
        _market(condition_id="0xa", reference=80_000.0, end_ms=END_MS),
        now_ms=START_MS + 5_000,
    )
    # Default max_entry_price is 0.95 — seed at 0.97.
    _seed_book(fresh_cache, YES_TOKEN_B, ask_price=0.97)
    new_market = _market(
        condition_id="0xb",
        reference=80_050.0,
        end_ms=END_MS + CYCLE_MS,
        yes_token=YES_TOKEN_B,
        no_token=NO_TOKEN_B,
    )
    assert strategy._evaluate_market(new_market, now_ms=END_MS + 30_000) is None


def test_skipped_when_vwap_below_min_entry_price(strategy, fresh_cache):
    _observe(
        strategy,
        _market(condition_id="0xa", reference=80_000.0, end_ms=END_MS),
        now_ms=START_MS + 5_000,
    )
    # Default min_entry_price is 0.05 — seed at 0.02.
    _seed_book(fresh_cache, YES_TOKEN_B, ask_price=0.02)
    new_market = _market(
        condition_id="0xb",
        reference=80_050.0,
        end_ms=END_MS + CYCLE_MS,
        yes_token=YES_TOKEN_B,
        no_token=NO_TOKEN_B,
    )
    assert strategy._evaluate_market(new_market, now_ms=END_MS + 30_000) is None


def test_skipped_when_book_unavailable(strategy, fresh_cache):
    _observe(
        strategy,
        _market(condition_id="0xa", reference=80_000.0, end_ms=END_MS),
        now_ms=START_MS + 5_000,
    )
    # No _seed_book — book empty → depth gate rejects.
    new_market = _market(
        condition_id="0xb",
        reference=80_050.0,
        end_ms=END_MS + CYCLE_MS,
        yes_token=YES_TOKEN_B,
        no_token=NO_TOKEN_B,
    )
    assert strategy._evaluate_market(new_market, now_ms=END_MS + 30_000) is None


# ---------------------------------------------------------------------------
# Master switch
# ---------------------------------------------------------------------------


def test_disabled_strategy_emits_nothing(fresh_cache):
    s = Crypto5mLastOutcomeStrategy()
    s.configure({"enabled": False})
    _seed_book(fresh_cache, YES_TOKEN_A, ask_price=0.55)
    event = DataEvent(
        event_type="crypto_update",
        source="test",
        timestamp=datetime.now(timezone.utc),
        payload={"markets": [_market()]},
    )
    result = asyncio.run(s.on_event(event))
    assert result == []


# ---------------------------------------------------------------------------
# Per-asset state isolation
# ---------------------------------------------------------------------------


def test_per_asset_state_is_isolated(fresh_cache):
    """BTC rollover must not change SOL's last_outcome and vice versa."""
    s = Crypto5mLastOutcomeStrategy()
    s.configure({"assets": ["BTC", "SOL"]})

    # BTC cycle 1 → cycle 2 with rising reference → BTC.last_outcome = YES
    _observe(s, _market(condition_id="0xbtc1", asset="BTC", reference=80_000.0,
                        end_ms=END_MS), now_ms=START_MS + 5_000)
    _observe(s, _market(condition_id="0xbtc2", asset="BTC", reference=80_100.0,
                        end_ms=END_MS + CYCLE_MS), now_ms=END_MS + 5_000)
    assert s._last_outcome["BTC"] == "YES"
    # SOL hasn't been observed at all yet.
    assert "SOL" not in s._last_outcome

    # Now observe SOL across a falling-reference rollover.
    _observe(s, _market(condition_id="0xsol1", asset="SOL", reference=150.0,
                        end_ms=END_MS), now_ms=START_MS + 5_000)
    _observe(s, _market(condition_id="0xsol2", asset="SOL", reference=149.0,
                        end_ms=END_MS + CYCLE_MS), now_ms=END_MS + 5_000)
    assert s._last_outcome["SOL"] == "NO"
    # BTC's outcome must remain untouched.
    assert s._last_outcome["BTC"] == "YES"


# ---------------------------------------------------------------------------
# Happy path full payload
# ---------------------------------------------------------------------------


def test_happy_path_opportunity_carries_full_context(strategy, fresh_cache):
    _observe(
        strategy,
        _market(condition_id="0xa", reference=80_000.0, end_ms=END_MS),
        now_ms=START_MS + 5_000,
    )
    _seed_book(fresh_cache, YES_TOKEN_B, ask_price=0.55)
    new_market = _market(
        condition_id="0xb",
        reference=80_050.0,
        end_ms=END_MS + CYCLE_MS,
        yes_token=YES_TOKEN_B,
        no_token=NO_TOKEN_B,
    )
    opp = strategy._evaluate_market(new_market, now_ms=END_MS + 30_000)
    assert opp is not None
    ctx = opp.strategy_context
    assert ctx["strategy"] == "crypto_5m_last_outcome"
    assert ctx["asset"] == "BTC"
    assert ctx["timeframe"] == "5min"
    assert ctx["side"] == "YES"
    assert ctx["previous_outcome"] == "YES"
    assert ctx["reference_price"] == pytest.approx(80_050.0)
    assert ctx["bet_size_usd"] == pytest.approx(15.0)
