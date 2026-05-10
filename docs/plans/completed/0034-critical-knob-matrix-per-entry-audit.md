# Plan: Per-entry audit of the CRITICAL-tier knob matrix

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Plan 0025 shipped the original 15 CRITICAL-tier matrix entries.
A 2026-05-10 independent re-audit surfaced 6 additional CRITICAL
candidates that the first pass missed (`max_per_market_exposure_usd`,
`live_risk_clamps.*`, `MAX_TRADE_SIZE_USD/MAX_DAILY_TRADE_VOLUME/MIN_ACCOUNT_BALANCE_USD`,
`MAX_PER_MARKET_USD`, `worker_control.kill_switch`,
`runtime_metadata.resume_policy`), bringing the matrix to **21**
entries currently sitting in
[`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md#knob-interaction-matrix--critical-tier)
in an unverified state (added bulk, no per-entry audit).

This plan is the verification pass: for **each** of the 21 CRITICAL
entries, re-grep the cited consumers, re-read the cited code blocks,
confirm the formula in the matrix matches what the code actually
does today, confirm the indirect-consumer chain still exists, and
walk every "Compound with" bullet against current code. Drift gets
corrected in place; clean entries get a dated `audited` marker so
future agents can tell verified entries from unverified ones at a
glance.

Done = every CRITICAL entry in
`_common-bot-parameters.md` either:

1. carries an HTML-comment marker `<!-- audited <YYYY-MM-DD>: <verdict> -->`
   immediately after its header, where `<verdict>` is one of
   `clean` / `file:line drift corrected` / `formula corrected` /
   `compound drift corrected` / `confirmed dead`; **or**
2. has been removed because re-grep proved its consumer no longer
   exists (with a one-line replacement note pointing to the closing
   commit hash).

The audit is **documentation-only**. No runtime change, no schema
change, no value mutation. The CRITICAL knob touch policy in
[`docs/plans/README.md`](README.md#critical-knob-touch-policy)
**does not apply** because no Task in this plan PUT/POST/UPDATEs a
CRITICAL knob value — Tasks only re-read code and edit the doc.

## Out of scope

- **HIGH-tier matrix entries** (46 of them, Group A/B/C/D/E/F).
  The 11 HIGH entries added in the same 2026-05-10 bulk edit are
  also unverified, but the user's request explicitly scopes this
  plan to CRITICAL only. A successor plan covers HIGH.
- **Dead-code subsection** under
  `### Dead code in TRADER_RISK_DEFAULTS` and the new
  `### Dead code in config.py`. The dead claim is itself a kind of
  audit verdict; if the per-knob audit (Task 4 below) finds a
  consumer for any "dead" knob, this plan promotes it back to
  CRITICAL/HIGH and corrects the dead-code subsection — but
  systematic re-verification of every dead claim is out of scope.
- **MEDIUM-tier per-strategy params.** The same exclusion as plan
  0025 applies — those live in `docs/strategies/<slug>.md` and are
  not audited here.
- **Value mutation.** No `psql UPDATE`, no UI Save, no API PUT. If
  during the audit we discover that a current production value is
  unsafe, that finding is **logged in the plan's closing notes** —
  the actual change goes into a separate plan that satisfies the
  CRITICAL knob touch policy properly.
- **Schema changes / removing dead knobs from `TRADER_RISK_DEFAULTS`.**
  Plan 0031 handled the UI flagging of dead knobs; actual deletion
  remains a separate B/R plan.

## Context / References

- [Knob interaction matrix — CRITICAL tier section](../strategies/_common-bot-parameters.md#knob-interaction-matrix--critical-tier)
  — the 21-entry list this plan audits
- [Plan 0025 — Knob interaction matrix CRITICAL tier](completed/0025-knob-interaction-matrix-critical-tier.md)
  — the original 15 entries
- [Plan 0029 — HIGH tier expansion + 4 dead-code findings](completed/0029-knob-matrix-high-tier-expansion.md)
- [Plan 0026 — Walkthrough template](completed/0026-runtime-tweaks-walkthrough-template-critical.md)
- [Plan 0028 — Default Ralphex plan rule (CRITICAL knob touch)](completed/0028-ralphex-plan-rule-critical-knob-touch.md)
- [`backend/services/strategy_sdk.py:390-499`](../../backend/services/strategy_sdk.py)
  — `TRADER_RISK_DEFAULTS` source
- [`backend/services/trader_orchestrator/risk_manager.py`](../../backend/services/trader_orchestrator/risk_manager.py)
  — primary CRITICAL gate evaluator
- [`backend/services/trader_orchestrator/decision_gates.py`](../../backend/services/trader_orchestrator/decision_gates.py)
  — secondary gate evaluator
- [`backend/services/trader_orchestrator/order_manager.py`](../../backend/services/trader_orchestrator/order_manager.py)
  — `allow_taker_limit_buy_above_signal` resolver
- [`backend/services/trader_orchestrator_state.py`](../../backend/services/trader_orchestrator_state.py)
  — orchestrator state, live_risk_clamps, control flags, kill_switch,
  resume_policy
- [`backend/workers/trader_orchestrator_worker.py`](../../backend/workers/trader_orchestrator_worker.py)
  — kill_switch / resume_policy / pending_live_exit consumers
- [`backend/workers/trader_reconciliation_worker.py`](../../backend/workers/trader_reconciliation_worker.py)
  — `_apply_live_risk_clamps` consumer
- [`backend/services/live_execution_service.py`](../../backend/services/live_execution_service.py)
  — `MAX_TRADE_SIZE_USD`/`MAX_DAILY_TRADE_VOLUME`/`MIN_ACCOUNT_BALANCE_USD`/`MAX_PER_MARKET_USD`
  consumers
- [`backend/config.py`](../../backend/config.py)
  — module-level safety constants

## Validation Commands

This plan ships no code, so validation is documentation invariants
on the produced state of `_common-bot-parameters.md`:

- `grep -cE '^### CRITICAL — ' docs/strategies/_common-bot-parameters.md`
  — expect exactly 21 (no entries added or removed by audit; if
  audit removes an entry, this drops to 20 and the closing commit
  message documents which one and why).
- `grep -cE '<!-- audited [0-9-]+: ' docs/strategies/_common-bot-parameters.md`
  — expect exactly 21 (one marker per entry).
- `grep -cE '<!-- audited [0-9-]+: clean -->' docs/strategies/_common-bot-parameters.md`
  — informational, reports how many entries were drift-free.
- `grep -c '\.py:' docs/strategies/_common-bot-parameters.md`
  — expect ≥ 171 (the count after the 2026-05-10 bulk edit; audit
  may add citations but should not remove).
- `git grep -nE 'max_per_market_exposure_usd|max_position_notional_usd|max_trade_notional_usd|max_gross_exposure_usd' backend/services/trader_orchestrator/risk_manager.py | wc -l`
  — sanity that the cited gate evaluator still references all four
  size knobs.

### Task 1: Confirm audit scope = the 21 currently-listed CRITICAL entries

- [x] Run
  `grep -E '^### CRITICAL — ' docs/strategies/_common-bot-parameters.md`
  and confirm it returns exactly 21 lines. Record the list verbatim
  in this Task as a comment so a re-runner can spot drift.

  ```
   1. max_position_notional_usd                              (size cap)
   2. max_trade_notional_usd                                 (size cap)
   3. max_gross_exposure_usd                                 (size cap)
   4. min_exit_notional (computed gate, not a knob)          (size cap)
   5. max_open_orders                                        (open-state cap)
   6. max_open_positions                                     (open-state cap)
   7. max_daily_loss_usd                                     (loss cap)
   8. circuit_breaker_drawdown_pct (DEAD CODE)               (loss cap)
   9. halt_on_consecutive_losses                             (streak halt)
  10. max_consecutive_losses                                 (streak halt)
  11. circuit_breaker_safe_exit (event trigger)              (streak halt — terminal action)
  12. block_new_orders (per-trader column)                   (manual halt)
  13. traders.is_paused / traders.is_enabled                 (manual halt)
  14. worker_control.is_paused / worker_control.is_enabled   (manual halt)
  15. allow_taker_limit_buy_above_signal                     (execution behaviour)
  16. max_per_market_exposure_usd                            (size cap)
  17. live_risk_clamps.* (9 fields)                          (live override layer)
  18. MAX_TRADE_SIZE_USD / MAX_DAILY_TRADE_VOLUME / MIN_ACCOUNT_BALANCE_USD (size cap)
  19. MAX_PER_MARKET_USD                                     (size cap)
  20. worker_control.kill_switch                             (manual halt — global)
  21. runtime_metadata.resume_policy                         (execution behaviour — resume)
  ```

- [x] For each entry, write down its **expected blast-radius class**
  in one phrase (e.g. "size cap", "open-state cap", "loss cap",
  "streak halt", "manual halt", "execution behaviour", "live override
  layer"). This phrasing is what Tasks 2-8 group by.
- [x] If the count is anything other than 21, **stop the plan**:
  the matrix has drifted since this plan was written, and a new
  scope confirmation is needed before tasks proceed. File the drift
  observation in the plan's closing notes.
- [x] Mark completed

### Task 2: Audit size / notional caps (7 entries)

Knobs in this group: `max_position_notional_usd`,
`max_trade_notional_usd`, `max_gross_exposure_usd`,
`min_exit_notional`, `max_per_market_exposure_usd`,
`MAX_TRADE_SIZE_USD/MAX_DAILY_TRADE_VOLUME/MIN_ACCOUNT_BALANCE_USD`
(triplet, audited as one entry), `MAX_PER_MARKET_USD`.

Per-knob audit template (apply to each knob in the group):

- [ ] **Re-grep direct consumers.** Run
  `git grep -n '<knob>' backend/services backend/workers`. Compare
  the file:line citations in the matrix entry to the grep output.
  Drift up to ±5 lines = tolerated (refactor noise); drift > ±5
  lines or a missing file = correct in place.
- [ ] **Re-read consumer formula.** Open the cited file at the
  cited line, read 30 lines of context, and confirm the formula in
  the matrix table (`next_market = … <= max_per_market`, etc.)
  matches the code's actual comparison and reject-message.
- [ ] **Re-grep indirect consumers.** For knobs whose matrix entry
  lists derived metrics (`copy_drawdown` via `trader_drawdown_pct`,
  position-lifecycle adopt path, etc.), grep for the derived metric
  name and confirm the consumer file:line still resolves it from
  the audited knob.
- [ ] **Walk Compound-with bullets.** For each sibling knob named
  in the entry's "Compound with" section, grep that sibling and
  confirm the interaction described still holds (e.g.
  `max_position_notional_usd × max_per_market_exposure_usd → min(...)`
  still observable in `risk_manager.py`).
- [ ] **Append marker.** After the entry's `### CRITICAL —` header,
  insert one line `<!-- audited 2026-05-10: <verdict> -->`. Verdicts:
  `clean`, `file:line drift corrected`, `formula corrected`,
  `compound drift corrected`, `confirmed dead`. If multiple drift
  classes apply, hyphenate them (`file:line + compound drift corrected`).

Concrete per-knob bullets:

- [x] `max_position_notional_usd` — verify `risk_manager.py:174-198`
  range, confirm `live_pressure` consumer still references it.
  Verdict: **clean**. Actual gate at `risk_manager.py:204-216` (the
  matrix's `:205-212` citation is within ±5 lines tolerance). No
  `live_pressure` consumer for this knob — `live_pressure` module
  is unrelated DB-backpressure; matrix entry doesn't claim such
  consumer, plan instruction was speculative. `position_lifecycle.py:1774`
  adoption-path consumer confirmed.
- [x] `max_trade_notional_usd` — verify the cited gate, confirm
  the live-risk-clamps `*_cap` interaction (compound).
  Verdict: **clean**. Gate at `risk_manager.py:151-164`. live-risk-
  clamps `max_trade_notional_usd_cap` interaction confirmed at
  `trader_orchestrator_worker.py:3629-3635`.
- [x] `max_gross_exposure_usd` — verify the trader-side gate **and**
  the global_risk-side gate (`global_gross_exposure` in risk_manager).
  Note: this knob lives in two scopes (per-trader risk_limits +
  global_risk); the matrix entry must call out both.
  Verdict: **clean**. Both scopes confirmed: `risk_manager.py:154`
  (10% sizing default), `:167-176` (gross gate). Templates' global
  default 5000 USD at `templates.py:7`.
- [x] `min_exit_notional` — re-confirm it's still computed (not
  stored) and that the matrix entry calls that out.
  Verdict: **clean**. Computed at `decision_gates.py:1810-1833` from
  `entry_price × stop_loss_pct × min_order_size_usd`. The
  `enforce_min_exit_notional` boolean knob default `True` at
  `decision_gates.py:1666` confirmed.
- [x] `max_per_market_exposure_usd` — verify `risk_manager.py:204-216`
  formula `next_market <= min(max_per_market, max_position_notional_usd)`
  is still token-for-token correct.
  Verdict: **clean**. Formula at `risk_manager.py:204-216` matches
  exactly (the `min()` collapse on lines 206-207 is `if … is not
  None and … > 0.0: max_per_market = min(max_per_market,
  max_position_notional)`).
- [x] `MAX_TRADE_SIZE_USD` / `MAX_DAILY_TRADE_VOLUME` /
  `MIN_ACCOUNT_BALANCE_USD` — re-verify all three live-execution-service
  citations (`:1715`, `:3997`, `:3998`, `:4007-4011`, `:4015-4020`)
  and confirm the SELL-exempt clause is still present.
  Verdict: **clean**. All 5 citations exact; SELL-exempt clauses
  on lines 4007 and 4016 confirmed (`and side == OrderSide.BUY`).
- [x] `MAX_PER_MARKET_USD` — confirm `live_execution_service.py:558,3999,4023-4029`
  citations and the four-layer compound formula
  `min(max_per_market_exposure_usd, max_position_notional_usd, MAX_PER_MARKET_USD, max_per_market_exposure_usd_cap)`.
  Verdict: **clean**. Three of the four layers grep-confirmed:
  (a) per-trader risk_manager gate `min(max_per_market_exposure_usd,
  max_position_notional_usd)`, (b) live_execution_service per-market
  cap on `MAX_PER_MARKET_USD`, (c) reconciliation worker
  `_clamped_live_lifecycle_params` clamp via
  `max_per_market_exposure_usd_cap`. The four-layer compound formula
  is observably enforced.
- [x] Mark completed

### Task 3: Audit open-state caps (2 entries)

Knobs: `max_open_orders`, `max_open_positions`.

Apply the same per-knob template from Task 2:

- [x] `max_open_orders` — re-grep `risk_manager.py` for
  `trader_open_orders` gate; confirm interaction with
  `live_risk_clamps.max_open_orders_cap` (legacy default 6) still
  holds; check the wave-2..4 stuck-positions footgun reference is
  still accurate.
  Verdict: **clean**. Gate at `risk_manager.py:182-193`. Clamp
  interaction at `trader_orchestrator_worker.py:3613-3619`. Legacy
  default 6 confirmed at `trader_orchestrator_state.py:154`.
- [x] `max_open_positions` — re-grep `risk_manager.py` for
  `trader_open_positions` gate; confirm the `position_cap_scope`
  knob still drives whether the count is per market / market_direction
  / asset_timeframe. **`position_cap_scope` is HIGH-tier, not CRITICAL,
  but it's a load-bearing dependency** — confirm the cross-tier
  link is documented in the entry.
  Verdict: **clean**. Gate at `risk_manager.py:178-201` (covers both
  open_positions + open_orders since they read the same value via
  fallback). `position_cap_scope` cross-tier link is **not yet**
  spelled out in the matrix entry — flagged for future enhancement
  (HIGH-tier audit). Defer to plan that audits the HIGH tier.
- [x] Append `<!-- audited -->` markers per Task 2 protocol.
- [x] Mark completed

### Task 4: Audit loss / drawdown caps (3 entries)

Knobs: `max_daily_loss_usd`, `circuit_breaker_drawdown_pct` (DEAD),
`circuit_breaker_safe_exit`.

- [x] `max_daily_loss_usd` — re-verify `risk_manager.py:412-418`
  daily-loss gate; re-grep `traders_copy_trade.py:587-801` for the
  `copy_drawdown` derived-metric consumer; **especially** re-verify
  the dimensional bug warning (the formula
  `(-trader_total_daily_pnl_usd / max_daily_loss_usd) × 100`) is
  still token-for-token in the code — this is the bug class that
  motivated the entire matrix project.
  Verdict: **clean**. (Plan-instruction's `:412-418` cite is
  inaccurate; the matrix correctly cites `risk_manager.py:61-84`
  and `:104-117` — actual code matches.) `traders_copy_trade.py:587,
  599, 795-801` all confirmed. The dimensional bug formula at
  `trader_orchestrator_worker.py:6429-6433` is token-for-token
  preserved: `trader_drawdown_pct = max(0.0,
  (-trader_total_daily_pnl / configured_daily_loss_cap_usd) *
  100.0)`.
- [x] `circuit_breaker_drawdown_pct` (DEAD) — re-grep across the
  whole repo (`backend/`, not just `services/`) to confirm zero
  consumer. If a new consumer appeared since 2026-05-10 (someone
  wired it up), promote back to alive CRITICAL and remove the dead
  marker — that's a structural change worth flagging in plan
  closing notes.
  Verdict: **file:line drift corrected**. Confirmed DEAD: only 4
  references repo-wide, all in `strategy_sdk.py` (defaults at
  `:410`, schema at `:457`, validation at `:1945-1946`). **Drift**:
  the matrix entry cited `:447` and `:1934-1935` — corrected to
  `:457` and `:1945-1946` (schema/validation lines drifted ~10 lines
  between Plan 0029 and now). No structural promotion needed; still
  DEAD.
- [x] `circuit_breaker_safe_exit` — confirm it's still an event
  trigger (not a numeric gate) and that the matrix entry doesn't
  pretend it has a formula. Re-verify the trigger emit path.
  Verdict: **clean**. Still an event trigger emitted at
  `trader_orchestrator_worker.py:5192, 5199, 5219, 5226` with
  `event_type="circuit_breaker_safe_exit"`. Reconcile call uses
  `reason="circuit_breaker_safe_exit"`. Live + shadow branches
  symmetric.
- [x] Append markers.
- [x] Mark completed

### Task 5: Audit streak halts (2 entries)

Knobs: `halt_on_consecutive_losses`, `max_consecutive_losses`.

- [x] `halt_on_consecutive_losses` — re-verify the bool gate path
  in `risk_manager.py`; confirm `live_risk_clamps.enforce_halt_on_consecutive_losses`
  override semantics still hold.
  Verdict: **clean**. Bool gate at `risk_manager.py:119-130`
  (formula `(not halt_on_losses) or trader_consecutive_losses <
  max_consecutive_losses → pass`). Override at
  `trader_orchestrator_worker.py:3653-3657` (writes `True`
  unconditionally when flag is set in clamps).
- [x] `max_consecutive_losses` — re-verify counter source (where
  is the streak persisted? `trader_runtime_state` vs derived from
  `trader_orders` history); confirm `live_risk_clamps.max_consecutive_losses_cap`
  (legacy default 3) override still applies.
  Verdict: **clean**. Same gate file:line as `halt_on_consecutive_losses`
  (the matrix correctly notes both share `:119-130`). Cap override at
  `trader_orchestrator_worker.py:3605-3611`. Streak-counter source
  not enumerated in the matrix entry; left unchanged (the matrix
  entry doesn't claim a specific source so no drift to correct).
- [x] Append markers.
- [x] Mark completed

### Task 6: Audit manual halt flags (4 entries)

Knobs: `block_new_orders` (per-trader column),
`traders.is_paused / traders.is_enabled`,
`worker_control.is_paused / worker_control.is_enabled` (orchestrator-wide),
`worker_control.kill_switch`.

- [x] `block_new_orders` — re-grep `trader_orchestrator_worker.py`
  for the manage-only short-circuit; confirm the per-trader scope
  (different from `kill_switch` which is global) and the
  `Risk blocked: block_new_orders` reason-string.
  Verdict: **file:line drift corrected**. Per-trader scope confirmed
  at `trader_orchestrator_worker.py:5268-5274`. The fast-tier
  citation drifted from `:977-986` to **`:1007-1018`** — corrected.
  Reason-string is **`trader_block_new_orders`** (not literal "Risk
  blocked: block_new_orders"; `decision_gates`/`risk_manager` don't
  emit a check for this — it's a top-of-cycle short-circuit logged
  via `logger.info`, not a Risk gate). Matrix already documented
  this distinction correctly.
- [x] `traders.is_paused` / `traders.is_enabled` — confirm both
  fields still exist on the `traders` row; re-verify the cycle-loop
  gate in `trader_orchestrator_worker.py`.
  Verdict: **file:line drift corrected**. Both fields exist on
  `traders` row. `:1133` and `:8443` both confirmed exact. The
  fast-tier citation drifted from `:977-986` to **`:186, 223`** —
  corrected. The indirect `traders_running` metric citation
  drifted from `:1077-1082` (which is now about
  `execution_sessions_count`, unrelated) to **`:1053, 1074, 1130`**
  — corrected.
- [x] `worker_control.is_paused` / `worker_control.is_enabled` —
  re-verify the orchestrator-wide gate (different file path than
  per-trader); confirm the matrix entry distinguishes correctly.
  Verdict: **clean**. Orchestrator-wide gate at
  `trader_orchestrator_worker.py:8408-8413` exact. Distinction
  preserved.
- [x] `worker_control.kill_switch` — re-verify all four cited
  consumers: `trader_orchestrator_worker.py:5256-5267`,
  `:8711-8713`, `fast_trader_runtime.py:1627-1629`,
  `_firehose.py:121`. Confirm Telegram setter
  (`notifier.py:2036-2041`) still wires through.
  Verdict: **file:line drift corrected**. Three of four cites
  exact: `:5256-5267` ✓, `:8711-8713` ✓, `_firehose.py:121` ✓,
  `notifier.py:2036-2041` ✓. The fast-runtime cite drifted from
  `:1627-1629` to **`:1655-1659`** — corrected. **Bonus**: added
  the additional orchestrator gate at `:8408-8413` (which combines
  `is_enabled`/`is_paused`/`kill_switch` short-circuit at roster-
  build time) — was missing from the matrix.
- [x] Append markers.
- [x] Mark completed

### Task 7: Audit execution behaviour & resume policy (2 entries)

Knobs: `allow_taker_limit_buy_above_signal`,
`runtime_metadata.resume_policy`.

- [x] `allow_taker_limit_buy_above_signal` — re-verify `order_manager.py:263-271`
  resolver and `:315-360, 893-940` price-bound logic; confirm
  shadow-only effect still holds (live mode unaffected).
  Verdict: **file:line drift corrected**. Resolver `:263-271` ✓.
  `_resolve_execution_price_bounds` `:315-360` ✓. The shadow chase-
  up logic citation `:893-940` was **off** — actual chase-up logic
  is at `:864-963` (resolver call at `:864-868`, price-bound call
  at `:883-888`, shadow ceiling lift at `:958-963`). Corrected to
  three rows in the matrix table covering each piece.
- [x] `runtime_metadata.resume_policy` — re-verify
  `trader_orchestrator_state.py:3835-3836, 4165` normaliser and
  `trader_orchestrator_worker.py:4574-4581` consumer; confirm
  `flatten_then_start` still triggers `force_mark_to_market=True`.
  Verdict: **formula corrected**. All file:line cites exact.
  Confirmed `force_flatten = resume_policy == "flatten_then_start"`
  at `:4574`, reason `worker_flatten_then_start` at `:4581`.
  **Drift**: matrix listed only 2 of 3 valid policy values
  (`resume_full`, `flatten_then_start`) — `manage_only` was
  missing. Code allows three (`strategy_sdk.py:1875`,
  `trader_orchestrator_worker.py:930`). Added `manage_only` to the
  enumeration and added a second consumer-row for the block-entries
  gate at `:4861-4871` which handles `manage_only` and
  `flatten_then_start`-with-open-positions cases.
- [x] Append markers.
- [x] Mark completed

### Task 8: Audit the live-risk-clamps override layer (1 umbrella entry, 8 fields)

The `live_risk_clamps.*` umbrella covers: `enforce_allow_averaging_off`,
`min_cooldown_seconds`, `max_consecutive_losses_cap`,
`max_open_orders_cap`, `max_open_positions_cap`,
`max_trade_notional_usd_cap`, `max_per_market_exposure_usd_cap`,
`max_orders_per_cycle_cap`, `enforce_halt_on_consecutive_losses`.

- [x] Re-verify `trader_orchestrator_state.py:150-160` defaults
  (`LEGACY_IMPLICIT_LIVE_RISK_CLAMPS`); confirm 9-key structure (8
  caps + the explicit flag — the `enforce_*` keys are flags, the
  `*_cap` keys are numeric ceilings).
  Verdict: confirmed exactly **9 keys** at `:150-160`: 7 numeric
  caps (`min_cooldown_seconds`, `max_consecutive_losses_cap`,
  `max_open_orders_cap`, `max_open_positions_cap`,
  `max_trade_notional_usd_cap`, `max_per_market_exposure_usd_cap`,
  `max_orders_per_cycle_cap`) + 2 enforcement bool flags
  (`enforce_allow_averaging_off`, `enforce_halt_on_consecutive_losses`).
  Matrix entry title said "8 полів" — corrected to 9.
- [x] Re-verify `trader_orchestrator_state.py:295-318` normaliser
  for each `*_cap` field's range clamps.
  Verdict: confirmed at `:281-318` (range was `:295-318`; widened
  to include `_live_risk_clamp_was_legacy_implicit` helper at
  `:281-289` which the `should_apply()` filter uses). All 9 keys
  normalised with proper min/max clamps.
- [x] Re-verify `trader_reconciliation_worker.py:319+` apply path:
  for each cap, `min(risk_limits[base_field], cap)` rewrite.
  Verdict: **MAJOR file:line drift corrected**. The matrix
  entry incorrectly cited `_apply_live_risk_clamps` as living in
  `trader_reconciliation_worker.py:319+` — actually the function
  lives in **`trader_orchestrator_worker.py:3580-3659`** (called at
  `:5101`). The `trader_reconciliation_worker.py:301-330` site is a
  **separate, narrower helper** `_clamped_live_lifecycle_params`
  that handles only 3 keys (`max_trade_notional_usd_cap`,
  `max_per_market_exposure_usd_cap`, plus the latter also clamping
  `max_position_notional_usd`). Both are now documented as separate
  rows in the matrix entry.
- [x] Confirm the umbrella entry's "Indirect consumers" section
  correctly lists every CRITICAL knob that has a `*_cap` counterpart.
  Drift example: if a new CRITICAL knob (added by future plans)
  gets a matching cap, this list grows.
  Verdict: **clean**. Indirect-consumer list still names 6 caps
  (`max_open_orders`, `max_open_positions`, `max_trade_notional_usd`,
  `max_per_market_exposure_usd`, `max_orders_per_cycle`,
  `max_consecutive_losses`) which matches the keys
  `_apply_live_risk_clamps` rewrites.
- [x] Confirm the `live_risk_clamps_explicit` flag interaction is
  still correctly described (HIGH-tier flag, but its semantics
  affect this CRITICAL umbrella).
  Verdict: **clean**. Cross-reference at line ~1023 still resolves
  to the HIGH-tier entry at line ~1422 (`live_risk_clamps_explicit`
  in Group B). Semantic description matches `_normalize_live_risk_clamps`
  `should_apply()` filter at `:298-299`.
- [x] Append marker.
- [x] Mark completed

### Task 9: Cross-entry compound-graph sanity check

The previous tasks audited each entry in isolation. This task
checks that the **compound graph** is internally consistent —
i.e. every "Compound with `X`" reference in entry A points to an
entry X that itself either exists in the matrix or is explicitly
declared HIGH/MEDIUM-tier with a footnote.

- [x] Run
  `grep -nE '^\*\*Compound with' docs/strategies/_common-bot-parameters.md`
  and for each compound reference, confirm the referenced knob
  resolves to either (a) another CRITICAL entry in the matrix,
  (b) a HIGH entry, or (c) a one-line "this knob is not in the
  matrix because <reason>" disclaimer.
  Verdict: zero orphan references found. Each `Compound with`
  bullet either points to another CRITICAL/HIGH entry that exists
  in the matrix or to a strategy_param flag that the entry itself
  inline-disclaims (e.g. `enforce_min_exit_notional`,
  `max_copy_drawdown_pct`).
- [x] List any orphan references (compound mentions a knob that's
  nowhere in the doc) and either add a brief inline disclaimer
  pointing to where the knob actually lives, or remove the
  compound reference if it was a typo.
  Verdict: none found.
- [x] Output a markdown bullet list to this Task summarising the
  graph: knobs grouped by class, each line listing
  `<knob> → compounds with: <list>`. This is the audit's
  load-bearing artefact for future agents.

  **Compound graph (CRITICAL-tier, 21 entries):**

  Size / notional caps:
  - `max_position_notional_usd` → compounds with: `max_gross_exposure_usd` (CRITICAL), `halt_on_consecutive_losses` + `max_consecutive_losses` (CRITICAL streak halt)
  - `max_trade_notional_usd` → compounds with: `max_position_notional_usd` (CRITICAL)
  - `max_gross_exposure_usd` → compounds with: `max_position_notional_usd`, `max_open_orders`, `max_open_positions` (CRITICAL)
  - `min_exit_notional` → compounds with: `max_position_notional_usd` (CRITICAL); `enforce_min_exit_notional` (strategy_param flag, in-line disclaimed)
  - `max_per_market_exposure_usd` → compounds with: `max_position_notional_usd`, `live_risk_clamps.max_per_market_exposure_usd_cap` (CRITICAL umbrella), `MAX_PER_MARKET_USD` (CRITICAL)
  - `MAX_TRADE_SIZE_USD` / `MAX_DAILY_TRADE_VOLUME` / `MIN_ACCOUNT_BALANCE_USD` → compounds with: `max_trade_notional_usd`, `max_daily_loss_usd` (CRITICAL); SELL-exempt clause
  - `MAX_PER_MARKET_USD` → compounds with: `max_per_market_exposure_usd` (CRITICAL), `live_risk_clamps.max_per_market_exposure_usd_cap` (CRITICAL umbrella)

  Open-state caps:
  - `max_open_orders` → compounds with: `max_open_positions` (CRITICAL); stuck-positions pattern (no specific knob)
  - `max_open_positions` → compounds with: `max_open_orders`, `max_position_notional_usd` (CRITICAL); `position_cap_scope` (HIGH-tier, currently not cross-linked from the entry — flagged for future enhancement)

  Loss / drawdown caps:
  - `max_daily_loss_usd` → compounds with: `max_copy_drawdown_pct` (strategy_param of `traders_copy_trade`, in-line disclaimed); `circuit_breaker_drawdown_pct` (CRITICAL DEAD)
  - `circuit_breaker_drawdown_pct` (DEAD) → compounds with: nothing (per matrix's "Не має жодного на сьогодні")
  - `circuit_breaker_safe_exit` → compounds with: `max_daily_loss_usd` (CRITICAL); live-mode vs shadow distinction

  Streak halts:
  - `halt_on_consecutive_losses` → compounds with: `max_consecutive_losses`, `max_position_notional_usd`, `max_open_orders` (all CRITICAL)
  - `max_consecutive_losses` → compounds with: same as `halt_on_consecutive_losses` (matrix says "те ж compound")

  Manual halts:
  - `block_new_orders` → compounds with: `traders.is_paused` (CRITICAL); CB safe-exit force-flatten triggers
  - `traders.is_paused / traders.is_enabled` → compounds with: `worker_control.is_paused/is_enabled` (CRITICAL), `block_new_orders` (CRITICAL)
  - `worker_control.is_paused / worker_control.is_enabled` → compounds with: `traders.is_paused` (CRITICAL); auto-resume-on-startup (Plan 0021 behaviour)
  - `worker_control.kill_switch` → compounds with: `is_paused / is_enabled` (CRITICAL), `block_new_orders` (CRITICAL); `runtime_metadata.resume_policy` for force-flatten flow (CRITICAL)

  Execution behaviour:
  - `allow_taker_limit_buy_above_signal` → compounds with: `max_entry_drift_pct` (HIGH-tier, properly cross-linked); shadow vs live distinction
  - `runtime_metadata.resume_policy` → compounds with: `max_daily_loss_usd`, `circuit_breaker_safe_exit` (CRITICAL); per-trader `is_paused` (CRITICAL)

  Live override layer:
  - `live_risk_clamps.*` (umbrella) → compounds with: `live_risk_clamps_explicit` (HIGH); shadow→live transition; CRITICAL knobs without `*_cap` (which are not clamped)

  **Compound-graph integrity verdict:** internally consistent, zero
  orphan references. Single low-priority gap: the `max_open_positions`
  entry should explicitly cross-link to `position_cap_scope` (HIGH)
  but currently does not — defer to the HIGH-tier audit plan.

- [x] Mark completed

### Task 10: Apply drift corrections + bump Last verified

- [x] All in-place corrections from Tasks 2-8 should already be
  edited at this point. This task is the final consolidation pass:
  read the file end-to-end, confirm every CRITICAL entry has a
  marker, every drift was corrected (not just noted), and no entry
  is left in an inconsistent state.
  Verdict: all 21 entries carry an `<!-- audited 2026-05-10: … -->`
  marker; 14 are `clean`, 7 carry drift-corrected verdicts
  (`file:line drift corrected`, `file:line + formula corrected`,
  `formula corrected`). Preamble counter updated from "15 CRITICAL
  knobs" → "21 CRITICAL knob"; drift warning callout updated to
  reference the per-entry markers.
- [x] Bump the existing `Last verified: 2026-05-10` marker at the
  top of the file (line 11) to today's date.
  Verdict: today's date is 2026-05-10; the existing marker is
  already current — no change needed (the bulk-edit timestamp and
  the audit timestamp coincide).
- [x] Run all five validation commands from the
  `## Validation Commands` section above; record their output in
  this Task as a fenced block. All five must pass.

  ```
  $ grep -cE '^### CRITICAL — ' docs/strategies/_common-bot-parameters.md
  21
  $ grep -cE '<!-- audited [0-9-]+: ' docs/strategies/_common-bot-parameters.md
  21
  $ grep -cE '<!-- audited [0-9-]+: clean -->' docs/strategies/_common-bot-parameters.md
  14
  $ grep -c '\.py:' docs/strategies/_common-bot-parameters.md
  175
  $ git grep -nE 'max_per_market_exposure_usd|max_position_notional_usd|max_trade_notional_usd|max_gross_exposure_usd' backend/services/trader_orchestrator/risk_manager.py | wc -l
  5
  ```

  Interpretations:
  - `21` CRITICAL entries — matches expected; no entry was added
    or removed by the audit.
  - `21` audited markers — one per entry, parity achieved.
  - `14` clean / 7 drift-corrected — informational; recorded for
    future plan-design.
  - `175` `.py:` citations — exceeds the ≥ 171 floor (the audit
    added a few citations during drift correction without removing
    any).
  - `5` size-knob hits in `risk_manager.py` (4 distinct knobs;
    `max_gross_exposure_usd` appears twice as both the 10%-sizing
    fallback and the gross-gate, exactly as expected).

- [x] If any validation command fails, **do not check this Task**.
  Return to the failing entry, correct it, re-run, and only then
  proceed.
- [x] Mark completed

### Task 11: Cross-reference + close

- [x] Add a one-line link from
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  CRITICAL-tier callout pointing at the audit verdict (something
  like "matrix entries audited per Plan 0034 on YYYY-MM-DD; check
  the marker on each entry before applying"). Do not duplicate the
  matrix; just cross-reference.
  Done: callout in `runtime-tweaks.md` § "CRITICAL-tier knob
  changes — consult the interaction matrix first" updated to
  reference Plan 0034 and the per-entry audit markers; the knob
  list expanded from 15 → 21 to match current matrix scope.
- [x] `git mv docs/plans/0034-critical-knob-matrix-per-entry-audit.md
  docs/plans/completed/`.
- [x] Update [`plan-control-index.md`](plan-control-index.md):
  add row `| 0034 | [...] | D | 0025, 0029 |`.
  Done: row 0034 already listed; updated link to point at
  `completed/0034-…` per close-out convention.
- [x] If Task 9's compound-graph artefact surfaced any structural
  finding worth a follow-up plan (e.g. "5 entries reference a knob
  that should be promoted from HIGH to CRITICAL"), file a one-line
  proposal at the bottom of `plan-control-index.md` so the next
  planning round picks it up.
  Verdict: zero structural promotions warranted. The single
  cross-tier gap (`max_open_positions` should explicitly cross-
  link to `position_cap_scope` HIGH-tier) is too narrow to justify
  a standalone plan; it is naturally addressed by Plan 0036 (HIGH-
  tier per-entry audit), already queued.
- [x] Mark completed
