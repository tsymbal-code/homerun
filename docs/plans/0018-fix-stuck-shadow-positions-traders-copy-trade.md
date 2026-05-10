# Plan: Fix stuck shadow positions on `traders_copy_trade`

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0018` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

The `Sandbox - Traders Copy Trade` bot (trader_id
`61dcbeb2b9bc42bd9e9635a09ae5e0c3`, mode=`shadow`) accumulated 120+
`trader_positions.status='open'` rows across 36 hours. Operator-side
spot-check on Polymarket showed many of those underlying markets are
**already resolved** (UFC 328, BTC 5-min binary, LoL NIP-vs-IG,
etc.). Reconciliation is running, but cannot drain.

Investigation surfaced **four compounding defects**, all on the
`source='traders'` shadow path. This plan fixes them in dependency
order — least invasive first, so each step can be observed in
production before the next lands.

### Defects

1. **`polymarket.get_market_by_condition_id` filters out resolved
   markets.** The gamma `/markets?condition_ids=...` call at
   [`backend/services/polymarket.py:969`](../../backend/services/polymarket.py:969)
   does not pass `closed=true`. Gamma defaults to
   `closed=false` and returns an empty list for resolved markets.
   Downstream consumers receive `market_info=None`, the shadow
   reconciler in
   [`position_lifecycle.reconcile_shadow_positions`](../../backend/services/trader_orchestrator/position_lifecycle.py)
   can't extract `winning_outcome_index`, and the position sits open
   forever. Affects every strategy that asks gamma for a resolved
   market by condition_id, but bites hardest on `traders_copy_trade`
   because it is the only source that opens positions on
   short-fuse markets (5-minute crypto, in-play sports).

2. **`traders_copy_trade` emits `direction='buy'` for non-YES/NO
   outcomes.** [`backend/services/strategies/traders_copy_trade.py:499-503`](../../backend/services/strategies/traders_copy_trade.py:499)
   explicitly fills the `direction` field with the literal `"buy"`
   when the outcome label is not `YES`/`NO`. This bypasses the
   `_resolve_leg_direction` fallback in
   [`session_engine.py:128`](../../backend/services/trader_orchestrator/session_engine.py:128)
   that would otherwise build `buy_yes`/`buy_no` from
   `(side, outcome)`. The literal `"buy"` is a dead end for every
   downstream consumer — see defects 3a and 3b. Also, the upstream
   resolver that decides what `outcome` to pass is too quick to
   give up on the YES/NO normalisation: most "categorical" events
   on Polymarket are actually N separate binary markets, and the
   leader trade *does* land on a binary market with a `["Yes","No"]`
   outcome list. `_resolve_market_snapshot` /
   `_process_wallet_trade_event` in
   [`backend/services/traders_copy_trade_signal_service.py`](../../backend/services/traders_copy_trade_signal_service.py)
   should normalise the outcome label by token-position
   (`tokens.index(token_id) → outcomes[idx]`) before falling back
   to the raw label.

3. **Two downstream layers reject anything outside
   `{buy_yes, buy_no}`.** Even after defect 2 is fixed, true
   multi-outcome single-market structures (rare — UFC fighter
   outright, LoL series) still need a code path:

   - **3a.** `simulation._direction_to_position_side`
     ([`backend/services/simulation.py:55-62`](../../backend/services/simulation.py:55))
     raises `ValueError("Unsupported direction '...'")`. This is the
     proximate cause of the `shadow_ledger_backfill_failed` warn
     event firing 1,451× over 36 hours on the Sandbox bot.
   - **3b.** `position_lifecycle._direction_outcome_index`
     ([`backend/services/trader_orchestrator/position_lifecycle.py:879-885`](../../backend/services/trader_orchestrator/position_lifecycle.py:879))
     returns `None` for everything outside `buy_yes`/`buy_no`. This
     blocks reverse-entry, position-state tracking, and resolution
     mark-to-market for the affected orders.

   Defensive widening that resolves outcome index from
   `payload.token_id` against `market.tokens[]` is the right shape:
   token_id is already authoritative at the CLOB submit path
   (live submit succeeds even for `direction='buy'` because
   `order_manager._resolve_token_id_for_leg` reads `outcome`/`token_id`,
   not `direction`).

4. **`shadow_ledger_backfill_failed` stays at `severity='warn'`
   indefinitely.** 1,451 occurrences in 36 hours produced no UI
   alarm or operator paging. Either rate-limit + escalate to
   `error` after N occurrences in a window, or flip the severity
   floor for this specific event-key once seen ≥ 50× in any
   trader's recent history. Without escalation, the same class of
   bug will hide for the next regression.

### What "done" looks like

- All 124 stuck `trader_orders.status='open'` on the Sandbox bot
  drain to terminal status (`closed_win`/`closed_loss`/`resolved_*`)
  within 2 reconciliation cycles after defects 1+3 land — without
  any manual SQL.
- Sandbox bot opens fresh copy-trade positions and the
  open-positions count tracks the underlying Polymarket market
  state (within 60 s of resolution).
- `shadow_ledger_backfill_failed` event count over rolling 1-hour
  window stays in single digits during steady-state operation.
- New regression tests pin the gamma `closed=true` fallback, the
  `direction='buy'` widening in simulator + lifecycle, and the
  `_resolve_leg_direction` fallback on a stripped `direction=""` leg.
- [`docs/strategies/traders-copy-trade.md`](../strategies/traders-copy-trade.md)
  and [`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md)
  describe the post-fix invariant; outcome-normalisation note added
  to [`copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md).

## Context / References

- Investigation memo: this conversation's findings on stuck shadow
  positions (Sandbox bot, 2026-05-10).
- Existing arch notes: [`copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md),
  [`trader-pipeline.md`](architecture/trader-pipeline.md),
  [`execution-and-fills.md`](architecture/execution-and-fills.md).
