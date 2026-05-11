from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy

from models.market import Event, Market, Token
from services.strategies.base import BaseStrategy
import services.strategy_backtester as strategy_backtester


@dataclass
class FakeOpportunity:
    id: str
    stable_id: str
    roi_percent: float
    title: str = "backtest"
    strategy_context: dict = None

    def __post_init__(self) -> None:
        if not isinstance(self.strategy_context, dict):
            self.strategy_context = {}

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "stable_id": self.stable_id,
            "roi_percent": self.roi_percent,
            "title": self.title,
            "strategy_context": dict(self.strategy_context),
        }


class DetectSyncOnlyStrategy(BaseStrategy):
    strategy_type = "unit_sync"
    name = "Unit Sync"
    description = "unit test"

    def detect(self, events, markets, prices):
        raise AssertionError("detect() should not be called when detect_sync() is overridden")

    def detect_sync(self, events, markets, prices):
        return [FakeOpportunity(id="sync_hit", stable_id="sync_hit", roi_percent=3.2)]


class DetectAsyncOnlyStrategy(BaseStrategy):
    strategy_type = "unit_async"
    name = "Unit Async"
    description = "unit test"

    def detect(self, events, markets, prices):
        raise AssertionError("detect() should not be called when detect_async() is overridden")

    async def detect_async(self, events, markets, prices):
        return [FakeOpportunity(id="async_hit", stable_id="async_hit", roi_percent=4.6)]


class ReplaySensitiveStrategy(BaseStrategy):
    strategy_type = "unit_replay"
    name = "Unit Replay"
    description = "unit test"

    def detect(self, events, markets, prices):
        opportunities = []
        for market in markets:
            if str(getattr(market, "id", "")) != "m1":
                continue
            yes = float(getattr(market, "yes_price", 0.0) or 0.0)
            no = float(getattr(market, "no_price", 0.0) or 0.0)
            if yes + no < 0.95:
                opportunities.append(FakeOpportunity(id="replay_hit", stable_id="replay_hit", roi_percent=6.5))
        return opportunities


class EvaluateSelectedStrategy(BaseStrategy):
    strategy_type = "unit_evaluate"
    name = "Unit Evaluate"
    description = "unit test"
    default_config = {
        "require_strict_ws_pricing": True,
        "require_live_market_revalidation": True,
        "max_market_data_age_ms": 1000,
    }

    def detect(self, events, markets, prices):
        return []

    def evaluate(self, signal, context):
        return SimpleNamespace(
            decision="selected",
            reason="selected",
            score=1.0,
            size_usd=25.0,
            checks=[],
        )


class MultiOpportunityStrategy(BaseStrategy):
    strategy_type = "unit_multi"
    name = "Unit Multi"
    description = "unit test"

    def detect(self, events, markets, prices):
        return [
            FakeOpportunity(id="opp_1", stable_id="opp_1", roi_percent=1.0),
            FakeOpportunity(id="opp_2", stable_id="opp_2", roi_percent=2.0),
            FakeOpportunity(id="opp_3", stable_id="opp_3", roi_percent=3.0),
        ]


class _FakeLoader:
    def __init__(self, instance):
        self._instance = instance

    def load(self, slug, source_code, config):
        return SimpleNamespace(instance=self._instance)

    def unload(self, slug):
        return None


def _make_market() -> Market:
    return Market(
        id="m1",
        condition_id="c1",
        question="Will unit test pass?",
        slug="unit-test-market",
        event_slug="event-1",
        tokens=[
            Token(token_id="yes_tok", outcome="Yes", price=0.6),
            Token(token_id="no_tok", outcome="No", price=0.4),
        ],
        clob_token_ids=["yes_tok", "no_tok"],
        outcome_prices=[0.6, 0.4],
        active=True,
        closed=False,
        liquidity=1000.0,
        volume=2000.0,
    )


def _make_event(market: Market) -> Event:
    return Event(
        id="event-1",
        slug="event-1",
        title="Unit Test Event",
        markets=[market],
        active=True,
        closed=False,
    )


