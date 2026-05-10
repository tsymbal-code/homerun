# Plan: Broaden binary-market outcome normalisation beyond literal Yes/No

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0023` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Plan 0018 reduced `shadow_ledger_backfill_failed` events from
1 451 / 36 h pre-fix to ~50 / h post-fix — a **smaller** drop than
expected. Investigation surfaced two specific stuck orders
(`247669fc155a4d7abc5f8ee9cd68bc04`,
`08bce1226e574413a4bbb70e05d1f8c7`) on the Sandbox bot that
**post-Plan-0018** still error every backfill cycle (~5 s) with:

```
"reason": "record_orchestrator_shadow_fill_failed",
"error":  "Unsupported direction 'buy'"
```

### Root cause (verified end-to-end)

Both orders are on a crypto BTC up/down binary market
(`market_id=0x0aca7808...`, outcomes `["Up","Down"]` from gamma).
The leader bought the `Down` side. Pipeline trace:

1. **Signal service** (`_resolve_market_snapshot` in
   [`traders_copy_trade_signal_service.py:801-811`](../../backend/services/traders_copy_trade_signal_service.py:801)):
   Plan 0018's binary-market normalisation only fires when the
   outcome list is literally `["Yes","No"]` (or contains one of
   them) — line 806: `if {"yes", "no"} & lowered:`. For
   `["Up","Down"]` this set intersection is empty, so the outcome
   passes through unchanged as `"down"`.

2. **Strategy** ([`traders_copy_trade.py:494-507`](../../backend/services/strategies/traders_copy_trade.py:494)):
   sees `outcome="DOWN"`, doesn't match `YES`/`NO`, emits
   `direction=""` (post-Plan-0018 fallback).

3. **`_resolve_leg_direction`** in
   [`session_engine.py:128-139`](../../backend/services/trader_orchestrator/session_engine.py:128):
   `direction=""`, `side="buy"`, `outcome="down"` (not in `{yes,no}`)
   → returns bare `"buy"`. Order persists with `direction='buy'`
   and `payload.live_market.selected_outcome='down'`.

4. **Backfill loop**
   ([`SimulationService._direction_to_position_side` in `simulation.py:55-116`](../../backend/services/simulation.py:55)):
   Plan 0018's defensive widening looks for a `token_ids[]` list in
   `payload.market` / `payload.live_market` / root payload. The
   actual `live_market` shape only carries `selected_token_id`
   (singular) and `selected_outcome` — no list — so widening
   finds `tokens=[]` and raises
   `Unsupported direction 'buy'`. Loop retries each cycle.

### Why broaden normalisation rather than re-extend widening

Plan 0018's normalisation guard
(`if {"yes","no"} & lowered:`) was introduced as a safety check to
avoid normalising true multi-outcome markets. But that condition
is already covered by `len(token_ids_list) == 2 AND
len(outcomes_list) == 2` — a 2-token market is binary by
construction in Polymarket's data model, regardless of label
vocabulary. The label is informational; the token-position is the
identity. Broadening the normalisation handles all binary label
vocabularies (Yes/No, Up/Down, Arsenal/Field, Trump/Other, …)
through a single code path, leaves multi-outcome markets
(>2 tokens) untouched, and makes the downstream widening
unnecessary for 100% of binary copy-trade signals.

Fixing here also avoids the trap of teaching every downstream
helper (simulator, lifecycle, future consumers) the
`live_market.selected_outcome → side` mapping. Normalisation at
the publish surface puts canonical `Yes`/`No` everywhere
downstream and lets the existing `buy_yes`/`buy_no` fast paths do
the work.

### What "done" looks like

- `shadow_ledger_backfill_failed` rate drops from ~50 / h
  to **<5 / h** (long-tail, mostly residual stuck orders that
  predate the fix and won't be retried).
- New copy-trade signals on BTC up/down (and any other binary
  market with non-Yes/No labels) flow end-to-end with
  `direction='buy_yes'`/`'buy_no'`, no widening needed.
- The two specific stuck orders
  (`247669fc155a4d7abc5f8ee9cd68bc04`,
  `08bce1226e574413a4bbb70e05d1f8c7`) drain via a one-shot SQL
  cleanup since they are `status='resolved_loss'` and have no
  business value in being backfilled (terminal P&L is unchanged
  whether the simulator records the entry leg or not).
- New regression tests pin the broader normalisation across
  multiple label vocabularies + the multi-outcome (>2 tokens)
  passthrough.
- Plan 0018's `_common-bot-parameters.md` § "How strategies emit
  positions_to_take" gets a one-paragraph appendix noting the
  broader normalisation.

## Context / References

- Defective normalisation:
  [`backend/services/traders_copy_trade_signal_service.py:801-811`](../../backend/services/traders_copy_trade_signal_service.py:801)
  (the `{"yes","no"} & lowered` guard).
- Strategy emit point:
  [`backend/services/strategies/traders_copy_trade.py:494-507`](../../backend/services/strategies/traders_copy_trade.py:494).
- Direction resolver fallback:
  [`backend/services/trader_orchestrator/session_engine.py:128-139`](../../backend/services/trader_orchestrator/session_engine.py:128).
- Widening that this fix sidesteps:
  [`backend/services/simulation.py:55-116`](../../backend/services/simulation.py:55).
- Existing normalisation tests:
  [`backend/tests/test_traders_copy_trade_signal_service.py`](../../backend/tests/test_traders_copy_trade_signal_service.py).
- Pre-this-plan event rate evidence:
  [`docs/plans/completed/0018-fix-stuck-shadow-positions-traders-copy-trade.md`](completed/0018-fix-stuck-shadow-positions-traders-copy-trade.md)
  + Sandbox post-deploy audit (in conversation).

## Out of scope

- **Multi-outcome single-market structures** (>2 tokens, e.g. some
  UFC fighter outright lists). Plan 0018 left these as a known
  follow-up for a first-class direction representation. This plan
  does NOT change that — `>2 tokens` paths still raise in the
  widening, which is correct.
- **Generic `_resolve_leg_direction` defensive widening** in
  `session_engine.py`. Once normalisation produces canonical
  `Yes`/`No` upstream, the resolver fallback no longer needs to
  swallow `up`/`down` etc. If a future signal source produces a
  binary market without going through the normaliser, widening
  the resolver becomes its own concern.
- **Other signal sources** (`traders_confluence`, `news_edge`,
  …). They build positions through different pipelines and
  already emit `buy_yes`/`buy_no` correctly per the
  `_common-bot-parameters.md` audit.

## Validation Commands

- `bash scripts/run_tests_remote.sh tests/test_traders_copy_trade_signal_service.py`
- `bash scripts/run_tests_remote.sh tests/test_simulation_orchestrator_ledger.py`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "SELECT count(*) FROM trader_events WHERE trader_id='\''61dcbeb2b9bc42bd9e9635a09ae5e0c3'\'' AND event_type='\''shadow_ledger_backfill_failed'\'' AND created_at > now() - interval '\''10 minutes'\''"'`

