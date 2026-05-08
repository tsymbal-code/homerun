# Architecture: WebSockets and event channels

Homerun has four messaging layers, easy to confuse with each other.
This note maps them on one page so you know which one carries
which kind of update, who publishes, who subscribes, and what to
do when one of them goes dark.

The four layers, top to bottom:

1. **External venue WebSockets** — Polymarket CLOB, Polymarket
   user-channel, Kalshi, Binance, Polymarket RTDS (Chainlink).
2. **In-memory caches** — `PriceCache`, `WalletStateCache`,
   `MarketCatalog`, `ChainlinkFeed`. Populated by layer 1.
3. **Frontend WebSocket `/ws`** — FastAPI `ConnectionManager`
   broadcasting JSON messages to the React UI.
4. **Cross-process channels** — Redis pub/sub between worker
   planes, plus the in-process `event_bus` inside one process.

## Purpose

This layer is responsible for:

1. Keeping live price + book state current with sub-second
   latency on `worker-trading`.
2. Tracking the operator's wallet (orders, fills, positions) in
   real time.
3. Pushing UI updates without polling — every visible widget that
   moves comes through `/ws`.
4. Carrying signals + trader events between the trading plane
   and the API plane (Redis pub/sub).
5. Decoupling strategies from raw event sources via the in-process
   `event_bus` (a strategy reacts to `MARKET_DATA_REFRESH` without
   knowing whether it came from CLOB or Kalshi).

It does **not**:

- Persist anything. All four layers are volatile. Durable state
  lives in Postgres.
- Carry settings or strategy code (those go through Postgres
  polling — see [`system-overview.md`](system-overview.md) §
  Cross-plane communication).

## Key files

### Layer 1 — external venue WebSockets

| Path | Class | Stream / channel |
|---|---|---|
| [`backend/services/ws_feeds.py`](../../../backend/services/ws_feeds.py) | `PolymarketWSFeed` (line 669+) | `wss://ws-subscriptions-clob.polymarket.com/ws/market` — public price/book per `asset_id` |
| [`backend/services/ws_feeds.py`](../../../backend/services/ws_feeds.py) | `KalshiWSFeed` | `wss://api.elections.kalshi.com/trade-api/ws/v2` (`KALSHI_WS_URL`) |
| [`backend/services/polymarket_user_feed.py`](../../../backend/services/polymarket_user_feed.py) | `PolymarketUserFeed` | `wss://ws-subscriptions-clob.polymarket.com/ws/user` — private fills/orders per `condition_id` |
| [`backend/services/binance_feed.py`](../../../backend/services/binance_feed.py) | `BinanceFeed` | combined-streams: `btcusdt@bookTicker`, `ethusdt@bookTicker`, `solusdt@bookTicker`, `xrpusdt@bookTicker` |
| [`backend/services/chainlink_feed.py`](../../../backend/services/chainlink_feed.py) | `ChainlinkFeed` | `wss://ws-live-data.polymarket.com` — topics `crypto_prices_chainlink`, `crypto_prices` |
| [`backend/services/chainlink_direct_feed.py`](../../../backend/services/chainlink_direct_feed.py) | direct Binance fallback when Chainlink RTDS is unhealthy | — |

All five live on `worker-trading`. Reconnect is exponential
backoff (1 → 60 s on PolymarketWSFeed, 0.2 → 30 s on
BinanceFeed). Heartbeats: PolymarketWSFeed pings every
`WS_HEARTBEAT_INTERVAL` (5 s); user feed pings every 8 s; Binance
trips on stale data after `BINANCE_WS_STALE_DATA_TIMEOUT_SECONDS`
(15 s).

### Layer 2 — in-memory caches

