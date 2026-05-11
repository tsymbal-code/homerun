# Architecture: Crypto fast-binary lane

This is the parallel high-speed market-ingest lane for crypto
binary markets (BTC, ETH, SOL, XRP). It runs alongside — and
deliberately **outside** — the standard scanner pipeline, fetching
short-cycle markets directly from Polymarket Gamma and reacting to
Binance ticks without ever entering `market_catalog`. The lane
exists because crypto binaries do not carry tags, so the
[market-filter](market-filter.md) cannot gate them.

The lane has an **operator-managed on/off toggle** introduced by
[`completed/0006-crypto-fast-binary-lane-toggle.md`](../completed/0006-crypto-fast-binary-lane-toggle.md).
That plan is the authoritative reference for toggle semantics and
the rationale behind the entire mechanism; this note distils the
runtime architecture so an agent can reason about the lane without
reading the plan history.

## Purpose

This lane is responsible for:

1. Fetching live and upcoming crypto-binary markets from Polymarket
   Gamma (16 series across BTC/ETH/SOL/XRP × 5m/15m/1h/4h
   timeframes) on a 2-second refresh cadence.
2. Reacting to Binance bookTicker WebSocket pushes by recomputing
   only the affected markets (no full payload rebuild).
3. Dispatching `EventType.CRYPTO_UPDATE` events to crypto strategies.
4. Honoring an operator on/off toggle that **collapses to "lane
   active = `is_enabled AND NOT is_paused`"** so the same control
   serves "permanent off" and "temporary pause" without code
   duplication.
5. Tracking the per-market `price_to_beat` (oracle snapshot at
   market start) so resolution-side strategies can verify against a
   canonical reference.

It does **not**:

- Pass through `market_catalog` or the tag-based whitelist
  ([`market-filter.md`](market-filter.md)) — the `CryptoMarket`
  shape carries no `tags` field.
- Cancel existing positions when the lane toggles off — the toggle
  stops new opportunity detection, not in-flight orders.
- Run on `worker-news` or `worker-discovery` — this is exclusively
  a `worker-trading` concern (see
  [`worker-trading.md`](worker-trading.md)).

## Key files

| Path | What it holds |
|---|---|
| [`backend/services/crypto_service.py`](../../../backend/services/crypto_service.py) | `CryptoService` singleton (line 1172); `CryptoMarket` dataclass (lines 125–160); `get_live_markets()` (line 393); `_fetch_series_market()` (line 468); price-to-beat tracking (lines 840–1006) |
| [`backend/services/market_runtime.py`](../../../backend/services/market_runtime.py) | `_crypto_lane_is_active()` (lines 255–267); `_refresh_crypto_markets()` (line 1390); `_drain_reactive_updates()` (line 1720) — the critical toggle gate; `_on_ws_price_update()` (line 1668) |
| [`backend/api/routes_workers.py`](../../../backend/api/routes_workers.py) | `POST /api/workers/crypto/{pause,start}` — generic worker controls used for the toggle |
| [`backend/models/database.py`](../../../backend/models/database.py) (line 3699) | `worker_control` table — `name`, `is_enabled`, `is_paused`, `interval_seconds`, `requested_run_at` |
| [`backend/services/binance_feed.py`](../../../backend/services/binance_feed.py) | The Binance WebSocket feed (combined bookTicker streams for BTC/ETH/SOL/XRP) — see [`websocket-and-events.md`](websocket-and-events.md) |

## Contracts

### `CryptoMarket` shape

```python
@dataclass(slots=True)
class CryptoMarket:
    id: str
    condition_id: str
    slug: str
    asset: str               # "BTC" | "ETH" | "SOL" | "XRP"
    timeframe: str           # "5m" | "15m" | "1h" | "4h"
    up_price: float
    down_price: float
    best_bid: float
    best_ask: float
    spread: float
    last_trade_price: float
    liquidity: float
    volume: float
    volume_24h: float
    clob_token_ids: list[str]
    up_token_index: int
    down_token_index: int
    event_slug: str
    event_title: str
    start_time: str
    end_time: str
    is_current: bool
    upcoming_markets: list[dict]
    price_to_beat: Optional[float]
    fees_enabled: bool
```

