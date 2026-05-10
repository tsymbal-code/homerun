# Architecture: Execution and fills

This is the layer that turns a `decision='selected'` row in
`trader_decisions` into a real (or simulated) fill. It owns two
parallel paths — **shadow** (Cox-PH fill simulator) and **live**
(Polymarket CLOB + Kalshi REST) — plus the supporting workers that
keep them honest: reconciliation, redeemer, position-mark, and the
provider-health gate.

The pipeline that **leads to** this layer is documented in
[`trader-pipeline.md`](trader-pipeline.md) (stages 1–5). What this
note covers is stages 5–7: how a decision becomes an order, how
fills are decided, and how books reconcile to venue truth.

## Purpose

This layer is responsible for:

1. Materialising `decision='selected'` into an `execution_session`
   plus one or more `execution_session_legs`.
2. Deciding fill outcomes:
   - **Shadow:** the Cox-PH model returns `executable: true|false`
     and (if true) a fill price within slippage budget.
   - **Live:** signing and submitting orders to Polymarket CLOB
     (via py-clob-client) and Kalshi REST.
3. Persisting results: the **orchestrator-driven shadow path**
   (the one used by every running bot today) writes to
   `trader_orders` (`mode='shadow'`) + `trader_positions` +
   `execution_sessions`. The legacy
   `simulation_trades` / `simulation_positions` /
   `simulation_accounts` triple is owned by the standalone
   replay simulator
   (`services/simulation/execution_simulator.py`) and is not
   exercised by the live orchestrator (zero `simulation_trades`
   rows in production at the time of writing).
4. Reconciling local books with venue truth (`trader_reconciliation_worker`).
5. Redeeming winnings on resolved markets (`redeemer_worker`).
6. Marking unrealised PnL on open positions
   (`_sync_position_marks_and_exit_registry`, every 10 s).
7. Gating live submission on provider health (`provider_health_block`).

It does **not**:

- Decide *whether* to trade. That's the strategy + risk-manager
  pipeline upstream.
- Run on `worker-news` (except for one specific exception:
  `cox_trainer_worker` lives there because it needs pandas/scipy
  for training; **inference** is hot on `worker-trading`).

## Key files

### Shadow path

The orchestrator-driven shadow path (current, used by every
running bot) and the legacy standalone-simulator shadow path live
side-by-side in the codebase. Operators meet the first one; the
second exists for replay/historical work.

#### Orchestrator-driven shadow (current)

| Path | What it holds |
|---|---|
| [`backend/services/trader_orchestrator/session_engine.py`](../../../backend/services/trader_orchestrator/session_engine.py) | `ExecutionSessionEngine.execute_signal()` orchestrates one signal end-to-end. `_persist_execution_projection()` is the single writer of the shadow ledger: it flushes `trader_orders` / `trader_positions` / `execution_sessions` rows and **must** call `await self.db.commit()` before returning (commit added in `936f96a4`; the regression test pins this invariant). On commit failure: rollback + ERROR `execution_session persist commit failed`; on success: INFO `execution_session persisted`. |
| [`backend/services/trader_orchestrator/order_manager.py`](../../../backend/services/trader_orchestrator/order_manager.py) | `submit_execution_leg()` — the per-leg submit pipeline; consumes Cox-PH inference for executability and slippage and writes the `trader_orders` row. |
| [`backend/services/fill_simulator/cox_inference.py`](../../../backend/services/fill_simulator/cox_inference.py) | `evaluate()` (lines 71–100). Returns `P(fill within horizon | covariates)`. Hot-path; no lifelines/pandas. Key covariates: `queue_ahead_shares`, `spread_bps`, `mid_distance_bps`, `recent_trade_intensity_per_sec`, `ttr_bucket_*` |
| [`backend/services/fill_simulator/cox_trainer.py`](../../../backend/services/fill_simulator/cox_trainer.py) | training pipeline; runs in `worker-news` plane via `cox_trainer_worker` |
| [`backend/services/fill_simulator/empirical_constants.py`](../../../backend/services/fill_simulator/empirical_constants.py), [`survival_features.py`](../../../backend/services/fill_simulator/survival_features.py), [`ensemble.py`](../../../backend/services/fill_simulator/ensemble.py), [`latency.py`](../../../backend/services/fill_simulator/latency.py) | covariate engineering and ensembling |