def _patch_common(monkeypatch, strategy_instance: BaseStrategy, market: Market, event: Event) -> None:
    monkeypatch.setattr(
        strategy_backtester,
        "validate_strategy_source",
        lambda source: {
            "valid": True,
            "errors": [],
            "warnings": [],
            "class_name": strategy_instance.__class__.__name__,
        },
    )
    monkeypatch.setattr(strategy_backtester, "StrategyLoader", lambda: _FakeLoader(strategy_instance))
    monkeypatch.setattr(strategy_backtester.scanner, "_cached_events", [event], raising=False)
    monkeypatch.setattr(strategy_backtester.scanner, "_cached_markets", [market], raising=False)
    monkeypatch.setattr(strategy_backtester.scanner, "_cached_prices", {}, raising=False)


@pytest.mark.asyncio
async def test_run_strategy_backtest_uses_detect_sync_override(monkeypatch):
    market = _make_market()
    event = _make_event(market)
    strategy = DetectSyncOnlyStrategy()

    _patch_common(monkeypatch, strategy, market, event)
    monkeypatch.setattr(strategy_backtester.scanner, "_market_price_history", {}, raising=False)

    result = await strategy_backtester.run_strategy_backtest(
        source_code="class Dummy: pass",
        slug="sync_test",
        use_ohlc_replay=False,
    )

    assert result.success is True
    assert result.runtime_error is None
    assert result.num_opportunities == 1
    assert result.opportunities[0]["id"] == "sync_hit"


@pytest.mark.asyncio
async def test_run_strategy_backtest_uses_detect_async_override(monkeypatch):
    market = _make_market()
    event = _make_event(market)
    strategy = DetectAsyncOnlyStrategy()

    _patch_common(monkeypatch, strategy, market, event)
    monkeypatch.setattr(strategy_backtester.scanner, "_market_price_history", {}, raising=False)

    result = await strategy_backtester.run_strategy_backtest(
        source_code="class Dummy: pass",
        slug="async_test",
        use_ohlc_replay=False,
    )

    assert result.success is True
    assert result.runtime_error is None
    assert result.num_opportunities == 1
    assert result.opportunities[0]["id"] == "async_hit"


@pytest.mark.asyncio
async def test_run_strategy_backtest_replays_ohlc_when_live_snapshot_empty(monkeypatch):
    market = _make_market()
    event = _make_event(market)
    strategy = ReplaySensitiveStrategy()

    _patch_common(monkeypatch, strategy, market, event)

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    history = [
        {"t": float(now_ms - 3_600_000), "yes": 0.44, "no": 0.44},
        {"t": float(now_ms - 1_800_000), "yes": 0.43, "no": 0.43},
    ]
    monkeypatch.setattr(strategy_backtester.scanner, "_market_price_history", {"m1": history}, raising=False)

    result = await strategy_backtester.run_strategy_backtest(
        source_code="class Dummy: pass",
        slug="replay_test",
        use_ohlc_replay=True,
        replay_lookback_hours=24,
        replay_timeframe="30m",
        replay_max_markets=1,
        replay_max_steps=12,
    )

    assert result.success is True
    assert result.runtime_error is None
    assert result.replay_mode == "ohlc_replay"
    assert result.replay_steps > 0
    assert result.replay_markets == 1
    assert result.num_opportunities == 1
    assert result.opportunities[0]["id"] == "replay_hit"
    ctx = result.opportunities[0].get("strategy_context") or {}
    assert "backtest_replay_ts_ms" in ctx


