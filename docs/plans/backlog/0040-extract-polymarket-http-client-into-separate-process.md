# Plan: Extract Polymarket HTTP client into a separate process

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](../README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](../completed/) on close. Every commit produced by
> this plan carries a `Plan: 0040` git trailer (see
> [Commits and traceability](../README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](../plan-control-index.md).
>
> **Status: BACKLOG.** Activate when one of:
> (a) a `py-spy` flamegraph shows Polymarket HTTP / gamma calls
>     (or related cooldown / retry waits) consuming ≥ 15 % of
>     `worker-trading`'s CPU **or** event-loop time, OR
> (b) `event_loop_watchdog` sustained-stall events on
>     `worker-trading` exceed 10/h for a 24 h window with no
>     other root cause identified, OR
> (c) operator wants to tune Polymarket-call rate limits / batching
>     in isolation without redeploying the trading hot-path.

## Overview

`worker-trading` currently hosts three orthogonal workloads on a
single Python process and event loop:

1. **Trading hot-path** — orchestrator cycles, strategy
   `evaluate()`, `_persist_execution_projection`, signal-bus
   consumption, fast-tier traders.
2. **Polymarket HTTP fetching** — gamma `/markets`, `/events`,
   trades lookup, condition_id resolution, market refresh,
   wallet position pulls. Synchronous-from-the-asyncio-loop's
   POV (we await each `_rate_limited_get`, but those waits add up
   under burst).
3. **Polymarket WS subscriptions** — `wallet_ws_monitor` per
   tracked wallet, market WS feed where applicable.

(2) is the noisy neighbour. Gamma calls have endpoint cooldowns
(see `_stamp_endpoint_cooldown` / `_endpoint_cooldown_remaining`
in [`backend/services/polymarket.py`](../../../backend/services/polymarket.py)),
exponential backoff on 429s, and bulk-refresh patterns that fire
batches of 80-condition_id queries. When the gamma side stalls
(observed historically: minutes-long 429 windows), the trading
hot-path inherits that stall — the same event loop is awaiting
the same sleep. Conversely, when the trading hot-path is
CPU-saturated (a known issue documented in plans
[`0003-profile-worker-trading-hotspots`](../completed/0003-profile-worker-trading-hotspots.md)
and [`0004-optimize-worker-trading-cpu-hotspots`](../backlog/0004-optimize-worker-trading-cpu-hotspots.md)),
the Polymarket HTTP fetcher loses its budget too.

Splitting the Polymarket HTTP client into a **separate process**
(its own container in `docker-compose.yml`) decouples the two
budgets. The trading hot-path queries the new service over a
local IPC channel (Redis pub/sub for fire-and-forget refresh
requests, plus a thin HTTP-or-Redis-RPC layer for the
synchronous `get_market_by_condition_id` reads that the
reconciler needs). Cache hits stay fast; cache misses become an
out-of-process round-trip that the trading event loop awaits
but the gamma call's cooldown / 429 window no longer parks the
trading loop itself.

WS subscriptions (item 3) are a separate question — they have
their own per-wallet connection model and do not necessarily
benefit from co-location with the HTTP client. They might end
up in the same new process, OR remain in `worker-trading`, OR
become a third small process. Pick after measuring (out of scope
here; revisit if WS is the actual bottleneck).

### What "done" looks like

- A new container `worker-polymarket` (name TBD) appears in
  `docker-compose.yml`, runs an asyncio process whose only job
  is gamma `/markets` / `/events` / trades / condition_id
  resolution requests, returning answers via Redis pub/sub
  request/response or a small HTTP RPC.
- `backend/services/polymarket.py` becomes a thin client that
  enqueues requests to the new service for cache misses, and
  hits the existing `MarketCacheService` for hits (no change to
  hot-path behaviour for cached reads).
- The persistent `MarketCacheService` (Postgres-backed) is the
  shared truth layer; the new process is the only writer to that
  cache for refreshed gamma rows. The trading process becomes a
  reader.
- Endpoint cooldowns / 429 state move to the new process — when
  gamma 429s the trading loop is unaffected (cache-only reads
  continue, and new misses fail open per the existing
  `_market_lookup_cooldown_until` pattern, just isolated).
- `event_loop_watchdog` stall rate on `worker-trading` drops by
  ≥ the gamma-attributable share (measure with `py-spy` before/after
  per the plan 0003 methodology).
- Regression tests pin the new IPC contract (cache hit shape,
  miss-then-fetch round-trip, fetch failure surfaces a clean
  error rather than blocking the caller).
- Deployment doc + rollback recipe in
  [`deploy/AGENTS.md`](../../../deploy/AGENTS.md) and
  [`docs/plans/architecture/system-overview.md`](../architecture/system-overview.md).

## Context / References

- Current Polymarket HTTP client (the chunk to extract):
  [`backend/services/polymarket.py`](../../../backend/services/polymarket.py)
  — ~2800 lines, the bulk under the `PolymarketClient` class.
  Key methods to wrap as IPC: `get_market_by_condition_id`,
  `get_market_by_token_id`, `get_market_trades`, `get_events`,
  `get_open_positions`, `get_closed_positions`, plus the
  cooldown / 429 helpers.
- Persistent cache layer (already shared across processes via
  Postgres):
  [`backend/services/market_cache_service.py`](../../../backend/services/market_cache_service.py)
  — this is the "only writer" target.
- WS monitor (NOT in scope unless measurement says so):
  [`backend/services/wallet_ws_monitor.py`](../../../backend/services/wallet_ws_monitor.py).
- worker-trading process model + CPU hotspots:
  [`docs/plans/architecture/worker-trading.md`](../architecture/worker-trading.md)
  + plans
  [`0003-profile-worker-trading-hotspots`](../completed/0003-profile-worker-trading-hotspots.md),
  [`backlog/0004-optimize-worker-trading-cpu-hotspots`](../backlog/0004-optimize-worker-trading-cpu-hotspots.md).
- Established multi-process pattern (templates to follow):
  - `worker-news`, `worker-discovery`, `worker-trading`
    container definitions in `docker-compose.yml`.
  - Their entrypoints in `backend/workers/host.py` (or
    sibling launchers).
- IPC patterns already used:
  - Redis pub/sub: `services/event_bus.py` (in-process) +
    `signal_bus_redis_bridge.py` (cross-process).
  - Postgres-backed shared state: `services/shared_state.py`.

## Validation Commands

- `bash scripts/run_tests_remote.sh tests/test_polymarket_client.py`
- `bash scripts/run_tests_remote.sh tests/test_polymarket_<ipc-shim>.py`  *(new file added by Task 2)*
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose ps' | grep worker-polymarket`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since=10m worker-polymarket 2>&1 | grep -ciE "ERROR|WARN"'` — sanity, expect low
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since=10m worker-trading 2>&1 | grep -c "Event-loop stall"'` — expected to drop vs pre-deploy

## Out of scope

- **Moving WS subscriptions out of worker-trading.** Separate
  question; only do if measurement points there. WS has its own
  per-connection lifetime + reconnection logic that's tightly
  coupled to wallet-discovery enrolment.
- **Replacing Polymarket-py-clob-client (the live execution
  HTTP path).** Live submit goes through
  `live_execution_service.py`; that's another HTTP path entirely
  and its latency budget is "as fast as possible" (single-shot
  per order). Co-locating with the slow gamma fetch process
  defeats the purpose.
- **Cache-shape redesign.** The existing
  `MarketCacheService` (Postgres) is the canonical store; the
  new process becomes its only writer for gamma-derived rows.
  Don't migrate to Redis-as-truth or anything else.
- **Rate-limit recompute / batching changes.** Apply the move
  first, measure, then tune in a separate plan.
- **Removing the in-process `_market_cache` LRU dict in
  `PolymarketClient`.** Keep the per-process LRU in front of
  the IPC call — same pattern as today, just one more level
  of indirection on miss.

### Task 1: Carve `PolymarketClient` into a server-side module + thin client

The current `PolymarketClient` mixes "HTTP transport + cooldown
+ cache" (server-side concerns) with "API surface methods
called by trading code" (client-side concerns). Split them so
the same Python class can run in either role.

- [ ] Inventory which `PolymarketClient` methods are called from
      `worker-trading` code paths. Grep
      `backend/{workers,services}/` for `polymarket_client.` and
      list every callsite. Note which methods are READS (gamma)
      vs WRITES (none today, but cache writes count).
- [ ] In a new
      `backend/services/polymarket_ipc.py` (sketch — name TBD):
      define a small Pydantic request/response schema for each
      READ method that needs to cross process boundaries
      (`get_market_by_condition_id`, `get_market_by_token_id`,
      `get_market_trades`, `get_events`,
      `get_open_positions`, `get_closed_positions`). One enum +
      typed payload per method.
- [ ] Write a `PolymarketIPCClient` that has the same surface as
      `PolymarketClient` for the listed methods but routes the
      call through Redis pub/sub (request channel +
      per-correlation-id response channel, with timeout). Cache
      hits go through the existing in-process `_market_cache`
      first; misses round-trip.
- [ ] Mark completed

### Task 2: New worker process

- [ ] Add `worker-polymarket` service in `docker-compose.yml`
      mirroring the shape of `worker-news`. Same image (it's
      just a different `command:`), separate restart policy,
      separate log volume.
- [ ] Add an entrypoint
      `backend/workers/polymarket_worker.py` that subscribes to
      the IPC request channel, dispatches to the existing
      `PolymarketClient` (now treated as the server-side
      implementation), and publishes the response.
- [ ] Wire the existing `MarketCacheService` writes through the
      new process (it becomes the only writer for
      gamma-derived rows; reads stay shared via Postgres).
- [ ] Configure the trading process to instantiate
      `PolymarketIPCClient` instead of `PolymarketClient` (a
      single import-time switch behind an env var like
      `POLYMARKET_CLIENT_MODE=ipc|inprocess` so we can roll
      back instantly without redeploy).
- [ ] Mark completed

### Task 3: Regression tests + deploy + measurement

- [ ] Unit tests for `PolymarketIPCClient`: happy round-trip,
      miss-then-fetch caches the result locally, fetch
      timeout surfaces as the same error class
      `PolymarketClient` already raises (so callers don't need
      to know about the split). Test file:
      `backend/tests/test_polymarket_ipc.py`.
- [ ] Integration test that runs both processes in a
      docker-compose-like fixture and confirms an end-to-end
      condition_id lookup returns identical data to the
      in-process baseline.
- [ ] Pre-deploy: capture `py-spy` flamegraph + 1-h
      `event_loop_watchdog` stall counts on `worker-trading`
      (per plan 0003 methodology). Record numbers in this task.
- [ ] Deploy: `./deploy/sync_remote.sh`. Confirm
      `worker-polymarket` container comes up healthy.
      `POLYMARKET_CLIENT_MODE=ipc` flips trading to IPC mode.
- [ ] Post-deploy: same measurement after 1 h soak. Expected:
      gamma-attributable CPU on `worker-trading` drops to ~0;
      stall events drop. Record numbers.
- [ ] Update
      [`docs/plans/architecture/worker-trading.md`](../architecture/worker-trading.md)
      to reflect the new process layout. Add a new note
      `docs/plans/architecture/worker-polymarket.md` documenting
      the IPC contract + cooldown ownership.
- [ ] Append a runtime-tweaks entry with the
      `POLYMARKET_CLIENT_MODE` rollback recipe (set to
      `inprocess` + redeploy → reverts to today's behaviour
      without code changes).
- [ ] `git mv docs/plans/0040-...md docs/plans/completed/`.
- [ ] Update [`plan-control-index.md`](../plan-control-index.md).
- [ ] Mark completed
