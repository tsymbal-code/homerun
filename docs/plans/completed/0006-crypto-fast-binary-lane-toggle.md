# Plan: Crypto fast-binary lane toggle in Settings

> **Plan policy.** This plan follows
> [`../README.md`](../README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`../completed/`](../completed/) on close. Every commit produced
> by this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](../README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`../plan-control-index.md`](../plan-control-index.md).

## Status

**COMPLETED** — 2026-05-07. All 8 tasks closed; plan moved to
`completed/`.

## Outcome

- Crypto fast-binary lane is now operator-toggleable from
  `Settings → Scanner` and via
  `POST /api/workers/crypto/{pause|start}`.
- When the lane is off (`is_enabled = false` OR
  `is_paused = true`):
  - `MarketRuntime.start()` skips the unconditional startup
    refresh and leaves the lane caches empty.
  - `_drain_reactive_updates` short-circuits before the
    Binance-tick payload rebuild, clears `_pending_tokens` /
    `_pending_assets`, and avoids any `_queue_opportunity_dispatch`
    work.
  - `_run_loop_iteration` clears `_crypto_markets` plus its
    three lookup dicts on the active→off transition, and
    triggers a one-shot `_refresh_crypto_markets(...,
    trigger="lane_re_enabled")` on the off→active transition.
- Re-profile of `worker-trading` with the lane disabled
  (60 s py-spy, `--rate 100`) shows `get_oracle_history` +
  `_oracle_move_from_history` and `copy.deepcopy` all collapsed
  to well below 5 %. **All hotspots targeted by plan 0004 were
  eliminated as a side-effect**, so plan 0004 was moved back to
  `backlog/` (second archival).

## Notable deviations from the plan as written

- **Pause semantics.** The plan was written as if `pause` flipped
  `is_enabled`, but `POST /api/workers/crypto/pause` actually
  flips `is_paused`. The implementation collapses both flags
  into a single
  [`_crypto_lane_is_active(control)`](../../../backend/services/market_runtime.py)
  helper (`is_enabled and not is_paused`) so both knobs map to
  "lane off" identically.
- **Test paths.** The backend test tree is flat, not under
  `services/` and `api/`, so the new tests live at
  `backend/tests/test_market_runtime_crypto_lane_toggle.py` and
  `backend/tests/test_routes_settings_scanner_crypto_lane.py`.
- **Verification command.** `/api/crypto-markets` does not
  exist; the lane state is inspected via
  `GET /api/workers/status` (`stats.market_count` and
  `current_activity` for `worker_name == "crypto"`).
- **Profile artefact.** Flamegraph saved to
  [`architecture/worker-trading-profile-2026-05-07-crypto-lane-off.svg`](../architecture/worker-trading-profile-2026-05-07-crypto-lane-off.svg).
  Numerical summary folded into
  [`architecture/worker-trading.md`](../architecture/worker-trading.md)
  ("After plan 0006").

## Overview

Plan 0005 added a tag-based whitelist filter at the Polymarket
ingest layer
([`scanner._is_market_tradable`](../../../backend/services/scanner.py)).
The post-filter profile revealed that the filter only gates one
of the two market ingest lanes: the Polymarket general scanner.
The **crypto fast-binary lane**
([`market_runtime._refresh_crypto_markets`](../../../backend/services/market_runtime.py))
runs a parallel pipeline that fetches crypto-binary markets
directly from Gamma via
[`crypto_service.get_live_markets()`](../../../backend/services/crypto_service.py)
— it never consults `market_catalog`, so the tag filter cannot
reach it. As a consequence, even when the operator filters to
`['sports']`, the crypto lane keeps firing
`get_oracle_history()` per market on every Binance tick (~36 %
of CPU-active samples in the 2026-05-07 post-filter profile).

`CryptoMarket` does not carry a `tags` field
([`crypto_service.py:125-160`](../../../backend/services/crypto_service.py)),
so applying the tag whitelist to this lane is semantically
wrong. Instead, this plan adds an **operator-managed on/off
toggle** for the crypto lane as a whole, surfaced in
`Settings → Scanner`.

The infrastructure mostly already exists. There is already a
`worker_control(name="crypto")` row read at
[`market_runtime.py:983-993`](../../../backend/services/market_runtime.py)
(`_read_crypto_control`) and respected by the periodic refresh
at [`market_runtime.py:968`](../../../backend/services/market_runtime.py).
There is already a generic API
(`POST /api/workers/crypto/{pause|start}`,
[`backend/api/routes_workers.py:69`](../../../backend/api/routes_workers.py)).
**Two gaps** prevent the existing toggle from fully silencing
the lane:

