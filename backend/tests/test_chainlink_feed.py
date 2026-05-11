from __future__ import annotations

from collections import deque
import json
import sys
import time
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models.database import Base, CryptoOracleHistory  # noqa: E402
from services.chainlink_feed import (  # noqa: E402
    BINANCE_TOPIC,
    CHAINLINK_TOPIC,
    ChainlinkFeed,
)
from sqlalchemy import select  # noqa: E402

from tests.postgres_test_db import build_postgres_session_factory  # noqa: E402


def test_handle_message_parses_snapshot_rows_with_parent_symbol():
    feed = ChainlinkFeed()
    # Use timestamps close to "now" so history entries survive pruning inside
    # _handle_message.
    now_s = int(time.time())
    ts0 = now_s - 10
    ts1 = now_s - 9
    msg = {
        "topic": CHAINLINK_TOPIC,
        "payload": {
            "symbol": "btc/usd",
            "data": [
                {"value": 67500.10, "timestamp": ts0},
                {"value": 67501.25, "timestamp": ts1},
            ],
        },
    }

    feed._handle_message(json.dumps(msg))

    latest = feed.get_price("BTC")
    assert latest is not None
    assert latest.price == 67501.25
    assert latest.updated_at_ms == ts1 * 1000
    assert latest.source == "chainlink"

    history = list(feed._history.get("BTC") or [])
    assert history == [(ts0 * 1000, 67500.10), (ts1 * 1000, 67501.25)]


def test_handle_message_parses_payload_list_and_respects_source_priority():
    feed = ChainlinkFeed()
    # Use timestamps close to "now" so history entries survive pruning inside
    # _handle_message.
    now_s = int(time.time())
    ts_binance = now_s - 10
    ts_chainlink = now_s - 9

    binance_msg = {
        "topic": BINANCE_TOPIC,
        "payload": [
            {
                "symbol": "ethusdt",
                "price": "1940.50",
                "timestamp": ts_binance,
            }
        ],
    }
    chainlink_msg = {
        "topic": CHAINLINK_TOPIC,
        "payload": {
            "symbol": "eth/usd",
            "value": 1939.80,
            "timestamp": ts_chainlink,
        },
    }

    feed._handle_message(json.dumps(binance_msg))
    first = feed.get_price("ETH")
    assert first is not None
    assert first.price == 1940.5
    assert first.source == "binance"
    # Binance prices are now stored in history as a fallback for
    # price_to_beat lookups when Chainlink WS updates are absent.
    assert list(feed._history.get("ETH") or []) == [(ts_binance * 1000, 1940.5)]

    feed._handle_message(json.dumps(chainlink_msg))
    latest = feed.get_price("ETH")
    assert latest is not None
    assert latest.price == 1939.8
    assert latest.source == "chainlink"

    by_source = feed.get_prices_by_source("ETH")
    assert set(by_source.keys()) == {"binance", "chainlink"}
    assert by_source["binance"].price == 1940.5
    assert by_source["chainlink"].price == 1939.8
    assert list(feed._history.get("ETH") or []) == [
        (ts_binance * 1000, 1940.5),
        (ts_chainlink * 1000, 1939.8),
    ]


def test_seed_history_from_klines_inserts_and_keeps_chronological_order():
    """Backfill must merge older klines without breaking later live-tick order."""
    feed = ChainlinkFeed()
    now_ms = int(time.time() * 1000)

    # Pre-existing live tick (recent)
    feed._history["BTC"] = deque([(now_ms - 60_000, 70_500.0)])

    # Backfill 5 older klines (representing minute closes)
    klines = [
        (now_ms - 360_000, 70_100.0),
        (now_ms - 300_000, 70_150.0),
        (now_ms - 240_000, 70_200.0),
        (now_ms - 180_000, 70_300.0),
        (now_ms - 120_000, 70_400.0),
    ]
    inserted = feed.seed_history_from_klines("BTC", klines)
    assert inserted == 5

    history = list(feed._history["BTC"])
    timestamps = [ts for ts, _ in history]
    assert timestamps == sorted(timestamps), "Backfilled history must be chronological"
    assert history[0] == (now_ms - 360_000, 70_100.0)
    assert history[-1] == (now_ms - 60_000, 70_500.0)


def test_seed_history_from_klines_skips_duplicates_and_stale_points():
    feed = ChainlinkFeed()
    now_ms = int(time.time() * 1000)
    feed._history["ETH"] = deque([(now_ms - 30_000, 3_200.0)])

    # Duplicate timestamp + one stale (>3h old) + one new
    klines = [
        (now_ms - 30_000, 3_200.5),  # duplicate ts → skipped
        (now_ms - 4 * 60 * 60 * 1000, 3_100.0),  # 4h old → outside cutoff
        (now_ms - 90_000, 3_180.0),  # new
    ]
    inserted = feed.seed_history_from_klines("ETH", klines)
    assert inserted == 1
    assert len(feed._history["ETH"]) == 2