Tables written by this path:

- `trader_orders` (`mode='shadow'`) — per-leg row
- `trader_positions` — open/closed position rows
- `execution_sessions` — per-decision state machine
  (`pending` → `running` → `completed`/`failed`)
- `execution_session_legs` — per-leg detail under the session

The Cox-PH model artefact lives in the `fill_probability_models`
table; the row with `active=True` is the current one. The trainer
bumps a `generation` counter on promotion.

#### Legacy standalone simulator (replay-only)

| Path | What it holds |
|---|---|
| [`backend/services/simulation/execution_simulator.py`](../../../backend/services/simulation/execution_simulator.py) | `ExecutionSimulator` (line 23). `run()` (line 251+) iterates selected legs, calls a private `submit_execution_leg()` from the legacy module, mutates `simulation_account.current_capital`. Writes `simulation_trades` / `simulation_positions` and decrements `simulation_accounts.current_capital`. **Not invoked by the orchestrator-driven path.** |

This module exists for historical replay and standalone simulation
runs. In live operation, every shadow trade goes through the
orchestrator path above; `simulation_trades` is empty in the
current production deployment. Treat it as a candidate for
future retirement (no code change in this plan).

#### Shadow ledger backfill (orchestrator → `SimulationService`)

The orchestrator-driven path also runs a back-channel **shadow
ledger backfill** that mirrors every successfully simulated
shadow leg into the legacy `simulation_*` tables for replay /
analytics consumers.  Entry point:

| Path | What it holds |
|---|---|
| [`backend/services/simulation.py`](../../../backend/services/simulation.py) | `SimulationService.record_orchestrator_shadow_fill()` writes one `simulation_trade` + one `simulation_position` for a shadow fill that already landed in `trader_orders` / `trader_positions`. Called from `trader_orchestrator_worker._shadow_ledger_backfill_step()` per cycle. Failures emit a `shadow_ledger_backfill_failed` `trader_event`. |
| [`backend/services/simulation.py`](../../../backend/services/simulation.py) `_direction_to_position_side(direction, payload=None)` | Maps a leg `direction` to a binary `(PositionSide, outcome_label)`. Canonical fast path: `buy_yes`/`sell_yes` → YES, `buy_no`/`sell_no` → NO. Defensive widening (Plan 0018): when `direction` is the bare `buy`/`sell` (emitted by `traders_copy_trade` for non-canonical outcome labels and by any pre-fix in-flight orders), the helper resolves via `payload['token_id']` against `payload['market']['token_ids']` (or `clob_token_ids`), index 0 → YES, index 1 → NO. Truly multi-outcome single-market structures (>2 tokens) and unknown `token_id` still raise `ValueError` so the backfill emits a `shadow_ledger_backfill_failed` event instead of silently misclassifying the leg. |

Severity escalation (Plan 0018):
`trader_orchestrator_worker._resolve_shadow_ledger_backfill_severity()`
counts `shadow_ledger_backfill_failed` events for the same
`trader_id` over a rolling 1 h window
(`SHADOW_LEDGER_BACKFILL_FAILED_ESCALATION_WINDOW_SECONDS=3600`);
once the count meets
`SHADOW_LEDGER_BACKFILL_FAILED_ESCALATION_THRESHOLD=50` the
emit flips from `severity='warn'` to `severity='error'` and
adds `escalated_from='warn'` plus `event_count_window_h=1` to
the payload, so the UI events strip can group it distinctly
and operators are paged on a recurring backfill regression
rather than the warn drowning in noise.

### Live path

| Path | What it holds |
|---|---|
| [`backend/services/live_execution_service.py`](../../../backend/services/live_execution_service.py) | `execute_live_order()` (lines 60–81). py-clob-client v2 imports + EIP-712 signing flow. `OrderArgs.signature_type` 0/1/2 for the three Polymarket schemes |
| [`backend/services/live_execution_adapter.py`](../../../backend/services/live_execution_adapter.py) | venue-agnostic wrapper around live execute. Holds the `idempotency_key` discipline |
| [`backend/services/kalshi_client.py`](../../../backend/services/kalshi_client.py) | Kalshi REST client (`place_order`, `cancel_order`, `get_order`). Member-token auth. Synchronous calls (no WS submit on Kalshi) |
| [`backend/workers/trader_reconciliation_worker.py`](../../../backend/workers/trader_reconciliation_worker.py) | `reconcile_live_provider_orders()` + position-inventory sync; default interval 1 s. Wallet-positions refresh every 30 s, stale-open-orders sweep every 60 s |
| [`backend/workers/redeemer_worker.py`](../../../backend/workers/redeemer_worker.py) | claims winnings on resolved markets every 120 s; dry-run + real timeouts both 240 s |