1. The startup refresh at
   [`market_runtime.py:581`](../../../backend/services/market_runtime.py)
   runs unconditionally — it populates `_crypto_markets` once
   on boot regardless of `is_enabled`.
2. The reactive Binance-tick path
   [`_drain_reactive_updates`](../../../backend/services/market_runtime.py)
   at line 1642 reads from the populated `_crypto_markets` cache
   and triggers `_rebuild_crypto_rows_from_cache` on every tick
   without checking the control. This is what keeps
   `get_oracle_history` and `_oracle_move_from_history`
   firing post-filter.

Done = with `worker_control(crypto).is_enabled = false`:
- The lane silences fully (cache cleared, no Binance-tick-driven
  payload rebuilds, no `crypto_markets_update` WS broadcasts).
- A re-profile of `worker-trading` under steady-state load
  shows `get_oracle_history` and `_oracle_move_from_history`
  combined dropping below 5 % (down from ~43 % currently).
- The toggle is surfaced in `Settings → Scanner` (a new
  "Crypto fast-binary lane" sub-section), defaults to **on**
  (backward-compatible).
- The existing `POST /api/workers/crypto/{pause|start}` API
  remains untouched; the Settings UI just wraps it.

## Out of scope

- **Filter on `CryptoMarket.tags`.** No such field exists.
- **Granular per-asset toggle (BTC vs ETH vs SOL vs XRP).** All
  or nothing for now. If demand emerges, add later as a
  per-asset extension.
- **Disabling the underlying `binance_feed` WS connection.**
  The feed keeps running (it's cheap, 4 connections, JSON parse
  ~1–5 µs per tick); we just skip the per-market payload
  rebuild that the ticks would otherwise drive. Tearing down
  the WS introduces reconnect/backoff complexity that's not
  worth it.
- **Changing the Polymarket general scanner behaviour.** That
  was plan 0005's territory. This plan touches only the crypto
  lane.
- **Plan 0004's CPU optimisations** (deepcopy halving,
  `get_oracle_history` TTL cache, orjson). Those remain
  separately useful even with the lane on. Not blocked by or
  blocking this plan.

## Context / References

- [Architecture: worker-trading process model + CPU profile](../architecture/worker-trading.md)
  — the 2026-05-07 profile that revealed the gap.
- [Architecture: market filter pipeline](../architecture/market-filter.md)
  — explains why the tag filter can't reach the crypto lane.
- [Plan 0005 — Tag-based market filter at ingest](../completed/0005-tag-based-market-filter-at-ingest.md)
  — sibling plan; this one fills its blind spot.
- [`market_runtime.py:581`](../../../backend/services/market_runtime.py)
  — startup refresh (gap 1).
- [`market_runtime.py:961-993`](../../../backend/services/market_runtime.py)
  — `_run_loop_iteration` + `_read_crypto_control` (existing
  control, partial coverage).
- [`market_runtime.py:1312-1340`](../../../backend/services/market_runtime.py)
  — `_refresh_crypto_markets` writes the cache.
- [`market_runtime.py:1642-1717`](../../../backend/services/market_runtime.py)
  — `_drain_reactive_updates` (gap 2).
- [`backend/api/routes_workers.py:42-69, 437-492`](../../../backend/api/routes_workers.py)
  — existing generic worker control API.
- [`frontend/src/components/SettingsPanel.tsx`](../../../frontend/src/components/SettingsPanel.tsx)
  — Scanner tab, where the new sub-section attaches.

