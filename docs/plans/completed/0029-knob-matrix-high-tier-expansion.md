# Plan: Extend knob interaction matrix — HIGH tier + dead-code findings

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Extends the [knob interaction matrix](../strategies/_common-bot-parameters.md#knob-interaction-matrix--critical-tier)
shipped by plan 0025. CRITICAL tier (15 entries) covered
state-flipping knobs. This plan adds the **HIGH tier** —
~30 cross-strategy / orchestrator / source-config / scanner
knobs that change runtime behaviour materially but are not
state-flipping. The same per-knob template is reused (Default,
Direct consumers with file:line + formula, Indirect consumers,
Compound effects).

In addition, the supporting research surfaced **four newly
confirmed dead-code knobs in `TRADER_RISK_DEFAULTS`**, all
UI-exposed but never read by any gate / decision / execution
path:

- `max_daily_spend_usd` — defaults + schema + coerce only.
- `retry_limit` — defaults + schema + coerce only.
- `retry_backoff_ms` — defaults + schema + coerce only.
- `order_ttl_seconds` — defaults + schema + coerce only.

(`circuit_breaker_drawdown_pct` was the fifth, already
documented in plan 0025.)

These join the matrix as a "Dead code in TRADER_RISK_DEFAULTS"
subsection so operators stop assuming they have any effect.
Implementing or removing them is out of scope; the matrix only
documents present reality.

Done = `_common-bot-parameters.md` gains:

1. A new `## Knob interaction matrix — HIGH tier` section with
   30 entries (10 in TRADER_RISK_DEFAULTS, 13 in orchestrator
   global_runtime / global_risk / live_market_context, 8 in
   `traders_copy_trade` strategy_params, 4 in scanner
   app-settings, 2 in live-trading proxy).
2. A subsection `### Dead code in TRADER_RISK_DEFAULTS` listing
   the four newly-confirmed dead knobs alongside the existing
   `circuit_breaker_drawdown_pct` callout.
3. The tier-classification preamble updated to reflect HIGH
   coverage.

This plan is **documentation-only**. No code, no schema, no
runtime change. It does not touch any CRITICAL knob.

## Out of scope

- **MEDIUM-tier per-strategy params** (~300 fields × 29
  strategies). Plan 0030 candidate. Each strategy already has
  a `docs/strategies/<slug>.md` doc with its `default_config`
  section; that's where MEDIUM tier markers belong, not the
  shared matrix.
- **Implementing or removing the 4 newly-confirmed dead
  knobs.** That's a code change. Documented here as an
  observation; cleanup goes in a separate B/R plan if the
  operator wants the UI cleaned up.
- **`live_provider_health.*` confirmation.** The research
  flagged them as suspect (no consumers in
  `live_execution_service.py`); a deeper grep is needed
  before adding them to the matrix. Skipped this round; will
  re-survey under plan 0030.
- **CRITICAL tier additions.** No new state-flipping knobs
  surfaced in the survey — the original 15 still cover the
  set. If a future change introduces one, it goes in plan 0025
  successor, not here.

## Context / References

- [Plan 0025 — Knob interaction matrix](completed/0025-knob-interaction-matrix-critical-tier.md)
- [Plan 0026 — Walkthrough template](completed/0026-runtime-tweaks-walkthrough-template-critical.md)
- [Plan 0027 — Agent memory rule](completed/0027-agent-memory-rule-critical-knob-walkthrough.md)
- [Plan 0028 — Default Ralphex plan rule](completed/0028-ralphex-plan-rule-critical-knob-touch.md)
- [`backend/services/strategy_sdk.py:390-420`](../../backend/services/strategy_sdk.py)
  — `TRADER_RISK_DEFAULTS`, the source of all per-trader
  risk knobs.
- [`backend/services/trader_orchestrator/risk_manager.py`](../../backend/services/trader_orchestrator/risk_manager.py)
  — primary HIGH-tier consumer (cooldown, slippage, spread,
  averaging, daily-loss).
- [`backend/services/trader_orchestrator/decision_gates.py`](../../backend/services/trader_orchestrator/decision_gates.py)
  — entry-drift, market-data-age, portfolio allocator,
  stacking-guard.
- [`backend/services/trader_orchestrator_state.py`](../../backend/services/trader_orchestrator_state.py)
  — orchestrator global_runtime/global_risk normaliser.
- [`backend/services/strategies/traders_copy_trade.py:19-59`](../../backend/services/strategies/traders_copy_trade.py)
  — copy-trade strategy_params source-of-truth.
- [`backend/api/routes_settings.py:181-268`](../../backend/api/routes_settings.py)
  — `ScannerSettingsModel`.

## Validation Commands

- `grep -q '## Knob interaction matrix — HIGH tier' docs/strategies/_common-bot-parameters.md`
- `grep -c '^### HIGH — ' docs/strategies/_common-bot-parameters.md` — at least 30
- `grep -q '### Dead code in TRADER_RISK_DEFAULTS' docs/strategies/_common-bot-parameters.md`
- `grep -c '\.py:' docs/strategies/_common-bot-parameters.md` — ≥ 110 file:line citations after merge (56 from CRITICAL plus ≥ 60 new)
- `grep -q 'max_daily_spend_usd' docs/strategies/_common-bot-parameters.md`
- `grep -q 'retry_limit' docs/strategies/_common-bot-parameters.md`

### Task 1: Confirm the 30 HIGH-tier candidates

The Explore-agent survey returned 30 candidates with
`UI Surface`, default, and consumer file:line. Verify each by
direct grep on the cited file:line; remove from list if the
consumer doesn't actually read the field there.

- [x] Group A (TRADER_RISK_DEFAULTS, ~10 confirmed-alive):
  `max_orders_per_cycle`, `position_cap_scope`,
  `cooldown_seconds`, `slippage_bps`, `max_spread_bps`,
  `allow_averaging`, `use_dynamic_sizing`,
  `max_entry_drift_pct`, `max_market_data_age_ms`,
  `portfolio.*`. Already verified during 0025 prep for the
  ones that overlap.
- [x] Group B (orchestrator global_runtime/global_risk):
  `run_interval_seconds`, `trader_cycle_timeout_seconds`,
  `runtime_trigger_cycle_timeout_seconds`,
  `global_risk.max_gross_exposure_usd`,
  `global_risk.max_daily_loss_usd`,
  `global_risk.max_orders_per_cycle`,
  `live_market_context.enabled`,
  `live_market_context.history_window_seconds`,
  `live_market_context.history_fidelity_seconds`,
  `live_market_context.max_history_points`,
  `live_market_context.timeout_seconds`,
  `live_market_context.strict_ws_pricing_only`,
  `live_market_context.max_market_data_age_ms`.
- [x] Group C (`traders_copy_trade` strategy_params):
  `min_confidence`, `max_signal_age_seconds` +
  `_hard_ceiling`, `min_source_notional_usd`,
  `copy_delay_seconds`, `proportional_sizing` +
  `proportional_multiplier`,
  `require_inventory_for_sells` +
  `allow_partial_inventory_sells` +
  `min_inventory_fraction`, `default_leader_weight` +
  `leader_weights`, `traders_scope.*`.
- [x] Group D (scanner): `scan_interval_seconds`,
  `min_profit_threshold`, `max_markets_to_scan`,
  `min_liquidity`.
- [x] Group E (live-trading proxy):
  `TradingProxySettings.timeout`,
  `TradingProxySettings.require_vpn`.
- [x] Mark completed

### Task 2: Confirm the 4 newly-suspect dead-code findings

- [x] `max_daily_spend_usd`: `git grep -rn` returns hits **only**
  in `strategy_sdk.py` (lines 399, 435, 1921-1922). No
  consumer file. Verified 2026-05-10.
- [x] `retry_limit`: same pattern (lines 404, 440, 1928).
- [x] `retry_backoff_ms`: same pattern (lines 405, 441, 1929).
- [x] `order_ttl_seconds`: same pattern (lines 401, 437, 1620,
  1925). Line 1620 is inside a list of fields handled by the
  same coerce loop, not a consumer.
- [x] If any is later found alive (e.g. via fast-runtime path
  not yet greppped), promote to HIGH tier in this same edit
  and remove from dead-code subsection.
- [x] Mark completed

### Task 3: Write the HIGH-tier section

- [x] Append `## Knob interaction matrix — HIGH tier` to
  [`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md)
  immediately after the CRITICAL matrix's "How to use this
  matrix" subsection and before the existing tail
  `## Посилання` block (or wherever the file's natural end is).
- [x] Use the same per-entry shape as CRITICAL: header
  `### HIGH — \`<knob>\``, Default + file:line, Direct
  consumers table, Indirect consumers (or "n/a"), Compound
  with bullets. Keep entries concise (≤ 25 lines each;
  CRITICAL averaged ~30 because of dimensional-bug warnings,
  HIGH doesn't have those).
- [x] Group entries by category: A (TRADER_RISK), B
  (orchestrator), C (traders_copy_trade source-config),
  D (scanner), E (live-trading proxy). Use `#### Group A —
  TRADER_RISK_DEFAULTS continued` style sub-headers so the
  reader can navigate.
- [x] Mark completed

### Task 4: Write the dead-code subsection

- [x] After the HIGH-tier entries, add
  `### Dead code in TRADER_RISK_DEFAULTS` with one short
  entry per dead knob:

  ```markdown
  - **`<knob>`** (default `<X>`, schema-only) —
    [`strategy_sdk.py:<line>`](../../backend/services/strategy_sdk.py).
    UI input visible (label `<UI label>`); validation
    coerces to `[<min>, <max>]`. **No gate / decision /
    execution consumer** found in repo grep on 2026-05-10.
    Tweaking has no runtime effect. If you need the
    behaviour, file a B/R plan to wire the consumer in.
  ```

- [x] Reference plan 0025's `circuit_breaker_drawdown_pct`
  callout to keep all dead-knob findings in one place.
- [x] Mark completed

### Task 5: Update the tier-classification preamble

- [x] In the existing `## Knob interaction matrix — CRITICAL
  tier` preamble, the bullet list reading
  `**HIGH** — змінює поведінку…` currently says HIGH/MEDIUM
  knobs are scattered across `docs/strategies/<slug>.md` and
  `trader-pipeline.md`. Update it to point at the new HIGH
  section in this same file (the per-strategy MEDIUM scatter
  remains true).
- [x] Bump the file's `Last verified:` line to today.
- [x] Mark completed

### Task 6: Cross-reference + close

- [x] In
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  add a one-line sentence inside the "CRITICAL-tier knob
  changes — consult the interaction matrix first" callout
  noting that HIGH-tier knobs **do not** require the
  walkthrough but operators are still encouraged to scan
  the matrix entry before tweaking. Phase 2 walkthrough
  remains CRITICAL-only.
- [x] `git mv docs/plans/0029-knob-matrix-high-tier-expansion.md
  docs/plans/completed/`.
- [x] Update [`plan-control-index.md`](plan-control-index.md):
  link target to `completed/0029-...md`.
- [x] Mark completed