@pytest.mark.asyncio
async def test_run_strategy_backtest_caps_opportunity_output(monkeypatch):
    market = _make_market()
    event = _make_event(market)
    strategy = MultiOpportunityStrategy()

    _patch_common(monkeypatch, strategy, market, event)
    monkeypatch.setattr(strategy_backtester.scanner, "_market_price_history", {}, raising=False)

    result = await strategy_backtester.run_strategy_backtest(
        source_code="class Dummy: pass",
        slug="multi_test",
        use_ohlc_replay=False,
        max_opportunities=2,
    )

    assert result.success is True
    assert result.runtime_error is None
    assert result.num_opportunities == 2
    assert [opp["id"] for opp in result.opportunities] == ["opp_1", "opp_2"]
    assert len(result.quality_reports) == 2
    assert any("truncated to 2 rows from 3 detected" in warning for warning in result.validation_warnings)


class ExitDecisionStrategy(BaseStrategy):
    strategy_type = "unit_exit"
    name = "Unit Exit"
    description = "unit test"

    def detect(self, events, markets, prices):
        return []

    def should_exit(self, position, market_state):
        if position.pnl_percent >= 10:
            return SimpleNamespace(action="close", reason="take profit", close_price=position.current_price)
        if position.pnl_percent >= 2:
            return SimpleNamespace(action="reduce", reason="trim", reduce_fraction=0.5)
        return SimpleNamespace(action="hold", reason="let it run")


class _FakeColumn:
    def desc(self):
        return self

    def __eq__(self, other):
        return ("eq", other)


class _FakeTraderPositionModel:
    status = _FakeColumn()
    first_order_at = _FakeColumn()
    created_at = _FakeColumn()


class _FakeLegacyTraderPositionModel:
    status = _FakeColumn()
    opened_at = _FakeColumn()
    created_at = _FakeColumn()


class _FakeTradeSignalEmissionModel:
    created_at = _FakeColumn()


class _FakeQuery:
    def __init__(self) -> None:
        self.order_by_args = ()

    def where(self, *args):
        return self

    def order_by(self, *args):
        self.order_by_args = args
        return self

    def limit(self, value):
        return self


class _FakeExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeAsyncSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, query):
        return _FakeExecuteResult(self._rows)


class _FakeSessionContext:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return _FakeAsyncSession(self._rows)

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_run_evaluate_backtest_skips_live_execution_freshness_gates(monkeypatch):
    strategy = EvaluateSelectedStrategy()
    now = datetime.now(timezone.utc)
    signals = [
        SimpleNamespace(
            id="sig_eval_1",
            market_id="m1",
            source="scanner",
            strategy_type="unit_evaluate",
            direction="buy_yes",
            created_at=now - timedelta(minutes=5),
            payload_json={},
        ),
    ]

    monkeypatch.setattr(
        strategy_backtester,
        "validate_strategy_source",
        lambda source: {
            "valid": True,
            "errors": [],
            "warnings": [],
            "class_name": strategy.__class__.__name__,
        },
    )
    monkeypatch.setattr(strategy_backtester, "StrategyLoader", lambda: _FakeLoader(strategy))

    import models.database as database_models

    monkeypatch.setattr(database_models, "AsyncSessionLocal", lambda: _FakeSessionContext(signals))
    monkeypatch.setattr(database_models, "TradeSignalEmission", _FakeTradeSignalEmissionModel)

    captured_query: dict[str, object] = {}

    def _fake_select(*args):
        query = _FakeQuery()
        captured_query["model"] = args[0]
        captured_query["query"] = query
        return query

    monkeypatch.setattr(sqlalchemy, "select", _fake_select)

    result = await strategy_backtester.run_evaluate_backtest(
        source_code="class Dummy: pass",
        slug="evaluate_test",
        max_signals=1,
    )

    assert result.success is True
    assert result.runtime_error is None
    assert result.num_signals == 1
    assert result.selected == 1
    assert result.blocked == 0
    assert captured_query["model"] is _FakeTradeSignalEmissionModel

    decision = result.decisions[0]
    assert decision["decision"] == "selected"
    assert any(g["gate"] == "strict_ws_pricing" and g["status"] == "skipped" for g in decision["platform_gates"])
    assert any(
        g["gate"] == "live_market_revalidation" and g["status"] == "skipped"
        for g in decision["platform_gates"]
    )
    assert any(
        g["gate"] == "market_data_freshness" and g["status"] == "skipped"
        for g in decision["platform_gates"]
    )


