"""Plan 0046 — regression tests for the crypto-strategy parameter sweep.

These exercise the path that ``POST /api/validation/code-backtest/
optimize-strategy`` walks: build a small synthetic Chainlink history
plus a known ``firehose_evaluation`` log, then call
``run_crypto_strategy_optimize`` with a tiny grid and assert that the
leaderboard is well-formed and sorted.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.param_optimizer import DEFAULT_PARAM_SPECS, TradingParameters


def test_trading_parameters_carries_crypto_fields():
    """The dataclass exposes every crypto 5m midcycle knob the strategy reads."""
    params = TradingParameters()
    for name in (
        "min_distance_bps",
        "max_entry_price",
        "min_entry_price",
        "midcycle_seconds",
        "min_seconds_to_resolution",
        "max_oracle_age_ms",
        "bet_size_usd",
    ):
        assert name in params.__dataclass_fields__, f"missing field: {name}"
        # Default is Optional[None] — non-crypto sweeps must remain valid.
        assert getattr(params, name) is None, f"{name} default must be None"


def test_default_param_specs_includes_crypto_rows():
    crypto_keys = {
        "min_distance_bps",
        "max_entry_price",
        "min_entry_price",
        "midcycle_seconds",
        "min_seconds_to_resolution",
        "max_oracle_age_ms",
        "bet_size_usd",
    }
    spec_names = {spec.name for spec in DEFAULT_PARAM_SPECS}
    missing = crypto_keys - spec_names
    assert not missing, f"DEFAULT_PARAM_SPECS missing crypto entries: {missing}"


def _firehose_payload(
    *,
    market_id: str,
    asset: str,
    end_ms: int,
    reference: float,
    spot: float,
    distance_bps: float,
    vwap_price: float,
    staleness_ms: float = 200.0,
    oracle_age_ms: float = 300.0,
    seconds_left: float = 150.0,
) -> dict:
    gates = [
        {"name": "timeframe", "label": "5-minute timeframe", "passed": True, "score": None, "detail": "timeframe=5m"},
        {"name": "market_id", "label": "Market id present", "passed": True, "score": None, "detail": ""},
        {"name": "asset_enabled", "label": "Asset in config list", "passed": True, "score": None, "detail": f"asset={asset}"},
        {"name": "end_timestamp", "label": "Cycle end timestamp parseable", "passed": True, "score": None, "detail": f"end_ts_ms={end_ms}"},
        {"name": "midcycle_crossed", "label": "Midcycle milestone crossed", "passed": True, "score": 150.0, "detail": ""},
        {"name": "min_seconds_to_resolution", "label": "Min seconds to resolution", "passed": True, "score": seconds_left, "detail": ""},
        {"name": "reference_price", "label": "Reference price available", "passed": True, "score": float(reference), "detail": ""},
        {"name": "fresh_chainlink", "label": "Fresh Chainlink oracle", "passed": True, "score": float(oracle_age_ms), "detail": "source=chainlink"},
        {"name": "spot_price", "label": "Spot price > 0", "passed": True, "score": float(spot), "detail": ""},
        {"name": "min_distance", "label": "Min distance from reference", "passed": True, "score": float(distance_bps), "detail": ""},
        {"name": "clob_tokens", "label": "CLOB token ids present", "passed": True, "score": None, "detail": "count=2"},
        {"name": "book_depth", "label": "Order book depth available", "passed": True, "score": None, "detail": "side=YES size_usd=15.00"},
        {"name": "book_fresh", "label": "Order book fresh", "passed": True, "score": float(staleness_ms), "detail": ""},
        {"name": "vwap_in_range", "label": "VWAP within entry range", "passed": True, "score": float(vwap_price), "detail": ""},
    ]
    return {
        "strategy_slug": "crypto_5m_midcycle",
        "source_key": "crypto",
        "market": {
            "market_id": market_id,
            "slug": f"{asset.lower()}-5min-{end_ms}",
            "question": f"{asset} 5m up-or-down",
            "asset": asset,
            "timeframe": "5m",
        },
        "outcome": "emitted",
        "gates": gates,
        "bound_trader_ids": ["test-trader"],
    }


@pytest.mark.asyncio
async def test_run_crypto_strategy_optimize_returns_leaderboard(monkeypatch):
    """End-to-end sweep: 4 cycles × 4 combos → 4 leaderboard rows, sorted."""
    from models.database import (
        Base,
        CryptoOracleHistory,
        Strategy as StrategyModel,
        TraderEvent,
    )
    import services.strategy_backtester as strategy_backtester
    from tests.postgres_test_db import build_postgres_session_factory

    engine, session_factory = await build_postgres_session_factory(
        Base, "param_optimizer_crypto_sweep"
    )
    import models.database as models_database

    monkeypatch.setattr(models_database, "AsyncSessionLocal", session_factory)

    # The sweep loads the strategy's source_code from the Strategy table —
    # insert a wrapper class that re-exports the in-process
    # Crypto5mMidcycleStrategy so the loader compiles it cleanly.
    # ``StrategyLoader`` AST-validates the class body, so re-declare
    # ``on_event`` explicitly (a passthrough to the parent).
    strategy_source = '''\
from services.strategies.crypto_5m_midcycle import Crypto5mMidcycleStrategy as _Base


class Crypto5mMidcycleStrategy(_Base):
    """Test wrapper — Plan 0046 sweep regression."""

    async def on_event(self, event):
        return await _Base.on_event(self, event)
'''

    # Seed 4 cycles spread across the window.
    base_ms = int((datetime.now(timezone.utc) - timedelta(hours=6)).timestamp() * 1000)
    cycles = [
        # (offset_min, spot_delta_pct, vwap, will_win)
        (5, 0.008, 0.55, True),    # +80 bps, $15 bet, wins → +$12.27
        (10, -0.009, 0.50, True),  # -90 bps, NO side wins
        (15, 0.012, 0.65, False),  # +120 bps, YES side loses
        (20, 0.011, 0.60, True),   # +110 bps, YES side wins
    ]
    reference = 200.0
    asset = "SOL"

    async with session_factory() as session:
        for idx, (off_min, dpct, vwap, will_win) in enumerate(cycles):
            end_ms = base_ms + off_min * 60 * 1000
            midcycle_ms = end_ms - 150_000
            spot = reference * (1.0 + dpct)
            distance_bps = (spot - reference) / reference * 10_000.0
            oracle_at_end = reference * (
                1.0 + dpct + (0.0 if will_win else -2.0 * dpct)
            )
            session.add(
                CryptoOracleHistory(
                    asset=asset,
                    timestamp_ms=midcycle_ms,
                    source="chainlink",
                    price=spot,
                )
            )
            session.add(
                CryptoOracleHistory(
                    asset=asset,
                    timestamp_ms=end_ms,
                    source="chainlink",
                    price=oracle_at_end,
                )
            )
            session.add(
                TraderEvent(
                    id=f"ev-{idx}",
                    event_type="firehose_evaluation",
                    severity="info",
                    verbosity="whisper",
                    source="crypto",
                    payload_json=_firehose_payload(
                        market_id=f"mkt-{idx}",
                        asset=asset,
                        end_ms=end_ms,
                        reference=reference,
                        spot=spot,
                        distance_bps=distance_bps,
                        vwap_price=vwap,
                    ),
                    created_at=datetime.fromtimestamp(
                        midcycle_ms / 1000.0, tz=timezone.utc
                    ),
                )
            )
        await session.commit()

    async with session_factory() as session:
        session.add(
            StrategyModel(
                id="strategy-crypto-5m-midcycle-test",
                slug="crypto_5m_midcycle",
                source_key="crypto",
                name="Crypto 5m Midcycle",
                description="test",
                source_code=strategy_source,
                class_name="Crypto5mMidcycleStrategy",
                enabled=True,
                status="loaded",
                config={},
            )
        )
        await session.commit()

    grid = {
        "min_distance_bps": [5.0, 10.0],
        "max_entry_price": [0.6, 0.7],
        "assets": [["SOL"]],
    }
    result = await strategy_backtester.run_crypto_strategy_optimize(
        strategy_slug="crypto_5m_midcycle",
        window_hours=24,
        grid=grid,
        top_k=10,
    )

    assert "leaderboard" in result
    leaderboard = result["leaderboard"]
    assert isinstance(leaderboard, list)
    assert len(leaderboard) == 4, f"expected 4 combos, got {len(leaderboard)}"

    for row in leaderboard:
        assert "params" in row and isinstance(row["params"], dict)
        assert "emit_count" in row
        assert "total_pnl_usd" in row
        assert "win_rate" in row
        assert "samples" in row
        assert "composite_score" in row

    scores = [r["composite_score"] for r in leaderboard]
    assert scores == sorted(scores, reverse=True), "leaderboard must be sorted desc"

    # Window metadata is round-tripped.
    assert result["window"]["hours"] == 24

    await engine.dispose()


@pytest.mark.asyncio
async def test_run_crypto_strategy_optimize_unknown_slug_returns_caveat(monkeypatch):
    """Sweeping a slug that isn't in the strategies table returns no leaderboard."""
    from models.database import Base
    import services.strategy_backtester as strategy_backtester
    from tests.postgres_test_db import build_postgres_session_factory

    engine, session_factory = await build_postgres_session_factory(
        Base, "param_optimizer_crypto_unknown"
    )
    import models.database as models_database

    monkeypatch.setattr(models_database, "AsyncSessionLocal", session_factory)

    result = await strategy_backtester.run_crypto_strategy_optimize(
        strategy_slug="does_not_exist_anywhere",
        window_hours=1,
        grid={"min_distance_bps": [5.0]},
        top_k=10,
    )
    assert result["leaderboard"] == []
    assert any("not present" in c or "no source_code" in c for c in result["caveats"])

    await engine.dispose()
