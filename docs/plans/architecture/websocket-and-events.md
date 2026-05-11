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

## Polymarket WS subscription discipline (Plan 0045)

> **Last verified: 2026-05-11 15:00 UTC (commits `2b44929b` through
> `68cf8dbd` — Plan 0045 WS-subscription discipline plus Plan 0049
> trader_events retention housekeeper).**

The Polymarket market-channel WS
(`wss://ws-subscriptions-clob.polymarket.com/ws/market`) imposes a
soft per-connection subscription cap that **silently drops the
freshest entries** once exceeded. Live capture in Plan 0045 saw
the server actively streaming books for **~9-26 active tokens**
while our local `_subscribed_assets` set held **6800+** stale
entries — every new subscribe past the live limit was acked but
never honoured. The cap was reproduced on a fresh process: a
clean `PolymarketWSFeed.subscribe([4 fresh tokens])` returned full
book data inside 1 s, while the live worker's identical call sat
silent because the connection's slot budget was already saturated
by accumulated subscriptions.

The cap is hard-coded on the server side; we cannot raise it.
**Every producer that calls `PolymarketWSFeed.subscribe()` must
diff against its previous active set and unsubscribe rotated-out
tokens**, otherwise its scope grows monotonically and starves
later subscribers in shared infrastructure.

### Producers + their discipline

| Producer | Site | Pattern | Notes |
|---|---|---|---|
| Crypto lane | [`market_runtime.py:_sync_crypto_subscriptions`](../../../backend/services/market_runtime.py) | Diff vs `self._crypto_subscribed_tokens` snapshot, unsubscribe stale, subscribe new | Runs every crypto refresh (~5 s); steady-state ~16 tokens |
| Scanner fast-scan | [`scanner.py:scan_fast`](../../../backend/services/scanner.py) | Diff vs `self._ws_subscribed_tokens`; **gated** on `settings.SCANNER_WS_SUBSCRIBE_ENABLED` (default OFF) | When toggle flips OFF mid-run, drains its own snapshot via `unsubscribe()` on the next scan |
| `btc_eth_*` strategies | [`btc_eth_directional_edge`](../../../backend/services/strategies/btc_eth_directional_edge.py), [`btc_eth_maker_quote`](../../../backend/services/strategies/btc_eth_maker_quote.py), [`btc_eth_convergence`](../../../backend/services/strategies/btc_eth_convergence.py) — each owns a `_BatchedCryptoMarketCache._subscribe_tokens_to_ws` | Diff vs per-cache `self._ws_subscribed_tokens` snapshot | Only fires when the strategy is enabled in `strategies.enabled` — disabled today on `polyhome-1` |
| `intent_runtime` hot-prewarm | [`intent_runtime.py:_ensure_hot_subscriptions`](../../../backend/services/intent_runtime.py) call sites at lines 1252 / 1757 / 1997 / 2509 | All four sites gated on `_allow_hot_subscription_for_source(source)` — scanner-source skipped unless the scanner toggle is on; other sources (crypto, traders, discovery, …) keep the full hot-prewarm | Without the gate, every scanner opportunity dripped 1-2 tokens into the set per emit |
| Recorder bulk subscriber | [`recorder_subscription_service.py:_ensure_subscribed`](../../../backend/services/recorder_subscription_service.py) | **No diff** — bulk-subscribes top-N-liquid (default 8000) markets every 60 s. **Gated** on `settings.RECORDER_SUBSCRIBE_ENABLED` (default OFF). When off, the loop idles instead of subscribing. | Was the hidden Plan 0045 producer (single `subscribe()` call added 6268 tokens in one shot). Operators who run backtests flip on via Settings → Scanner |
| Trader reconciliation | [`trader_reconciliation_worker.py:1377`](../../../backend/workers/trader_reconciliation_worker.py) | Subscribes only LIVE-mode open-order tokens | Bounded by open-order count; for shadow-only deployments this is always 0 |
| Live position marks (`main.py`) | [`main.py:667, 752`](../../../backend/main.py) | Subscribes wallet-position tokens | Backend process, separate `PolymarketWSFeed` singleton; not in worker-trading's set |
| Fast submit (`fast_submit.py:859`) | One-shot subscribe of the fill token immediately before submission | Single-shot, scope=1 token | Bounded |

### Operator-facing DB toggles in Settings → Scanner

Both default OFF; flip on only when the workflow that needs them is
in play.

| Toggle | Column | Runtime attr | Effect when OFF |
|---|---|---|---|
| Scanner WS price overlay | `app_settings.scanner_ws_subscribe_enabled` | `settings.SCANNER_WS_SUBSCRIBE_ENABLED` | Scanner falls back to HTTP polling; its hot-tier candidate tokens stay out of `_subscribed_assets` |
| Recorder bulk subscriber | `app_settings.recorder_subscribe_enabled` | `settings.RECORDER_SUBSCRIBE_ENABLED` | `recorder_subscription_service.run_loop` idles every 60 s; no bulk top-N-liquid subscribe |