@pytest.mark.asyncio
async def test_run_exit_backtest_uses_existing_position_columns_and_tracks_actions(monkeypatch):
    strategy = ExitDecisionStrategy()
    now = datetime.now(timezone.utc)

    positions = [
        SimpleNamespace(
            id="p_close",
            market_id="m1",
            market_question="Market close",
            direction="buy_yes",
            mode="shadow",
            total_notional_usd=1000.0,
            avg_entry_price=0.4,
            first_order_at=now - timedelta(minutes=90),
            created_at=now - timedelta(minutes=95),
            payload_json={"entry_price": 0.4, "last_price": 0.45, "strategy_context": {"tag": "a"}},
        ),
        SimpleNamespace(
            id="p_reduce",
            market_id="m2",
            market_question="Market reduce",
            direction="buy_no",
            mode="shadow",
            total_notional_usd=800.0,
            avg_entry_price=0.5,
            first_order_at=now - timedelta(minutes=45),
            created_at=now - timedelta(minutes=50),
            payload_json={"entry_price": 0.5, "last_price": 0.515, "strategy_context": {"tag": "b"}},
        ),
        SimpleNamespace(
            id="p_hold",
            market_id="m3",
            market_question="Market hold",
            direction="buy_yes",
            mode="shadow",
            total_notional_usd=600.0,
            avg_entry_price=0.6,
            first_order_at=now - timedelta(minutes=20),
            created_at=now - timedelta(minutes=25),
            payload_json={"entry_price": 0.6, "last_price": 0.59, "strategy_context": {"tag": "c"}},
        ),
    ]

    monkeypatch.setattr(
        strategy_backtester,
        "validate_strategy_source",
        lambda source: {
            "valid": True,
            "errors": [],
            "warnings": [],
            "class_name": strategy.__class__.__name__,
        },
    )
    monkeypatch.setattr(strategy_backtester, "StrategyLoader", lambda: _FakeLoader(strategy))

    import models.database as database_models

    monkeypatch.setattr(database_models, "AsyncSessionLocal", lambda: _FakeSessionContext(positions))
    monkeypatch.setattr(database_models, "TraderPosition", _FakeTraderPositionModel)

    captured_query: dict[str, object] = {}

    def _fake_select(*args):
        query = _FakeQuery()
        captured_query["model"] = args[0]
        captured_query["query"] = query
        return query

    monkeypatch.setattr(sqlalchemy, "select", _fake_select)

    result = await strategy_backtester.run_exit_backtest(source_code="class Dummy: pass", slug="exit_test")

    assert result.success is True
    assert result.runtime_error is None
    assert result.num_positions == 3
    assert result.would_close == 1
    assert result.would_reduce == 1
    assert result.would_hold == 1
    assert result.errors == 0
    assert len(result.exit_decisions) == 3

    decisions = {row["position_id"]: row for row in result.exit_decisions}
    assert decisions["p_close"]["action"] == "close"
    assert decisions["p_reduce"]["action"] == "reduce"
    assert decisions["p_reduce"]["reduce_fraction"] == 0.5
    assert decisions["p_hold"]["action"] == "hold"
    assert decisions["p_close"]["age_minutes"] > 0
    assert decisions["p_close"]["mode"] == "shadow"
    assert captured_query["model"] is _FakeTraderPositionModel
    assert len(getattr(captured_query["query"], "order_by_args", ())) == 2