| Path | What it holds |
|---|---|
| [`backend/services/ws_feeds.py:269-625`](../../../backend/services/ws_feeds.py) | `PriceCache` — best_bid/best_ask, full book, monotonic + exchange timestamps, sequence. Fan-out via `on_change_callbacks` and `on_update_callbacks`. `_safe_binary_mid()` (line 199) drops degenerate spreads on 0–1 binaries |
| [`backend/services/wallet_state_cache.py`](../../../backend/services/wallet_state_cache.py) | `WalletStateCache` — `WalletPosition`, `WalletOrder`, `WalletFill`. `apply_trade()` / `apply_order()` / `seed_from_rest()` mutate; `get_position()` / `get_open_orders()` are hot-path reads. Emits `wallet_state.changed` on every mutation |
| [`backend/services/market_cache.py`](../../../backend/services/market_cache.py) | `MarketCatalog` — `CachedMarket`, `CachedUsername`. SQL-backed; 2 s LRU. Source for autocomplete and label resolution |
| [`backend/services/chainlink_feed.py`](../../../backend/services/chainlink_feed.py) | `ChainlinkFeed` — `OraclePrice` per asset, plus a rolling `deque` of up to 40 K snapshots for history-based strategies |

### Layer 3 — frontend `/ws`

| Path | What it holds |
|---|---|
| [`backend/api/websocket.py`](../../../backend/api/websocket.py) | `ConnectionManager` (line 36). Singleton at line 238. `connect`, `disconnect`, `broadcast`, `send_personal`, `broadcast_*` helpers per message type |
| [`frontend/src/hooks/useWebSocket.ts`](../../../frontend/src/hooks/useWebSocket.ts) | Module-level singleton (`sharedWs`, line 11). Multiple consumers subscribe to the same connection. Exponential reconnect, `CLIENT_PING_INTERVAL_MS=15000`, visibility-aware presence |

### Layer 4 — cross-process / in-process

| Path | What it holds |
|---|---|
| [`backend/services/redis_client.py`](../../../backend/services/redis_client.py) | Redis singleton with health check (15 s) and 1.5 s socket timeout; soft-fails on outage so the API stays up |
| [`backend/services/signal_bus_redis_bridge.py`](../../../backend/services/signal_bus_redis_bridge.py) | Subscribes to `signal_emission` + `signal_batch`; re-publishes to in-process `event_bus` as `trade_signal_emission` / `trade_signal_batch`. Dedup ring of 2048 IDs |
| [`backend/services/trader_events_bridge.py`](../../../backend/services/trader_events_bridge.py) | Subscribes to `trader_events`; broadcasts to UI WS as `trader_event` |
| [`backend/services/wallet_state_bus.py`](../../../backend/services/wallet_state_bus.py) | (referenced from `host.py:1212-1226`) wallet-deltas channel between trading plane and consumers |
| [`backend/services/event_bus.py`](../../../backend/services/event_bus.py) | In-process pub/sub singleton. `subscribe`, `publish`, `unsubscribe`. Wildcard `*` matches everything |
| [`backend/services/event_dispatcher.py`](../../../backend/services/event_dispatcher.py) | Strategy-aware dispatcher: `subscribe(strategy_slug, event_type, handler)`, `dispatch(event)` |

## Contracts

### Frontend `/ws` message types

Every payload is `{type, data}`. The full list (with the file
location of the `broadcast_*` helper):

