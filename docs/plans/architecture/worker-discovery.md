# Architecture: worker-discovery plane

This is the discovery plane: one of the three worker processes
Homerun launches. It owns wallet research, tracked-wallet trade
crawling, provider data import, the strategy reverse-engineer agent
loop, and the backtest queue. The whole plane is REST-bound and
intentionally isolated from the trading hot path so its in-flight
HTTP fan-out does not stall the orchestrator.

The companion notes are
[`worker-trading.md`](worker-trading.md) (the hot path) and
[`worker-news.md`](worker-news.md) (the ML plane). Strategy
reverse-engineer has its own deeper note —
[`strategy-reverse-engineer.md`](strategy-reverse-engineer.md).

## Purpose

This plane is responsible for:

1. **Wallet discovery** — sweeping Polymarket markets and the
   leaderboard to populate `discovered_wallets` with profitability
   metrics.
2. **Tracked-wallet trade crawling** — pulling trade tape for the
   operator-curated `tracked_wallets` set so the
   `traders_confluence` strategy can see their activity.
3. **Provider import** — draining `provider_import_jobs` to ingest
   external datasets (currently polybacktest snapshots) used by
   reverse-engineer and backtests.
4. **Strategy reverse-engineer** — the LLM agent loop that
   synthesises a `BaseStrategy` to mimic a wallet's behaviour. See
   [`strategy-reverse-engineer.md`](strategy-reverse-engineer.md)
   for the deep dive.
5. **Backtest worker** — running queued `backtest_runs`, each of
   which can saturate one CPU core for several minutes.

It does **not**:

- Execute trades.
- Subscribe to the Polymarket user-channel WebSocket (that is
  exclusively `worker-trading`).
- Consume `trade_signals` — it only **produces** them via
  `bridge_opportunities_to_signals` from `tracked_traders_worker`.

## Key files

| Path | What it holds |
|---|---|
| [`backend/workers/host.py:194-252`](../../../backend/workers/host.py) | `_PLANE_CONFIGS["discovery"]` — plane manifest: which workers start, no live execution, no intent runtime |
| [`backend/workers/discovery_worker.py`](../../../backend/workers/discovery_worker.py) | Wallet discovery loop. `DISCOVERY_RUN_INTERVAL_MINUTES` (default 15 min) |
| [`backend/services/wallet_discovery.py`](../../../backend/services/wallet_discovery.py) | The discovery engine. `_run_with_bounded_workers()` (lines 45–114) caps concurrency at 15; `_discover_wallets_from_market()` (line 1770), `_discover_wallets_from_leaderboard()` (line 1824) |
| [`backend/workers/tracked_traders_worker.py`](../../../backend/workers/tracked_traders_worker.py) | Tracked wallets pool + confluence. `FULL_SWEEP_INTERVAL=30 min`, `INCREMENTAL_REFRESH=2 min`, `POOL_RECOMPUTE=1 min` |
| [`backend/workers/provider_import_worker.py`](../../../backend/workers/provider_import_worker.py) | Drains `ProviderImportJob` queue. Poll interval `HOMERUN_PROVIDER_IMPORT_POLL_INTERVAL_SECONDS` (default 5 s) |
| [`backend/workers/strategy_reverse_engineer_worker.py`](../../../backend/workers/strategy_reverse_engineer_worker.py) | Drains `strategy_reverse_engineer_jobs` queue. Poll interval `HOMERUN_REVERSE_ENGINEER_POLL_INTERVAL_SECONDS` (default 5 s) |
| [`backend/workers/backtest_worker.py`](../../../backend/workers/backtest_worker.py) | Drains `backtest_runs`. CPU-heavy 1M-snapshot replays. Poll interval `HOMERUN_BACKTEST_POLL_INTERVAL_SECONDS` (default 3 s) |
| [`backend/services/external_data/polymarket.py`](../../../backend/services/external_data/polymarket.py) | The shared Polymarket client used by all of the above. Cooldown state at line 321 / 486 |

## Contracts

### Concurrency discipline

The plane's reason for existing is **event-loop discipline**.
Specifically (`system-overview.md` § Why three worker planes,
[`workers/host.py`](../../../backend/workers/host.py) line ~95):

- A typical wallet sweep keeps ~90 in-flight `asyncio` tasks.
- Co-located with the trading plane, this caused 5–8 s event-loop
  stalls and a p99 of 384 active tasks during a 5-hour soak.
- Splitting it out brings the trading-plane p99 active-tasks back
  below 100.

The single rule: this plane uses `_run_with_bounded_workers()`
([`wallet_discovery.py:45-114`](../../../backend/services/wallet_discovery.py))
or equivalent semaphores — never bare `asyncio.gather()` over
hundreds of awaitables.