def test_seed_history_from_klines_creates_history_for_new_asset():
    feed = ChainlinkFeed()
    now_ms = int(time.time() * 1000)
    inserted = feed.seed_history_from_klines("SOL", [(now_ms - 60_000, 87.5), (now_ms - 30_000, 87.6)])
    assert inserted == 2
    assert "SOL" in feed._history
    assert list(feed._history["SOL"]) == [(now_ms - 60_000, 87.5), (now_ms - 30_000, 87.6)]


def test_get_price_at_or_after_time_returns_first_point_within_delay():
    feed = ChainlinkFeed()
    now_s = int(time.time())
    target_ms = now_s * 1000
    feed._history["BTC"] = deque(
        [
            (target_ms - 20_000, 67000.0),
            (target_ms + 15_000, 67020.0),
            (target_ms + 45_000, 67050.0),
        ]
    )

    assert feed.get_price_at_or_after_time("BTC", float(now_s), max_delay_seconds=20.0) == 67020.0
    assert feed.get_price_at_or_after_time("BTC", float(now_s), max_delay_seconds=5.0) is None


def test_seed_history_then_get_oracle_history_via_reference_runtime_keeps_old_klines():
    """Time-windowed get_oracle_history must surface backfilled klines even
    when many recent live ticks exist — count-based last-N would crop them."""
    from services.reference_runtime import ReferenceRuntime
    feed = ChainlinkFeed()
    runtime = ReferenceRuntime.__new__(ReferenceRuntime)  # bypass singleton boot
    runtime._chainlink_feed = feed
    now_ms = int(time.time() * 1000)

    # Backfilled klines: minute closes covering minutes -10..-1
    klines = [(now_ms - i * 60_000, 70_000.0 + i * 5.0) for i in range(10, 0, -1)]
    feed.seed_history_from_klines("BTC", klines)
    # Many recent live ticks (last 5 seconds, 10/s) — would dominate a
    # last-N-by-count slice and erase the backfill from the chart.
    for i in range(50):
        feed._history["BTC"].append((now_ms - 5_000 + i * 100, 70_500.0 + i * 0.1))

    # Window 900s, capped at 80 points: must include both kline-era and
    # tick-era points (not purely the dense recent ticks).
    history = runtime.get_oracle_history("BTC", points=80, max_age_seconds=900.0)
    assert len(history) > 1
    timestamps = [pt["t"] for pt in history]
    earliest_returned = timestamps[0]
    latest_returned = timestamps[-1]
    assert earliest_returned <= now_ms - 8 * 60_000, (
        f"expected backfilled klines (≤-8min) to survive sampling, got earliest={earliest_returned}"
    )
    assert latest_returned >= now_ms - 1_000, (
        f"latest point must be the live-tick tail, got {latest_returned}"
    )


def test_get_oracle_history_filters_by_max_age_seconds():
    from services.reference_runtime import ReferenceRuntime
    feed = ChainlinkFeed()
    runtime = ReferenceRuntime.__new__(ReferenceRuntime)
    runtime._chainlink_feed = feed
    now_ms = int(time.time() * 1000)
    feed._history["ETH"] = deque(
        [
            (now_ms - 7200_000, 3_000.0),  # 2h old
            (now_ms - 3600_000, 3_100.0),  # 1h old
            (now_ms - 600_000, 3_200.0),   # 10min old
            (now_ms - 60_000, 3_250.0),    # 1min old
            (now_ms - 5_000, 3_260.0),     # 5s old
        ]
    )
    # 15min window must drop the 1h-old and 2h-old points.
    history = runtime.get_oracle_history("ETH", points=80, max_age_seconds=900.0)
    timestamps = [pt["t"] for pt in history]
    assert all(ts >= now_ms - 900_000 for ts in timestamps)
    # Without max_age, all five points come back.
    history_full = runtime.get_oracle_history("ETH", points=80)
    assert len(history_full) == 5