### Provider health

| Path | What it holds |
|---|---|
| [`backend/workers/trader_orchestrator_worker.py`](../../../backend/workers/trader_orchestrator_worker.py) | `_live_provider_health_event_due()` (line 3375), `provider_health_snapshot` (line 4865), `_live_provider_entry_blocked_until` dict (line 4861). Threshold: ≥ `provider_health_min_errors` errors in `window_seconds` ⇒ block for `block_seconds` |

### Position mark loop

| Path | What it holds |
|---|---|
| [`backend/workers/trader_reconciliation_worker.py`](../../../backend/workers/trader_reconciliation_worker.py) | `_sync_position_marks_and_exit_registry()` (line 1194). `_POSITION_MARK_SYNC_INTERVAL_SECONDS=10.0` (line 59). Updates `unrealized_pnl` from `PriceCache` |
| [`backend/services/position_mark_state.py`](../../../backend/services/position_mark_state.py) | `PositionMarkState` — the per-position record updated by the loop |

## Contracts

### Execution session state machine

```
trader_decisions.id (decision='selected')
  ↓
execution_sessions row (status='pending')
  ↓ first leg starts
status='running'
  ↓ all legs done
status='completed' | 'failed'
  ↓
completed_at set
```

`execution_sessions` ([`database.py:4179-4231`](../../../backend/models/database.py))
fields: `id, trader_id, signal_id, decision_id, status, mode,
legs_total, legs_completed, legs_failed, legs_open,
requested_notional_usd, executed_notional_usd,
unhedged_notional_usd`.

`execution_session_legs` (line 4234): `session_id, leg_index,
market_id, token_id, side, outcome, price_policy
(maker_limit | aggressive_limit | market), time_in_force
(GTC | IOC | FOK), target_price, requested_notional_usd,
requested_shares, filled_notional_usd, filled_shares,
avg_fill_price, status`.

### Order / position rows

| Table | Mode | Notes |
|---|---|---|
| `trader_orders` ([`database.py:~4090`](../../../backend/models/database.py)) | live | `provider_order_id`, `provider_clob_order_id`, `verification_status` (`local`/`verified`), `actual_profit` populated by the verifier child table |
| `trader_positions` (line 4345) | live | unique on `(trader_id, mode, market_id, direction)` |
| `simulation_trades` (line 662) | shadow | `account_id, opportunity_id, positions_data (JSON), total_cost, expected_profit, slippage, status, actual_payout, actual_pnl, fees_paid` |
| `simulation_positions` (line 621) | shadow | `account_id, market_id, token_id, side, quantity, entry_price, entry_cost, current_price, unrealized_pnl` |
| `simulation_accounts` (line 595) | shadow | `current_capital` is mutated by `ExecutionSimulator.run()`; `slippage_bps`, `max_position_size_pct`, `max_open_positions` are the per-account knobs |
| `fill_probability_models` (Cox artefact) | both | one row with `active=True`; `generation` increments on promotion |

### `AppSettings` and runtime knobs

```
MAX_SLIPPAGE_PERCENT          (config.py:371; default 2.0; global ceiling)
max_slippage_percent          (runtime override; line 944)
POLYMARKET_CLIENT_IO_CONCURRENCY (default 8; line 525 in live_execution_service)
provider_health_min_errors    (runtime; trader_orchestrator_worker.py:4841)
provider_health_window_seconds (runtime)
provider_health_block_seconds  (runtime)
HOMERUN_COX_TRAIN_INTERVAL_SECONDS (env; default 6 h)
```

The full live-execution + Cox-PH knob list is in the runtime
section of `trader_orchestrator_worker.py` (lines 4740–4859).

## Dependencies (both directions)

**This layer depends on:**

- The trader pipeline (stages 1–5) producing `decision='selected'`
  rows — see [`trader-pipeline.md`](trader-pipeline.md).
- The active Cox-PH artefact in `fill_probability_models`. If
  none active, shadow mode degrades to a conservative heuristic
  (the inference module documents the fallback).