- Strategy doc: [`docs/strategies/traders-copy-trade.md`](../strategies/traders-copy-trade.md).
- Common patterns: [`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md)
  § "Як стратегії емітять `positions_to_take`".
- Code:
  - [`backend/services/polymarket.py:934-1015`](../../backend/services/polymarket.py)
  - [`backend/services/strategies/traders_copy_trade.py:499-503`](../../backend/services/strategies/traders_copy_trade.py)
  - [`backend/services/traders_copy_trade_signal_service.py`](../../backend/services/traders_copy_trade_signal_service.py)
  - [`backend/services/simulation.py:55-62`](../../backend/services/simulation.py)
  - [`backend/services/trader_orchestrator/position_lifecycle.py:879-885`](../../backend/services/trader_orchestrator/position_lifecycle.py)
  - [`backend/services/trader_orchestrator/session_engine.py:128-139`](../../backend/services/trader_orchestrator/session_engine.py)

## Validation Commands

- `cd backend && pytest tests/test_polymarket_client.py -q`
- `cd backend && pytest tests/test_traders_copy_trade_signal_service.py -q`
- `cd backend && pytest tests/test_traders_copy_trade_strategy.py -q`
- `cd backend && pytest tests/test_simulation_orchestrator_ledger.py -q`
- `cd backend && pytest tests/test_trader_position_lifecycle_resolution.py -q`
- `cd backend && pytest tests/test_execution_session_engine.py -q`
- `cd backend && pytest tests/test_trader_orchestrator_shadow_backfill.py -q`
- `cd backend && ruff check`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "SELECT count(*) FROM trader_orders WHERE trader_id='\''61dcbeb2b9bc42bd9e9635a09ae5e0c3'\'' AND status='\''open'\'' AND first_order_at < now() - interval '\''2 hours'\''"'` — must return 0 within 2 reconciliation cycles after Task 3 lands.

## Order of operations

Tasks land in this order so each landing reduces the in-flight
problem by the largest visible amount:

1. **Task 1 (gamma `closed=true` fallback)** — unblocks the
   majority of the stuck `buy_yes`/`buy_no` orders on already-resolved
   markets. Expected: ~half of the 124 stuck orders drain on the
   next reconcile cycle.
2. **Task 2 (signal-service outcome normalisation)** — stops
   producing `direction='buy'` for the common categorical case
   (each candidate is its own binary market). Going forward, new
   copy-trade signals get `buy_yes`/`buy_no` correctly.
3. **Task 3 (defensive widening in simulator + lifecycle)** —
   handles existing in-flight orders that already shipped with
   `direction='buy'`, plus the residual real multi-outcome edge
   case. After this lands, the remaining stuck orders drain.
4. **Task 4 (strip explicit `direction='buy'` from strategy emit)** —
   alignment fix; strategy now relies on `_resolve_leg_direction`
   fallback like every other strategy. Future-proofs against the
   next regression.
5. **Task 5 (severity escalation for `shadow_ledger_backfill_failed`)** —
   operator visibility. Lands last so we don't drown in alerts
   while the queue is still draining.
6. **Task 6 (post-fix verification + doc updates)** — confirm the
   drain completes, bump architecture-note `Last verified`, archive
   plan.

## Out of scope

- Refactoring the broader `direction` vocabulary across the
  codebase (e.g. introducing a `Direction` enum). Defects 3a/3b
  are widened defensively; the canonical type stays a string.
- Backporting the gamma `closed=true` fix to other gamma helpers
  in `polymarket.py` (e.g. `get_market_by_token_id`,
  `get_events`). Out-of-scope unless investigation reveals the
  same pattern affects another reconciler. If found, file a
  follow-up plan.
- Live-mode CLOB submission changes. Order submission already
  succeeds on `direction='buy'` because token_id is authoritative;
  only state tracking and reconciliation broke. No live-mode
  change is required to fix the observed bug.
- Polymarket "single market with N outcomes" full support (rare
  outright formats). Task 3's defensive widening is sufficient to
  unblock such positions; first-class support is a future plan if
  volume justifies it.

### Task 1: gamma `closed=true` fallback in `polymarket.get_market_by_condition_id`

The current implementation hits gamma once with `{"condition_ids": cid, "limit": 80}`.
Resolved markets return empty. We need a second-chance lookup with
`closed=true` so the reconciler can see them.

- [x] In [`backend/services/polymarket.py:968-1002`](../../backend/services/polymarket.py:968)
      change the `for params in (...):` tuple to two probes:
      first the existing one (active markets), then
      `{"condition_ids": condition_id, "limit": 80, "closed": "true"}`
      as fallback. Preserve cache write-through behaviour; both
      probes write to the same `_market_cache` keyed by
      `condition_id`.
- [x] Confirm `_extract_market_info` populates `resolved`,
      `winning_outcome`, `closed`, `archived`, `accepting_orders`
      from the gamma row (these fields drive `position_lifecycle`
      settlement). If a key is missing from the parser, add it.
- [x] Add regression test in existing
      [`backend/tests/test_polymarket_client.py`](../../backend/tests/test_polymarket_client.py)
      next to the other `test_get_market_by_condition_id_*` tests
      (line ~217): `test_get_market_by_condition_id_falls_back_to_closed_when_first_probe_empty`
      — mocked `_rate_limited_get` returns empty list on first
      call (active probe) and a resolved-market row on second call
      (closed probe). Assert both probes hit `condition_ids=...`,
      that the second call's params include `closed="true"`, the
      returned dict has `closed=True`, and the cache is populated.
- [x] Add a sister regression test in the same file:
      `test_get_market_by_condition_id_does_not_probe_closed_when_active_match`
      — assert `_rate_limited_get` is called exactly once when the
      active probe already returns a row. Pin the no-double-call
      contract so we don't accidentally double the gamma load.
- [x] Run validation: `cd backend && pytest tests/test_polymarket_client.py -q`.
- [x] Mark completed

### Task 2: outcome normalisation in `traders_copy_trade_signal_service`

When the leader trade lands on a binary market (most common
"categorical" case), `outcome` should be `"Yes"` or `"No"`, not
`"Arsenal"`. The fix uses `tokens[]` / `outcomes[]` parallel
arrays from gamma to map back from `token_id` to the canonical
binary outcome label.

- [x] In [`backend/services/traders_copy_trade_signal_service.py`](../../backend/services/traders_copy_trade_signal_service.py)
      `_resolve_market_snapshot` (the function that gathers
      `market.outcome` for a given `token_id`): when the resolved
      market has exactly two `tokens[]` and exactly two
      `outcomes[]` and one of the outcomes is in `{"Yes","No"}`
      (case-insensitive), force `outcome` to the canonical
      `"Yes"`/`"No"` matching the index of the trader's `token_id`
      in `tokens[]`. Otherwise (true multi-outcome single-market),
      pass the original outcome label through unchanged.
- [x] Add regression test in existing
      [`backend/tests/test_traders_copy_trade_signal_service.py`](../../backend/tests/test_traders_copy_trade_signal_service.py)
      (next to `test_process_wallet_trade_skips_unresolved_token_outcome`
      at line 155): `test_resolve_market_snapshot_normalises_binary_market_outcome_to_canonical_yes_no`
      — feed a fake gamma snapshot with `outcomes=["Yes","No"]`
      and `tokens=[t0,t1]`; the leader trade's `token_id=t1`
      should resolve to `outcome="No"` (not lowercase, not
      capitalized as "NO" pre-normalisation).
- [x] Add a sister regression test in the same file:
      `test_resolve_market_snapshot_passes_through_true_multi_outcome_label`
      — feed a snapshot with `outcomes=["Fighter A","Fighter B","Fighter C"]`
      and `tokens=[t0,t1,t2]`; leader's `token_id=t1` should
      resolve to the unchanged `outcome="Fighter B"`. Pins that
      we don't accidentally over-normalise the rare true
      multi-outcome case.
- [x] Run validation: `cd backend && pytest tests/test_traders_copy_trade_signal_service.py -q`.
- [x] Mark completed

### Task 3: defensive widening in shadow simulator and position lifecycle

After Tasks 1+2 land, the only remaining stuck-position class is
existing in-flight orders that shipped before Task 2 (with
`direction='buy'`) plus genuine multi-outcome orders. Both need
the simulator and lifecycle to learn `direction='buy'` + token_id.

- [x] In [`backend/services/simulation.py:55-62`](../../backend/services/simulation.py:55)
      change `_direction_to_position_side` from a `(direction)` →
      `(PositionSide, str)` to `(direction, payload=None)` →
      `(PositionSide, str)`. When direction is `buy`/`sell` (not
      `_yes`/`_no`-suffixed), look up `payload.token_id` in
      `payload.market.tokens[]` (or `clob_token_ids[]`) and resolve
      `PositionSide` from the index (0 → YES, 1 → NO). For
      genuine multi-outcome (>2 tokens), keep raising
      `ValueError` — those need first-class support, out of scope
      here. Update every caller in `simulation.py` to thread the
      payload through.
- [x] **Follow-up after Task 6 deploy.** While verifying drain we
      observed `Unsupported direction 'sell_no'` errors still firing
      on legacy in-flight orders — the canonical `sell_yes`/`sell_no`
      directions had never been mapped in either helper (pre-existing
      bug, same defect-3a class). Extended both `_direction_to_position_side`
      and `_direction_outcome_index` so `sell_yes` resolves to YES
      side / index 0 and `sell_no` resolves to NO side / index 1
      (the leg sits on the same side of the binary market regardless
      of whether the action is buy or sell). Added regression tests
      `test_direction_to_position_side_resolves_sell_yes_to_yes_side`,
      `test_direction_to_position_side_resolves_sell_no_to_no_side`,
      `test_direction_outcome_index_canonical_sell_yes_resolves_to_yes_index`.
- [x] In [`backend/services/trader_orchestrator/position_lifecycle.py:879-885`](../../backend/services/trader_orchestrator/position_lifecycle.py:879)
      change `_direction_outcome_index(direction)` to
      `_direction_outcome_index(direction, *, market_info=None, token_id=None)`.
      For `buy_yes`/`buy_no` keep the existing 0/1 mapping. For
      bare `buy`/`sell` with a token_id, resolve via
      `_extract_market_token_ids(market_info)` then
      `tokens.index(token_id)`. Update every caller (search for
      `_direction_outcome_index(` across the file) to pass the
      already-available `market_info` and `payload.token_id`.
- [x] Add regression tests in existing
      [`backend/tests/test_simulation_orchestrator_ledger.py`](../../backend/tests/test_simulation_orchestrator_ledger.py)
      (next to the three ledger-recording tests):
      - `test_direction_to_position_side_resolves_buy_via_token_id_at_index_zero`
        — fake payload with `direction='buy'`, market.tokens=[t0,t1],
        `token_id=t0`; assert `PositionSide.YES, "YES"`.
      - `test_direction_to_position_side_resolves_buy_via_token_id_at_index_one`
        — same but `token_id=t1`; assert `PositionSide.NO, "NO"`.
      - `test_direction_to_position_side_still_raises_on_truly_multi_outcome`
        — payload with three tokens; pin that we still raise
        rather than guessing.
      - `test_record_orchestrator_shadow_fill_succeeds_with_bare_buy_direction`
        — end-to-end: the existing happy path with `direction='buy'`
        instead of `buy_yes` should now succeed (no `ValueError`),
        proving Task 3a unblocks the shadow ledger backfill path.
- [x] Add regression tests in existing
      [`backend/tests/test_trader_position_lifecycle_resolution.py`](../../backend/tests/test_trader_position_lifecycle_resolution.py)
      (sibling section to the existing `test_load_market_info_*` block):
      - `test_direction_outcome_index_resolves_buy_via_token_id_with_market_info`
        — `market_info` dict with two clob_token_ids; assert
        `_direction_outcome_index('buy', market_info=..., token_id=token_ids[1])` returns `1`.
      - `test_direction_outcome_index_returns_none_when_token_id_not_in_market`
        — token_id not present in tokens → `None` (no silent
        misclassification).
      - `test_direction_outcome_index_canonical_buy_yes_unchanged_by_widening`
        — pin that `buy_yes`/`buy_no` shortcut still returns 0/1
        without consulting market_info (perf + back-compat).
- [x] Run validation:
      `cd backend && pytest tests/test_simulation_orchestrator_ledger.py tests/test_trader_position_lifecycle_resolution.py -q`.
- [x] Mark completed

### Task 4: strip explicit `direction='buy'` from `traders_copy_trade._build_copy_opportunity`

After Task 2 fixes upstream and Task 3 makes downstream defensive,
the strategy itself should align with the rest of the codebase:
**don't emit a synthetic `direction` for cases the resolver can't
handle.** Empty string lets `_resolve_leg_direction` take over.

- [x] In [`backend/services/strategies/traders_copy_trade.py:499-503`](../../backend/services/strategies/traders_copy_trade.py:499)
      change the `direction` field in the `positions_to_take`
      dict from `"buy"` (else-branch) to `""`. Keep the
      `buy_yes`/`buy_no` explicit cases unchanged — they are
      authoritative when the outcome is canonical YES/NO.
      Document the empty-string contract in the inline comment
      block above (one short sentence on why "" lets fallback
      run).
- [x] Create new file
      `backend/tests/test_traders_copy_trade_strategy.py` (no
      sibling exists — verified via `ls backend/tests/ | grep
      traders_copy_trade` returns only the signal-service file)
      with three tests:
      - `test_build_copy_opportunity_emits_buy_yes_for_yes_outcome`
        — assert `direction="buy_yes"` for `outcome="YES"`.
      - `test_build_copy_opportunity_emits_buy_no_for_no_outcome`
        — assert `direction="buy_no"` for `outcome="NO"`.
      - `test_build_copy_opportunity_emits_empty_direction_for_non_binary_outcome`
        — payload with `outcome="Fighter A"` asserts
        `opportunity.positions_to_take[0]["direction"] == ""` (NOT
        `"buy"`). Pins the post-fix contract.
- [x] Add regression tests in existing
      [`backend/tests/test_execution_session_engine.py`](../../backend/tests/test_execution_session_engine.py)
      (next to `test_build_plan_normalizes_limit_sell_action_from_position_payload`
      at line 205):
      - `test_resolve_leg_direction_uses_explicit_direction_when_present`
        — leg dict with `direction="buy_yes"` and conflicting
        side/outcome; explicit wins.
      - `test_resolve_leg_direction_builds_buy_yes_from_side_and_outcome_when_direction_empty`
        — leg with `direction=""`, `side="buy"`, `outcome="yes"`
        → returns `"buy_yes"`. Pins the fallback that Task 4 now
        relies on.
      - `test_resolve_leg_direction_falls_back_to_bare_buy_for_non_binary_outcome`
        — leg with `direction=""`, `side="buy"`, `outcome="fighter a"`
        → returns `"buy"`. Pins the contract that bare-buy is the
        documented terminal output for true multi-outcome.
- [x] Run validation:
      `cd backend && pytest tests/test_traders_copy_trade_strategy.py tests/test_execution_session_engine.py -q`.
- [x] Mark completed

### Task 5: escalate `shadow_ledger_backfill_failed` severity on repeat

Find the emit-site in
`backend/workers/trader_orchestrator_worker.py` (search for
`shadow_ledger_backfill_failed`). Current call writes
`severity='warn'`. After N occurrences for the same trader_id in
a rolling window, escalate to `severity='error'` so the UI events
strip flags it.

- [x] In `trader_orchestrator_worker.py`, near the
      `shadow_ledger_backfill_failed` emit site, count recent
      occurrences for `(trader_id, event_key='shadow_ledger_backfill_failed')`
      from `trader_events` over the last 1 hour. If count ≥ 50,
      flip `severity` to `error` and add a payload field
      `escalated_from='warn'` for traceability. Threshold value
      lives in a single constant
      `SHADOW_LEDGER_BACKFILL_FAILED_ESCALATION_THRESHOLD = 50`
      near the top of the worker file so it is tunable in one
      place.
- [x] Confirm the UI events strip already groups
      `severity='error'` distinctly. If not, no UI work is
      needed — operator-side `trader_events` query
      (`SELECT severity, count(*) FROM trader_events ... GROUP BY 1`)
      will surface it via the existing diagnostic.
- [x] Add regression tests in existing
      [`backend/tests/test_trader_orchestrator_shadow_backfill.py`](../../backend/tests/test_trader_orchestrator_shadow_backfill.py)
      (next to the existing single happy-path test):
      - `test_shadow_ledger_backfill_failed_emits_warn_below_threshold`
        — 49 prior events; next emit lands as `severity='warn'`,
        no `escalated_from` payload field.
      - `test_shadow_ledger_backfill_failed_escalates_to_error_at_threshold`
        — 50 prior events for the same `(trader_id, event_key)`
        within the 1-hour window; next emit lands as
        `severity='error'` with `escalated_from='warn'` in
        payload.
      - `test_shadow_ledger_backfill_failed_escalation_is_per_trader`
        — 100 events for trader A do not escalate trader B's
        next event. Pins isolation so a noisy bot doesn't
        cascade.
- [x] Run validation:
      `cd backend && pytest tests/test_trader_orchestrator_shadow_backfill.py -q`.
- [x] Mark completed

### Task 6: deploy, verify drain, update docs, archive

After Tasks 1-5 are committed and pushed, deploy and observe.

- [ ] Run `./deploy/sync_remote.sh` from the local checkout.
      Confirm `worker-trading` restarts cleanly: no startup
      error, last_run_at advancing on the Sandbox bot row.
- [ ] After **2 reconciliation cycles** (≈ 60 s), query stuck
      open-orders count for the Sandbox bot:
      ```bash
      ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T \
        postgres psql -U homerun -d homerun -c \
        "SELECT count(*) FROM trader_orders \
         WHERE trader_id='\''61dcbeb2b9bc42bd9e9635a09ae5e0c3'\'' \
           AND status='\''open'\'' \
           AND first_order_at < now() - interval '\''2 hours'\''"'
      ```
      Expected: drops from 124 → near 0 (only orders on truly
      still-open Polymarket markets remain). Record the actual
      number in this task's checkbox.
- [ ] Query `trader_events` for `shadow_ledger_backfill_failed`
      count over the last hour. Expected: < 10 (steady-state
      noise from any genuinely unsupported true-multi-outcome
      ordere; if higher, defect is not fully fixed).
- [ ] Update [`docs/strategies/traders-copy-trade.md`](../strategies/traders-copy-trade.md)
      § "Pipeline сигналу" and § "Дефолти, які треба перекрити перед live"
      to reflect the new contract: outcome normalisation in the
      signal service, empty-direction emit pattern in the
      strategy, defensive token_id resolution in the simulator
      and lifecycle. Bump `Last verified:` to the deploy date.
- [ ] Update [`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md)
      § "Архітектурне припущення: всі ринки бінарні" — note that
      simulator and lifecycle now defensively widen via token_id
      for the rare single-market multi-outcome case.
- [ ] Update [`docs/plans/architecture/copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md):
      add a paragraph in the "Publish surface" section about the
      outcome normalisation step in `_resolve_market_snapshot`.
      Bump `Last verified:` to the deploy date.
- [ ] Update [`docs/plans/architecture/execution-and-fills.md`](architecture/execution-and-fills.md)
      § "Shadow path" — `_direction_to_position_side` now
      accepts `payload` for token-id widening. Bump
      `Last verified:`.
- [ ] `git mv docs/plans/0018-fix-stuck-shadow-positions-traders-copy-trade.md docs/plans/completed/`.
- [ ] Update the row in [`plan-control-index.md`](plan-control-index.md)
      to point at the `completed/` path.
- [ ] `git log --grep='Plan: 0018'` shows the full commit chain.
- [ ] Mark completed