| Type | Carries | Producer |
|---|---|---|
| `init` | initial snapshot (opportunities, status, workers, traders, execution_sessions) | `connect` ([websocket.py:739](../../../backend/api/websocket.py)) |
| `opportunities_update` | new batch | `broadcast_opportunities` (line 853) |
| `prices_update` | per-market `(market_id, yes_price, no_price, yes_sequence, no_sequence, is_fresh)` | FeedManager callback (line 428) |
| `position_marks_update` | mark-to-market deltas | `_on_position_marks_changed` (line 584) |
| `wallet_trade_event` | wallet trade with `(wallet_address, token_id, side, size, price, tx_hash, confirmed, source)` | `broadcast_wallet_ws_event` (line 872) |
| `scanner_status` / `scanner_activity` | scanner lifecycle | lines 921, 926 |
| `news_update` | new articles | line 933 |
| `crypto_markets_update` | live crypto markets | line 938 |
| `weather_update` / `weather_status` | weather pipeline | lines 943, 957 |
| `events_update` / `events_status` | events pipeline | lines 962, 976 |
| `worker_status_update` | worker heartbeat | line 695 |
| `trader_orchestrator_status` | trader lifecycle | line 735 |
| `trader_event` | per-trader decision/order/execution (via `trader_events_bridge`) | line 991 |
| `subscribe` / `subscribed` | channel subscription handshake | line 786 |
| `ping` / `pong` | heartbeat | line 818 |
| `ui_presence` / `presence_ack` | tab visibility | line 806 |

Channels (subscription scopes) are: `core`, `opportunities`,
`trading`, `crypto`, `weather`, `news`, `events`, `workers`,
`signals`, `wallet`, plus dynamic topics like `prices:<market>`
and `opportunities.<strategy>`.

### Redis pub/sub channels

| Channel | Publisher | Subscriber | Payload |
|---|---|---|---|
| `signal_emission` | `signal_bus` (any plane that publishes a signal) | `signal_bus_redis_bridge` on every plane | one signal payload |
| `signal_batch` | same | same | batch upsert payload |
| `trader_events` | `worker-trading` (decisions, orders, executions) | `trader_events_bridge` on the API plane | trader event JSON |
| wallet-state deltas (`wallet_state_bus`) | `worker-trading` (apply_trade/apply_order) | wallet consumers | wallet-state delta |

Redis has **no persistence** in this deployment (`--save ""`,
`--appendonly no`). A restart wipes pub/sub; durable state lives
only in Postgres. Consumers re-seed from DB after reconnect.

### In-process `event_bus`

Confirmed `EventType` values used across the codebase:

- `wallet_state.changed` — emitted by `WalletStateCache` after
  every mutation.
- `trade_signal_emission` / `trade_signal_batch` — re-emitted by
  the Redis bridge so strategies inside the same process can react
  without round-tripping Redis again.
- `MARKET_DATA_REFRESH`, `NEWS_UPDATE`, `WALLET_TRADE` and the
  rest of the strategy-facing events — see
  [`event_dispatcher.py`](../../../backend/services/event_dispatcher.py).

### Cross-plane communication summary

| Data | Transport |
|---|---|
| `AppSettings`, `Strategy` code, `Opportunity`, `DataSource` | Postgres (polling / change-fed) |
| Real-time signals | Redis pub/sub (`signal_emission`, `signal_batch`) → re-emitted on local `event_bus` |
| Trader events (UI fan-out) | Redis pub/sub (`trader_events`) → API plane → `/ws` `trader_event` |
| Wallet state deltas | Redis pub/sub (`wallet_state_bus`) |
| Live prices (UI) | `/ws` `prices_update` (FeedManager callback registers on `PriceCache`) |
| Strategy events within one process | `event_bus` directly |

## Dependencies (both directions)

**This layer depends on:**

- External venues (Polymarket, Kalshi, Binance) keeping their WS
  endpoints up.
- Redis being reachable. The client soft-fails on outage so API
  stays up — but real-time fan-out stops working until Redis is
  back.
- The exclusive Polymarket user-channel WS slot (one connection
  per API key) belonging to this deployment.

**Depended on by:**

- Every strategy (`event_bus`).
- The trader orchestrator (consumes `trade_signal_emission`,
  reads `WalletStateCache`).
- The frontend (every visible counter that moves).
- `worker-news` and `worker-discovery` indirectly: they write to
  Postgres; the API plane reads and broadcasts via `/ws`.

## Extension points

