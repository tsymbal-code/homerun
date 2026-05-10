# Plan: Per-leader / per-market analytics endpoint and Performance-tab tile

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](../README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](../completed/) on close. Every commit produced by
> this plan carries a `Plan: 0025` git trailer (see
> [Commits and traceability](../README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](../plan-control-index.md).
>
> **Status: BACKLOG.** Activate when the conservative live-risk
> change from the 2026-05-10 runtime-tweaks entry has produced
> enough fresh terminal orders that operator wants a recurring
> visual on per-leader performance (instead of one-off SQL pulls).
> Conservative trigger: ≥ 100 fresh terminal orders OR operator
> explicitly requests visibility for ongoing pruning decisions.

## Overview

The 2026-05-10 audit established that the Sandbox bot's profit
is concentrated in a few leader wallets and a few market topics
(crypto carries; sports/esports prop bets drag). The data lives
in `trader_orders.actual_profit` joined to
`trade_signals.payload_json`, but the platform exposes no
aggregated view. Operator can only get per-leader P&L by hand-
crafting SQL, which makes ongoing pruning decisions friction-heavy.

This plan adds a thin aggregation layer — a single read-only API
endpoint and a small tile in the Performance subtab — that
groups realised P&L by leader wallet (and by market) for any
trader bot. No new scoring, no auto-action. The endpoint is a
SELECT-and-aggregate; the UI tile is sortable and lets the
operator click "Exclude" to flip the
`discovered_wallets.source_flags.pool_manual_exclude` flag
without leaving the dashboard.

### Why now / what done looks like

- Operator can open Sandbox bot → Performance subtab → "By
  leader" panel, sort by P&L, see the worst 10 leaders, and
  exclude the bottom ones via one click.
- Same for "By market" (groups by `market_id` →
  `market_question`) — surface the markets that consistently
  drag.
- The endpoint also returns aggregates by source-tag categories
  if those exist, so the operator can see at a glance "crypto
  +$X, esports −$Y".
- Decisions become data-driven and recurrent instead of one-off
  SQL pulls.
- New regression tests pin the SQL aggregation correctness
  (mocked `trader_orders` + `trade_signals` rows → expected
  per-leader rollup).

### What this plan deliberately does NOT do

- No new scoring or auto-action. Operator-driven only.
- No change to `SmartWalletPoolService` or its recompute.
- No category filter at strategy level (separate plan if needed).
- No real-time live updates — the endpoint is on-demand pull,
  refreshed when the panel mounts. WS push is overkill for this.

## Context / References

- Existing aggregator (returns total per-trader, no per-leader
  breakdown):
  [`backend/services/trader_orchestrator_state.py:10527`](../../../backend/services/trader_orchestrator_state.py:10527)
  (`get_trader_orders_summary`).
- Existing route hook:
  [`backend/api/routes_traders.py:670`](../../../backend/api/routes_traders.py:670)
  (`GET /api/traders/orders/summary`).
- Frontend Performance subtab:
  [`frontend/src/components/TradingPanel.tsx:11807-12520`](../../../frontend/src/components/TradingPanel.tsx:11807).
- Pool exclusion flag plumbing (the click target):
  [`backend/services/smart_wallet_pool.py:87-88`](../../../backend/services/smart_wallet_pool.py:87)
  + the existing `PUT /api/discovery/wallets/{address}` route.
- Schema notes on where the leader wallet is stored:
  `trade_signals.payload_json -> 'strategy_context' -> 'copy_event' ->> 'wallet_address'`.
  Join key: `trader_orders.signal_id = trade_signals.id`.

## Validation Commands

- `bash scripts/run_tests_remote.sh tests/test_trader_orchestrator_state_signals.py`
- `bash scripts/run_tests_remote.sh tests/test_routes_traders_endpoints.py` (or whatever the routes test file is)
- `cd frontend && npm run typecheck`
- `ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/api/traders/61dcbeb2b9bc42bd9e9635a09ae5e0c3/performance/by-leader' | jq .leaders[0:5]'`

## Out of scope

- **Auto-blacklist on rolling P&L.** This plan only adds
  visibility + a manual exclusion button. Auto-action is plan
  0028 if/when warranted.
- **Per-leader analytics for non-copy-trade strategies.** The
  endpoint can be reused later, but the initial UI tile is
  copy-trade-specific because that's where leader-attribution
  makes sense.
- **Historical replay analytics.** The endpoint queries live
  `trader_orders` rows; backfill of old data is not relevant
  because everything is already in the table.

### Task 1: New aggregator function in `trader_orchestrator_state.py`

Add `get_trader_performance_by_leader(session, trader_id, *, since=None) -> list[dict]`
returning `[{leader_wallet, n, wins, losses, win_rate, pnl_total, pnl_avg, last_seen_at}]`,
ordered by `pnl_total` desc.

- [ ] Add the function in
      [`backend/services/trader_orchestrator_state.py`](../../../backend/services/trader_orchestrator_state.py)
      next to `get_trader_orders_summary`. SQL query joins
      `trader_orders` to `trade_signals` on `signal_id`,
      extracts leader from
      `payload_json -> 'strategy_context' -> 'copy_event' ->> 'wallet_address'`,
      filters on terminal statuses
      (`{closed_win, closed_loss, resolved_win, resolved_loss}`),
      groups by leader.
- [ ] Add a sister `get_trader_performance_by_market(session, trader_id, *, since=None)`
      that groups by `(market_id, market_question)` instead.
- [ ] Defensive: if `signal_id` is NULL on some orders (orphans
      seen in the audit), include them in a `null_leader` bucket
      rather than dropping silently.
- [ ] Mark completed

### Task 2: Routes

Two new endpoints under the existing `/api/traders/{trader_id}` namespace.

- [ ] Add `GET /api/traders/{trader_id}/performance/by-leader`
      in
      [`backend/api/routes_traders.py`](../../../backend/api/routes_traders.py)
      that calls `get_trader_performance_by_leader` and returns a
      Pydantic-shaped response. Optional `since=ISO8601` query
      param.
- [ ] Add `GET /api/traders/{trader_id}/performance/by-market`
      same shape.
- [ ] Both endpoints: 404 if trader doesn't exist; 503 on
      retryable DB errors per the project's standard pattern.
- [ ] Mark completed

### Task 3: Regression tests

Add to existing
[`backend/tests/test_trader_orchestrator_state_signals.py`](../../../backend/tests/test_trader_orchestrator_state_signals.py)
(closest sibling) and a routes test file (existing or new).

- [ ] `test_get_trader_performance_by_leader_aggregates_terminal_orders`:
      seed 3 leaders × varying win/loss mix, assert P&L sum and
      win_rate match.
- [ ] `test_get_trader_performance_by_leader_includes_null_leader_bucket`:
      seed 1 order with `signal_id=NULL`, assert it shows up as
      `leader_wallet=null` rather than being dropped.
- [ ] `test_get_trader_performance_by_leader_respects_since_filter`:
      seed orders before/after a cutoff, assert only post-cutoff
      counted.
- [ ] `test_get_trader_performance_by_market_groups_by_market_id`:
      seed 2 markets × 3 orders each, assert correct rollup.
- [ ] HTTP test for `GET /api/traders/{id}/performance/by-leader`:
      happy path + 404 for unknown trader.
- [ ] Mark completed

### Task 4: Frontend tile

Add a "By leader" panel to the Performance subtab. Re-use the
existing react-query / shadcn table primitives.

- [ ] In
      [`frontend/src/services/api.ts`](../../../frontend/src/services/api.ts)
      (or wherever traders API client lives) add
      `getTraderPerformanceByLeader(traderId, options)` and
      `getTraderPerformanceByMarket(traderId, options)`.
- [ ] In
      [`frontend/src/components/TradingPanel.tsx`](../../../frontend/src/components/TradingPanel.tsx)
      Performance subtab: append a sortable table (leader,
      n, wins, losses, win_rate, P&L). Default sort: P&L asc
      (worst at top so operator sees the drag first).
- [ ] Each row: a small "Exclude from pool" button that calls
      `PUT /api/discovery/wallets/{address}` with
      `source_flags.pool_manual_exclude=true` and refetches.
      Confirm with a small dialog ("This wallet will stop being
      copied within 60 s").
- [ ] Mirror the same for "By market" tab.
- [ ] Mark completed

### Task 5: Deploy + smoke + close-out

- [ ] Run `./deploy/sync_remote.sh`. Confirm clean restart.
- [ ] Smoke: open Sandbox bot Performance → By leader. Confirm
      data populates and matches the SQL recipe in plan 0024
      Task 1.
- [ ] Append a dated entry to
      [`docs/operational/runtime-tweaks.md`](../../operational/runtime-tweaks.md)
      noting the new endpoint and tile.
- [ ] Update
      [`docs/plans/architecture/trader-pipeline.md`](../architecture/trader-pipeline.md)
      § Diagnostic playbook with a pointer to the new view.
- [ ] `git mv docs/plans/0025-...md docs/plans/completed/`.
- [ ] Update [`plan-control-index.md`](../plan-control-index.md).
- [ ] Mark completed