## Validation Commands

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest -q backend/tests/services/test_market_runtime_crypto_lane_toggle.py'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend ruff check backend/services/market_runtime.py backend/api/routes_workers.py'`
- `cd frontend && npm run typecheck`
- `ssh polyhome-1 "curl -fsS -X POST http://127.0.0.1:8888/api/workers/crypto/pause | jq .is_enabled"` — expect `false`
- `ssh polyhome-1 "curl -fsS http://127.0.0.1:8888/api/workers/crypto/status 2>/dev/null || curl -fsS http://127.0.0.1:8888/api/workers/status | jq '.[\"crypto\"]'"`

### Task 1: Plug gap 1 — startup refresh respects `worker_control`

- [x] Edit
  [`market_runtime.py:574-583`](../../../backend/services/market_runtime.py)
  `start()`: before calling `_refresh_crypto_markets`, read
  `_read_crypto_control()` and skip the unconditional startup
  refresh when `is_enabled is False`. Initialise the lane caches
  (`_crypto_markets = []`, `_crypto_markets_by_lookup = {}`,
  `_crypto_token_to_market_ids = {}`,
  `_crypto_asset_to_market_ids = {}`) so downstream readers see
  the empty state. **Note (deviation):** the actual condition
  collapses both `is_enabled = false` and `is_paused = true`
  into one "lane active" check (`_crypto_lane_is_active(control)`).
  The plan was written assuming `pause` flipped `is_enabled`,
  but `POST /api/workers/crypto/pause` actually only flips
  `is_paused` (see `set_worker_paused` in `worker_state.py`).
- [x] Log a single INFO line on startup if the lane is disabled
  (`"Crypto fast-binary lane disabled by worker_control; skipping
  startup refresh"`) so the operational journal can correlate
  empty WS broadcasts with the toggle.
- [x] Mark completed

### Task 2: Plug gap 2 — `_drain_reactive_updates` respects control

