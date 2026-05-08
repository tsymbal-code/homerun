# Plan: Fix `trader_decisions` FK race for in-process `source='traders'` publishes

> **Plan policy.** This plan follows
> [`README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Plan 0009 removed the
`signal_bus._strategy_runtime_metadata` gate that hid every
`traders_copy_trade` signal from the orchestrator on
`latency_class=normal`. With the gate gone, those signals now
flow into `trader_orchestrator_worker` end-to-end. The post-deploy
soak (`2026-05-08T04:30..05:00Z`) immediately surfaced a
**second, pre-existing bug** that the gate had been masking:
every decision write hits

```
sqlalchemy.exc.IntegrityError: ForeignKeyViolationError
DETAIL: Key (signal_id)=(<id>) is not present in table "trade_signals".
```

In a 15-minute window this manifested as `151` failed
`trader_signal_consumption` rows for the Copy Trade trader
(`61dcbeb2b9bc42bd9e9635a09ae5e0c3`) and **zero** persisted
`trader_decisions`. The retry path also fails — when the
orchestrator falls back to writing a `trader_signal_consumption`
row referencing the failed `decision_id`, that write hits
`trader_signal_consumption_decision_id_fkey` for the same reason.

**Root cause (publish/projection race).**
[`backend/services/intent_runtime.py`](../../backend/services/intent_runtime.py)`.publish_opportunities`
mutates `self._signals_by_id` (the in-memory snapshot map) and
calls `publish_signal_batch(...)` (Redis fan-out) in the SAME
`async with self._lock:` block, BEFORE `_enqueue_projection`
hands the snapshot off to the projection loop, which is the
single owner of the `trade_signals` UPSERT path
([`signal_bus._upsert_trade_signal_row`](../../backend/services/signal_bus.py)).
The orchestrator's `list_unconsumed_signals` reads the
in-memory map directly (intent_runtime.py:2395) and the Redis
batch pings the consumer worker immediately, so the orchestrator
can pick up a `signal_id` whose `trade_signals` row does not yet
exist in Postgres. For scanner-source signals the projection
loop has typically already drained by the time the trader
orchestrator's 60-second cycle next ticks; for `traders` source
signals the publish→consume gap is microseconds (in-process
wallet-WS callback → in-process orchestrator queue), so the race
fires almost every time.

This plan eliminates the FK race without re-introducing the gate
that plan 0009 removed.

Done =
- `trader_decisions_signal_id_fkey` and
  `trader_signal_consumption_decision_id_fkey` violations no
  longer appear for `source='traders'` signals (verified via
  `worker-trading` log scrape — 0 IntegrityError rows in 30
  minutes under steady-state load);
- `Sandbox - Traders Copy Trade` on `latency_class=normal,
  is_paused=false, mode=shadow` records `trader_decisions` rows
  every cycle, at the same per-cycle cadence as
  `Sandbox - Traders Confluence` and other normal-tier traders
  (the `Done =` bullet from plan 0009 that was blocked
  downstream);
- `trader_signal_consumption` rows for the Copy Trade trader
  carry the actual decision outcome (`selected`/`skipped`/
  `blocked`/`failed`) referencing a real `trader_decisions.id`,
  not the placeholder `failed/Signal processing failed
  (IntegrityError)` payload from the FK race.

## Out of scope

- **Plan 0009's gate fix.** Already shipped; this plan does not
  touch `_strategy_runtime_metadata` or any `execution_activation`
  routing.
- **`traders_confluence` publish path.** Already works (signals
  born with `runtime_sequence`, decisions persist normally).
- **Orchestrator decision logic.** The race is purely about
  signal-existence, not decision content.
- **Generic projection-loop overhaul.** If the projection loop
  needs broader hardening (back-pressure, batching, etc.), that
  is a separate plan. This plan picks the smallest fix that
  closes the FK race.

## Context / References

- [Plan 0008 — Investigate `source='traders'` routing on
  normal-tier](completed/0008-investigate-traders-source-routing-on-normal.md)
  (the diagnosis chain that led to plan 0009).
- [Plan 0009 — Fix `source='traders'` deferred-state gate](completed/0009-fix-traders-source-on-normal.md)
  (the prerequisite; this plan exists because that one
  unmasked the race).
- [Architecture: Copy-Trade Pipeline](architecture/copy-trade-pipeline.md)
  (canonical end-to-end pipeline doc; the "Post-fix flow" /
  publish-projection split lives there).
- [`backend/services/intent_runtime.py:1987`](../../backend/services/intent_runtime.py)
  (`publish_opportunities` — entry point that mutates
  `self._signals_by_id`, calls `publish_signal_batch`, then
  enqueues the projection without awaiting the DB commit).
- [`backend/services/intent_runtime.py:2395`](../../backend/services/intent_runtime.py)
  (`list_unconsumed_signals` — in-memory dict scan, no
  `trade_signals` JOIN, sees rows the projection loop has not
  yet committed).
- [`backend/services/signal_bus.py:1380`](../../backend/services/signal_bus.py)
  (`_upsert_trade_signal_row` insert branch — `session.add(row)`
  and `_record_signal_emission`; the projection loop awaits
  `session.commit()` after the batch).
- [`backend/workers/trader_orchestrator_worker.py:7081`](../../backend/workers/trader_orchestrator_worker.py)
  (`create_trader_decision_checks` — the call site that hits
  the FK).
- [`backend/services/trader_orchestrator_state.py:5853`](../../backend/services/trader_orchestrator_state.py)
  (`session.flush()` of the new `TraderDecision` row — the FK
  fires here).
- [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  (operational journal entry — post-Plan-0009 deploy notes the
  FK race and points at this plan).

## Validation Commands

- `cd backend && ruff check services/intent_runtime.py services/signal_bus.py`
- `cd backend && python -c "import services.intent_runtime, services.signal_bus"`
  (smoke import).
- `docker compose exec -T backend pytest -q
  tests/test_intent_runtime_publish_opportunities_traders_source.py
  tests/test_intent_runtime_publish_opportunities*.py
  tests/test_signal_bus*.py
  tests/test_trader_orchestrator*.py`
  (regression on publish + signal-bus + orchestrator paths).
- After deploy, on `polyhome-1`, with the orchestrator unpaused
  and `Sandbox - Traders Copy Trade` on
  `latency_class=normal, is_paused=false, mode=shadow`:
  ```bash
  ssh polyhome-1 'cd /home/polyhome/homerun && \
    docker compose logs --since=15m worker-trading 2>&1 | \
    grep -c "ForeignKeyViolationError\|trader_decisions_signal_id_fkey"'
  ```
  Should be `0`.
- After 10 minutes of orchestrator runtime under the same
  conditions:
  ```bash
  ssh polyhome-1 'cd /home/polyhome/homerun && \
    docker compose exec -T postgres psql -U homerun -d homerun -c "
    select decision, count(*) n
    from trader_decisions
    where trader_id = '\''61dcbeb2b9bc42bd9e9635a09ae5e0c3'\''
      and created_at > now() - interval '\''10 minutes'\''
    group by decision order by n desc;"'
  ```
  Should return at least one row with `n > 0` (the actual
  end-state of plan 0009).
- `trader_signal_consumption` outcomes are no longer dominated
  by the placeholder `failed`:
  ```bash
  ssh polyhome-1 'cd /home/polyhome/homerun && \
    docker compose exec -T postgres psql -U homerun -d homerun -c "
    select outcome, count(*) n
    from trader_signal_consumption
    where trader_id = '\''61dcbeb2b9bc42bd9e9635a09ae5e0c3'\''
      and consumed_at > now() - interval '\''10 minutes'\''
    group by outcome order by n desc;"'
  ```
  At least one non-`failed` row with `reason` referencing the
  actual decision (not `Signal processing failed
  (IntegrityError)`).

### Task 1: Reproduce the race deterministically in a unit test

Before changing publish-side or consumer-side code, write a
test that drives a `traders` opportunity through
`intent_runtime.publish_opportunities` and asserts an
externally observable invariant: by the time
`list_unconsumed_signals` returns the signal, the corresponding
`trade_signals` row is committed in the test DB.

- [ ] Add `backend/tests/test_intent_runtime_publish_projection_durability.py`
  covering:
  - Publish a `traders` opportunity, immediately call
    `list_unconsumed_signals` → returned `signal_id` MUST exist
    in `trade_signals` (DB query; not in-memory).
  - Same invariant for `scanner` source (regression).
  - Same invariant for the upsert branch (re-publish of an
    existing `signal_id`): the row must remain queryable by
    `signal_id` for the entire window the snapshot is in
    `self._signals_by_id`.
- [ ] Run the new file in the production backend container
  (`docker cp` since the image excludes `tests/`); confirm it
  fails on `main` (current behaviour) before any fix lands.
- [ ] Mark completed

### Task 2: Pick a fix strategy

Three plausible options. Decide based on which one ships the
smallest, most-localised change without re-introducing the
deferred-state pattern that plan 0009 retired.

1. **Synchronous DB commit inside `publish_opportunities`.**
   Move `trade_signals` UPSERT out of the projection loop and
   into the publish path itself, so the function returns only
   after the row is committed. Keeps the in-memory map and the
   DB in lockstep at the cost of slowing every publish to one
   DB round-trip. Risk: the projection loop currently absorbs
   spikes (1000 scanner emits per cycle); making them
   synchronous could re-surface contention.
2. **Idempotent `INSERT ... ON CONFLICT DO NOTHING` in the
   orchestrator's decision write.** Wrap the
   `TraderDecision.signal_id` insert in a CTE that first
   ensures the `trade_signals` row exists by re-deriving it
   from the in-memory snapshot the orchestrator already holds.
   Smallest change but spreads `trade_signals` write
   responsibility to the consumer side.
3. **Wait-for-projection in `list_unconsumed_signals`.** Block
   the orchestrator's read until the projection loop has
   processed every signal_id whose snapshot it would return.
   Probably racy and slow under load; mentioned only for
   completeness.

- [ ] Evaluate options against the FK race regression test from
  Task 1 + the per-cycle-cadence end-state from plan 0009.
  Document the choice in this checkbox with rationale.
- [ ] Mark completed

### Task 3: Land the fix

- [ ] Implement the chosen option in
  [`backend/services/intent_runtime.py`](../../backend/services/intent_runtime.py)
  and/or
  [`backend/workers/trader_orchestrator_worker.py`](../../backend/workers/trader_orchestrator_worker.py)
  /
  [`backend/services/trader_orchestrator_state.py`](../../backend/services/trader_orchestrator_state.py).
- [ ] Re-run Task 1's test → must pass.
- [ ] Run the full regression set from
  `## Validation Commands` → must pass.
- [ ] Mark completed

### Task 4: Update architecture notes

- [ ] Update
  [`copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md):
  - Add a "Publish/projection durability (post Plan 0010)"
    section to the existing "Post-fix flow" block, describing
    the new invariant that `trade_signals` is committed before
    `list_unconsumed_signals` returns the signal.
  - Update the ASCII pipeline diagram if the publish-path
    sequencing changed (Task 2 option 1) or note the
    consumer-side ON CONFLICT pattern (Task 2 option 2).
- [ ] Update
  [`trader-pipeline.md`](architecture/trader-pipeline.md)'s
  "Common end-state symptoms" table — drop the
  `IntegrityError`/`ForeignKeyViolationError` callout from the
  "Copy-trade bot idle" row.
- [ ] Mark completed

### Task 5: Update the operational journal

- [ ] Append an entry to
  [`runtime-tweaks.md`](../operational/runtime-tweaks.md)
  closing out the FK race callout filed at the post-deploy
  point of plan 0009. Include: the verification commands (the
  two `psql`/log greps from `## Validation Commands`), the
  before/after `trader_decisions` count, and a CLOSED status.
- [ ] Mark completed

### Task 6: Deploy and verify on `polyhome-1`

- [ ] `./deploy/sync_remote.sh` to deploy the fix.
- [ ] Verify backend health (`docker compose ps`,
  `curl /api/strategies`).
- [ ] Run the four post-deploy checks from
  `## Validation Commands` (FK violation grep, `trader_decisions`
  count, `trader_signal_consumption` outcomes, plan 0009's
  `without_seq=0` invariant — must all pass).
- [ ] Mark completed

### Task 7: Close

- [ ] All check-boxes above are `[x]`.
- [ ] `git mv docs/plans/0010-fix-traders-publish-fk-race.md
  docs/plans/completed/`.
- [ ] Update [`plan-control-index.md`](plan-control-index.md):
  link target to `completed/0010-...md`. Update the per-plan
  note to reflect outcome.
- [ ] Mark completed