Toggles are re-read every loop tick, so flipping ON via UI takes
effect within one cycle (≤ 60 s recorder, ≤ next fast-scan for
scanner) without restarting the worker.

### Rule for new producers

When adding a new code path that calls
`feed_manager.polymarket_feed.subscribe(...)`, **do not** call it
additively. The required shape:

```python
new_active: set[str] = set(...)             # the tokens you need NOW
previous = self._<scope>_subscribed_tokens  # last snapshot
to_subscribe = new_active - previous
to_unsubscribe = previous - new_active
if to_unsubscribe:
    await feed.unsubscribe(sorted(to_unsubscribe))
if to_subscribe:
    await feed.subscribe(sorted(to_subscribe))
self._<scope>_subscribed_tokens = (previous - to_unsubscribe) | new_active
```

If the producer is operator-toggleable, add a DB column to
`app_settings`, expose it via `ScannerSettingsModel`, mirror the
runtime attribute in `config.py:apply_app_settings`, and add a
Settings → Scanner toggle pattern matching
`scanner_ws_subscribe_enabled`. Default OFF for any producer that
is not strictly required by the trading hot path.

### Retention (Plan 0049)

> Plan 0044's cross-mode firehose binding cache made every shadow
> bot's every tick land a `firehose_evaluation` row in
> `trader_events` (~262 k rows / h ≈ 8.4 GB / day after Postgres
> overhead). Without retention the table reaches 30 GB in 4 days
> and the `polyhome-1` host's `/dev/sda1` runs out within a month.
> Plan 0049 added a two-tier housekeeper that keeps the table
> bounded.

Two retention horizons, both DB-backed
([`app_settings.trader_events_firehose_retention_days`](../../../backend/models/database.py)
and `app_settings.trader_events_other_retention_days`):

| Tier | Default | Covers | Why |
|---|---|---|---|
| `firehose_evaluation` | **7 days** | The bulk (~99 % of volume); every shadow-bot tick. | Backtester (Plan 0046) reaches back at most 24 h; A/B-test windows are days, not months. Steady-state size: ~44 M rows ≈ ~59 GB on disk. |
| Everything else | **90 days** | Low-volume audit trail (`decision`, `order`, `provider_health`, `circuit_breaker`, ...). | Diagnostic / regulatory; volume is negligible. |

Service:
[`backend/services/trader_events_retention_service.py`](../../../backend/services/trader_events_retention_service.py).
6 h cadence, 60 s startup grace, 50 000-row batches with 100 ms
pauses between batches (avoids runaway autovacuum / replica lag /
table-level locks on the first run that drains the backlog).
Each cycle reads the DB-backed knobs first, so flipping them in
Settings → DB Maintenance takes effect within ≤ 6 h without a
worker restart.

Lives on the `news` worker plane (the trading plane MUST stay clear
of long-running DELETE batches; news has the lightest hot-path
budget) — wired from
[`backend/workers/host.py`](../../../backend/workers/host.py)
`_initialize_services`. Idempotency lease via Redis key
`trader_events_housekeeper_running` (NX + 1 h TTL); soft-fails to
"always acquire" when Redis is disabled so single-host dev still
runs the sweep.

Operator dry-run (no DELETE):

```bash
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T \
    backend python -m scripts.trader_events_housekeeper_dry_run --json'
```

**First-run cost.** The first sweep after the housekeeper lands
must drain every row older than 7 days in one sitting. By the time
the activation triggers fire (table size > 30 GB ⇒ T+4 days), that
backlog is already past the new 7-day floor; if activation is
delayed to threshold (b)/(c) (>=14 d × 8.4 GB/day ≈ ~118 GB), the
50 000-row batches with pauses are what keeps Postgres responsive
during the drain. Monitor `pg_total_relation_size('trader_events')`
and `docker compose logs -f worker-news --since 1h` for the first
24 h after enabling.

### Diagnostic logs still in code (TEMP)

Plan 0045 left four short-lived INFO logs in place for one week
of stable-trading verification (target removal: 2026-05-18). They
are clearly marked with `(TEMP plan 0044)` / `(TEMP plan 0045)`
in the log message:

- `Polymarket WS diag (TEMP plan 0045)` — every 30 s heartbeat
  dump from `_heartbeat_loop`, sizes + message-type counters.
- `Polymarket WS subscribe call (TEMP plan 0045)` — per-call
  caller-frame attribution from inside `subscribe()`.
- `crypto_5m_midcycle WS subscribe issued (TEMP)` — per-event
  log inside `_ensure_ws_subscribed_for_5m`.
- `crypto_5m_midcycle gate reject` — MURMUR-tier rejection log
  inside `_emit_reject` (kept until `firehose_evaluation` is
  battle-tested as the persistent path).

All four go in a single cleanup commit referenced from Plan 0045
Task 6. Until then they live in code and are safe to ignore
operationally — log volume is bounded (≤ 5 lines / 30 s).

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

Last verified: 2026-05-11