### Task 1: Drop the `{"yes","no"} & lowered` guard

The current normaliser block at
[`backend/services/traders_copy_trade_signal_service.py:801-811`](../../backend/services/traders_copy_trade_signal_service.py:801)
guards canonicalisation behind the presence of a literal Yes/No
label in the gamma response. Drop the guard so any 2-token binary
market normalises by token-position.

- [ ] In `_resolve_market_snapshot`, remove the
      `if {"yes", "no"} & lowered:` check (line 806). The outer
      `len(token_ids_list) == 2 AND len(outcomes_list) == 2`
      condition is sufficient — a 2-token Polymarket market is
      binary by construction regardless of label vocabulary.
- [ ] Update the surrounding comment block (lines 789-800) to
      reflect the new contract: "Any binary market (exactly two
      tokens) gets canonical `Yes`/`No` labels by token-position
      regardless of gamma's label vocabulary (`Up`/`Down`,
      `Arsenal`/`Field`, etc.). True multi-outcome single-market
      structures (>2 tokens) skip this normalisation and pass
      the original label through unchanged."
- [ ] Mark completed

### Task 2: Regression tests for broader normalisation

Add to existing
[`backend/tests/test_traders_copy_trade_signal_service.py`](../../backend/tests/test_traders_copy_trade_signal_service.py)
next to the Plan 0018 normalisation tests.