| When you want to… | Touch |
|---|---|
| Add a new live feed (third venue, oracle, etc.) | New module under `services/`; subclass the local feed pattern (connect → subscribe → on_message → land in cache + emit `event_bus`); register in `host.py` plane manifest. **Do not** open a second Polymarket user-channel WS — the slot is single-use. |
| Add a new `/ws` message type | New `broadcast_<name>` helper in `websocket.py`; route call sites to it; update `frontend/src/hooks/useWebSocket.ts` `messageTypes` filter if applicable; add a row to the message-type table above. |
| Add a new Redis channel | Pick a name that does not collide with `signal_*`, `trader_*`, `wallet_*`. Add a publisher and a bridge consumer. Keep payload JSON-serialisable; never assume delivery. |
| Add an in-process `EventType` | Add the constant in `event_dispatcher.py`; document the producer and consumer side-by-side; do not invent new EventTypes from a one-off subscriber. |

## Known footguns

- **Polymarket user-channel exclusivity.** Two processes with the
  same API key kill each other's session. The compose file is
  designed to launch exactly **one** `worker-trading`. If you
  spin a second instance for "redundancy," you'll get neither.
- **WS reconnect storms.** Binance's stale-data timeout was 8 s
  earlier and produced 5+ reconnects/hour under load; current
  default is 15 s
  ([`config.py:80`](../../../backend/config.py)).
- **Redis pub/sub has no persistence.** A restart wipes everything
  in flight. Code that relies on a Redis message must also be
  reachable through Postgres polling — and is, today, for every
  channel in the table above.
- **Frontend duplicate WS connection.** Bypassing the `useWebSocket`
  singleton (e.g. a hook that calls `new WebSocket(...)` directly)
  results in duplicate event handling. There is no defence; review
  every PR that touches WS clients.
- **Degenerate price books.** A `>50¢` spread on a 0–1 binary
  causes `_safe_binary_mid()`
  ([`ws_feeds.py:199-238`](../../../backend/services/ws_feeds.py))
  to return `None`. Strategies must tolerate `None`; downstream
  exit-evaluators skip the tick.
- **Signal dedup window.** The bridge keeps a ring of 2048 IDs.
  More than 2048 distinct signals in the dedup window
  (~minutes) can re-deliver an old ID. Practically: every
  observed surge is well below this; budget for it if you
  multiply emission rates 5×.
- **`is_fresh=false` on `prices_update`.** Means the price hasn't
  refreshed within `WS_PRICE_STALE_SECONDS` (30 s UI / 10 s
  trader). Strategies that act on stale ticks will get filtered
  by the trader's freshness gate; UI just dims the cell.

## Test coverage

- `backend/tests/test_websocket_topics.py` — message routing
- `backend/tests/test_ws_feeds.py` — `PolymarketWSFeed`,
  FeedManager
- `backend/tests/test_wallet_state_cache.py`
- `backend/tests/test_wallet_rtds_feed.py`
- `backend/tests/test_binance_feed.py`
- `backend/tests/test_chainlink_feed.py`
- `backend/tests/test_chainlink_direct_feed.py`
- `backend/tests/test_redis_client.py`
- `backend/tests/test_signal_bus_redis_bridge.py`
- `backend/tests/test_trader_events_bridge.py`
- `backend/tests/test_data_events_contracts.py`
- `backend/tests/test_intent_runtime_ws_freshness.py`
- `backend/tests/test_feed_availability.py`

## Where to look next

| Topic | File |
|---|---|
| Frontend hook + atoms | [`frontend-architecture.md`](frontend-architecture.md) |
| What sits on the worker-trading hot path that consumes these caches | [`worker-trading.md`](worker-trading.md), [`trader-pipeline.md`](trader-pipeline.md) |
| What happens after a trader event reaches execution | [`execution-and-fills.md`](execution-and-fills.md) |
| Three-plane runtime overview | [`system-overview.md`](system-overview.md) |

Last verified: 2026-05-08
