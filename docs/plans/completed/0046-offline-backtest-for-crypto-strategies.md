# Plan: Offline backtest harness for crypto 5m strategies

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](../README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](../completed/) on close. Every commit produced by
> this plan carries a `Plan: 0046` git trailer (see
> [Commits and traceability](../README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](../plan-control-index.md).
>
> **Status: COMPLETE.** Implemented 2026-05-11 by adding (1) the
> `crypto_oracle_history` table + 1 Hz throttled persistence in
> `ChainlinkFeed` with a 14-day housekeeper, (2) the
> `_run_crypto_replay_detection` path in `strategy_backtester.py`
> that reconstructs synthetic `crypto_update` events from persisted
> `firehose_evaluation` rows and computes PnL from
> `crypto_oracle_history`, and (3) the `optimize-strategy` endpoint
> with crypto-aware `TradingParameters` fields and grid sweep.

## Overview

Today the only way to compare configurations of `crypto_5m_midcycle`
is to spin up parallel shadow traders and wait for `trader_events`
rows to accumulate. With ≤ 3 fire-eligible cycles per hour and a
narrow gate-pass funnel (single-digit % of cycles emit), 24 h of
live shadow per config yields fewer than 5–10 actual entries — not
enough statistical mass to choose between, say, `min_distance_bps`
of 5 vs 7 vs 10.

A 2026-05-11 audit (this session) found that the existing
backtest infra (`backend/services/strategy_backtester.py`,
`backend/services/param_optimizer.py`) **cannot run
`crypto_5m_midcycle`**:

1. `strategy_backtester` only replays Polymarket Gamma payloads
   (YES/NO token history). It never constructs
   `DataEvent(event_type="crypto_update")`, which is the only event
   type `crypto_5m_midcycle.on_event` accepts (early `return []`
   otherwise — see
   [crypto_5m_midcycle.py:323](../../../backend/services/strategies/crypto_5m_midcycle.py:323)).
2. There is no durable Chainlink price series. `ChainlinkFeed`
   keeps an in-memory `deque` with a 3h TTL
   ([chainlink_feed.py:106-107](../../../backend/services/chainlink_feed.py:106)).
   Nothing is written to Postgres, so there is no way to look up the
   oracle price at `cycle_end_ms` for PnL evaluation post-hoc.
3. `TradingParameters` ([param_optimizer.py:94-141](../../../backend/services/param_optimizer.py:94))
   has no fields for any crypto knob (`min_distance_bps`,
   `max_entry_price`, `min_entry_price`, `midcycle_seconds`,
   `max_oracle_age_ms`, `bet_size_usd`). The grid spec
   (`DEFAULT_PARAM_SPECS`) likewise has zero crypto entries.

This plan delivers the three pieces that close the gap: a
persistent Chainlink series, a `crypto_update` replay path in the
backtester, and crypto-aware `TradingParameters` + API. Done state
is the operator being able to POST a parameter grid for
`crypto_5m_midcycle` against a chosen historical window (e.g. last
7 days) and get back a leaderboard of (param-set → emit count,
expected PnL, win rate) in minutes, not days.

## Context / References

- [Architecture: trader-pipeline](../architecture/trader-pipeline.md)
- [Architecture: execution-and-fills](../architecture/execution-and-fills.md)
- [Architecture: websocket-and-events](../architecture/websocket-and-events.md)
- [Strategy doc: crypto-5m-midcycle](../../strategies/crypto-5m-midcycle.md)
- [crypto_5m_midcycle.py:399-721](../../../backend/services/strategies/crypto_5m_midcycle.py) — gate chain that backtest must reproduce
- [chainlink_feed.py](../../../backend/services/chainlink_feed.py) — current in-memory oracle store
- [strategy_backtester.py](../../../backend/services/strategy_backtester.py)
- [param_optimizer.py](../../../backend/services/param_optimizer.py)
- [api/routes_validation.py](../../../backend/api/routes_validation.py)
- [services/strategies/_firehose.py:467](../../../backend/services/strategies/_firehose.py:467) — replay source of truth

## Validation Commands

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest backend/tests/test_chainlink_feed.py backend/tests/test_strategy_backtester.py backend/tests/test_param_optimizer_crypto.py -q'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend ruff check backend/services/chainlink_feed.py backend/services/strategy_backtester.py backend/services/param_optimizer.py backend/api/routes_validation.py'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend alembic upgrade head'` (after Task 1's migration lands)
- End-to-end smoke: `ssh polyhome-1 'curl -fsS -X POST http://127.0.0.1:8888/api/validation/code-backtest/optimize-strategy -H "Content-Type: application/json" -d "{\"strategy_slug\":\"crypto_5m_midcycle\",\"window_hours\":24,\"grid\":{\"min_distance_bps\":[5,10,15]}}"' | jq '.leaderboard'`

