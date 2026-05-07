# Plan: Fix `source='traders'` deferred-state gate so normal-tier traders consume copy-trade signals

> **Plan policy.** This plan follows
> [`README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Plan 0008 traced end-to-end why `Sandbox - Traders Copy Trade` on
`latency_class=normal` produces zero `trader_decisions` while other
normal-tier traders consume signals normally in the same window. The
gate is in
[`backend/services/signal_bus.py:493-524`](../../backend/services/signal_bus.py)
(`_strategy_runtime_metadata`):

```python
if source_key == "crypto":      execution_activation = "immediate"
elif source_key == "scanner":   execution_activation = "ws_current"
else:                           execution_activation = "ws_post_arm_tick"   # ← traders falls here
```

`source_key="traders"` lands in the `else` branch and is given
`execution_activation = "ws_post_arm_tick"`, which causes
`intent_runtime.publish_opportunities`
([lines 2129-2141 and 2186-2195](../../backend/services/intent_runtime.py))
to mark the snapshot as deferred:

```python
self._set_deferred_state_locked(signal_id,
    required_token_ids=..., reason="awaiting_post_arm_ws_tick")
snapshot["deferred_until_ws"] = True
snapshot["runtime_sequence"] = None
```

Both the in-memory cache filter
(`intent_runtime.list_unconsumed_signals` lines 2432, 2440-2442) and
the DB-fallback (`cursor_runtime_sequence` index) hide rows with
`runtime_sequence IS NULL`, so neither the normal-tier
`trader_orchestrator_worker` nor the fast-tier `fast_trader_runtime`
can see the signal until
`_reactivate_deferred_signals_for_token` (intent_runtime.py:1298)
fires for the required token. That reactivation depends on a fresh
CLOB price quote, which the trading-plane `feed_manager` does not
typically subscribe to for leader-wallet tokens (the scanner
catalog does not include them). Result: the signal expires after
its 15-min TTL still in `awaiting_post_arm_ws_tick` state.

Production proof (`2026-05-07T19:30Z`):

```
 strategy_type      | status  |  n  | with_seq | without_seq
--------------------+---------+-----+----------+-------------
 traders_confluence | pending |  30 |       30 |           0
 traders_copy_trade | pending | 445 |        0 |         445
```

`traders_confluence` signals get a `runtime_sequence` because they
are published by `tracked_traders_worker` on the discovery plane
through a different code path that skips the deferred-state branch.
`traders_copy_trade` signals, born in the trading plane through
`bridge_opportunities_to_signals → intent_runtime.publish_opportunities`,
all hit the gate.

The fast-tier runtime sometimes trickles a few signals through
(empirically ~0.1 % of daily volume) when a leader trade happens to
hit a token that *is* in the CLOB feed, but on normal-tier the
60-second cycle plus the rare-quote race window means **zero**
copy-trade orders ever materialise.

This is a **latent regression**: the `else: ws_post_arm_tick`
fallback was meant to enforce strict-WS pricing for arbitrary new
sources, but `traders` is not a fee-arbitrage source where stale
pricing causes a loss — it is a directional copy of a leader's
intent, where strict pre-arm pricing is unnecessary and actively
harmful (it converts the source into a noop).

Done =
- `signal_bus._strategy_runtime_metadata` no longer routes
  `traders` to the `ws_post_arm_tick` activation;
- All `traders_copy_trade` signals are born with a non-NULL
  `runtime_sequence` and are visible to both fast and normal-tier
  consumers;
- Production observation under `latency_class=normal` shows
  `traders_copy_trade` decisions accruing at the same per-cycle
  cadence as `traders_confluence` and other normal-tier traders;
- The architecture note
  [`copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md)
  is updated to reflect the post-fix flow (no deferred-state
  branch on the `traders` source).

## Out of scope

- **Other deferred-state branches.** `scanner` source's
  `ws_current` branch and `crypto` source's `immediate` branch
  are working as designed and are not touched.
- **`traders_confluence` publish path.** It already works (signals
  are born with a non-NULL `runtime_sequence`); not modified.