# ---------------------------------------------------------------------------
# Plan 0046 — durable oracle history persistence + housekeeper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_flush_writes_one_row_per_asset_per_second_slot(monkeypatch):
    """1 Hz throttle: simulate ~5 ticks/second across 60 seconds and the
    once-per-second background flusher; expect exactly 60 rows per asset,
    each containing the LATEST sample seen in its 1-s slot."""
    engine, session_factory = await build_postgres_session_factory(
        Base, "chainlink_persist_1hz"
    )
    import models.database as models_database
    monkeypatch.setattr(models_database, "AsyncSessionLocal", session_factory)

    feed = ChainlinkFeed()
    base_s = 1_800_000_000  # arbitrary epoch second
    for second in range(60):
        # 5 sub-ticks per second; the last one's timestamp/price should win.
        for sub in range(5):
            ts_ms = (base_s + second) * 1000 + sub * 200
            for asset, price_base in (("BTC", 70_000.0), ("ETH", 2_000.0)):
                feed._record_persist_sample(
                    asset=asset,
                    source="chainlink",
                    timestamp_ms=ts_ms,
                    price=price_base + second + sub * 0.01,
                )
        # Simulate the 1Hz background flush at the slot boundary.
        await feed._flush_persist_buffer()

    async with session_factory() as session:
        for asset, price_base in (("BTC", 70_000.0), ("ETH", 2_000.0)):
            result = await session.execute(
                select(CryptoOracleHistory)
                .where(
                    CryptoOracleHistory.asset == asset,
                    CryptoOracleHistory.source == "chainlink",
                )
                .order_by(CryptoOracleHistory.timestamp_ms.asc())
            )
            rows = list(result.scalars().all())
            assert len(rows) == 60, (
                f"expected 60 rows for {asset}, got {len(rows)} "
                f"(1Hz throttle should collapse 5 ticks/s into 1 row/s)"
            )
            buckets = [row.timestamp_ms // 1000 for row in rows]
            assert buckets == sorted(set(buckets))  # one row per bucket, ordered
            for second, row in enumerate(rows):
                # The most recent sub-tick (sub=4) carries the largest
                # timestamp_ms within each second and the largest price.
                expected_ts = (base_s + second) * 1000 + 4 * 200
                expected_price = price_base + second + 4 * 0.01
                assert row.timestamp_ms == expected_ts, (
                    f"asset={asset} second={second}: expected latest "
                    f"sub-tick ts={expected_ts}, got {row.timestamp_ms}"
                )
                assert abs(row.price - expected_price) < 1e-6

    await engine.dispose()


@pytest.mark.asyncio
async def test_persist_roundtrip_by_asset_and_end_ms(monkeypatch):
    """Reads back the latest sample at-or-before a given end_ms — this
    is the lookup shape the backtester uses for cycle resolution."""
    engine, session_factory = await build_postgres_session_factory(
        Base, "chainlink_persist_roundtrip"
    )
    import services.chainlink_feed as chainlink_feed
    monkeypatch.setattr(chainlink_feed, "AsyncSessionLocal", session_factory, raising=False)
    import models.database as models_database
    monkeypatch.setattr(models_database, "AsyncSessionLocal", session_factory)

    feed = ChainlinkFeed()
    base_ms = 1_800_000_000_000
    for i in range(0, 30):
        feed._record_persist_sample(
            asset="SOL",
            source="chainlink",
            timestamp_ms=base_ms + i * 1000,
            price=87.0 + i * 0.01,
        )
        await feed._flush_persist_buffer()

    end_ms = base_ms + 15_000  # between bucket 15 and 16
    async with session_factory() as session:
        result = await session.execute(
            select(CryptoOracleHistory)
            .where(
                CryptoOracleHistory.asset == "SOL",
                CryptoOracleHistory.source == "chainlink",
                CryptoOracleHistory.timestamp_ms <= end_ms,
            )
            .order_by(CryptoOracleHistory.timestamp_ms.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        assert row is not None
        assert row.timestamp_ms == base_ms + 15_000
        assert abs(row.price - (87.0 + 15 * 0.01)) < 1e-9

    await engine.dispose()


@pytest.mark.asyncio
async def test_housekeeper_drops_aged_rows_and_preserves_fresh(monkeypatch):
    engine, session_factory = await build_postgres_session_factory(
        Base, "chainlink_persist_housekeeper"
    )
    import services.chainlink_feed as chainlink_feed
    monkeypatch.setattr(chainlink_feed, "AsyncSessionLocal", session_factory, raising=False)
    import models.database as models_database
    monkeypatch.setattr(models_database, "AsyncSessionLocal", session_factory)

    now_ms = int(time.time() * 1000)
    aged_ms = now_ms - 20 * 24 * 3600 * 1000  # 20 days old → outside 14d retention
    fresh_ms = now_ms - 60 * 1000  # 1 minute old

    async with session_factory() as session:
        session.add(
            CryptoOracleHistory(
                asset="BTC",
                timestamp_ms=aged_ms,
                source="chainlink",
                price=50_000.0,
            )
        )
        session.add(
            CryptoOracleHistory(
                asset="BTC",
                timestamp_ms=fresh_ms,
                source="chainlink",
                price=70_000.0,
            )
        )
        await session.commit()

    feed = ChainlinkFeed()
    deleted = await feed._housekeeper_once()
    assert deleted == 1

    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(CryptoOracleHistory).order_by(
                        CryptoOracleHistory.timestamp_ms.asc()
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].timestamp_ms == fresh_ms

    await engine.dispose()
