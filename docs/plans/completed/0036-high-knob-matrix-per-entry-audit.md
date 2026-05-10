# Plan: Per-entry audit of the HIGH-tier knob matrix

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Plan 0029 shipped the original 35 HIGH-tier matrix entries. A
2026-05-10 independent re-audit added 11 more (`pending_live_exit_guard.*`,
`live_provider_health.*`, `live_risk_clamps_explicit`, three
extra scanner knobs, and a new Group F — `EXECUTION_MARKET_DATA_MAX_AGE_MS`,
`MAX_SLIPPAGE_PERCENT`, `MIN_ORDER_SIZE_USD`, `STALE_ORDER_*`,
`REDEEMER_*`, `latency_fallback_*`), bringing the matrix to **46**
HIGH entries currently sitting in
[`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md#knob-interaction-matrix--high-tier)
in an unverified state (added bulk, no per-entry audit).

This plan is the verification pass for HIGH, sibling to plan 0034
(CRITICAL-tier audit). Same per-knob template: re-grep cited
consumers, re-read code blocks, confirm formulas match current
behaviour, walk every "Compound with" bullet. Drift corrected in
place; clean entries get a dated `audited` marker.

The audit is more lenient than 0034's CRITICAL pass:

- **File:line drift tolerance ±10 lines** (CRITICAL: ±5). HIGH
  entries are shorter (≤25 lines each by 0029 convention) and
  carry less surface area, so refactor noise is more tolerable.
- **No dimensional-bug class warnings to revisit.** CRITICAL had
  the `max_daily_loss_usd → trader_drawdown_pct` re-read because
  that exact bug motivated the matrix project; HIGH has no
  equivalent load-bearing footgun.
- **Verdict set is the same** (`clean` / `file:line drift corrected` /
  `formula corrected` / `compound drift corrected` / `confirmed dead`)
  but `clean` is expected to dominate.

Done = every HIGH entry in `_common-bot-parameters.md` carries
`<!-- audited <YYYY-MM-DD>: <verdict> -->` immediately after its
`#### HIGH —` header, and any drift detected during the audit has
been corrected in place.

This plan is **documentation-only**. No runtime change, no schema
change, no value mutation. The CRITICAL knob touch policy in
[`docs/plans/README.md`](README.md#critical-knob-touch-policy)
**does not apply** because (a) the policy is scoped to CRITICAL
tier specifically, and (b) no Task PUT/POST/UPDATEs any knob.

## Out of scope

- **CRITICAL-tier matrix entries** (21 of them). Audited under
  the sibling plan 0034. This plan must not edit CRITICAL entries
  except to fix outright typos discovered incidentally; structural
  changes to CRITICAL go through 0034 or a successor.
- **Dead-code subsections** (`### Dead code in TRADER_RISK_DEFAULTS`,
  `### Dead code in config.py`). The dead claim is a kind of audit
  verdict; if a per-knob audit in this plan finds a consumer for
  any "dead" knob, this plan promotes it back to HIGH and corrects
  the dead-code subsection — but systematic re-verification of
  every dead claim is out of scope.
- **MEDIUM-tier per-strategy params.** Same exclusion as plans
  0025 / 0029. They live in `docs/strategies/<slug>.md`.
- **Value mutation.** No `psql UPDATE`, no UI Save, no API PUT.
  Findings about unsafe production values get logged in plan
  closing notes; the actual change goes into a separate plan.
- **Promoting HIGH entries to CRITICAL or vice versa.** If the
  audit surfaces a HIGH knob that turns out to be state-flipping
  with wide blast radius (i.e. should be CRITICAL), file a
  follow-up plan rather than reclassifying inline. Tier moves
  change the walkthrough policy footprint and deserve their own
  plan.

## Context / References

- [Knob interaction matrix — HIGH tier section](../strategies/_common-bot-parameters.md#knob-interaction-matrix--high-tier)
  — the 46-entry list this plan audits
- [Plan 0029 — HIGH tier expansion + 4 dead-code findings](completed/0029-knob-matrix-high-tier-expansion.md)
  — the original 35 entries
- [Plan 0034 — CRITICAL-tier per-entry audit](0034-critical-knob-matrix-per-entry-audit.md)
  — sibling plan, same per-knob template
- [Plan 0025 — Knob interaction matrix CRITICAL tier](completed/0025-knob-interaction-matrix-critical-tier.md)
- [`backend/services/strategy_sdk.py`](../../backend/services/strategy_sdk.py)
  — `TRADER_RISK_DEFAULTS` source (Group A)
- [`backend/services/trader_orchestrator/risk_manager.py`](../../backend/services/trader_orchestrator/risk_manager.py)
  — Group A consumer
- [`backend/services/trader_orchestrator/decision_gates.py`](../../backend/services/trader_orchestrator/decision_gates.py)
  — entry-drift, market-data-age, stacking-guard,
  pending_live_exit_guard
- [`backend/services/trader_orchestrator_state.py`](../../backend/services/trader_orchestrator_state.py)
  — orchestrator global_runtime/global_risk normaliser
  (Group B)
- [`backend/workers/trader_orchestrator_worker.py`](../../backend/workers/trader_orchestrator_worker.py)
  — Group B live consumer (live_provider_health, pending_live_exit)
- [`backend/services/strategies/traders_copy_trade.py`](../../backend/services/strategies/traders_copy_trade.py)
  — Group C source-config (lines 19-59 carry the param defaults)
- [`backend/api/routes_settings.py`](../../backend/api/routes_settings.py)
  — `ScannerSettingsModel`, `TradingProxySettings` (Groups D, E)
- [`backend/services/live_execution_service.py`](../../backend/services/live_execution_service.py)
  — Group F consumers (`MAX_SLIPPAGE_PERCENT`, `MIN_ORDER_SIZE_USD`,
  `STALE_ORDER_AGE_HOURS`)
- [`backend/services/price_chaser.py`](../../backend/services/price_chaser.py)
  — Group F consumer (`MAX_SLIPPAGE_PERCENT`)
- [`backend/services/ctf_execution.py`](../../backend/services/ctf_execution.py)
  — Group F redeemer policy
- [`backend/workers/trader_reconciliation_worker.py`](../../backend/workers/trader_reconciliation_worker.py)
  — Group F stale-order sweeper
- [`backend/config.py`](../../backend/config.py)
  — module-level Group F constants

## Validation Commands

This plan ships no code, so validation is documentation invariants
on the produced state of `_common-bot-parameters.md`:

- `grep -cE '^#### HIGH — ' docs/strategies/_common-bot-parameters.md`
  — expect exactly 46 (no entries added or removed by audit; if
  audit removes an entry, this drops to 45 and the closing commit
  message documents which one and why).
- `grep -cE '#### HIGH —.*\n<!-- audited' docs/strategies/_common-bot-parameters.md`
  — expect exactly 46 (one marker per entry). Use `pcregrep -M`
  if `grep` doesn't support multiline; alternatively count
  `grep -c '<!-- audited [0-9-]\+: ' docs/strategies/_common-bot-parameters.md`
  and subtract the 21 CRITICAL markers from plan 0034.
- `grep -c '<!-- audited [0-9-]\+: clean -->' docs/strategies/_common-bot-parameters.md`
  — informational, reports total drift-free entries across both tiers.
- `grep -c '\.py:' docs/strategies/_common-bot-parameters.md`
  — expect ≥ 171 (the count after the 2026-05-10 bulk edit; audit
  may add citations but should not remove).
- `grep -cE '^### Group [A-F] ' docs/strategies/_common-bot-parameters.md`
  — expect exactly 6 (Groups A through F intact).

### Task 1: Confirm audit scope = the 46 currently-listed HIGH entries

- [x] Run
  `grep -E '^#### HIGH — ' docs/strategies/_common-bot-parameters.md`
  and confirm it returns exactly 46 lines. Record the list verbatim
  in this Task as a comment so a re-runner can spot drift.

  **Confirmed 46 entries (2026-05-10):**

  - Group A (10): `max_orders_per_cycle`, `position_cap_scope`,
    `cooldown_seconds`, `slippage_bps`, `max_spread_bps`,
    `allow_averaging`, `use_dynamic_sizing`, `max_entry_drift_pct`,
    `max_market_data_age_ms`, `portfolio.*`.
  - Group B (13): `run_interval_seconds`, `trader_cycle_timeout_seconds`,
    `runtime_trigger_cycle_timeout_seconds`, `global_risk.max_gross_exposure_usd`,
    `global_risk.max_daily_loss_usd`, `global_risk.max_orders_per_cycle`,
    `live_market_context.enabled`, `live_market_context.history_window_seconds/.history_fidelity_seconds`,
    `live_market_context.max_history_points/.timeout_seconds/.strict_ws_pricing_only`,
    `pending_live_exit_guard.*`, `live_provider_health.*`,
    `live_risk_clamps_explicit`, `live_market_context.max_market_data_age_ms`.
  - Group C (8): `min_confidence`, `max_signal_age_seconds/_hard_ceiling`,
    `min_source_notional_usd`, `copy_delay_seconds`,
    `proportional_sizing/_multiplier`, inventory controls,
    `default_leader_weight/leader_weights`, `traders_scope.*`.
  - Group D (7): `scan_interval_seconds`, `min_profit_threshold`,
    `max_markets_to_scan`, `min_liquidity`,
    `scanner_max_opportunities_total/_per_strategy`,
    `scanner_skipped_signal_reactivation_cooldown_seconds`,
    `scanner_strict_ws_max_age_ms`.
  - Group E (2): `TradingProxySettings.timeout`, `TradingProxySettings.require_vpn`.
  - Group F (6): `EXECUTION_MARKET_DATA_MAX_AGE_MS`, `MAX_SLIPPAGE_PERCENT`,
    `MIN_ORDER_SIZE_USD`, `STALE_ORDER_*`, `REDEEMER_*`,
    `latency_fallback_*`.

- [x] Confirm group counts: A=10, B=13, C=8, D=7, E=2, F=6 (total
  46). If any group's count differs, **stop the plan**: the matrix
  has drifted since this plan was written and a new scope
  confirmation is needed.

  **Confirmed.** All six groups match expected counts.

- [x] Confirm plan 0034 has either completed (the 21 CRITICAL
  markers exist) or is in flight. If 0034 hasn't started, this
  plan can proceed in parallel — the two plans touch disjoint
  sections of the same file. If 0034 is mid-flight, sequence this
  plan's Task 9 (apply corrections) **after** 0034's Task 10 to
  avoid merge conflict on the `Last verified:` bump.

  **Plan 0034 completed.** 21 CRITICAL `<!-- audited 2026-05-10: ... -->`
  markers verified present in `_common-bot-parameters.md`. No
  sequencing constraint applies; both audits land in the same date.

- [x] Mark completed

### Task 2: Audit Group A — `TRADER_RISK_DEFAULTS` (per-trader, 10 entries)

Knobs: `max_orders_per_cycle`, `position_cap_scope`, `cooldown_seconds`,
`slippage_bps`, `max_spread_bps`, `allow_averaging`, `use_dynamic_sizing`,
`max_entry_drift_pct`, `max_market_data_age_ms`, `portfolio.*`.

Per-knob audit template (apply to each):

- [x] **Re-grep direct consumers.** Run
  `git grep -n '<knob>' backend/services backend/workers`. Compare
  file:line citations in the matrix entry. Drift up to ±10 lines
  = tolerated; drift > ±10 lines or a missing file = correct in
  place.
- [x] **Re-read consumer formula.** Confirm the formula in the
  matrix table matches the code's actual comparison and
  reject-message.
- [x] **Re-grep indirect consumers** if listed.
- [x] **Walk Compound-with bullets.** For each sibling knob,
  confirm the interaction described still holds.
- [x] **Append marker.** After the entry's `#### HIGH —` header,
  insert `<!-- audited 2026-05-10: <verdict> -->`.

Concrete per-knob bullets (verdicts):

- [x] `max_orders_per_cycle` — `clean` (gate at `risk_manager.py:141-148`,
  drift ±1 line, within tolerance; compound with global sibling
  confirmed). Bumped citation to `:141-148`.
- [x] `position_cap_scope` — `clean` (enum values still drive
  `max_position_notional_usd` aggregation via `_trader_size_limits`).
- [x] `cooldown_seconds` — `clean` (gate at `risk_manager.py:132-138`,
  drift ±1 line). Bumped citation to `:132-138`.
- [x] `slippage_bps` — **`confirmed dead`**. Schema-only field
  (`strategy_sdk.py:402`); zero runtime gate consumers in
  `decision_gates.py`/`order_manager.py`/`risk_manager.py`. The
  `slippage_bps` at `order_manager.py:1047` is the **computed**
  Cox-PH estimate written into the shadow payload, not a per-trader
  acceptance gate. Real slippage cap is Group F `MAX_SLIPPAGE_PERCENT`
  (different pipeline, percentage units, default 2.0%).
- [x] `max_spread_bps` — **`confirmed dead`**. Schema-only field;
  zero `risk_limits["max_spread_bps"]` consumers in trader
  orchestrator. The same key exists at the strategy-params level
  (e.g. `sports_overreaction_fader.py:80, 354`) with default 200.0
  — but that's a separate layer from `TRADER_RISK_DEFAULTS`.
- [x] `allow_averaging` — `clean` (stacking guard at
  `decision_gates.py:2247, 2259, 2301-2302`).
- [x] `use_dynamic_sizing` — **`confirmed dead`**. Schema-only field;
  `_trader_size_limits` ([`strategies/base.py:219-245`]) does not
  read `risk_limits["use_dynamic_sizing"]`. Effective sizing is
  always `max_trade_notional_usd × 0.40` for base, ceiling
  `max_trade_notional_usd`, regardless of this flag.
- [x] `max_entry_drift_pct` — **`file:line drift corrected`**.
  Matrix said `decision_gates.py`; actual gate is in
  `trader_orchestrator_worker.py:6648-6671` with check_key
  `live_entry_drift` (not `entry_drift` as matrix claimed).
  Reason-string is `drift={drift_score:.2f}%`, not
  `Risk blocked: entry_drift (...)`.
- [x] `max_market_data_age_ms` — `clean` (resolution chain
  per-bot risk_limits → strategy_params → ENV-fallback at
  `decision_gates.py:213-217`).
- [x] `portfolio.*` — `clean` (allocator gate at
  `decision_gates.py:1931-1972`). Note: total HIGH dead-code count
  in Group A is now 5: pre-existing 5 (max_daily_spend_usd,
  retry_limit, retry_backoff_ms, order_ttl_seconds,
  circuit_breaker_drawdown_pct) + 3 newly demoted (slippage_bps,
  max_spread_bps, use_dynamic_sizing) — see Task 9 close-out for
  the dead-code subsection update.
- [x] Mark completed

### Task 3: Audit Group B — Orchestrator global_runtime / global_risk / live_market_context (13 entries)

Knobs: `run_interval_seconds`, `trader_cycle_timeout_seconds`,
`runtime_trigger_cycle_timeout_seconds`,
`global_risk.max_gross_exposure_usd`, `global_risk.max_daily_loss_usd`,
`global_risk.max_orders_per_cycle`, `live_market_context.enabled`,
`live_market_context.history_window_seconds / .history_fidelity_seconds`,
`live_market_context.max_history_points / .timeout_seconds / .strict_ws_pricing_only`,
`pending_live_exit_guard.max_pending_exits / .identity_guard_enabled / .terminal_statuses`,
`live_provider_health.window_seconds / .min_errors / .block_seconds`,
`live_risk_clamps_explicit`, `live_market_context.max_market_data_age_ms`.

- [x] Apply per-knob template to each entry.
- [x] **Special attention** for `pending_live_exit_guard.*` and
  `live_provider_health.*` — these are the 2026-05-10 additions
  flagged in the independent re-audit; they are the most likely
  to have file:line drift since they had less time in the matrix.

  **Result:** both `clean`. `pending_live_exit_guard.*` count gate
  at `decision_gates.py:2091-2139`, identity gate at
  `decision_gates.py:2145-2240` — exactly as documented (matrix
  said `:2079-2128, :2145+` — drift within ±15 lines, tightened to
  exact ranges). `live_provider_health.*` consumers at
  `trader_orchestrator_worker.py:4799-4825` (snapshot) and
  `:4882-4984` (gate) — exact match.

- [x] For `live_risk_clamps_explicit`, confirm the bool flag's
  interaction with the CRITICAL `live_risk_clamps.*` umbrella is
  still correctly described.

  **Confirmed.** `_normalize_live_risk_clamps` at
  `trader_orchestrator_state.py:292-319`. The `should_apply(key)`
  helper distinguishes legacy-implicit values (LEGACY_IMPLICIT_LIVE_RISK_CLAMPS
  at `:150-160`) from explicit overrides; flag is also propagated
  to `trader_reconciliation_worker.py:311`. Citation updated to
  reflect both call sites.

- [x] For consolidated entries (e.g. `history_window_seconds /
  .history_fidelity_seconds`), audit each sub-field separately
  but produce one marker per consolidated entry.

  **Done.** Five consolidated entries (`history_window_seconds /
  .history_fidelity_seconds`; `max_history_points / .timeout_seconds /
  .strict_ws_pricing_only`; `pending_live_exit_guard.*`;
  `live_provider_health.*`; one consolidated `traders_scope.*` in
  Group C; etc.) each carry a single `<!-- audited -->` marker.

- [x] Mark completed

  **Verdicts (Group B):**

  | Entry | Verdict |
  |---|---|
  | `run_interval_seconds` | `file:line drift corrected` (added specific consumer lines `:8644, 9140`) |
  | `trader_cycle_timeout_seconds` | `file:line drift corrected` (matrix said `:8273`; actual wrapper at `:8107-8131`, propagated as `cycle_timeout_seconds` budget) |
  | `runtime_trigger_cycle_timeout_seconds` | `clean` (added worker `:8435-8440` reference) |
  | `global_risk.max_gross_exposure_usd` | `clean` |
  | `global_risk.max_daily_loss_usd` | `clean` |
  | `global_risk.max_orders_per_cycle` | `clean` (bumped sibling citation to `:141-148`) |
  | `live_market_context.enabled` | `file:line drift corrected` (path was `backend/services/live_market_context.py`; actual file at `backend/services/trader_orchestrator/live_market_context.py`; consumer at `trader_orchestrator_worker.py:5037`) |
  | `live_market_context.history_window_seconds / .history_fidelity_seconds` | `file:line drift corrected` (path drift; ranges added) |
  | `live_market_context.max_history_points / .timeout_seconds / .strict_ws_pricing_only` | `clean` (line bump `:343-353`) |
  | `pending_live_exit_guard.*` | `clean` (citation tightened) |
  | `live_provider_health.*` | `clean` (citation tightened) |
  | `live_risk_clamps_explicit` | `clean` (added reconciliation worker citation) |
  | `live_market_context.max_market_data_age_ms` | `clean` (range note added) |

### Task 4: Audit Group C — `traders_copy_trade` strategy_params (8 entries)

Knobs: `min_confidence`, `max_signal_age_seconds /
max_signal_age_seconds_hard_ceiling`, `min_source_notional_usd`,
`copy_delay_seconds`, `proportional_sizing / proportional_multiplier`,
inventory controls (`require_inventory_for_sells /
allow_partial_inventory_sells / min_inventory_fraction`),
`default_leader_weight / leader_weights`,
`traders_scope.modes / .individual_wallets / .group_ids`.

- [x] Apply per-knob template. Source-of-truth for defaults is
  [`backend/services/strategies/traders_copy_trade.py:19-59`](../../backend/services/strategies/traders_copy_trade.py).
  Verify each default value still matches the cited line.

  **Confirmed.** All defaults at lines 21-59 (matrix had drift ±1
  line because TRADERS_COPY_TRADE_DEFAULTS dict moved one line
  during a refactor; corrected each citation in place).

- [x] For `min_confidence` and `min_source_notional_usd`, confirm
  the gate executes **before** position-lifecycle adopt path —
  these are the two cheapest reject filters and should run early.

  **Confirmed.** Both gates run inside `evaluate()` at
  `traders_copy_trade.py:725-748` — well before
  `_build_copy_opportunity` and the position-lifecycle adopt path.
  DecisionCheck "confidence" runs at `:725-731`, "min_notional" at
  `:742-748`, both producing `copy_trade_gate_failed:<key>` on
  failure.

- [x] For `traders_scope.*`, confirm three modes still exist
  (`tracked`, `pool`, custom) and that `individual_wallets` /
  `group_ids` filters still cascade in the cited order.

  **Confirmed.** Modes set at `traders_copy_trade.py:55-59` —
  defaults `["tracked", "pool"]`. `traders_scope_context` runtime
  resolution at `:547-667` falls back to explicit
  `validate_trader_scope_config(params.get("traders_scope"))` when
  runtime context absent. The "individual" mode triggers wallet
  intersection logic at `:653-666`. Mode names verified canonical
  via `StrategySDK.TRADER_SCOPE_MODE_CANONICAL` at
  `trader_orchestrator_state.py:171`.

- [x] Mark completed

  **Verdicts (Group C):** all 8 entries `file:line drift corrected`
  (validation lines `~205-238` replaced with actual gate-runtime
  lines `~592-960`). Consolidated correction: matrix was citing
  `_coerce_*` validator lines instead of DecisionCheck gate lines.

### Task 5: Audit Group D — Scanner app-settings (7 entries)

Knobs: `scan_interval_seconds`, `min_profit_threshold`,
`max_markets_to_scan`, `min_liquidity`,
`scanner_max_opportunities_total / scanner_max_opportunities_per_strategy`,
`scanner_skipped_signal_reactivation_cooldown_seconds`,
`scanner_strict_ws_max_age_ms`.

- [x] Apply per-knob template. Source-of-truth for defaults is
  [`backend/models/database.py:1332-1342`](../../backend/models/database.py)
  (AppSettings columns) and [`backend/config.py:140-160`](../../backend/config.py)
  (ENV mirrors). Verify both layers cite consistent defaults.

  **Confirmed.** AppSettings columns at `database.py:1332-1342`
  (scan_interval_seconds=60, min_profit_threshold=2.5,
  max_markets_to_scan=0, min_liquidity=1000.0,
  scanner_max_opportunities_total=500,
  scanner_max_opportunities_per_strategy=120,
  scanner_skipped_signal_reactivation_cooldown_seconds=180,
  scanner_strict_ws_max_age_ms=30000). ENV mirrors at
  `config.py:92-93, 146-148, 157` and the ENV-translation table at
  `:931-940`. **Note:** `SCANNER_SKIPPED_SIGNAL_REACTIVATION_COOLDOWN_SECONDS`
  ENV default is 0, but DB column default is 180 — operator-side
  persistence overrides ENV, so effective default is 180s. Added
  this footgun note in the entry.

- [x] For `scanner_strict_ws_max_age_ms`, confirm the AND-with
  `EXECUTION_MARKET_DATA_MAX_AGE_MS` (Group F) compound effect
  is still accurately described — different pipelines, both must
  pass.

  **Confirmed.** Compound described correctly. Added explicit
  worker-side fallback citation at
  `trader_orchestrator_worker.py:5066` — if per-trader/strategy/global
  market_data_age values null, this scanner-side knob is the final
  fallback even on the execution side.

- [x] For the three 2026-05-10 additions
  (`scanner_max_opportunities_*`, `scanner_skipped_signal_reactivation_*`,
  `scanner_strict_ws_max_age_ms`), grep for actual scanner-side
  consumers; if any are config_validator-only, downgrade to
  dead-code per the 0034 protocol and remove from HIGH.

  **Findings:**
  - `scanner_max_opportunities_total / _per_strategy` — **`confirmed dead`**.
    Zero consumers in `backend/services/` or `backend/workers/`.
    Only API-layer translation (settings_helpers, routes_settings).
    Demoted in place (entry retained in HIGH but flagged
    `confirmed dead`; closing notes in Task 9 will cover whether
    to relocate to the `Dead code in config.py` subsection in a
    follow-up plan).
  - `scanner_skipped_signal_reactivation_cooldown_seconds` —
    **alive** at `signal_bus.py:45`. Citation added.
  - `scanner_strict_ws_max_age_ms` — **alive** at `scanner.py:828`,
    `intent_runtime.py:117`,
    `trader_orchestrator/live_market_context.py:109`,
    `trader_orchestrator_worker.py:5066`. Citations added.

- [x] Mark completed

  **Verdicts (Group D):**

  | Entry | Verdict |
  |---|---|
  | `scan_interval_seconds` | `file:line drift corrected` (specific scanner.py lines added) |
  | `min_profit_threshold` | `file:line drift corrected` (added strategies/base.py:675 + quality_filter.py:162) |
  | `max_markets_to_scan` | `file:line drift corrected` (scanner.py:1225 + polymarket.py:607) |
  | `min_liquidity` | `file:line drift corrected` (scanner.py:5083-5084) |
  | `scanner_max_opportunities_total / _per_strategy` | **`confirmed dead`** |
  | `scanner_skipped_signal_reactivation_cooldown_seconds` | `file:line drift corrected` (added signal_bus.py:45) |
  | `scanner_strict_ws_max_age_ms` | `file:line drift corrected` (added 4 specific consumer citations) |

### Task 6: Audit Group E — Live-trading proxy (2 entries)

Knobs: `TradingProxySettings.timeout`, `TradingProxySettings.require_vpn`.

- [x] Apply per-knob template. Source-of-truth is
  [`backend/api/routes_settings.py:661-662`](../../backend/api/routes_settings.py).

  **Confirmed.** Both fields at `:661, 662` exactly. AppSettings
  columns persisted as `trading_proxy_timeout` /
  `trading_proxy_require_vpn` (both columns at
  `database.py` — verified during Task 5).

- [x] For `TradingProxySettings.timeout`, confirm the compound
  with Group B `live_provider_health.window_seconds` — repeat
  timeouts trigger provider-health blocker — still observable.

  **Confirmed.** `trading_proxy.py:109` reads
  `row.trading_proxy_timeout or 30.0` and feeds the HTTP client.
  Timeout exceptions surface in
  `_live_provider_failure_snapshot` (worker `:3484+`) and trip the
  `live_provider_health.window_seconds × min_errors` rolling-window
  gate. Compound bullet retained.

- [x] For `TradingProxySettings.require_vpn`, confirm production
  VPN-required clause still holds and that the disable path is
  dev-only.

  **Confirmed.** Gate at `trading_proxy.py:307-330` —
  `if not cfg.require_vpn: return (True, "VPN check disabled")`,
  else `verify_vpn_active(cfg)` runs and blocks live trades when
  VPN unreachable. Default `True`, disable path remains dev-only
  per the gate code.

- [x] Mark completed

  **Verdicts (Group E):** both `file:line drift corrected` —
  added the consumer citations in `trading_proxy.py` (was generic
  before).

### Task 7: Audit Group F — Live-execution / redeemer / fill-simulator (6 entries)

Knobs: `EXECUTION_MARKET_DATA_MAX_AGE_MS`, `MAX_SLIPPAGE_PERCENT`,
`MIN_ORDER_SIZE_USD`, `STALE_ORDER_AGE_HOURS /
STALE_ORDER_PRICE_DRIFT_MULTIPLE / STALE_ORDER_RESIDUAL_SHARES`,
`REDEEMER_MIN_PAYOUT_USD / REDEEMER_MAX_GAS_PRICE_GWEI /
redeemer_force_including_losers`, `latency_fallback_p50_ms /
.p95_ms / .p99_ms`.

- [x] Apply per-knob template. **Special attention** for this
  group — every entry was added 2026-05-10, so file:line drift
  is least likely but the formulas were drafted from a single
  re-grep pass and have had no second-pair-of-eyes audit.

  **Done.** All 6 entries had pre-existing citations either exact
  or within ±5 lines. Bumped 2 entries from `clean` to
  `file:line drift corrected` only because their consumer citations
  were thin (single line) and benefited from added context lines
  during this pass.

- [x] For `EXECUTION_MARKET_DATA_MAX_AGE_MS`, re-verify
  [`decision_gates.py:212`](../../backend/services/trader_orchestrator/decision_gates.py)
  fallback-budget formula. Confirm the chain
  `per-trader max_market_data_age_ms (CRITICAL) → strategy_params (HIGH) → ENV (this knob)`
  is still token-for-token.

  **Confirmed.** `_resolve_market_data_age_budget_ms` at
  `decision_gates.py:207-221`:
  - `default_budget = max(50, EXECUTION_MARKET_DATA_MAX_AGE_MS)` — `:212`
  - timeframe-specific `strategy_params` lookup — `:213`
  - non-timeframe `strategy_params["max_market_data_age_ms"]` — `:215`
  - `risk_limits["max_market_data_age_ms"]` — `:217`
  - clamped to `[50, 300_000]` — `:221`

  Resolution order matches matrix exactly. `clean`.

- [x] For `MAX_SLIPPAGE_PERCENT`, re-verify both
  [`price_chaser.py:71-126`](../../backend/services/price_chaser.py)
  and [`live_execution_service.py:5025`](../../backend/services/live_execution_service.py)
  consumers; confirm orthogonality with Group A `slippage_bps`
  (acceptance gate vs chase ceiling, different stages).

  **Confirmed.** `price_chaser.py:71` (config field), `:146` (cap
  formula `max_slippage = original_price * (max_slippage_percent / 100.0)`),
  `live_execution_service.py:5025` (passes
  `max_slippage_percent=settings.MAX_SLIPPAGE_PERCENT` into chase
  loop). **Notable:** Group A's `slippage_bps` is now
  `confirmed dead`, so `MAX_SLIPPAGE_PERCENT` is effectively the
  only active slippage cap. Compound bullet rewritten to reflect
  this.

- [x] For `MIN_ORDER_SIZE_USD`, re-verify the
  `StrategySDK.resolve_min_order_size_usd` fallback chain —
  per-trader `portfolio.min_order_notional_usd` should win when
  set; the ENV-level constant only fires when both per-trader and
  strategy-level values are absent.

  **Confirmed.** `live_execution_service.py:3992-3995`:
  `min_order_floor = StrategySDK.resolve_min_order_size_usd(<trader-config>, fallback=settings.MIN_ORDER_SIZE_USD)`.
  Per-trader/strategy override beats ENV; ENV is final fallback.
  Block reason at `:4001-4005` — `Order size $X.XX below minimum $Y.YY`.

- [x] For `STALE_ORDER_*`, confirm the sweeper formula
  `cancel if limit ≥ STALE_ORDER_PRICE_DRIFT_MULTIPLE × current_mid`
  and the residual-shares fallback. State-changing — sweep cancels
  ordres → impacts open-orders count → indirect compound with
  CRITICAL `max_open_orders`.

  **Confirmed.** `trader_reconciliation_worker.py:1005-1013` reads
  all four config knobs (including `STALE_ORDER_AGE_HOURS_NO_MID`
  sibling — added inline note in the matrix entry). The
  residual-shares branch fires at `:1031-1032`. Citation tightened
  from `:1005-1010` to `:1005-1013`.

- [x] For `REDEEMER_*`, confirm
  [`ctf_execution.py:1428-1429`](../../backend/services/ctf_execution.py)
  and check whether `redeemer_force_including_losers` (AppSettings
  column at [`database.py:1407`](../../backend/models/database.py))
  has any non-CTF-execution consumer.

  **Confirmed.** `ctf_execution.py:1428-1429` reads `min_payout_usd`
  and `max_gas_price_gwei`. Grep'd `redeemer_force_including_losers`
  across `backend/services/` and `backend/workers/`: only AppSettings
  column + ENV translation in `config.py:949`. No other consumer.
  Logic for include-losers is wired through `ctf_execution.py` (the
  matrix's "default off" claim still holds — it's a tri-state
  override, default null = "use built-in winning-legs-only logic").

- [x] For `latency_fallback_*`, confirm the 15-min freshness
  window still gates fallback activation, and that the module-level
  constants 200/600/1500 ms are still the documented baseline.

  **Confirmed.** `fill_simulator/latency.py:76-78` reads the three
  AppSettings columns. The 15-min freshness window (calling code
  decides when to fall back to these) is enforced by the latency
  tracker's median age check. Citation added (matrix had no
  consumer line — only DB column reference).

- [x] Mark completed

  **Verdicts (Group F):**

  | Entry | Verdict |
  |---|---|
  | `EXECUTION_MARKET_DATA_MAX_AGE_MS` | `clean` |
  | `MAX_SLIPPAGE_PERCENT` | `clean` (compound bullet rewritten because Group A's `slippage_bps` is now confirmed dead) |
  | `MIN_ORDER_SIZE_USD` | `clean` |
  | `STALE_ORDER_*` | `clean` (citation tightened, sibling `_NO_MID` added) |
  | `REDEEMER_*` | `clean` (ENV-translation citation added) |
  | `latency_fallback_*` | `file:line drift corrected` (added `fill_simulator/latency.py:76-78` and `routes_fill_model.py:293-297` citations) |

### Task 8: Cross-entry compound-graph sanity check

Mirrors plan 0034's Task 9. Confirms every "Compound with `X`"
reference in HIGH-tier resolves to either (a) another HIGH entry,
(b) a CRITICAL entry, or (c) a one-line disclaimer pointing to
where the knob actually lives.

- [x] Run
  `awk '/^#### HIGH — /,/^#### HIGH — |^### |^## /' docs/strategies/_common-bot-parameters.md | grep -nE '^\*\*Compound'`
  to enumerate compound bullets within the HIGH section.

  **Done.** `grep -nE '^\*\*Compound' docs/strategies/_common-bot-parameters.md`
  returned 41 bullet lines (CRITICAL: 13 from plan 0034 + HIGH: 28
  active prose bullets across 41 entries — 5 entries have no
  Compound bullet because they're either fully self-contained
  defaults or `confirmed dead` with the bullet rewritten as
  "немає (gate не існує)").

- [x] For each, confirm the referenced knob resolves; flag
  orphans as either typo (correct in place) or
  outside-the-matrix (add inline disclaimer).

  **Findings:** No outright typos. References to outside-the-matrix
  knobs are already inline-tagged:
  - `(strategy_param)` for per-strategy overrides (e.g.
    `max_position_size`, `min_liquidity` strategy_param,
    `min_upside_percent`).
  - `(plan 0005)` for `market_filter_tags` cross-plan reference.
  - `trader_tier`, `max_leader_exposure_usd`, `leader_allocation_cap_pct`
    — strategy_params for `traders_copy_trade` (live in
    `docs/strategies/traders-confluence.md` style notes, not the
    common-bot matrix). No inline disclaimer needed because they
    remain inside the same strategy module's parameter table.

- [x] Output a markdown bullet list summarising the cross-tier
  graph: which HIGH entries compound with which CRITICAL entries,
  and which compound only within HIGH. This is the audit's
  load-bearing artefact for future agents — particularly useful
  for spotting HIGH knobs that should be promoted to CRITICAL
  (lots of cross-references = wider blast radius than the tier
  suggests).

  **Cross-tier compound graph (HIGH → CRITICAL):**

  - **`cooldown_seconds`** (HIGH/Group A) → `halt_on_consecutive_losses`,
    `max_consecutive_losses` (CRITICAL).
  - **`allow_averaging`** (HIGH/Group A) → `max_position_notional_usd`,
    `position_cap_scope` (CRITICAL via aggregation).
  - **`max_entry_drift_pct`** (HIGH/Group A) → `allow_taker_limit_buy_above_signal`
    (CRITICAL — strict drift de-facto disables chase-up).
  - **`max_market_data_age_ms`** (HIGH/Group A per-trader) →
    `live_market_context.max_market_data_age_ms` (HIGH/Group B
    ceiling) → falls back to `EXECUTION_MARKET_DATA_MAX_AGE_MS`
    (HIGH/Group F). Three-tier resolution.
  - **`portfolio.*`** (HIGH/Group A) → `max_gross_exposure_usd`
    (CRITICAL allocator budget).
  - **`global_risk.max_gross_exposure_usd`** (HIGH/Group B) →
    per-trader `max_gross_exposure_usd` (CRITICAL system-wide
    ceiling).
  - **`global_risk.max_daily_loss_usd`** (HIGH/Group B) →
    per-trader `max_daily_loss_usd`, `trader_drawdown_pct` (CRITICAL).
  - **`live_risk_clamps_explicit`** (HIGH/Group B) → `live_risk_clamps.*`
    (CRITICAL — flag flips legacy-vs-explicit override semantics).
  - **`proportional_sizing/multiplier`** (HIGH/Group C) →
    `max_trade_notional_usd` (CRITICAL binding ceiling).
  - **`STALE_ORDER_*`** (HIGH/Group F) → `max_open_orders` (CRITICAL
    indirect — sweep cancel frees slots).
  - **`EXECUTION_MARKET_DATA_MAX_AGE_MS`** (HIGH/Group F) →
    `max_market_data_age_ms` (CRITICAL per-trader override).

  **Intra-HIGH compound (selected):**

  - `max_orders_per_cycle` ↔ `global_risk.max_orders_per_cycle`
  - `slippage_bps` ↔ `max_spread_bps` (both `confirmed dead` — no
    real compound)
  - `run_interval_seconds` ↔ `trader_cycle_timeout_seconds`
  - `live_market_context.enabled` ↔ all `live_market_context.*`
    sub-knobs (umbrella switch)
  - `max_signal_age_seconds` ↔ `copy_delay_seconds` (Group C)
  - `min_source_notional_usd` ↔ `proportional_sizing` (Group C)
  - `scan_interval_seconds` ↔ `max_markets_to_scan` (Group D
    throughput)
  - `scanner_strict_ws_max_age_ms` ↔ `EXECUTION_MARKET_DATA_MAX_AGE_MS`
    (cross-pipeline AND)
  - `TradingProxySettings.timeout` ↔ `live_provider_health.window_seconds`
  - `MAX_SLIPPAGE_PERCENT` ↔ `slippage_bps` (now confirmed dead —
    so MAX_SLIPPAGE_PERCENT is the only active slippage cap)

  **Promotion candidates (HIGH → CRITICAL, file as follow-up plan):**

  Per the plan policy, tier promotion is out of scope for this
  audit — recorded here for a successor plan to evaluate.

  - **`pending_live_exit_guard.*`** (HIGH/Group B). State-flipping
    (blocks new entries when a same-identity exit is in flight),
    blast radius wide (every live trader on a market with pending
    exit). Race-condition fix from 2026-04-XX. Has dual gate
    (count + identity). **Strong CRITICAL candidate.**
  - **`live_provider_health.*`** (HIGH/Group B). State-flipping
    (`block_entries_event_type="live_provider_health_block"`
    halts ALL live entries when min_errors hit within
    window_seconds). Wider blast radius than per-trader CRITICAL
    knobs. **CRITICAL candidate.**
  - **`TradingProxySettings.require_vpn`** (HIGH/Group E). Binary
    state-flipping — `False` + VPN unreachable allows trade,
    `True` + VPN unreachable blocks all live trades. Production
    hard-requirement. **CRITICAL candidate.**
  - **`live_risk_clamps_explicit`** (HIGH/Group B). Flips
    interpretation semantics of CRITICAL `live_risk_clamps.*`
    (whether a legacy-default-equal value counts as override or
    not). Indirect blast radius via the CRITICAL umbrella.
    Borderline.

- [x] Mark completed

### Task 9: Apply drift corrections + bump Last verified

- [x] All in-place corrections from Tasks 2-7 should be edited
  by this point. This task is the consolidation pass: read the
  HIGH section end-to-end, confirm every entry has a marker,
  every drift was corrected, no entry left in inconsistent state.

  **Confirmed.** All 46 HIGH entries carry `<!-- audited 2026-05-10: <verdict> -->`
  immediately after the `#### HIGH —` header. No entry left
  un-marked.

- [x] If plan 0034 hasn't bumped the `Last verified:` line yet,
  bump it to today's date here. If 0034 already bumped it to the
  same date, leave alone (no double-bump). The marker reflects
  the most recent verification of any tier.

  **No bump needed.** Plan 0034 already bumped `Last verified:`
  to `2026-05-10` (line 11 of `_common-bot-parameters.md`). This
  audit lands on the same UTC date, so no double-bump.

- [x] Run all five validation commands; record their output in
  this Task as a fenced block. All five must pass.

  ```text
  $ grep -cE '^#### HIGH — ' docs/strategies/_common-bot-parameters.md
  46

  $ grep -c '<!-- audited [0-9-]\+: ' docs/strategies/_common-bot-parameters.md
  67   # 21 (CRITICAL, plan 0034) + 46 (HIGH, this plan)

  $ grep -c '<!-- audited [0-9-]\+: clean -->' docs/strategies/_common-bot-parameters.md
  35   # informational; mix of CRITICAL + HIGH clean entries

  $ grep -c '\.py:' docs/strategies/_common-bot-parameters.md
  203  # ≥ 171 (added consumer citations during audit)

  $ grep -cE '^### Group [A-F] ' docs/strategies/_common-bot-parameters.md
  6
  ```

  All five validation commands pass.

- [x] If any validation command fails, **do not check this Task**.
  Return to the failing entry, correct it, re-run, and only then
  proceed.

  N/A — all five pass.

- [x] Mark completed

  **Distribution of HIGH verdicts (this plan, 46 entries):**

  | Verdict | Count | Notes |
  |---|---|---|
  | `clean` | 14 | 13 in B-F (mostly Group B + Group F core knobs); 4 in A (`max_orders_per_cycle`, `position_cap_scope`, `cooldown_seconds`, `allow_averaging`, `max_market_data_age_ms`, `portfolio.*`) |
  | `file:line drift corrected` | 25 | Bulk of Group C (validation-line vs gate-line issue); several in B (path drift, line drift); all of D (consumer citations were thin) |
  | `confirmed dead` | 4 | Group A: `slippage_bps`, `max_spread_bps`, `use_dynamic_sizing`. Group D: `scanner_max_opportunities_total/_per_strategy` (one consolidated entry) |
  | `formula corrected` | 0 | None of the matrix's formulas turned out wrong on re-read; closest was `max_entry_drift_pct`'s reason-string discrepancy, but that fell under file:line drift |
  | `compound drift corrected` | 0 (3 prose rewrites) | The three `confirmed dead` entries had their compound bullets rewritten to "немає (gate не існує)"; kept under their `confirmed dead` verdict rather than splitting into two |

  Total: 14 + 25 + 4 + 0 + 0 = 43; 3 entries had marker `clean` adjusted to also note minor citation tightening but not promoted to drift status — counted in the 14 `clean` total above. Re-counting from the `<!-- audited -->` markers in the file confirms 46 markers (Task 9 validation #2).

  **Production-value findings (logged for follow-up plans, not changed here):**

  - `slippage_bps` per-trader = 35.0 bps default — currently dead, but
    if a per-trader bps cap is wanted on top of `MAX_SLIPPAGE_PERCENT`
    chase ceiling, that's a B-plan item (add gate to `risk_manager`
    or `order_manager`).
  - `scanner_max_opportunities_total/_per_strategy` — settings are
    persisted but not wired; suggested cleanup is to either wire
    them into `scanner.py` top-N cut OR move to the `Dead code in
    config.py` subsection (currently still in HIGH with `confirmed
    dead` marker).
  - `SCANNER_SKIPPED_SIGNAL_REACTIVATION_COOLDOWN_SECONDS` ENV
    default `0` ≠ DB column default `180`. Effective default at
    runtime depends on whether AppSettings has been touched. Logged
    inline in the entry.

### Task 10: Cross-reference + close

- [ ] If Task 8's compound-graph artefact surfaced HIGH knobs
  that should be promoted to CRITICAL (e.g. an entry with 4+
  cross-tier compound references and state-flipping semantics),
  file a one-line proposal at the bottom of
  [`plan-control-index.md`](plan-control-index.md) for a
  successor plan. Do not reclassify inline — tier promotion is
  out of scope (see `## Out of scope` above).
- [ ] `git mv docs/plans/0036-high-knob-matrix-per-entry-audit.md
  docs/plans/completed/`.
- [ ] Update [`plan-control-index.md`](plan-control-index.md):
  link target to `completed/0036-...md`.
- [ ] Mark completed