**No `tags` field by design.** This is the load-bearing reason for
the separate toggle: tag-based filtering simply cannot apply.
Serialised via `to_dict()` (line 163) for API and WebSocket
broadcast.

### Lane-active definition

```python
def _crypto_lane_is_active(control: dict) -> bool:
    enabled = bool(control.get("is_enabled", True))
    paused  = bool(control.get("is_paused", False))
    return enabled and not paused
```
([`market_runtime.py:255-267`](../../../backend/services/market_runtime.py))

`is_enabled = false` (permanent off) and `is_paused = true`
(temporary API-driven pause) collapse to the same lane-off state.
The `worker_control` row for `name='crypto'` is the single source
of truth.

### Lifecycle gates

There are three places where the lane checks active state:

1. **Startup** ([`market_runtime.py:590-618`](../../../backend/services/market_runtime.py)).
   If lane inactive, skips refresh, clears caches, logs once.
2. **Periodic refresh** (`_refresh_crypto_markets`, line 1390).
   Called from the main loop iteration; gated on the same control
   read.
3. **Reactive ticks** (`_drain_reactive_updates`, line 1720) —
   **the critical performance gate**. Reads
   `_read_crypto_control_cached(ttl=5s)` on every drain. If
   inactive, drops `_pending_tokens`/`_pending_assets` and exits
   without touching the payload.

The 5-second TTL on `_read_crypto_control_cached` (line 1058)
means a Settings UI toggle takes up to 5 s to propagate to the
reactive path. Periodic refresh (~1 s cadence) keeps the cache
warm in the normal case.

### Transition detection

`_run_loop_iteration` tracks `_crypto_lane_was_enabled`
([`market_runtime.py:1000-1032`](../../../backend/services/market_runtime.py)):

- **Active → off:** clears `_crypto_markets`,
  `_crypto_token_to_market_ids`,
  `_crypto_asset_to_market_ids`. Drops in-flight pending state.
- **Off → active:** sets `_crypto_lane_pending_refresh`; next
  iteration triggers
  `_refresh_crypto_markets(trigger="lane_re_enabled")`.

### Three lookup dicts

The reactive path needs O(1) market discovery from a Binance tick:

```
_crypto_markets_by_lookup    : slug → market_dict
_crypto_token_to_market_ids  : token_id → set[market_id]
_crypto_asset_to_market_ids  : "BTC"|"ETH"|... → set[market_id]
```

All three live in `market_runtime` instance state; cleared on
active → off transition; rebuilt on the next refresh.

### Series configuration

16 hard-defaulted series IDs, DB-overridable
([`config.py:422-437`](../../../backend/config.py)):

```
BTC: 5m=10684 / 15m=10192 / 1h=... / 4h=...
ETH: 5m=10683 / 15m=...
SOL: 5m=10686 / 15m=...
XRP: 5m=10685 / 15m=...
```

`_fetch_all()` (line 598) parallelises the 16 fetches via a
`ThreadPoolExecutor(max_workers=16)`. Each `_fetch_series_market`
returns the current event plus three upcoming.

### Strategy integration

Strategies with `source_key = "crypto"` (case-insensitive)
subscribe to `EventType.CRYPTO_UPDATE`. The current set:

| Strategy | Source file |
|---|---|
| `crypto_5m_midcycle` | [`backend/services/strategies/crypto_5m_midcycle.py`](../../../backend/services/strategies/crypto_5m_midcycle.py) |
| `crypto_spike_reversion` | [`backend/services/strategies/crypto_spike_reversion.py`](../../../backend/services/strategies/crypto_spike_reversion.py) |
| `crypto_entropy_maker` | [`backend/services/strategies/crypto_entropy_maker.py`](../../../backend/services/strategies/crypto_entropy_maker.py) |
| `btc_eth_directional_edge` | [`backend/services/strategies/btc_eth_directional_edge.py`](../../../backend/services/strategies/btc_eth_directional_edge.py) |
| `btc_eth_maker_quote` | [`backend/services/strategies/btc_eth_maker_quote.py`](../../../backend/services/strategies/btc_eth_maker_quote.py) |
| `btc_eth_convergence` | [`backend/services/strategies/btc_eth_convergence.py`](../../../backend/services/strategies/btc_eth_convergence.py) |