- [x] Edit
  [`market_runtime.py:1642-1717`](../../../backend/services/market_runtime.py).
  At the top of the function, after the debounce sleep and
  before reading `self._pending_tokens`, read the control via
  `await self._read_crypto_control_cached()`. If the lane is
  not active (`is_enabled = false` OR `is_paused = true`):
  - Clear `self._pending_tokens` and `self._pending_assets`
    (so they don't accumulate forever).
  - Return early — no payload rebuild, no
    `_queue_opportunity_dispatch` call.
- [x] To avoid hitting the DB on every Binance tick, the new
  `_read_crypto_control_cached(ttl_seconds=5.0)` method caches
  the response on the `MarketRuntime` instance for the TTL
  window. Backed by the same `_crypto_control_cache` slot the
  uncached path also writes through, so the periodic
  `_run_loop_iteration` read keeps the cache warm.
- [x] Mark completed

### Task 3: Cache invalidation on transition to disabled

- [x] In `_run_loop_iteration`
  ([`market_runtime.py:961-981`](../../../backend/services/market_runtime.py)),
  detect the transition `active → off` and explicitly clear
  `_crypto_markets` + the three lookup dicts. Tracked via
  `self._crypto_lane_was_enabled = bool(...)` (set during
  `start()` and updated each iteration). "Active" combines
  `is_enabled and not is_paused`.
- [x] Symmetric: detect the transition `off → active` and
  trigger a one-shot `_refresh_crypto_markets(...,
  trigger="lane_re_enabled")` on the same iteration via the
  `_crypto_lane_pending_refresh` flag, so the cache repopulates
  without waiting an extra `interval_seconds`.
- [x] Mark completed

### Task 4: Surface the toggle in `Settings → Scanner`

- [x] In
  [`backend/api/routes_settings.py`](../../../backend/api/routes_settings.py),
  inside `GET /settings/scanner`, add a derived field
  `crypto_lane_enabled: bool` populated by
  `_read_crypto_lane_enabled()`, which collapses
  `is_enabled and not is_paused` from
  `worker_control(crypto)`. Read-only on this endpoint — the
  writes go through the existing
  `POST /workers/crypto/{pause|start}` API (no duplicate write
  path).
- [x] In
  [`frontend/src/components/SettingsPanel.tsx`](../../../frontend/src/components/SettingsPanel.tsx),
  inside the Scanner tab, added the "Crypto fast-binary lane"
  sub-section between "Pool Caps" and "Market Tag Filter".
  Reads via the existing settings query; writes go through
  `startWorker('crypto')` / `pauseWorker('crypto')` and
  invalidate the `settings` query.
- [x] `npm run typecheck` clean (verified during deploy build).
- [x] Mark completed

### Task 5: Tests

- [x] `backend/tests/test_market_runtime_crypto_lane_toggle.py`
  (path adjusted: backend test tree is flat, not under
  `services/`):
  - `start()` skips refresh when `is_enabled = False` or
    `is_paused = True`; runs when active.
  - `_drain_reactive_updates` returns early and clears
    pending tokens/assets when the lane is off (both via
    `is_enabled = False` and `is_paused = True`).
  - Transition `active → off` clears the cache (covered for
    both flag flavours).
  - Transition `off → active` triggers a refresh on the same
    iteration via `_crypto_lane_pending_refresh`.
  - `_read_crypto_control_cached` honours the TTL.
- [x] `backend/tests/test_routes_settings_scanner_crypto_lane.py`
  (path adjusted, see above):
  - `GET /settings/scanner` exposes `crypto_lane_enabled`
    consistent with the `worker_control` row, including the
    paused case and the DB-error fallback (defaults to True).
- [x] No frontend tests are conventional in this repo (per
  [`docs/plans/architecture/testing.md`](../architecture/testing.md));
  smoke via the operator's browser is the gate.
- [x] Mark completed

### Task 6: Deploy + verify

- [x] `./deploy/sync_remote.sh` from local checkout.
- [x] After redeploy, confirmed all containers Up healthy and
  no new tracebacks in `worker-trading` logs (the standard
  `Missing Polymarket API credentials` errors remained
  pre-existing).
- [x] Smoke A — toggled off via the new UI (and equivalently
  via `POST /api/workers/crypto/pause`), observed within one
  Binance-tick cycle:
  - `/api/crypto-markets` is not exposed (404); used
    `curl -fsS http://127.0.0.1:8888/api/workers/status |
    jq '[.workers[] | select(.worker_name=="crypto") |
    {market_count: .stats.market_count, current_activity}]'`
    instead. After pause: `market_count: 0,
    current_activity: "Paused"`.
- [x] Smoke B — toggled back on; after one
  `interval_seconds` plus DB read jitter, market count
  repopulated to 15 and activity flipped to `"Live"`.
- [x] Mark completed

### Task 7: Re-profile worker-trading + decide on plan 0004

- [x] With the lane disabled and one fast trader still running
  (Sandbox - Traders Copy Trade), applied the temporary
  `cap_add: [SYS_PTRACE]` to `worker-trading` in
  `docker-compose.yml`.
- [x] Captured a 60 s py-spy profile (`--rate 100`). Saved as
  [`docs/plans/architecture/worker-trading-profile-2026-05-07-crypto-lane-off.svg`](../architecture/worker-trading-profile-2026-05-07-crypto-lane-off.svg).
- [x] Compared top-10 self-time table to the post-filter
  baseline (2026-05-07): `get_oracle_history` +
  `_oracle_move_from_history` combined collapsed from ~42 %
  to well below 5 %; `copy.deepcopy` chain also dropped well
  below 5 %. **All targeted hotspots from plan 0004
  effectively eliminated.**
- [x] Appended an "After plan 0006" subsection to
  [`architecture/worker-trading.md`](../architecture/worker-trading.md)
  "Measured CPU profile" with the new numbers and the
  decision to re-archive plan 0004.
- [x] **Decision**: plan 0004's targeted hotspots are gone, so
  the plan was moved back to `backlog/` (second archival).
  See its updated overview for the rationale.
- [x] Reverted the `cap_add`. Confirmed bootstrap loop healthy.
- [x] Mark completed

### Task 8: Update related architecture notes + close

- [x] In
  [`architecture/worker-trading.md`](../architecture/worker-trading.md)
  appended "After plan 0006" under "Measured CPU profile" and
  updated the "Recommendation" section to reflect that
  plan 0006's lane toggle eliminated plan 0004's targeted
  hotspots, so plan 0004 is back in `backlog/`.
- [x] In
  [`architecture/market-filter.md`](../architecture/market-filter.md)
  added a "Sibling toggles" section explaining how the
  tag-whitelist (catalog scope) and the crypto-lane on/off
  (parallel-lane scope) compose.
- [x] In
  [`docs/operational/runtime-tweaks.md`](../../operational/runtime-tweaks.md),
  appended a `2026-05-07 ~17:00 UTC — Plan 0006: crypto
  fast-binary lane toggle live` entry recording the deploy,
  verification, re-profile results, and the chosen lane
  state at handover.
- [x] `git mv docs/plans/0006-crypto-fast-binary-lane-toggle.md
  docs/plans/completed/` (executed at the close of this task).
- [x] Updated
  [`plan-control-index.md`](../plan-control-index.md): plan 0006
  row now links to `completed/`; plan 0004 row reflects the
  re-archival to `backlog/` (second archival), with the
  per-plan note updated to explain the rationale.
- [x] Mark completed