### Worker queues

Three of the five workers here are queue drainers; each pulls one
job at a time from a Postgres table:

| Worker | Queue table | One-at-a-time? |
|---|---|---|
| `provider_import_worker` | `provider_import_jobs` ([`database.py:3516`](../../../backend/models/database.py)) | yes |
| `strategy_reverse_engineer_worker` | `strategy_reverse_engineer_jobs` ([`database.py:3564`](../../../backend/models/database.py)) | yes — atomic `WITH FOR UPDATE SKIP LOCKED` claim ([`service.py:141-184`](../../../backend/services/strategy_reverse_engineer/service.py)) |
| `backtest_worker` | `backtest_runs` ([`database.py:3359`](../../../backend/models/database.py)) | yes — keeps a single CPU core saturated rather than thrashing |

The other two (`discovery_worker`, `tracked_traders_worker`) are
periodic loops with their own settings-driven cadences.

### Output rows

| Table | What it holds | Cited in |
|---|---|---|
| `discovered_wallets` | full wallet profiles, profitability metrics | [`database.py:2411`](../../../backend/models/database.py) |
| `tracked_wallets` | operator-curated set | [`database.py:706`](../../../backend/models/database.py) |
| `wallet_trades` | trade tape for tracked wallets (NOT a count of all-time activity — that's an aggregate elsewhere; see footguns) | [`database.py:732`](../../../backend/models/database.py) |
| `autoresearch_experiments` / `_iterations` | per-trader / per-strategy LLM-driven param tuning | [`database.py:2340, 2378`](../../../backend/models/database.py) |
| `strategy_reverse_engineer_jobs` / `_iterations` | the wallet-mimicry pipeline state | [`database.py:3564, 3646`](../../../backend/models/database.py) |
| `backtest_runs` | backtest queue + results | [`database.py:3359`](../../../backend/models/database.py) |
| `provider_import_jobs` | external data ingest queue | [`database.py:3516`](../../../backend/models/database.py) |
| `provider_datasets` | catalog populated by completed import jobs | (alembic 202605040001) |
| `trade_signals` (source='traders/*') | published by `tracked_traders_worker` via `bridge_opportunities_to_signals` | [`tracked_traders_worker.py:744`](../../../backend/workers/tracked_traders_worker.py) |

### `AppSettings` columns

```
DISCOVERY_RUN_INTERVAL_MINUTES               (env-driven, 15 min default)
DISCOVERY_MAX_MARKETS_PER_RUN                (default 100)
DISCOVERY_MAX_WALLETS_PER_MARKET             (default 50)
discovery_max_discovered_wallets             (default 20_000)
TRACKED_WALLETS                              (env-driven list[str])
WS_WALLET_MONITOR_ENABLED                    (default True; trading-plane consumer though)
CLEANUP_WALLET_TRADE_DAYS                    (default 60)
CLEANUP_WALLET_ACTIVITY_ROLLUP_DAYS          (default 60)
CLEANUP_WALLET_ACTIVITY_DEDUPE_ENABLED       (default True)
reverse_engineer_max_iterations              (1312)
reverse_engineer_target_score                (1313)
reverse_engineer_max_cost_usd                (1314)
reverse_engineer_max_wallet_trades           (1315)
autoresearch_*                               (1655-1663) — eleven columns
HOMERUN_PROVIDER_IMPORT_POLL_INTERVAL_SECONDS  (env, default 5 s)
HOMERUN_REVERSE_ENGINEER_POLL_INTERVAL_SECONDS (env, default 5 s)
HOMERUN_BACKTEST_POLL_INTERVAL_SECONDS         (env, default 3 s)
```

## Dependencies (both directions)

**This plane depends on:**

- Polymarket Gamma API and Data API ([`polymarket.py`](../../../backend/services/external_data/polymarket.py)) — the only external data path used by discovery itself. Polygon RPC is **not** consumed directly here; chain data already comes through Polymarket's APIs.
- Polybacktest external SaaS API ([`polybacktest_client.py`](../../../backend/services/external_data/polybacktest_client.py)) — used by `provider_import_worker` and by the reverse-engineer agent's `polybacktest_*` tools.
- `LLMManager` for the reverse-engineer agent loop and (where used) for `autoresearch_service`.
- The shared `signal_bus_redis_bridge` for fan-out of any signals it publishes via `bridge_opportunities_to_signals`.

**Depended on by:**

- `worker-trading` orchestrator — reads `trade_signals` produced by `tracked_traders_worker` (`source='traders/*'`).
- `worker-trading` for `wallet_state_cache` rebuilds (it consumes the same `wallet_trades` table).
- The frontend Wallets / Discovery / Backtests tabs (`routes_discovery.py`, `routes_strategy_reverse_engineer.py`, `routes_backtest.py`).
- The Strategy Library — promoted reverse-engineer outputs land in `strategies` table.

## Extension points

| When you want to… | Touch |
|---|---|
| Add a new wallet-discovery source (e.g. another leaderboard) | Add a method on `wallet_discovery.py` and register it in the orchestrating loop. Respect the bounded-worker pattern. |
| Add a new external dataset provider | New service under `services/external_data/`, new `DataProviderKind` value, a migration for any new fields, then handle the kind in `provider_import_worker.run_job(...)`. |
| Add a new background job queue on this plane | New table + new worker module + add to `_PLANE_CONFIGS["discovery"].worker_modules` in `host.py`. Use the SKIP-LOCKED claim pattern from `strategy_reverse_engineer/service.py`. |
| Tune wallet-discovery cadence | `DISCOVERY_RUN_INTERVAL_MINUTES`, `DISCOVERY_MAX_MARKETS_PER_RUN`, `DISCOVERY_MAX_WALLETS_PER_MARKET`. |
| Tighten / loosen reverse-engineer budget | `reverse_engineer_max_iterations`, `reverse_engineer_max_cost_usd`. See [`strategy-reverse-engineer.md`](strategy-reverse-engineer.md). |

## Known footguns

- **Polymarket 429 cool-down.** Aggressive fan-out triggers
  per-endpoint cool-downs in `polymarket_client`
  ([`polymarket.py:486`](../../../backend/services/external_data/polymarket.py)).
  The retry is exponential and silent; symptom is wallet sweep
  duration creeping up without an explicit error.
- **`tracked_wallets.total_trades` is an aggregate, not a row count.**
  It reflects the wallet's historical trading on Polymarket.
  `wallet_trades` is the **local cache** of crawled trades and can
  be empty even when `total_trades=500` — already documented in
  [`trader-pipeline.md`](trader-pipeline.md) § Footguns.
- **Long reverse-engineer jobs block the queue.** The worker is
  one-at-a-time by design (each job is heavy). If a single job
  exhausts its iteration budget without finalizing, the queue
  stalls. Operators can `cancel` a job from the UI; otherwise
  consider raising `max_iterations` rather than spinning new jobs.
- **Backtest CPU saturation.** Each backtest pegs one core. Three
  concurrent backtests on a 4-vCPU host (the `polyhome-1` size)
  starves everything else on this plane. The single-job-at-a-time
  worker is intentional.
- **Polybacktest empty data.** A successful import with zero
  snapshots is silent — the reverse-engineer agent will then loop
  scoring against an empty backtest. The
  `wallet_market_coverage` tool exposes this; if it's reporting
  many uncovered markets, the agent should filter, not iterate.
- **`is_paused` on this plane is not the trading kill-switch.**
  Pausing discovery does not pause traders. Conversely, pausing
  traders does not pause discovery. Both are independent
  `worker_control` rows.

## Test coverage

- `backend/tests/test_wallet_discovery_accuracy.py`
- `backend/tests/test_wallet_discovery_growth.py`
- `backend/tests/test_wallet_discovery_leaderboard.py`
- `backend/tests/test_smart_wallet_pool_and_confluence.py`
- `backend/tests/test_wallet_state_cache.py`
- `backend/tests/test_wallet_rtds_feed.py`
- `backend/tests/test_wallet_ws_monitor.py`
- `backend/tests/test_wallet_state_bus.py`
- `backend/tests/test_routes_discovery_trader_signals.py`
- `backend/tests/test_routes_discovery_pool_members.py`
- `backend/tests/test_routes_discovery_trader_network.py`
- `backend/tests/test_strategy_backtester.py`
- `backend/tests/test_backtest_engine.py`
- `backend/tests/test_param_optimizer_backtest_manifest.py`

## Where to look next

| Topic | File |
|---|---|
| Strategy reverse-engineer pipeline | [`strategy-reverse-engineer.md`](strategy-reverse-engineer.md) |
| `traders/*` signal family + copy-trade | [`copy-trade-pipeline.md`](copy-trade-pipeline.md) |
| Wallet scoring + risk-detection stack (insider, anomaly, intelligence) | [`wallet-intelligence.md`](wallet-intelligence.md) |
| Trader pipeline that consumes the produced signals | [`trader-pipeline.md`](trader-pipeline.md) |
| Three-plane runtime overview | [`system-overview.md`](system-overview.md) |
| The other two worker planes | [`worker-trading.md`](worker-trading.md), [`worker-news.md`](worker-news.md) |

Last verified: 2026-05-08