@pytest.mark.asyncio
async def test_run_exit_backtest_supports_opened_at_fallback_column(monkeypatch):
    strategy = ExitDecisionStrategy()
    now = datetime.now(timezone.utc)

    positions = [
        SimpleNamespace(
            id="p_legacy",
            market_id="m_legacy",
            market_question="Legacy opened_at position",
            direction="buy_yes",
            mode="shadow",
            total_notional_usd=500.0,
            avg_entry_price=0.45,
            created_at=now - timedelta(minutes=70),
            payload_json={"entry_price": 0.45, "last_price": 0.5, "strategy_context": {"tag": "legacy"}},
        ),
    ]

    monkeypatch.setattr(
        strategy_backtester,
        "validate_strategy_source",
        lambda source: {
            "valid": True,
            "errors": [],
            "warnings": [],
            "class_name": strategy.__class__.__name__,
        },
    )
    monkeypatch.setattr(strategy_backtester, "StrategyLoader", lambda: _FakeLoader(strategy))

    import models.database as database_models

    monkeypatch.setattr(database_models, "AsyncSessionLocal", lambda: _FakeSessionContext(positions))
    monkeypatch.setattr(database_models, "TraderPosition", _FakeLegacyTraderPositionModel)

    captured_query: dict[str, object] = {}

    def _fake_select(*args):
        query = _FakeQuery()
        captured_query["model"] = args[0]
        captured_query["query"] = query
        return query

    monkeypatch.setattr(sqlalchemy, "select", _fake_select)

    result = await strategy_backtester.run_exit_backtest(source_code="class Dummy: pass", slug="exit_legacy")

    assert result.success is True
    assert result.runtime_error is None
    assert result.num_positions == 1
    assert result.would_close == 1
    assert len(result.exit_decisions) == 1
    assert captured_query["model"] is _FakeLegacyTraderPositionModel
    assert len(getattr(captured_query["query"], "order_by_args", ())) == 2


# ---------------------------------------------------------------------------
# Plan 0046 — crypto cycle replay regression
# ---------------------------------------------------------------------------


def _firehose_payload(
    *,
    market_id: str,
    asset: str,
    end_ms: int,
    reference: float,
    spot: float,
    distance_bps: float,
    vwap_price: float,
    staleness_ms: float,
    oracle_age_ms: float,
    seconds_left: float = 150.0,
    outcome: str = "emitted",
) -> dict[str, object]:
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
        {"name": "book_depth", "label": "Order book depth available", "passed": True, "score": None, "detail": f"side={'YES' if distance_bps > 0 else 'NO'} size_usd=15.00"},
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
        "outcome": outcome,
        "gates": gates,
        "bound_trader_ids": ["test-trader"],
    }