### Task 1: persist Chainlink oracle history to Postgres

- [x] Create Alembic migration adding `crypto_oracle_history` table:
      columns `(asset varchar(8), timestamp_ms bigint, price double precision, source varchar(32), created_at timestamp)`,
      compound PK on `(asset, timestamp_ms, source)`,
      btree index on `(asset, timestamp_ms DESC)`. Migration file
      lives at `backend/alembic/versions/<date>_add_crypto_oracle_history.py`.
- [x] Add the matching SQLAlchemy model `CryptoOracleHistory` to
      [`backend/models/database.py`](../../../backend/models/database.py) alongside other crypto-related models.
- [x] In [`chainlink_feed.py`](../../../backend/services/chainlink_feed.py), add an async writer hook
      invoked from the existing ingest path (around the `_history`
      append). Throttle: at most one row per `(asset, source)` per
      1000 ms — drop intermediate readings, write the latest sample
      at each 1-second slot to keep table size bounded
      (~86k rows/day/asset × 4 assets × 2 sources ≈ 700k rows/day,
      retention ≤ 14 days handled by a separate housekeeper task).
- [x] Reuse existing async DB session pattern from sibling services
      (e.g. `recorder_subscription_service.py`) — `AsyncSessionLocal`
      context manager, no new connection pool.
- [x] Regression test in
      `backend/tests/services/test_chainlink_feed.py` (create if
      missing): inject 100 mock readings over 60 simulated seconds,
      assert exactly 60 rows per asset are persisted (1 Hz throttle),
      that the most recent reading wins within each 1-s slot, and
      that `crypto_oracle_history` round-trips back through a
      sample query selecting by `(asset, end_ms)`.
- [x] Add a TTL housekeeper task: delete rows older than 14 days,
      run once per 6 h via the existing scheduled-tasks plumbing in
      `backend/services/scheduled_tasks/`. Regression test asserts
      housekeeper drops aged rows but preserves fresh ones.
- [x] Update [architecture/execution-and-fills.md](../architecture/execution-and-fills.md)
      with a line under "Chainlink path" noting that ingested
      readings are also persisted to `crypto_oracle_history` for
      backtest replay.
- [x] Mark completed

### Task 2: synthesize `crypto_update` DataEvents in the backtester

- [x] In [`strategy_backtester.py`](../../../backend/services/strategy_backtester.py),
      add `_run_crypto_replay_detection(strategy, asset, window_ms_start, window_ms_end)`
      modelled on `_run_ohlc_replay_detection` (`:397-566`) but
      emitting `DataEvent(event_type="crypto_update", payload={"markets": [...], ...})`
      shaped exactly as the live `crypto-worker` would.
- [x] Reconstruct each 5-minute cycle's market dict from the
      Polymarket markets table (filter by 5-minute slug pattern in
      the chosen window). Fill `oracle_prices_by_source` from
      `crypto_oracle_history` for each tick.
- [x] For the `book_depth` / `book_fresh` / `vwap_in_range` gates,
      **prefer the persisted `firehose_evaluation` row** for that
      market+cycle — it already contains the exact `vwap_price`,
      `slippage_bps`, `staleness_ms` the live run saw. Fall back to
      "skipped — no book snapshot" with `outcome="rejected_no_data"`
      when the row is absent. Document the limitation explicitly:
      varying `bet_size_usd` away from the live value invalidates
      the persisted VWAP — flag it in the API response.