- `PriceCache` and `WalletStateCache` from
  [`websocket-and-events.md`](websocket-and-events.md). Without
  fresh prices, position-mark and execution-feasibility decisions
  go stale.
- Polymarket CLOB + Polygon RPC for live submission; Kalshi REST
  for Kalshi submission.

**Depended on by:**

- `simulation_accounts.current_capital` — the source of truth for
  shadow-mode "did I just lose money."
- The Performance / Positions / Sandbox tabs.
- The reconciliation telemetry (mismatches surface as
  `verification_status='local'` outliers).
- The Telegram notifier (when configured) for fills + closures.

## Extension points

| When you want to… | Touch |
|---|---|
| Add a new venue (live) | New client under `services/<venue>_client.py`, hook into `live_execution_adapter.execute_live_order` via venue dispatch, add columns on `trader_orders` if the venue's order shape differs |
| Tune the Cox-PH model | `cox_trainer.train_and_persist(window_days=N)` in a one-off job; promote via the trainer's normal flow |
| Change the slippage envelope | Adjust `simulation_accounts.slippage_bps` per account, or `max_slippage_percent` runtime setting for global |
| Add a price policy | New value on `execution_session_legs.price_policy` + handler in `live_execution_adapter` and `execution_simulator` |
| Tighten the provider-health gate | `provider_health_min_errors`, `_window_seconds`, `_block_seconds` runtime settings |

## Known footguns

- **`limit_price_not_executable`** is the single most common
  "selected but no fill" reason in shadow mode. The Cox-PH model
  judged the limit unreachable given current depth + spread. The
  fix is upstream (looser `slippage_bps`, `max_spread_bps`, or
  `price_policy: market`), not in this layer.
- **Polymarket signing pitfalls.** EIP-712 domain separation is
  strict; `signature_type` 0/1/2 must match what Polymarket
  expects for the order maker. A mismatched signature returns a
  cryptic 4xx; check `live_execution_service.py:971` and the
  signing thread comment near line 313.
- **Polygon RPC instability.** `redeemer_worker._verify_boot_invariants`
  hard-stops if RPC is unhealthy, with a 900 s retry cooldown
  ([`redeemer_worker.py:59-61`](../../../backend/workers/redeemer_worker.py)).
  Symptoms: redemption queue grows, no errors in logs except
  the boot-invariant line.
- **Reconciliation race vs WS user channel.** `WalletStateCache`
  may receive a fill via WS before the orchestrator has written
  its `trader_orders` row — reconcile sees an "extra" venue order
  and tries to back-fill. The worker's verify pass handles this,
  but transient mismatches in `verification_status='local'` are
  expected.
- **`selected_account_id` not set on a shadow trader.** Decisions
  reach `selected` and execution sessions exist, but
  `simulation_trades` never appear because there's no ledger to
  write to. Symptom looks like a fill failure; root cause is
  configuration. The bot field overrides the orchestrator
  default; check both.
- **Cox-PH model staleness.** If `cox_trainer_worker` has been
  off for weeks (operator paused `worker-news`), the active model
  predicts on increasingly out-of-distribution covariates. There
  is no auto-detection; check `fill_probability_models.created_at`
  on the active row.
- **Concurrency cap on Polymarket.** `POLYMARKET_CLIENT_IO_CONCURRENCY=8`
  bounds in-flight CLOB calls. Higher values trip rate limits;
  lower values cap throughput on heavy days.
- **Missing commit in `_persist_execution_projection`.** Symptom:
  `selected` decisions exist, the
  `trader_open_positions` blocker fires on > 80 % of cycles,
  `trader_orders` and `execution_sessions` tables stay empty
  despite the orchestrator log line claiming a successful
  submission. Root cause: the persister flushed rows but did
  not commit; the async session close then rolled them back.
  Diagnosed and fixed in commit `936f96a4` (full diagnosis +
  rollback recipe in
  [`docs/operational/runtime-tweaks.md`](../../operational/runtime-tweaks.md)
  `2026-05-09 ~07:30 UTC` entry). The regression test
  `test_execute_signal_shadow_persists_with_commit_so_async_session_close_does_not_rollback`
  pins the invariant; do not refactor the persister without
  re-running it. INFO `execution_session persisted` and ERROR
  `execution_session persist commit failed` are the
  permanent fingerprints to grep for.