Operator-facing per-strategy docs: `docs/strategies/crypto-*.md`,
`docs/strategies/btc-eth-*.md`.

### REST cache-prime hook (Plan 0051)

The `crypto_5m_last_outcome` strategy adds a fire-and-forget REST
cache-prime when the synchronous `book_depth` gate misses the
`PriceCache`. The strategy module
([`backend/services/strategies/crypto_5m_last_outcome.py`](../../../backend/services/strategies/crypto_5m_last_outcome.py))
calls `FeedManager.get_order_book(token_id)` from inside the
reject branch; that method is async and routes through the
already-registered `_build_polymarket_http_fallback_order_book`
fallback, writing the fetched book back into the cache. The
strategy's own ~6 Hz on_event firing rate then finds a populated
book on the next tick (~150 ms) and passes the gate.

A per-token `_REST_PRIME_COOLDOWN_S = 3.0` guard caps HTTP load at
~20 fetches/min/token even in the worst case. The behaviour is
gated by `rest_book_fallback_enabled` in the strategy config
(default `True`); flipping the toggle off in the strategy-manager
UI restores the prior pure-WS behaviour.

This is **scoped to one strategy on purpose**. The other crypto
strategies (`crypto_5m_midcycle`, `crypto_convergence`,
`btc_eth_directional_edge`, `btc_eth_maker_quote`,
`crypto_entropy_maker`, `crypto_spike_reversion`) keep the
sync-only path. A general SDK-level fallback that benefits every
strategy is the natural follow-up but was deliberately deferred —
the call shape and rate-limit interaction need a wider design
pass.

### Toggle API

Generic worker-control API
([`routes_workers.py`](../../../backend/api/routes_workers.py)):

- `POST /api/workers/crypto/start` — sets `is_paused=false`.
- `POST /api/workers/crypto/pause` — sets `is_paused=true`.

The Settings UI does not write `is_enabled` directly; operators
change `is_paused` via the buttons, and `is_enabled` stays at its
deploy-time default (true). Both fields collapse to "lane active"
via the helper function above.

`GET /api/settings/scanner` exposes a derived
`crypto_lane_enabled: bool` field
([`routes_settings.py:2417-2435`](../../../backend/api/routes_settings.py)),
read-only, sourced from `worker_control`. DB-error fallback: true.

## Dependencies (both directions)

**This lane depends on:**

- Polymarket Gamma REST (`/markets/series/...`) for the 16 series
  fetches.
- The Binance WebSocket feed (combined bookTicker streams) for
  reactive tick updates — see
  [`websocket-and-events.md`](websocket-and-events.md).
- The Chainlink/`ChainlinkFeed` rolling history for price-to-beat
  fallback resolution (lines 840–1006 in `crypto_service.py`).
  Plan 0046 added a 1 Hz throttled write of every ingested
  reading to the `crypto_oracle_history` Postgres table (housekeeper
  prunes rows older than 14 days every 6 hours). That table is the
  source-of-truth for the offline crypto-strategy backtester
  (`POST /api/validation/code-backtest/optimize-strategy`); the
  live trading path still reads from the in-memory deque.
- Polymarket crypto-price API (primary `price_to_beat` source).

**Depended on by:**

- The six crypto / BTC-ETH strategies in
  `backend/services/strategies/`.
- The Crypto UI tab (`routes_crypto.py` endpoints + WebSocket
  `crypto_markets_update` channel).
- The CPU profile of `worker-trading` — Plan 0006 demonstrated
  that turning the lane off collapses the
  `get_oracle_history` + `_oracle_move_from_history` hotspots
  from ~42 % to < 2 % of CPU samples (see
  [`worker-trading.md`](worker-trading.md)).

## Performance characteristics

`worker-trading` py-spy profiles before vs after Plan 0006 (the
toggle):

| State | `get_oracle_history` + `_oracle_move_from_history` | `copy.deepcopy` chain |
|---|---|---|
| Lane on | ~42–43 % CPU samples | high |
| Lane off | < 2 % | < 5 % |

The mechanism: when the lane is off, `_drain_reactive_updates`
short-circuits **before** the payload rebuild, so no downstream
oracle dereferencing runs on Binance ticks. The Binance WS feed
itself stays connected (cheap; ~1–5 µs JSON parse per tick), but
the expensive per-market work never fires.

