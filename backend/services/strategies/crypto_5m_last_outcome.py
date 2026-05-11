"""Crypto 5m last-outcome-follow strategy.

For each 5-minute Polymarket up-or-down cycle on an enabled crypto
asset, this strategy opens a position on the **same side that won the
immediately preceding cycle**. There is no oracle-distance gate, no
microstructure gate, no edge-percent gate — the spec is "direct
repetition of the last result without additional filters."

Knowing the previous cycle's outcome does not require capturing the
closing Chainlink read. Polymarket sets every new cycle's
``price_to_beat`` to the Chainlink price at cycle start, which equals
the Chainlink price at the previous cycle's end. So as soon as we
observe a market with a new ``condition_id`` for an asset we have
already seen, the previous outcome is exactly::

    prev = "YES" if price_to_beat_new > price_to_beat_old else "NO"

The first cycle we observe for an asset (cold boot, or after a long
gap that pruned state) silently produces no trade because the
"previous outcome" is unknown — this is intentional and matches the
spec.

Defaults ship the strategy enabled for BTC only — the asset the
operator picked as the starting point. The asset list is editable
in the strategy-manager UI so the same code path can be exercised on
ETH/SOL/XRP without code changes.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from models import Market, Opportunity
from services.data_events import DataEvent
from services.strategies._firehose import (
    GateResult,
    MURMUR,
    WHISPER,
    emit_emit_nowait,
    emit_evaluation_nowait,
)
from services.strategies.base import BaseStrategy
from services.strategy_sdk import StrategySDK
from utils.converters import to_float
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Defaults — every value is overridable via the DB ``config`` column.
# ---------------------------------------------------------------------------

_ALL_ASSETS: tuple[str, ...] = ("BTC", "ETH", "SOL", "XRP")

DEFAULT_CONFIG: dict[str, Any] = {
    # Per-asset enable list. Default ships BTC only — the operator
    # asked for the BTC market as the starting point. SOL/XRP/ETH can
    # be re-enabled from the strategy-manager UI.
    "assets": ["BTC"],
    # VWAP entry-price band. We keep this knob (and only this knob)
    # because executing a buy at e.g. 0.97 or 0.02 makes no
    # economic sense regardless of which side "won" last cycle.
    "max_entry_price": 0.95,
    "min_entry_price": 0.05,
    # Notional per trade (USD).
    "bet_size_usd": 15.0,
    # Seconds after cycle start at which we evaluate. 30 s gives the
    # Polymarket WS book a moment to populate at cycle rollover; below
    # ~15 s the book is often empty and the depth gate rejects.
    "entry_seconds_after_start": 30.0,
    # Master switch (also exposed at the row level via Strategy.enabled).
    "enabled": True,
}


def crypto_5m_last_outcome_config_schema() -> dict[str, Any]:
    """Return the param-fields schema for the strategy-manager UI."""
    return {
        "param_fields": [
            {
                "key": "enabled",
                "label": "Enabled",
                "type": "boolean",
                "default": True,
                "phase": "signal",
            },
            {
                "key": "assets",
                "label": "Assets",
                "type": "list",
                "options": list(_ALL_ASSETS),
                "default": ["BTC"],
                "phase": "signal",
            },
            {
                "key": "max_entry_price",
                "label": "Max VWAP Entry Price",
                "type": "number",
                "min": 0.0,
                "max": 1.0,
                "default": 0.95,
                "phase": "execution",
            },
            {
                "key": "min_entry_price",
                "label": "Min VWAP Entry Price",
                "type": "number",
                "min": 0.0,
                "max": 1.0,
                "default": 0.05,
                "phase": "execution",
            },
            {
                "key": "bet_size_usd",
                "label": "Bet Size (USD)",
                "type": "number",
                "min": 1.0,
                "max": 10_000.0,
                "default": 15.0,
                "phase": "execution",
            },
            {
                "key": "entry_seconds_after_start",
                "label": "Entry Milestone (sec since cycle start)",
                "type": "number",
                "min": 1.0,
                "max": 299.0,
                "default": 30.0,
                "phase": "signal",
            },
        ]
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_asset(value: Any) -> str:
    asset = str(value or "").strip().upper()
    if asset == "XBT":
        return "BTC"
    return asset


_FIVE_MIN_SLUG_RE = re.compile(r"(?:^|[-_])5(?:min|m|-minute)(?:[-_]|$)")


def _detect_5m(market: dict[str, Any]) -> bool:
    """True when the market dict represents a 5-minute crypto cycle."""
    timeframe = str(market.get("timeframe") or "").strip().lower()
    if timeframe in ("5m", "5min", "5-minute", "5 minute", "5 min"):
        return True
    slug = str(market.get("slug") or "").lower()
    return bool(_FIVE_MIN_SLUG_RE.search(slug))


def _build_market(d: dict[str, Any]) -> Market:
    """Deserialize a crypto-worker market dict to a typed ``Market``."""
    market_id = str(d.get("condition_id") or d.get("id") or "")
    up_price = float(d.get("up_price") or 0.0)
    down_price = float(d.get("down_price") or 0.0)
    liquidity = max(0.0, float(d.get("liquidity") or 0.0))
    slug = d.get("slug") or market_id
    question = d.get("question") or slug

    end_date = None
    end_time_raw = d.get("end_time")
    if isinstance(end_time_raw, str) and end_time_raw.strip():
        try:
            from datetime import datetime as _dt
            end_date = _dt.fromisoformat(end_time_raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    raw_token_ids = d.get("clob_token_ids") or []
    clob_token_ids = [
        str(t).strip()
        for t in raw_token_ids
        if str(t).strip() and len(str(t).strip()) > 20
    ]

    return Market(
        id=market_id,
        condition_id=market_id,
        question=question,
        slug=slug,
        outcome_prices=[up_price, down_price],
        liquidity=liquidity,
        end_date=end_date,
        platform="polymarket",
        clob_token_ids=clob_token_ids,
    )


# ---------------------------------------------------------------------------
# Strategy class
# ---------------------------------------------------------------------------


class Crypto5mLastOutcomeStrategy(BaseStrategy):
    """Repeat the previous 5m cycle's winning side, per asset."""

    strategy_type = "crypto_5m_last_outcome"
    name = "Crypto 5m Last-Outcome Follow"
    description = (
        "On each new 5-minute crypto over-under cycle, open a position on "
        "the same side that won the immediately preceding cycle. The previous "
        "outcome is reconstructed from the change in price_to_beat across the "
        "cycle rollover. No oracle-distance / microstructure / edge filters — "
        "only the unavoidable VWAP-band, book-depth, and timeframe gates."
    )
    source_key = "crypto"
    market_categories = ["crypto"]
    requires_historical_prices = False
    subscriptions = ["crypto_update"]
    supports_entry_take_profit_exit = False
    default_open_order_timeout_seconds = 30.0

    default_config = dict(DEFAULT_CONFIG)

    def __init__(self) -> None:
        super().__init__()
        self.min_profit = 0.0
        self.fee = 0.0
        # Per-asset state: maps asset symbol → most recently observed
        # (market_id, price_to_beat). Used to detect cycle rollover and
        # to compute the just-ended cycle's outcome.
        self._last_seen: dict[str, tuple[str, float]] = {}
        # Per-asset most recently computed outcome of the cycle that
        # JUST ended (the one whose winner the upcoming cycle should
        # follow). ``None`` until the first rollover for that asset.
        self._last_outcome: dict[str, Optional[str]] = {}
        # Set of market_ids we have already emitted an opportunity for.
        # Once a cycle's emit succeeds we stop re-firing for that cycle;
        # on the next cycle the market_id changes so it falls out of
        # this set naturally. We intentionally do NOT use a one-shot
        # CycleTracker milestone — the book is often not yet populated
        # at our 30 s threshold, so the strategy must keep retrying
        # within the cycle until the book-depth / VWAP gates pass.
        self._emitted_market_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def configure(self, config: dict) -> None:
        merged = dict(self.default_config)
        if config:
            merged.update(config)
        # Sanitize: assets list normalized to upper-case canonical names
        raw_assets = merged.get("assets") or []
        if isinstance(raw_assets, str):
            raw_assets = [a.strip() for a in raw_assets.split(",")]
        normalized_assets: list[str] = []
        seen: set[str] = set()
        for a in raw_assets:
            n = _normalize_asset(a)
            if n and n in _ALL_ASSETS and n not in seen:
                seen.add(n)
                normalized_assets.append(n)
        merged["assets"] = normalized_assets
        self.config = merged

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    async def on_event(self, event: DataEvent) -> list[Opportunity]:
        if event.event_type != "crypto_update":
            return []
        if not self.config.get("enabled", True):
            return []

        markets = event.payload.get("markets") or []
        if not markets:
            return []

        self._ensure_ws_subscribed_for_5m(markets)

        opportunities: list[Opportunity] = []
        now_ms = int(time.time() * 1000)
        for market in markets:
            if not isinstance(market, dict):
                continue
            opp = self._evaluate_market(market, now_ms=now_ms)
            if opp is not None:
                opportunities.append(opp)
        return opportunities

    @staticmethod
    def _ensure_ws_subscribed_for_5m(markets: list[Any]) -> None:
        """Fire-and-forget: ensure the Polymarket WS feed has the 5m
        markets' CLOB tokens subscribed so the book-depth gate has a
        populated cache by the time it runs. Mirrors the same helper
        in crypto_5m_midcycle."""
        token_ids: list[str] = []
        for market in markets:
            if not isinstance(market, dict):
                continue
            if not _detect_5m(market):
                continue
            for raw in market.get("clob_token_ids") or []:
                tok = str(raw or "").strip()
                if len(tok) > 20:
                    token_ids.append(tok)
        if not token_ids:
            return
        try:
            from services.ws_feeds import get_feed_manager
            feed_mgr = get_feed_manager()
            if not getattr(feed_mgr, "_started", False):
                return
            pm_feed = feed_mgr.polymarket_feed
            import asyncio as _asyncio
            _asyncio.ensure_future(pm_feed.subscribe(token_ids=token_ids))
        except Exception as exc:
            logger.debug(
                "crypto_5m_last_outcome WS subscribe failed (non-critical): %s",
                exc,
            )

    # ------------------------------------------------------------------
    # State maintenance
    # ------------------------------------------------------------------

    def _update_rollover_state(
        self,
        *,
        asset: str,
        market_id: str,
        reference: float,
    ) -> None:
        """Detect cycle rollover for an asset and compute the previous
        outcome.

        Called on every per-market evaluation where the asset is in the
        configured list and ``market_id`` + ``reference`` are valid. If
        the incoming ``(asset, market_id)`` differs from the stored
        one, the previous cycle just ended: the new ``reference`` IS
        the Chainlink price at the close of the previous cycle (= open
        of the new one), and the old stored ``reference`` was that
        previous cycle's strike. Comparing the two yields the previous
        outcome exactly.
        """
        prev = self._last_seen.get(asset)
        if prev is None:
            # First observation for this asset — record, no outcome yet.
            self._last_seen[asset] = (market_id, reference)
            return

        prev_market_id, prev_reference = prev
        if prev_market_id == market_id:
            # Same cycle, possibly with a slightly different float
            # representation of price_to_beat (shouldn't happen, but
            # ignore drift to keep state stable).
            return

        # Rollover detected. Compute outcome strictly: equal
        # references → leave previous_outcome unchanged (unknown).
        if reference > prev_reference:
            self._last_outcome[asset] = "YES"
        elif reference < prev_reference:
            self._last_outcome[asset] = "NO"
        else:
            self._last_outcome[asset] = None
        self._last_seen[asset] = (market_id, reference)

    # ------------------------------------------------------------------
    # Per-market gate
    # ------------------------------------------------------------------

    def _evaluate_market(
        self,
        market: dict[str, Any],
        *,
        now_ms: int,
    ) -> Optional[Opportunity]:
        gates: list[GateResult] = []
        entry_milestone = float(self.config.get("entry_seconds_after_start", 30.0))
        max_entry = float(self.config.get("max_entry_price", 0.95))
        min_entry = float(self.config.get("min_entry_price", 0.05))

        def _emit_reject(verbosity: str = MURMUR) -> None:
            emit_evaluation_nowait(
                strategy_slug="crypto_5m_last_outcome",
                market=market,
                gates=gates,
                outcome="rejected",
                verbosity=verbosity,
            )

        # Gate 1: 5-minute timeframe.
        is_5m = _detect_5m(market)
        gates.append(GateResult(
            "timeframe", "5-minute timeframe", is_5m,
            detail=f"timeframe={market.get('timeframe') or '?'} slug={market.get('slug') or '?'}",
        ))
        if not is_5m:
            _emit_reject(WHISPER)
            return None

        market_id = str(market.get("condition_id") or market.get("id") or "")
        gates.append(GateResult(
            "market_id", "Market id present", bool(market_id),
            detail="condition_id or id required",
        ))
        if not market_id:
            _emit_reject(WHISPER)
            return None

        # Gate 2: per-asset enable list.
        asset = _normalize_asset(
            market.get("asset") or market.get("symbol") or market.get("coin")
        )
        configured_assets = self.config.get("assets") or []
        asset_passed = bool(asset) and asset in configured_assets
        gates.append(GateResult(
            "asset_enabled", "Asset in config list", asset_passed,
            detail=f"asset={asset or '?'} configured={','.join(configured_assets) or 'none'}",
        ))
        if not asset_passed:
            _emit_reject(WHISPER)
            return None

        end_ms_value = StrategySDK._coerce_end_ts_ms(market)
        gates.append(GateResult(
            "end_timestamp", "Cycle end timestamp parseable", end_ms_value is not None,
            detail=f"end_ts_ms={end_ms_value}",
        ))
        if end_ms_value is None:
            _emit_reject(WHISPER)
            return None

        # Capture the cycle's strike (price_to_beat) and maintain
        # per-asset rollover state. This must happen BEFORE the
        # milestone-gate short-circuits so the rollover detection runs
        # on every tick, not only on the milestone fire.
        reference = to_float(market.get("price_to_beat"), None)
        ref_passed = reference is not None and reference > 0.0
        gates.append(GateResult(
            "reference_price", "Reference price available", ref_passed,
            score=reference,
            detail=f"price_to_beat={reference}",
        ))
        if not ref_passed:
            _emit_reject(WHISPER)
            return None
        self._update_rollover_state(
            asset=asset, market_id=market_id, reference=reference,
        )

        # Gate 3: entry-milestone passed AND not yet emitted for this
        # cycle. We deliberately allow re-evaluation on every tick once
        # ``entry_seconds_after_start`` has elapsed — the order book
        # often isn't populated at the first tick after rollover, and
        # missing the depth gate at exactly 30 s used to silently waste
        # the whole cycle. Once an Opportunity is built, the market_id
        # is added to ``_emitted_market_ids`` and subsequent ticks for
        # the same cycle short-circuit here.
        seconds_into_cycle = max(0.0, 300.0 - (end_ms_value - now_ms) / 1000.0)
        already_emitted = market_id in self._emitted_market_ids
        milestone_elapsed = seconds_into_cycle + 1e-3 >= entry_milestone
        milestone_passed = milestone_elapsed and not already_emitted
        gates.append(GateResult(
            "entry_milestone", "Entry milestone crossed (not yet emitted)",
            milestone_passed,
            score=seconds_into_cycle,
            detail=(
                f"milestone={entry_milestone:.0f}s "
                f"elapsed={seconds_into_cycle:.1f}s "
                f"emitted={already_emitted}"
            ),
        ))
        if not milestone_passed:
            _emit_reject(WHISPER)
            return None

        # Gate 4: we need a known previous outcome to follow.
        previous_outcome = self._last_outcome.get(asset)
        outcome_known = previous_outcome in ("YES", "NO")
        gates.append(GateResult(
            "previous_outcome_known", "Previous cycle outcome known",
            outcome_known,
            detail=f"prev_outcome={previous_outcome}",
        ))
        if not outcome_known:
            _emit_reject(MURMUR)
            return None
        side = previous_outcome

        typed_market = _build_market(market)
        token_ids_present = bool(typed_market.clob_token_ids)
        gates.append(GateResult(
            "clob_tokens", "CLOB token ids present", token_ids_present,
            detail=f"count={len(typed_market.clob_token_ids)}",
        ))
        if not token_ids_present:
            _emit_reject(MURMUR)
            return None

        bet_size_usd = float(self.config.get("bet_size_usd", 15.0))
        depth = StrategySDK.get_order_book_depth(
            typed_market, side=side, size_usd=bet_size_usd,
        )
        depth_present = depth is not None
        gates.append(GateResult(
            "book_depth", "Order book depth available", depth_present,
            detail=f"side={side} size_usd={bet_size_usd:.2f}",
        ))
        if not depth_present:
            _emit_reject(MURMUR)
            return None
        is_fresh = bool(depth.get("is_fresh", False))
        gates.append(GateResult(
            "book_fresh", "Order book fresh", is_fresh,
            score=float(depth.get("staleness_ms") or 0.0),
            detail=f"staleness_ms={depth.get('staleness_ms')}",
        ))
        if not is_fresh:
            _emit_reject(MURMUR)
            return None

        vwap_price = float(depth.get("vwap_price") or 0.0)
        vwap_passed = vwap_price > 0.0 and min_entry <= vwap_price <= max_entry
        gates.append(GateResult(
            "vwap_in_range", "VWAP within entry range", vwap_passed,
            score=vwap_price,
            detail=f"vwap={vwap_price:.4f} range=[{min_entry:.2f},{max_entry:.2f}]",
        ))
        if not vwap_passed:
            _emit_reject(MURMUR)
            return None

        # All gates passed — build the Opportunity.
        edge_per_share = 1.0 - vwap_price
        edge_percent = (edge_per_share / vwap_price) * 100.0 if vwap_price > 0 else 0.0
        # We have no empirical win-rate yet (the strategy has never
        # run live), so the ``confidence`` we report is the neutral
        # coin-flip prior 0.50. ``edge_percent`` here is the **raw
        # max-ROI** if YES wins — NOT an expected-value adjustment by
        # the prior — because:
        #
        # (1) The user explicitly asked for "no additional filters" —
        #     applying a coin-flip prior to edge would gate every emit
        #     to negative ROI at VWAP > 0.50 and the strategy could
        #     never collect data to refine the prior.
        # (2) ``BaseStrategy._base_evaluate`` checks
        #     ``edge_percent >= min_edge_percent`` on the trader, and
        #     the operator is the one who picks an honest threshold
        #     (e.g. min_edge_percent=10 means "I want at least 10%
        #     possible upside per share"). The composition
        #     `edge × confidence` gives them expected value if they
        #     want it.
        # (3) Once we accumulate live PnL, the prior gets revised; the
        #     edge convention stays the same as midcycle's so cross-
        #     strategy comparisons are apples-to-apples (midcycle
        #     packs win_prob into custom_roi but its empirical
        #     win_prob is 0.80 and emits never go negative there).
        win_prob_estimate = 0.50
        expected_payout = 1.0  # Polymarket pays $1 per winning share
        token_id = typed_market.clob_token_ids[0 if side == "YES" else 1]

        slug = str(market.get("slug") or market_id)
        title = f"Crypto 5m last-outcome: {slug} {side}"
        description = (
            f"follow last outcome | {asset} 5m | "
            f"prev_outcome={previous_outcome} | "
            f"VWAP entry={vwap_price:.4f}"
        )

        seconds_left = (end_ms_value - now_ms) / 1000.0
        opp = self.create_opportunity(
            title=title,
            description=description,
            total_cost=vwap_price,
            expected_payout=expected_payout,
            markets=[typed_market],
            positions=[
                {
                    "action": "BUY",
                    "outcome": side,
                    "price": vwap_price,
                    "token_id": token_id,
                    "_last_outcome_context": {
                        "asset": asset,
                        "timeframe": "5min",
                        "reference_price": reference,
                        "previous_outcome": previous_outcome,
                        "vwap_price": vwap_price,
                        "vwap_slippage_bps": float(depth.get("slippage_bps") or 0.0),
                        "seconds_left": seconds_left,
                        "side": side,
                        "bet_size_usd": bet_size_usd,
                        "win_prob_estimate": win_prob_estimate,
                    },
                }
            ],
            is_guaranteed=False,
            skip_fee_model=True,
            custom_roi_percent=edge_percent,  # raw max-ROI if we win
            custom_risk_score=1.0 - win_prob_estimate,
            confidence=win_prob_estimate,
        )
        if opp is None:
            emit_evaluation_nowait(
                strategy_slug="crypto_5m_last_outcome",
                market=market,
                gates=gates,
                outcome="rejected",
                verbosity=MURMUR,
                extra={"reason": "create_opportunity returned None"},
            )
            return None

        # Mark the cycle as emitted so the milestone gate short-circuits
        # for the remainder of this market's ticks. Cleared implicitly
        # on cycle rollover because the new cycle has a different
        # market_id.
        self._emitted_market_ids.add(market_id)

        emit_emit_nowait(
            strategy_slug="crypto_5m_last_outcome",
            market=market,
            detail=(
                f"{asset} {side} • prev={previous_outcome} • "
                f"vwap={vwap_price:.4f}"
            ),
            extra={
                "side": side,
                "asset": asset,
                "previous_outcome": previous_outcome,
                "vwap_price": vwap_price,
                "bet_size_usd": bet_size_usd,
            },
        )
        emit_evaluation_nowait(
            strategy_slug="crypto_5m_last_outcome",
            market=market,
            gates=gates,
            outcome="emitted",
            verbosity=WHISPER,
        )

        opp.risk_factors = [
            f"Crypto 5m last-outcome follow ({asset})",
            f"Previous cycle winner: {previous_outcome}",
            f"VWAP entry: {vwap_price:.4f} (range [{min_entry:.2f},{max_entry:.2f}])",
        ]
        opp.strategy_context = {
            "source_key": "crypto",
            "strategy": "crypto_5m_last_outcome",
            "asset": asset,
            "timeframe": "5min",
            "reference_price": reference,
            "previous_outcome": previous_outcome,
            "vwap_price": vwap_price,
            "side": side,
            "seconds_left": seconds_left,
            "bet_size_usd": bet_size_usd,
            "win_prob_estimate": win_prob_estimate,
        }
        return opp