- **CLOB feed coverage for leader-wallet tokens.** Auto-subscribing
  the trading-plane `feed_manager` to leader-wallet tokens is a
  defensible alternative fix but it is more invasive (touches the
  feed layer, can race against rate-limits, and changes the
  scanner's market-catalog semantics). This plan picks the
  simpler fix; if a future requirement calls for strict-WS
  pricing on copy-trade entries, that becomes a separate plan.
- **Live execution at venue.** `mode=live` Polymarket CLOB submit
  semantics are unchanged. Whether the operator runs Copy Trade in
  shadow or live is independent of the gate fix.

## Context / References

- [Plan 0008 — Investigate `source='traders'` routing on normal-tier](completed/0008-investigate-traders-source-routing-on-normal.md)
  (the research that produced this fix plan).
- [Architecture: Copy-Trade Pipeline](architecture/copy-trade-pipeline.md)
  (the canonical end-to-end pipeline doc; "The gate" section
  describes exactly what this plan removes).
- [Architecture: Trader Pipeline & Diagnostics](architecture/trader-pipeline.md)
  (general signal-to-order flow; minor update on close to drop
  the "deferred at publish" caveat from the symptoms table).
- [`backend/services/signal_bus.py:493-524`](../../backend/services/signal_bus.py)
  (`_strategy_runtime_metadata` — the single source of the gate).
- [`backend/services/intent_runtime.py:2129-2141`](../../backend/services/intent_runtime.py)
  and [`:2186-2195`](../../backend/services/intent_runtime.py)
  (the upsert + new-signal publish branches that consume the
  `execution_activation` and set `runtime_sequence=None`).
- [`backend/services/strategies/traders_copy_trade.py`](../../backend/services/strategies/traders_copy_trade.py)
  (`source_key = "traders"`, `strategy_type = "traders_copy_trade"` —
  the producer that gets caught by the gate).
- [`backend/services/strategy_signal_bridge.py:18`](../../backend/services/strategy_signal_bridge.py)
  (the bridge that hands the opportunity to `intent_runtime.publish_opportunities`).
- [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  (operational journal — the operator workaround "set the trader to
  `latency_class=fast`" landed there from plan 0008's
  observation; replaced by this fix once it ships).

## Validation Commands

- `cd backend && ruff check services/signal_bus.py services/intent_runtime.py`
- `cd backend && python -c "import services.signal_bus, services.intent_runtime"`
  (smoke import).
- `docker compose exec -T backend pytest -q tests/test_signal_bus_strategy_runtime_metadata.py
  tests/test_intent_runtime_publish_opportunities_traders_source.py`
  (new test files added by Task 2; **must fail on `main` before
  the fix lands and pass after**).
- `docker compose exec -T backend pytest -q tests/test_intent_runtime_publish_opportunities*.py
  tests/test_signal_bus*.py` (regression on the surrounding paths).
- After deploy, on `polyhome-1`, with `Sandbox - Traders Copy Trade`
  set to `latency_class=normal, is_paused=false, mode=shadow` and
  the orchestrator unpaused:
  ```bash
  ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres \
    psql -U homerun -d homerun -c "
    select strategy_type, status, count(*) n,
      sum((runtime_sequence is not null)::int) with_seq,
      sum((runtime_sequence is null)::int)     without_seq
    from trade_signals
    where strategy_type in (\"traders_copy_trade\",\"traders_confluence\")
      and created_at > now() - interval \"5 minutes\"
    group by strategy_type, status order by 1, 2;"'
  ```
  Both rows should report `without_seq = 0` (post-fix invariant);
  on `main` the `traders_copy_trade` row reports
  `with_seq = 0`.
- After 10 minutes of orchestrator runtime under the same
  conditions:
  ```bash
  ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres \
    psql -U homerun -d homerun -c "
    select count(*)
    from trader_decisions
    where trader_id = \"61dcbeb2b9bc42bd9e9635a09ae5e0c3\"
      and created_at > now() - interval \"10 minutes\";"'
  ```
  Should be > 0 (any non-zero value confirms the gate is gone).

### Task 1: Pick the activation value for `source='traders'`

Decide what `execution_activation` value `traders` signals should
get. The 0008 investigation surfaced three plausible options:

1. **`immediate`** — same as crypto. Signals are visible to
   consumers as soon as they are published; no WS-quote
   precondition. Simplest; matches the actual semantics of a
   leader-wallet copy (we don't need a fresh CLOB quote to decide
   to follow the leader — we need the current order book at
   submit time, which `risk_manager` and `fast_submit` already
   read independently).
2. **`ws_current`** — same as scanner. Requires a fresh quote
   only when the signal first lands; if it does not have one, the
   signal is born without `runtime_sequence`, but the
   reactivation pipeline picks it up the moment a quote arrives.
   This is more conservative than `immediate` but does not
   address the underlying fact that leader-wallet tokens are
   typically *not* subscribed to the CLOB feed, so the signal
   would still expire silently in many cases.
3. **`immediate` for the explicit `source_key="traders"` branch,
   with a tightened `else` clause** that does not silently fall
   through to `ws_post_arm_tick` for unknown sources. Future new
   sources should explicitly opt into a strict-WS activation; the
   `else` clause becomes a hard error or a logged warning instead.

- [ ] Document the decision in this task's checkbox below. The
  recommendation is **option 1 (`immediate`) plus tightening the
  `else` branch (option 3)**; both edits land together because
  option 3 is one line of work and prevents the next regression.
- [ ] Mark completed

### Task 2: Add tests that prove the gate behaviour and fail before the fix

Before changing any production code, write tests that *currently*
fail on `main` and will pass once the fix lands. This is the
"red" of red-green refactor and ensures the fix is locked in.

- [ ] Create `backend/tests/test_signal_bus_strategy_runtime_metadata.py`
  covering:
  - `source_key="crypto"` returns
    `execution_activation="immediate"` (regression).
  - `source_key="scanner"` returns
    `execution_activation="ws_current"` (regression).
  - `source_key="traders"` returns
    `execution_activation="immediate"` (the fix invariant — fails on `main`).
  - An unknown `source_key` raises `ValueError` (or logs a
    warning) instead of silently producing
    `ws_post_arm_tick` (the option-3 invariant — fails on `main`).
- [ ] Create
  `backend/tests/test_intent_runtime_publish_opportunities_traders_source.py`
  covering:
  - Publishing an opportunity with
    `payload.strategy_runtime.source_key="traders"` and
    `required_token_ids` non-empty produces a snapshot with
    `runtime_sequence != None` and `deferred_until_ws=False`
    (fails on `main`).
  - The same call routes a `runtime_signal_batch` with a
    populated `source_signal_ids["traders"]` to the
    `general` lane via
    `runtime_signal_queue._queues["general"]` (regression).
  - `intent_runtime.list_unconsumed_signals(sources=["traders"])`
    returns the signal immediately after publish (fails on `main`).
- [ ] Run both files; confirm they fail on `main` for the
  intended reasons (mismatched `execution_activation` /
  `runtime_sequence is None`).
- [ ] Mark completed

### Task 3: Land the fix in `signal_bus._strategy_runtime_metadata`

- [ ] Edit
  [`backend/services/signal_bus.py`](../../backend/services/signal_bus.py)
  `_strategy_runtime_metadata`:
  - Add `elif source_key == "traders": execution_activation = "immediate"`
    before the `else` clause.
  - Replace the silent `else: execution_activation = "ws_post_arm_tick"`
    fallback. Replace it with an explicit log-warn-and-fall-through
    to `"immediate"` so unknown sources don't get gated by
    accident. The exact form (warning + fallback vs raising) is
    decided in Task 1; the current recommendation is the warn +
    safe fallback, since refusing to publish would silently
    erase work.
- [ ] Verify the new tests from Task 2 now pass.
- [ ] Run full backend test suite (`docker compose exec -T backend
  pytest -q backend/tests/test_signal_bus*.py
  backend/tests/test_intent_runtime*.py
  backend/tests/test_runtime_signal_queue*.py`) — no regressions.
- [ ] Mark completed

### Task 4: Update the architecture note

- [ ] Edit
  [`docs/plans/architecture/copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md):
  - Update "The gate" section: change the prose from "the gate
    drops the signal at publish" to "as of plan 0009, the
    `traders` source is published with
    `execution_activation='immediate'`; signals are visible to
    consumers as soon as they land in `_signals_by_id`."
  - Update the ASCII pipeline diagram: in the `intent_runtime.publish_opportunities`
    box, drop the `_ea == "ws_post_arm_tick"` branch from the
    activation table (or mark it as historical and not reachable
    for any current source).
  - Update the conclusion section from "this is a bug, fix is
    deferred to plan 0009" to "fixed by plan 0009; the
    operational workaround (`latency_class=fast`) is no longer
    necessary."
- [ ] Edit
  [`docs/plans/architecture/trader-pipeline.md`](architecture/trader-pipeline.md):
  - In the "Common end-state symptoms" table, update the
    "Copy-trade bot idle" row to drop the `awaiting_post_arm_ws_tick`
    callout. The new diagnostic is the standard Stage 1 / Stage 5
    flow.
  - In the "Known footguns" section, drop the "publishes via the
    in-process wallet-WS callback" footgun; the publish path is
    no longer surprising once the gate is gone.
- [ ] Mark completed

### Task 5: Update the operational journal

- [ ] Append an entry to
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  documenting:
  - The 2026-05-07 operator workaround ("Copy Trade traders set
    to `latency_class=fast`") is now obsolete.
  - The fix shipped in this plan; a one-line `psql` command to
    set affected traders back to `latency_class=normal` if the
    operator wants to revert the workaround.
- [ ] Mark completed

### Task 6: Deploy and verify on `polyhome-1`

- [ ] `./deploy/sync_remote.sh` to deploy the fix.
- [ ] Verify backend health (`/health/live`,
  `docker compose ps`).
- [ ] Run the post-deploy SQL check from "Validation Commands":
  both `traders_copy_trade` and `traders_confluence` rows show
  `without_seq = 0`.
- [ ] Set `Sandbox - Traders Copy Trade` back to
  `latency_class=normal` (if the operator's earlier workaround
  is still in place):
  ```sql
  update traders set latency_class = 'normal'
  where id = '61dcbeb2b9bc42bd9e9635a09ae5e0c3';
  ```
- [ ] Wait 10 minutes; run the `trader_decisions` count check.
  Should be > 0.
- [ ] Mark completed

### Task 7: Close

- [ ] All check-boxes above are `[x]`.
- [ ] `git mv docs/plans/0009-fix-traders-source-on-normal.md
  docs/plans/completed/`.
- [ ] Update [`plan-control-index.md`](plan-control-index.md):
  link target to `completed/0009-...md`. Update the per-plan note
  to reflect outcome.
- [ ] Mark completed