@pytest.mark.asyncio
async def test_run_crypto_replay_detection_matches_emit_count_and_pnl(monkeypatch):
    """Plan 0046 task 2 regression: feed synth firehose_evaluation rows +
    oracle history, replay through ``Crypto5mMidcycleStrategy``, and
    verify the emitted opportunity count, PnL signs, and monotonic
    response to ``min_distance_bps``."""
    import sys
    from pathlib import Path

    BACKEND_ROOT = Path(__file__).resolve().parents[1]
    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))

    from models.database import Base, CryptoOracleHistory, TraderEvent
    from services.strategies.crypto_5m_midcycle import Crypto5mMidcycleStrategy
    from tests.postgres_test_db import build_postgres_session_factory

    engine, session_factory = await build_postgres_session_factory(
        Base, "strategy_backtester_crypto_replay"
    )
    import models.database as models_database

    monkeypatch.setattr(models_database, "AsyncSessionLocal", session_factory)

    # Synth 4 cycles spread across 24h: 2 with distance_bps high, both
    # should be emitted. One YES that wins, one NO that wins, one YES
    # that loses, one cycle below 5-bps that should never fire.
    base_ms = int((datetime.now(timezone.utc) - timedelta(hours=12)).timestamp() * 1000)
    cycles = []
    asset = "SOL"
    reference = 200.0
    for idx, (end_offset_min, spot_delta_pct, vwap, will_win) in enumerate(
        [
            (5, 0.005, 0.60, True),    # +50 bps → YES, wins (oracle stays above)
            (10, -0.004, 0.55, True),  # -40 bps → NO, wins (oracle below at end)
            (15, 0.006, 0.65, False),  # +60 bps → YES, loses (oracle flips below at end)
            (20, 0.0002, 0.50, True),  # +2 bps → below 5-bps gate, should be dropped
        ]
    ):
        end_ms = base_ms + end_offset_min * 60 * 1000
        spot = reference * (1.0 + spot_delta_pct)
        distance_bps = (spot - reference) / reference * 10_000.0
        cycles.append(
            {
                "market_id": f"5m-{asset}-{idx}",
                "end_ms": end_ms,
                "midcycle_ms": end_ms - 150_000,
                "spot": spot,
                "distance_bps": distance_bps,
                "vwap": vwap,
                "will_win": will_win,
                "oracle_at_end": (
                    reference * (1.0 + spot_delta_pct + (0.0 if will_win else -2 * spot_delta_pct))
                ),
                "side": "YES" if distance_bps > 0 else "NO",
            }
        )

    async with session_factory() as session:
        for cyc in cycles:
            session.add(
                CryptoOracleHistory(
                    asset=asset,
                    timestamp_ms=cyc["midcycle_ms"],
                    source="chainlink",
                    price=cyc["spot"],
                )
            )
            session.add(
                CryptoOracleHistory(
                    asset=asset,
                    timestamp_ms=cyc["end_ms"],
                    source="chainlink",
                    price=cyc["oracle_at_end"],
                )
            )
            session.add(
                TraderEvent(
                    id=f"ev-{cyc['market_id']}",
                    event_type="firehose_evaluation",
                    severity="info",
                    verbosity="whisper",
                    source="crypto",
                    payload_json=_firehose_payload(
                        market_id=cyc["market_id"],
                        asset=asset,
                        end_ms=cyc["end_ms"],
                        reference=reference,
                        spot=cyc["spot"],
                        distance_bps=cyc["distance_bps"],
                        vwap_price=cyc["vwap"],
                        staleness_ms=200.0,
                        oracle_age_ms=300.0,
                    ),
                    created_at=datetime.fromtimestamp(
                        cyc["midcycle_ms"] / 1000.0, tz=timezone.utc
                    ),
                )
            )
        await session.commit()

    window_start = base_ms - 60 * 60 * 1000
    window_end = base_ms + 24 * 60 * 60 * 1000

    # (a) With min_distance_bps=5 the first three cycles fire and emit.
    strategy = Crypto5mMidcycleStrategy()
    strategy.configure(
        {
            **Crypto5mMidcycleStrategy.default_config,
            "min_distance_bps": 5.0,
            "assets": ["SOL"],
        }
    )
    run = await strategy_backtester._run_crypto_replay_detection(
        strategy,
        strategy_slug="crypto_5m_midcycle",
        window_ms_start=window_start,
        window_ms_end=window_end,
    )
    assert run.runtime_error is None
    assert run.emit_count == 3, f"expected 3 emitted (matches firehose log), got {run.emit_count}"
    # PnL signs: two wins, one loss → wins exceed losses.
    assert run.win_count == 2
    assert run.loss_count == 1
    assert run.total_pnl_usd != 0.0

    # (c) Monotonicity: tightening the distance gate must not increase emit count.
    emit_counts: list[int] = []
    for threshold in (3.0, 10.0, 50.0, 70.0):
        strat = Crypto5mMidcycleStrategy()
        strat.configure(
            {
                **Crypto5mMidcycleStrategy.default_config,
                "min_distance_bps": threshold,
                "assets": ["SOL"],
            }
        )
        run_t = await strategy_backtester._run_crypto_replay_detection(
            strat,
            strategy_slug="crypto_5m_midcycle",
            window_ms_start=window_start,
            window_ms_end=window_end,
        )
        emit_counts.append(run_t.emit_count)

    assert emit_counts == sorted(emit_counts, reverse=True), (
        f"emit count must decrease monotonically as min_distance_bps rises, got {emit_counts}"
    )

    await engine.dispose()