This is why Plan 0004 ("Optimize worker-trading CPU hotspots")
was re-archived to backlog after Plan 0006 closed: turning the
lane off was a higher-leverage solution than micro-optimising the
hotspots themselves.

## Configuration

Hard-coded refresh cadence: 2 s for `start_fast_scan` broadcast
(line 728), ~1 s for the main loop iteration that reads the
control row.

Series IDs are DB-overridable via `AppSettings`
([`config.py:422-437`](../../../backend/config.py)). Defaults
shipped in the env are the production values for
`polyhome-1`.

`ThreadPoolExecutor(max_workers=16)` for parallel Gamma fetches —
hard-coded, not tunable. With 16 series this is one worker per
fetch; lower would serialize, higher would not help.

## Extension points

| When you want to… | Touch |
|---|---|
| Add a new asset (e.g. SUI, AVAX) | New series IDs in `config.py:422-437`; verify Binance feed has the corresponding stream; add per-strategy docs if a new strategy targets it |
| Change refresh cadence | `start_fast_scan(interval_seconds=...)` for the broadcast; the main loop iteration interval lives in `market_runtime` |
| Add a new crypto strategy | New module in `backend/services/strategies/` with `source_key="crypto"` and an `EventType.CRYPTO_UPDATE` subscription; matching `docs/strategies/<slug>.md` |
| Tune lane-off propagation latency | `_read_crypto_control_cached(ttl_seconds=...)` at [`market_runtime.py:1058`](../../../backend/services/market_runtime.py) — default 5 s |

## Known footguns

- **Lane on without consumers.** If the operator has no crypto
  strategies enabled but the lane is on, CPU is wasted on the
  refresh + rebuild work. Operators should pair lane-on with at
  least one active crypto strategy.
- **Toggle does not cancel positions.** Turning the lane off while
  open crypto orders are live leaves them in place — they fill or
  cancel through the normal trader pipeline. The toggle gates
  *new* opportunity detection only. Plan 0006 documents this
  explicitly.
- **5-second control-cache TTL.** Settings UI toggle takes up to
  5 s to reach the reactive Binance-tick path. Acceptable for a
  human-driven toggle; surprising if you expect immediate effect.
- **Binance feed survives toggle.** The WebSocket connection is
  not torn down on lane-off; ticks are simply discarded in
  `_drain_reactive_updates`. By design (avoids reconnect-backoff
  complexity); cost is negligible.
- **DB error fallback defaults to lane on.** If `worker_control`
  cannot be read, both the runtime helper and the Settings API
  default to `is_enabled=true, is_paused=false`. Surface DB errors
  in the heartbeat; do not rely on the lane being off after a DB
  outage.
- **`upcoming_markets` is informational only.** The lane reports
  next-three-upcoming for UI prefetch. Strategies that act on
  `upcoming_markets` rather than the current event must handle the
  rotation transition themselves.

## Test coverage

- `backend/tests/test_market_runtime_crypto_lane_toggle.py` —
  startup skip, transition detection, cache clearing,
  control-cache TTL behaviour
- `backend/tests/test_routes_settings_scanner_crypto_lane.py` —
  Settings API consistency with `worker_control` for both
  `is_enabled` and `is_paused`; DB-error fallback
- `backend/tests/test_crypto_service.py` — fetch, market rotation
  detection, price-to-beat resolution
- `backend/tests/test_crypto_5m_midcycle_strategy.py` and
  per-strategy tests — gate logic on `CRYPTO_UPDATE` events

## Where to look next

| Topic | File |
|---|---|
| Plan 0006 — toggle rationale + measured impact | [`completed/0006-crypto-fast-binary-lane-toggle.md`](../completed/0006-crypto-fast-binary-lane-toggle.md) |
| `worker-trading` plane and CPU profile | [`worker-trading.md`](worker-trading.md) |
| Live feeds (Binance, Chainlink, Polymarket WS) | [`websocket-and-events.md`](websocket-and-events.md) |
| Tag-based market intake (the lane this one bypasses) | [`market-filter.md`](market-filter.md) |
| Per-strategy operator references (Ukrainian) | `docs/strategies/crypto-*.md`, `docs/strategies/btc-eth-*.md` |

Last verified: 2026-05-11