## Test coverage

- `backend/tests/test_execution_simulator.py`
- `backend/tests/test_live_execution_adapter.py`
- `backend/tests/test_execution_session_engine.py`
- `backend/tests/test_trader_reconciliation_risk_params.py`
- `backend/tests/test_simulation_service.py`
- `backend/tests/test_fill_monitor.py`
- `backend/tests/test_execution_latency_metrics.py`
- `backend/tests/test_ctf_execution.py`
- `backend/tests/test_execution_plan_contracts.py`

## Observability

Two dedicated modules instrument the entire submit path with
microsecond-level timing and persist the samples for offline
analysis:

| File | Owns |
|---|---|
| [`backend/services/execution_latency_metrics.py`](../../../backend/services/execution_latency_metrics.py) (193) | the metric definitions and aggregation; `PipelineLatencyLog` model writer |
| [`backend/services/latency_tracker.py`](../../../backend/services/latency_tracker.py) (243) | the per-stage `LatencyTracker` that samples and persists to the `latency_tracker` table |

Stages instrumented (decision → fill):

| Stage | Typical | Diagnostic threshold |
|---|---|---|
| signal → decision | < 100 ms | > 300 ms = strategy-side latency |
| decision → submit | < 200 ms | > 1 000 ms = orchestrator stall (`ps_decision_writes` in [`trader-pipeline.md`](trader-pipeline.md)) |
| submit → fill (live) | venue-bound | > 5 s = CLOB pressure or token circuit-breaker territory |
| submit → fill (shadow Cox-PH) | < 50 ms | > 500 ms = retraining lag or model artefact issue |

The `Trader cycle slow` WARN log
(`trader_orchestrator_worker.py:7944`) emits a per-stage breakdown
when any cycle exceeds half the timeout — that log is the
operator-facing surface of this instrumentation. Aggregate
queries:

```sql
-- p95 submit latency over the last hour
select stage, percentile_cont(0.95) within group (order by duration_ms)
  from latency_tracker
  where sampled_at > now() - interval '1 hour'
  group by stage;
```

Cox-PH retraining cadence (`HOMERUN_COX_TRAIN_INTERVAL_SECONDS`)
is the natural denominator for the fill-side stage; if the
trainer has been off, `submit → fill (shadow)` will drift up
without a code change.

## Related defence-layer modules

The submission path is wrapped by a stack of defensive gates and
post-submit monitors. They sit alongside this note's content but
warrant their own reference:

- **Pre-submit gates:** `market_tradability`, `live_market_detector`,
  `execution_safety`, `execution_tiers`, `buy_pre_submit_gate`.
- **Submit retries:** `price_chaser`.
- **Post-submit, per-token:** `token_circuit_breaker`,
  `live_pressure`.
- **Background:** `position_monitor` (shadow TP/SL),
  `stuck_position_monitor` (live blocked-exit alerts).

Full coverage in [`execution-defense.md`](execution-defense.md).
The provider-health gate documented above is the only one that
lives natively in this note.

## Where to look next

| Topic | File |
|---|---|
| Pipeline that produces `decision='selected'` | [`trader-pipeline.md`](trader-pipeline.md) |
| Submission-side defence gates (9-module map) | [`execution-defense.md`](execution-defense.md) |
| Pre-scanner market gating + depth analysis | [`market-quality-and-prioritization.md`](market-quality-and-prioritization.md) |
| `worker-trading` process model that hosts most of this layer | [`worker-trading.md`](worker-trading.md) |
| Where Cox-PH **training** runs | [`worker-news.md`](worker-news.md) |
| `PriceCache` / `WalletStateCache` | [`websocket-and-events.md`](websocket-and-events.md) |
| Sandbox account model (capital, slippage, max position size) | [`settings-and-secrets.md`](settings-and-secrets.md) |

Last verified: 2026-05-10 (Plan 0018: shadow-ledger backfill subsection added with `_direction_to_position_side` widening contract and `_resolve_shadow_ledger_backfill_severity` escalation; real-diff against `simulation.py` and `trader_orchestrator_worker.py` shows symbols match.); previously 2026-05-09 (Shadow-path key-files block rewritten to separate orchestrator-driven shadow from legacy standalone simulator after commit `936f96a4`; missing-commit footgun added; corresponds to plan 0016.)