- [x] PnL computation: for each emitted opportunity, look up the
      Chainlink price at `end_ms` from `crypto_oracle_history`
      (`order by timestamp_ms desc limit 1 where timestamp_ms <= end_ms`).
      Outcome: `YES` wins iff `oracle_at_end > reference_price`,
      else `NO` wins. Payout `$1 - vwap` on win, `-vwap` on loss.
- [x] Regression test in
      `backend/tests/services/test_strategy_backtester.py`: synth
      24 h of mock `crypto_oracle_history` + a known
      `firehose_evaluation` log, replay through
      `_run_crypto_replay_detection` with the live config and
      assert (a) emit count matches the log's
      `outcome=emitted` rows, (b) PnL signs match the live shadow
      ledger, (c) varying `min_distance_bps` from 3 → 15 reduces
      emit count monotonically.
- [x] Mark completed

### Task 3: parameter sweep API for crypto strategies

- [x] Extend `TradingParameters` dataclass in
      [`param_optimizer.py:94-141`](../../../backend/services/param_optimizer.py:94) with optional
      fields: `min_distance_bps`, `max_entry_price`, `min_entry_price`,
      `midcycle_seconds`, `min_seconds_to_resolution`,
      `max_oracle_age_ms`, `bet_size_usd`. Keep them `Optional[float]`
      so non-crypto sweeps still work. Bump
      `_module_code_sha()` invalidation by editing module body so
      cached optimizer results don't collide.
- [x] Add matching `ParameterSpec` rows to `DEFAULT_PARAM_SPECS` with
      sensible bounds (mirror the UI schema from
      `crypto_5m_midcycle.py:103-191`).
- [x] Add a new endpoint
      `POST /api/validation/code-backtest/optimize-strategy` to
      [`api/routes_validation.py`](../../../backend/api/routes_validation.py)
      accepting `{strategy_slug, window_hours, grid: dict[str, list]}`.
      Loads the strategy class from the in-memory registry by slug
      (no `source_code` round-trip), constructs the grid, dispatches
      through `ParameterOptimizer.run_grid_search` against
      `_run_crypto_replay_detection`, returns
      `{leaderboard: [GridConfigResult, …], window: {…}, caveats: [list]}`.
- [x] Caveats list MUST include `"bet_size_usd swept on persisted
      VWAP — replayed slippage assumes the live bet size at the time
      the row was logged"` whenever `bet_size_usd` is in the grid.
- [x] Frontend: no new screen in this plan. Operators consume the
      result via the API or by re-using the existing
      `/validation/optimization-results` UI — confirm that page
      renders crypto fields without modification (add the new
      columns to its table mapping if it has a hard-coded one).
- [x] Regression test in
      `backend/tests/services/test_param_optimizer.py`: small grid
      (`min_distance_bps: [5, 10]`, `max_entry_price: [0.6, 0.7]`),
      24 h synth window, assert the API returns 4 leaderboard rows,
      each with `emit_count`, `total_pnl_usd`, `win_rate`,
      `samples` populated and ordered by `composite_score`
      descending.
- [x] Mark completed

### Task 4: docs + close

- [x] Update [`docs/strategies/crypto-5m-midcycle.md`](../../strategies/crypto-5m-midcycle.md)
      with a new section "How to tune via offline backtest" linking
      to the new endpoint, with one curl example and one example
      reading of the leaderboard.
- [x] Update [`docs/plans/architecture/trader-pipeline.md`](../architecture/trader-pipeline.md)
      with a paragraph noting that `crypto_oracle_history` is the
      backtest oracle source-of-truth and that
      `firehose_evaluation` rows are the VWAP source-of-truth.
- [x] Move this file from `backlog/` to `completed/` and update the
      `plan-control-index.md` link target.
- [x] Mark completed
