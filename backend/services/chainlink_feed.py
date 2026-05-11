"""Chainlink oracle price feed via Polymarket's Real Time Data Stream (RTDS).

Subscribes to ``wss://ws-live-data.polymarket.com`` topics:
  - ``crypto_prices_chainlink`` — Chainlink oracle prices (resolution source)
  - ``crypto_prices`` — Binance exchange prices (more frequent updates)

These are the **exact prices** Polymarket uses to resolve 15-minute
crypto markets.  Maintains a rolling history buffer so the "price to beat"
can be looked up for any recent timestamp, even if the app started mid-window.

See: https://docs.polymarket.com/developers/RTDS/RTDS-crypto-prices
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional, Callable

import websockets

from utils.logger import get_logger

logger = get_logger(__name__)

WS_URL = "wss://ws-live-data.polymarket.com"
CHAINLINK_TOPIC = "crypto_prices_chainlink"
BINANCE_TOPIC = "crypto_prices"
RECONNECT_BASE_MS = 500
RECONNECT_MAX_MS = 10_000

# Rolling history: keep enough oracle context for fast crypto strategies that
# validate 5m/30m/2h motion before submitting live orders.
HISTORY_MAX_AGE_MS = 3 * 60 * 60 * 1000
HISTORY_MAX_ENTRIES = 40_000

# ---------------------------------------------------------------------------
# Persistence (Plan 0046) — extend the in-memory deque with a durable rolling
# table so backtests can replay any cycle in the retention window.
# ---------------------------------------------------------------------------
# Throttle: at most one row per (asset, source) per 1-second slot — drop
# intermediate readings, keep the latest sample for the slot.
PERSIST_FLUSH_INTERVAL_S = 1.0
# Housekeeper: prune rows older than this every six hours.
PERSIST_RETENTION_DAYS = 14
PERSIST_HOUSEKEEPER_INTERVAL_S = 6 * 3600

# Maps RTDS symbols to our canonical asset names
# Chainlink uses "btc/usd", Binance uses "btcusdt"
_SYMBOL_MAP = {
    "btc/usd": "BTC",
    "eth/usd": "ETH",
    "sol/usd": "SOL",
    "xrp/usd": "XRP",
    "btcusdt": "BTC",
    "ethusdt": "ETH",
    "solusdt": "SOL",
    "xrpusdt": "XRP",
    # Fallback substring matches
    "btc": "BTC",
    "bitcoin": "BTC",
    "eth": "ETH",
    "ethereum": "ETH",
    "sol": "SOL",
    "solana": "SOL",
    "xrp": "XRP",
    "ripple": "XRP",
}


class OraclePrice:
    """A single oracle price snapshot."""

    __slots__ = ("asset", "price", "updated_at_ms", "source")

    def __init__(self, asset: str, price: float, updated_at_ms: Optional[int] = None):
        self.asset = asset
        self.price = price
        self.updated_at_ms = updated_at_ms or int(time.time() * 1000)
        self.source = "chainlink_polymarket_ws"

    def to_dict(self) -> dict:
        return {
            "asset": self.asset,
            "price": self.price,
            "updated_at_ms": self.updated_at_ms,
            "age_seconds": (time.time() * 1000 - self.updated_at_ms) / 1000 if self.updated_at_ms else None,
            "source": self.source,
        }


class ChainlinkFeed:
    """WebSocket client for Polymarket's Chainlink oracle price feed.

    Usage::

        feed = ChainlinkFeed()
        await feed.start()

        # Get latest prices
        btc = feed.get_price("BTC")  # -> OraclePrice or None
        all_prices = feed.get_all_prices()  # -> dict[str, OraclePrice]

        await feed.stop()
    """

    def __init__(self, ws_url: str = WS_URL):
        self._ws_url = ws_url
        self._prices: dict[str, OraclePrice] = {}
        # Latest price seen per canonical symbol per source.
        self._prices_by_source: dict[str, dict[str, OraclePrice]] = {}
        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        self._on_update: Optional[Callable[[OraclePrice], None]] = None
        # Rolling price history per asset: deque of (timestamp_ms, price)
        self._history: dict[str, deque] = {}
        # Plan 0046 persistence buffer. Per (asset, source) we keep the
        # latest seen (timestamp_ms, price); _persist_loop drains it once
        # per second, writing one row per (asset, source, 1s-slot).
        self._persist_pending: dict[tuple[str, str], tuple[int, float]] = {}
        self._persist_last_bucket: dict[tuple[str, str], int] = {}
        self._persist_task: Optional[asyncio.Task] = None
        self._housekeeper_task: Optional[asyncio.Task] = None

    @property
    def started(self) -> bool:
        return self._task is not None and not self._task.done()

    def get_price(self, asset: str) -> Optional[OraclePrice]:
        """Get the latest oracle price for an asset (e.g. 'BTC')."""
        return self._prices.get(asset.upper())

    def get_all_prices(self) -> dict[str, OraclePrice]:
        """Get all latest oracle prices."""
        return dict(self._prices)

    def get_prices_by_source(self, asset: str) -> dict[str, OraclePrice]:
        """Get latest prices grouped by source for a canonical asset.

        Keys are source IDs (e.g. ``chainlink`` / ``binance``).
        """
        return dict(self._prices_by_source.get(asset.upper(), {}))

    def seed_history_from_klines(
        self,
        asset: str,
        klines: list[tuple[int, float]],
    ) -> int:
        """Insert backfilled (timestamp_ms, price) pairs into the rolling history.

        Used by the crypto worker to seed a market window's worth of
        oracle history right after startup so the UI sparklines aren't
        empty until enough live ticks arrive.  Skips points already
        present at the same timestamp and points older than
        ``HISTORY_MAX_AGE_MS``; re-sorts the deque after insertion so
        live ticks appended later remain in chronological order.
        """
        asset_upper = str(asset or "").strip().upper()
        if not asset_upper or not klines:
            return 0
        if asset_upper not in self._history:
            self._history[asset_upper] = deque(maxlen=HISTORY_MAX_ENTRIES)
        history = self._history[asset_upper]
        existing_ts = {int(ts) for ts, _ in history}
        cutoff = int(time.time() * 1000) - HISTORY_MAX_AGE_MS
        inserted = 0
        for ts_ms, price in klines:
            try:
                ts_int = int(ts_ms)
                price_f = float(price)
            except (TypeError, ValueError):
                continue
            if ts_int < cutoff or price_f <= 0 or ts_int in existing_ts:
                continue
            history.append((ts_int, price_f))
            existing_ts.add(ts_int)
            inserted += 1
        if inserted > 0:
            ordered = sorted(history, key=lambda pair: int(pair[0]))
            history.clear()
            for pair in ordered:
                history.append(pair)
        return inserted

    def get_price_at_time(self, asset: str, timestamp_s: float) -> Optional[float]:
        """Get the oracle price closest to a given Unix timestamp (seconds).

        Searches the rolling history buffer for the Chainlink price
        recorded closest to ``timestamp_s``.  Returns None if no history
        is available within 60 seconds of the target time.

        This is used to determine the "price to beat" for a market whose
        ``eventStartTime`` is known.
        """
        asset = asset.upper()
        history = self._history.get(asset)
        if not history:
            return None

        target_ms = timestamp_s * 1000
        best_price = None
        best_dist = float("inf")

        for ts_ms, price in history:
            dist = abs(ts_ms - target_ms)
            if dist < best_dist:
                best_dist = dist
                best_price = price

        # Only return if within 60 seconds of target
        if best_dist <= 60_000:
            return best_price
        return None

    def get_price_at_or_after_time(
        self,
        asset: str,
        timestamp_s: float,
        *,
        max_delay_seconds: float = 300.0,
    ) -> Optional[float]:
        """Get the first Chainlink price at-or-after ``timestamp_s``.

        Returns None when no recorded price exists within ``max_delay_seconds``
        after the target timestamp.
        """
        asset = asset.upper()
        history = self._history.get(asset)
        if not history:
            return None

        target_ms = int(float(timestamp_s) * 1000.0)
        max_delay_ms = max(0, int(float(max_delay_seconds) * 1000.0))

        for ts_ms, price in history:
            if ts_ms < target_ms:
                continue
            if ts_ms - target_ms > max_delay_ms:
                return None
            return price
        return None

    def _record_persist_sample(
        self,
        *,
        asset: str,
        source: str,
        timestamp_ms: int,
        price: float,
    ) -> None:
        """Stage the latest sample for the persistence flusher.

        Plan 0046 — drop intermediate readings, keep only the most
        recent sample per ``(asset, source)`` until the periodic
        flusher writes it. The 1-second bucket throttle lives in the
        flusher itself.
        """
        key = (asset.upper(), str(source or "").strip().lower())
        if not key[0] or not key[1]:
            return
        try:
            ts_int = int(timestamp_ms)
            price_f = float(price)
        except (TypeError, ValueError):
            return
        if not (price_f > 0):
            return
        self._persist_pending[key] = (ts_int, price_f)

    def update_from_chainlink_direct(
        self,
        asset: str,
        price: float,
        bid: Optional[float],
        ask: Optional[float],
        timestamp_ms: int,
    ) -> None:
        """Ingest a direct Chainlink Data Streams price into ``_prices_by_source``.

        Called from the :class:`~services.chainlink_direct_feed.ChainlinkDirectFeed`
        on_update callback. Writes to source ``"chainlink_direct"`` alongside
        the existing ``"chainlink"`` (RTDS-relayed) and ``"binance"`` entries
        so the source-comparison panel can render the relay-vs-direct delta.

        Does NOT overwrite ``_prices[asset]`` — the RTDS-relayed Chainlink
        remains the primary price for backwards compatibility with all
        existing strategy code that reads ``feed.get_price(asset)``.
        """
        asset = asset.upper()
        oracle = OraclePrice(asset=asset, price=price, updated_at_ms=timestamp_ms)
        oracle.source = "chainlink_direct"
        if asset not in self._prices_by_source:
            self._prices_by_source[asset] = {}
        self._prices_by_source[asset]["chainlink_direct"] = oracle

        # History append so price-to-beat lookups have direct data when the
        # RTDS relay is delayed. Same pruning rules as other sources.
        if asset not in self._history:
            self._history[asset] = deque(maxlen=HISTORY_MAX_ENTRIES)
        self._history[asset].append((int(timestamp_ms), float(price)))
        cutoff = int(time.time() * 1000) - HISTORY_MAX_AGE_MS
        while self._history[asset] and self._history[asset][0][0] < cutoff:
            self._history[asset].popleft()
        self._record_persist_sample(
            asset=asset,
            source="chainlink_direct",
            timestamp_ms=timestamp_ms,
            price=price,
        )

    def update_from_binance_direct(
        self,
        asset: str,
        mid: float,
        bid: float,
        ask: float,
        timestamp_ms: int,
    ) -> None:
        """Ingest a direct Binance price update into ``_prices_by_source``.

        Called by the :class:`~services.binance_feed.BinanceFeed` on_update
        callback (wired in ``crypto_worker``).  Writes to source
        ``"binance_direct"`` alongside the existing ``"binance"``
        (RTDS-relayed) and ``"chainlink"`` entries.

        Does NOT overwrite ``_prices[asset]`` — Chainlink remains the
        primary price.  Only sets ``_prices[asset]`` as a last resort if
        no other source has written yet.
        """
        asset = asset.upper()
        oracle = OraclePrice(asset=asset, price=mid, updated_at_ms=timestamp_ms)
        oracle.source = "binance_direct"

        if asset not in self._prices_by_source:
            self._prices_by_source[asset] = {}
        self._prices_by_source[asset]["binance_direct"] = oracle

        # Only write to primary price dict if no other source exists yet.
        existing = self._prices.get(asset)
        if existing is None:
            self._prices[asset] = oracle
            if self._on_update:
                try:
                    self._on_update(oracle)
                except Exception:
                    pass

        # Store in history so get_price_at_time() can resolve price_to_beat
        # even when Chainlink WS updates are absent.
        if asset not in self._history:
            self._history[asset] = deque(maxlen=HISTORY_MAX_ENTRIES)
        self._history[asset].append((int(timestamp_ms), float(mid)))
        cutoff = int(time.time() * 1000) - HISTORY_MAX_AGE_MS
        while self._history[asset] and self._history[asset][0][0] < cutoff:
            self._history[asset].popleft()
        self._record_persist_sample(
            asset=asset,
            source="binance_direct",
            timestamp_ms=timestamp_ms,
            price=mid,
        )

    def on_update(self, callback: Callable[[OraclePrice], None]) -> None:
        """Register a callback for price updates."""
        self._on_update = callback

    async def start(self) -> None:
        """Start the WebSocket connection in the background."""
        if self._task and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run_loop())
        # Plan 0046 — durable oracle history for backtest replay.
        if self._persist_task is None or self._persist_task.done():
            self._persist_task = asyncio.create_task(self._persist_loop())
        if self._housekeeper_task is None or self._housekeeper_task.done():
            self._housekeeper_task = asyncio.create_task(self._housekeeper_loop())
        logger.info(f"ChainlinkFeed: starting connection to {self._ws_url}")

    async def stop(self) -> None:
        """Stop the WebSocket connection."""
        self._stopped = True
        for task_attr in ("_task", "_persist_task", "_housekeeper_task"):
            task = getattr(self, task_attr, None)
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            setattr(self, task_attr, None)
        # Flush any remaining staged samples before returning so a stop
        # right after a final tick doesn't drop the row.
        try:
            await self._flush_persist_buffer()
        except Exception as exc:
            logger.debug("ChainlinkFeed: final persist flush failed: %s", exc)
        logger.info("ChainlinkFeed: stopped")

    async def _flush_persist_buffer(self) -> int:
        """Write staged samples to ``crypto_oracle_history``.

        Plan 0046 — bucket each ``(asset, source)`` sample to a
        1-second slot; drop intermediate readings; write the latest
        sample for each new slot. Returns the number of rows written.
        """
        if not self._persist_pending:
            return 0
        # Snapshot the pending map and clear immediately so concurrent
        # ingestion can keep recording while we're inside the DB call.
        pending = self._persist_pending
        self._persist_pending = {}

        rows: list[dict] = []
        new_buckets: dict[tuple[str, str], int] = {}
        for key, (ts_ms, price) in pending.items():
            bucket = ts_ms // 1000
            if self._persist_last_bucket.get(key) == bucket:
                continue
            rows.append(
                {
                    "asset": key[0],
                    "source": key[1],
                    "timestamp_ms": int(ts_ms),
                    "price": float(price),
                }
            )
            new_buckets[key] = bucket
        if not rows:
            return 0
        try:
            from models.database import AsyncSessionLocal, CryptoOracleHistory
            from sqlalchemy.dialects.postgresql import insert as pg_insert
        except Exception as exc:
            logger.debug("ChainlinkFeed: persist deferred (model import): %s", exc)
            # Put the staged samples back so the next flush retries.
            for key, (ts_ms, price) in pending.items():
                self._persist_pending.setdefault(key, (ts_ms, price))
            return 0

        try:
            async with AsyncSessionLocal() as session:
                stmt = pg_insert(CryptoOracleHistory).values(rows)
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["asset", "timestamp_ms", "source"]
                )
                await session.execute(stmt)
                await session.commit()
        except Exception as exc:
            logger.debug("ChainlinkFeed: persist flush failed: %s", exc)
            for key, (ts_ms, price) in pending.items():
                self._persist_pending.setdefault(key, (ts_ms, price))
            return 0

        for key, bucket in new_buckets.items():
            self._persist_last_bucket[key] = bucket
        return len(rows)

    async def _persist_loop(self) -> None:
        """Background flusher — calls ``_flush_persist_buffer`` at 1 Hz."""
        while not self._stopped:
            try:
                await asyncio.sleep(PERSIST_FLUSH_INTERVAL_S)
                if self._stopped:
                    break
                await self._flush_persist_buffer()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("ChainlinkFeed: persist loop tick failed: %s", exc)

    async def _housekeeper_loop(self) -> None:
        """Prune ``crypto_oracle_history`` rows older than the retention horizon.

        Runs once per ``PERSIST_HOUSEKEEPER_INTERVAL_S`` (6h) and deletes
        rows whose ``timestamp_ms`` is older than ``PERSIST_RETENTION_DAYS``
        days.
        """
        # Short grace period before the first prune so startup isn't
        # dominated by housekeeping.
        try:
            await asyncio.sleep(min(60.0, PERSIST_HOUSEKEEPER_INTERVAL_S))
        except asyncio.CancelledError:
            return
        while not self._stopped:
            try:
                await self._housekeeper_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "ChainlinkFeed: housekeeper tick failed: %s", exc
                )
            try:
                await asyncio.sleep(PERSIST_HOUSEKEEPER_INTERVAL_S)
            except asyncio.CancelledError:
                break

    async def _housekeeper_once(self) -> int:
        """Delete rows older than the retention horizon. Returns row count."""
        try:
            from models.database import AsyncSessionLocal, CryptoOracleHistory
            from sqlalchemy import delete
        except Exception as exc:
            logger.debug("ChainlinkFeed: housekeeper deferred (import): %s", exc)
            return 0
        cutoff_ms = int(
            (
                datetime.now(timezone.utc) - timedelta(days=PERSIST_RETENTION_DAYS)
            ).timestamp()
            * 1000
        )
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                delete(CryptoOracleHistory).where(
                    CryptoOracleHistory.timestamp_ms < cutoff_ms
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def _run_loop(self) -> None:
        """Reconnecting WebSocket loop."""
        reconnect_ms = RECONNECT_BASE_MS

        while not self._stopped:
            try:
                async with websockets.connect(
                    self._ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    reconnect_ms = RECONNECT_BASE_MS
                    logger.info("ChainlinkFeed: connected")

                    # Subscribe to both Chainlink (resolution source) and
                    # Binance (more frequent updates) price feeds
                    sub_msg = json.dumps(
                        {
                            "action": "subscribe",
                            "subscriptions": [
                                {
                                    "topic": CHAINLINK_TOPIC,
                                    "type": "update",
                                    "filters": "",
                                },
                                {
                                    "topic": BINANCE_TOPIC,
                                    "type": "update",
                                },
                            ],
                        }
                    )
                    await ws.send(sub_msg)
                    logger.info(f"ChainlinkFeed: subscribed to {CHAINLINK_TOPIC} + {BINANCE_TOPIC}")

                    async for raw in ws:
                        if self._stopped:
                            break
                        self._handle_message(raw)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._stopped:
                    break
                logger.debug(f"ChainlinkFeed: connection error: {e}")
                await asyncio.sleep(reconnect_ms / 1000.0)
                reconnect_ms = min(int(reconnect_ms * 1.5), RECONNECT_MAX_MS)

    def _handle_message(self, raw: str | bytes) -> None:
        """Parse a WebSocket message and update the price cache + history."""
        msg_str = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        if not msg_str.strip():
            return

        try:
            data = json.loads(msg_str)
        except json.JSONDecodeError:
            return

        topic = data.get("topic")
        if topic not in (CHAINLINK_TOPIC, BINANCE_TOPIC):
            return

        payload = data.get("payload")
        if payload is None:
            return
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return
        payload_rows: list[dict] = []
        if isinstance(payload, dict):
            data_points = payload.get("data")
            if isinstance(data_points, list):
                base_symbol = payload.get("symbol") or payload.get("pair") or payload.get("ticker")
                for row in data_points:
                    if not isinstance(row, dict):
                        continue
                    if (
                        base_symbol
                        and row.get("symbol") is None
                        and row.get("pair") is None
                        and row.get("ticker") is None
                    ):
                        normalized_row = dict(row)
                        normalized_row["symbol"] = base_symbol
                        payload_rows.append(normalized_row)
                    else:
                        payload_rows.append(row)
            else:
                payload_rows.append(payload)
        elif isinstance(payload, list):
            payload_rows.extend([row for row in payload if isinstance(row, dict)])
        else:
            return

        if not payload_rows:
            return

        # Determine source priority: Chainlink is authoritative (resolution source)
        is_chainlink = topic == CHAINLINK_TOPIC
        source = "chainlink" if is_chainlink else "binance"

        for row in payload_rows:
            # Extract symbol
            symbol = str(row.get("symbol") or row.get("pair") or row.get("ticker") or "").lower()

            # Map to canonical asset -- try exact match first, then substring
            asset = _SYMBOL_MAP.get(symbol)
            if not asset:
                for keyword, canonical in _SYMBOL_MAP.items():
                    if keyword in symbol:
                        asset = canonical
                        break
            if not asset:
                continue

            # Extract price (field is "value" per RTDS docs)
            price_val = row.get("value") or row.get("price") or row.get("current")
            try:
                price = float(price_val)
            except (TypeError, ValueError):
                continue
            if not (price > 0):
                continue

            # Extract timestamp
            ts_val = row.get("timestamp") or row.get("updatedAt")
            updated_at_ms = None
            if ts_val is not None:
                try:
                    ts_float = float(ts_val)
                    updated_at_ms = int(ts_float * 1000) if ts_float < 1e12 else int(ts_float)
                except (TypeError, ValueError):
                    pass
            if updated_at_ms is None:
                updated_at_ms = int(time.time() * 1000)

            # Update latest price (Chainlink takes priority over Binance)
            oracle = OraclePrice(
                asset=asset,
                price=price,
                updated_at_ms=updated_at_ms,
            )
            oracle.source = source

            if asset not in self._prices_by_source:
                self._prices_by_source[asset] = {}
            self._prices_by_source[asset][source] = oracle

            existing = self._prices.get(asset)
            if existing is None or is_chainlink or source == existing.source:
                self._prices[asset] = oracle

                # Crypto latency harness: record wire arrival per asset.
                # Use local-receive time (not the oracle's source-claimed
                # ``updated_at_ms``) so the metric is consistent with the
                # Binance/Polymarket recorders — those also stamp
                # local-receive.  This makes ``freshest_wire_ts_ms()``
                # mean "the most recent message the local process saw",
                # which is the comparison we need for end-to-end pipeline
                # latency measurement.  Sub-microsecond cost; recorder is
                # try/except-wrapped.
                try:
                    from services.crypto_latency_trace import record_wire_event

                    record_wire_event("chainlink", asset, int(time.time() * 1000))
                except Exception:
                    pass

                if self._on_update:
                    try:
                        self._on_update(oracle)
                    except Exception:
                        pass

            # Store in history for price-to-beat lookups.
            # Chainlink is the canonical resolution source, but Binance
            # prices are stored as a fallback so get_price_at_time() can
            # still resolve when Chainlink WS updates are absent.
            if asset not in self._history:
                self._history[asset] = deque(maxlen=HISTORY_MAX_ENTRIES)
            self._history[asset].append((updated_at_ms, price))

            # Prune old entries
            cutoff = int(time.time() * 1000) - HISTORY_MAX_AGE_MS
            while self._history[asset] and self._history[asset][0][0] < cutoff:
                self._history[asset].popleft()

            # Plan 0046 — stage for durable persistence.
            self._record_persist_sample(
                asset=asset,
                source=source,
                timestamp_ms=updated_at_ms,
                price=price,
            )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[ChainlinkFeed] = None


def get_chainlink_feed() -> ChainlinkFeed:
    global _instance
    if _instance is None:
        _instance = ChainlinkFeed()
    return _instance