- [ ] Add `test_resolve_market_snapshot_normalises_up_down_to_yes_no`:
      mock gamma response with `tokens=[t0,t1]`,
      `outcomes=["Up","Down"]`. Leader's `token_id=t1`. Assert
      resolved `outcome` is `"No"` (idx 1 → No).
- [ ] Add `test_resolve_market_snapshot_normalises_candidate_field_to_yes_no`:
      same shape with `outcomes=["Arsenal","Field"]`,
      `token_id=t0`. Assert `outcome="Yes"` (idx 0 → Yes).
- [ ] Add `test_resolve_market_snapshot_still_passes_through_truly_multi_outcome`:
      gamma response with `outcomes=["Fighter A","Fighter B","Fighter C"]`
      (3 tokens). Assert outcome is preserved as `"Fighter B"`
      when `token_id=t1`. Pins that >2-token markets are NOT
      normalised.
- [ ] Add `test_resolve_market_snapshot_still_normalises_yes_no_when_present`:
      regression on Plan 0018's original case to confirm we
      didn't break it. `outcomes=["Yes","No"]`, `token_id=t1` →
      `outcome="No"`.
- [ ] Run validation:
      `bash scripts/run_tests_remote.sh tests/test_traders_copy_trade_signal_service.py`.
- [ ] Mark completed

### Task 3: Deploy, drain 2 stuck orders, verify event drop, update docs, close out

- [ ] Pre-deploy `shadow_ledger_backfill_failed` rate:
      ```bash
      ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "SELECT count(*) FROM trader_events WHERE trader_id='\''61dcbeb2b9bc42bd9e9635a09ae5e0c3'\'' AND event_type='\''shadow_ledger_backfill_failed'\'' AND created_at > now() - interval '\''10 minutes'\''"'
      ```
      Record the count.
- [ ] Run `./deploy/sync_remote.sh`. Confirm `worker-trading`
      restarts cleanly.
- [ ] Drain the two known-stuck orders via SQL one-shot. Both are
      `status='resolved_loss'` (terminal — no business P&L impact
      from skipping their backfill). Find the right column to mark
      "backfill done" — either `simulation_ledger` boolean (if it
      exists) or set `payload_json -> 'backfill_skipped'` true.
      First check schema: `\d trader_orders` to confirm. Then:
      ```sql
      UPDATE trader_orders
      SET payload_json = jsonb_set(payload_json::jsonb, '{backfill_skipped}', 'true'::jsonb)::json
      WHERE id IN ('247669fc155a4d7abc5f8ee9cd68bc04',
                   '08bce1226e574413a4bbb70e05d1f8c7');
      ```
      If the backfill loop honours that flag (verify by reading
      `_backfill_simulation_ledger_for_active_shadow_orders` in
      `trader_orchestrator_worker.py`), this drains them. If it
      doesn't, the orders simply continue to noisily fail until
      they're aged out by `_ACTIVE_ORDER_STATUSES` filtering —
      acceptable since they're terminal and won't take new fills.
- [ ] Post-deploy `shadow_ledger_backfill_failed` rate (10-min
      window after the SQL cleanup): expected **< 5**, ideally
      **0**. Record the count.
- [ ] Update
      [`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md)
      § "How strategies emit positions_to_take": append a short
      paragraph noting that the signal-service normaliser now
      canonicalises any binary market (not just Yes/No-labelled),
      so all binary copy-trade signals flow through the
      `buy_yes`/`buy_no` direction fast paths regardless of the
      gamma label vocabulary.
- [ ] Append a new entry to
      [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
      with deploy date, surface (the signal service file),
      pre/post event-rate, regression-test reference, and
      rollback (`git revert <SHA>` + redeploy → guard returns,
      stuck-order pattern returns).
- [ ] `git mv docs/plans/0023-broaden-binary-outcome-normalisation-beyond-yes-no.md docs/plans/completed/`.
- [ ] Update the row in [`plan-control-index.md`](plan-control-index.md)
      to point at the `completed/` path.
- [ ] `git log --grep='Plan: 0023'` shows the full commit chain.
- [ ] Mark completed
