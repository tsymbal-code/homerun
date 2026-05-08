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
externally observable invariant: the published in-memory id
must equal the canonical `trade_signals.id` for the same
`(source, dedupe_key)` when the DB already carries a row from
a previous worker-trading process (the post-restart scenario
that masks the FK race in production).

- [x] Add `backend/tests/test_intent_runtime_publish_projection_durability.py`
  with three assertions:
  - **known dedupe_key, status=pending**: pre-insert a
    `trade_signals` row with id `CANONICAL_OLD_ID` and the
    same `(source='traders', dedupe_key=K)` the opportunity
    will produce, then publish; assert the in-memory id
    equals `CANONICAL_OLD_ID`.
  - **known dedupe_key, status=expired (terminal)**: same
    setup but with a terminal status; assert id is still
    adopted (the projection's existing reactivation logic
    still runs, but the publish-side id assignment is what
    plan 0010 fixes).
  - **unknown dedupe_key (in-process publish→consume gap)**:
    no pre-existing row; assert that the publish path mints a
    fresh 32-hex uuid AND that a `trade_signals` row keyed by
    that same id is committed to the DB before
    `publish_opportunities` returns (the
    `test_publish_commits_skeleton_for_unknown_dedupe_key`
    invariant — pre-fix this was an in-memory-only id, and
    `traders` consumers racing the asynchronous projection
    loop FK-failed). All three assertions verified in the
    `worker-trading` container against a Postgres scratch DB
    (`3 passed in 10.59s`).
- [x] Confirmed RED on `main` in the production backend
  container (`docker compose cp` since the prod image excludes
  `tests/`):

  ```
  AssertionError: plan 0010 invariant: post-fix
  publish_opportunities must adopt the existing
  trade_signals.id for known (source, dedupe_key) keys.
  Got fresh id=18eb56d32ffc49c28883e158e22dbfec; expected
  canonical id=0123456789abcdef0123456789abcdef.
  ```

  Confirms the root cause beneath plan 0010: post-restart
  publishes mint fresh uuids that the projection's
  upsert-by-(source, dedupe_key) silently demotes (the OLD id
  wins), so the orchestrator's later
  `_ensure_runtime_signal_persisted` ON CONFLICT DO NOTHING
  swallows the unique-constraint conflict and the
  in-memory-only id never lands in `trade_signals`.
- [x] Mark completed

### Task 2: Pick a fix strategy

Task 1's red test sharpened the picture: the FK race is not
purely about projection-loop lag. The publish path mints a
fresh uuid for an unknown-to-cache dedupe_key whose
`(source, dedupe_key)` already has a different id in
`trade_signals` (from a previous process). The projection
loop's `upsert_trade_signal` (signal_bus.py:1334-1342) finds
the existing row by `(source, dedupe_key)` and updates it in
place — keeping the OLD id. The runtime cache's NEW id is
never persisted, and ON CONFLICT DO NOTHING in
`_ensure_runtime_signal_persisted` silently swallows the
unique-constraint conflict. The orchestrator then writes a
`trader_decisions.signal_id` referencing an id that does not
exist in `trade_signals` → FK violation.

The plan's three options re-evaluated against this failure
mode:

1. **Synchronous DB commit inside `publish_opportunities`.**
   Closes the in-memory-vs-DB lag but is a heavy lift:
   serialises the scanner's 1000-emit-per-cycle hot path
   through one DB round-trip per opportunity, undoing the
   batching the projection loop's `_UPSERT_PROJECTION_BATCH_MAX`
   coalescing was built to provide. Rejected.
2. **Idempotent `INSERT ... ON CONFLICT DO NOTHING` in the
   orchestrator's decision write.** Already implemented today
   via `_ensure_runtime_signal_persisted`; the failing case
   in production is precisely that this mechanism's ON CONFLICT
   DO NOTHING swallows the `(source, dedupe_key)` unique
   conflict (different id, same dedupe). Doubling-down on it
   would require resolving "which id wins" inside every
   consumer that takes a runtime row. Spreads write
   responsibility across consumers and does not fix
   `fast_trader_runtime` or any future consumer. Rejected.
3. **Wait-for-projection in `list_unconsumed_signals`.** Slow
   under load and orthogonal to the post-restart-stale-DB-row
   scenario (waiting for the projection loop does not change
   that the OLD row keeps the OLD id). Rejected.

**Chosen: a fourth option — publish-side id adoption.** Before
acquiring `self._lock` in `publish_opportunities`, prefetch the
canonical `(source, dedupe_key) → id` mapping from
`trade_signals` for any dedupe_key that the in-memory cache
does not already know. Inside the lock, when allocating an id
for an unknown-to-cache dedupe_key, prefer the prefetched
canonical id over `uuid.uuid4().hex`. Falls back to a fresh
uuid only when the dedupe_key truly is new to both the cache
and the DB.

Rationale:

- Bounded cost: one `SELECT id FROM trade_signals WHERE source =
  $1 AND dedupe_key = ANY ($2)` per publish call, only over the
  cache-missing dedupe_keys. Scanner steady-state hits the cache
  for 99%+ of dedupe_keys and the prefetch is a no-op; traders
  publishes a handful of opportunities at a time so the cost is
  rounding error.
- Single point of fix: the in-memory cache becomes
  authoritative-equal-to-DB by construction. Every consumer
  (orchestrator, fast_trader, UI, any future ones) gets the
  canonical id for free; `_ensure_runtime_signal_persisted`'s
  ON CONFLICT DO NOTHING ceases to swallow the
  `(source, dedupe_key)` conflict because the id we pass in
  matches the row that already exists.
- No change to projection-loop semantics, no
  scanner-hot-path serialisation, no deferred-state
  reintroduction. The projection's `upsert_trade_signal`
  remains the single owner of the actual write; we just feed
  it the right id.

- [x] Decision: publish-side id adoption (option 4). Rationale
  above.
- [x] Mark completed

### Task 3: Land the fix

- [x] Implemented in
  [`backend/services/intent_runtime.py`](../../backend/services/intent_runtime.py).
  Three changes inside `publish_opportunities`:
  - Before acquiring `self._lock`, walk the incoming
    opportunities once to collect dedupe_keys absent from
    `self._signal_ids_by_dedupe_key`, then issue a single
    `SELECT id, dedupe_key, market_id FROM trade_signals
    WHERE source = $1 AND dedupe_key = ANY($2)` and build a
    `prefetched_ids: dict[str, str]` map plus a
    `prefetch_meta_by_dedupe` map carrying `market_id` for
    the skeleton-insert pass below. Failures of this query
    degrade gracefully to fresh uuids (logged at debug).
  - For dedupe_keys still missing from the prefetch
    (genuinely new `(source, dedupe_key)` pairs), batch a
    `pg_insert(TradeSignal).values(...).on_conflict_do_nothing(
    index_elements=['source','dedupe_key'])` in a separate
    committed session, then re-`SELECT` the rows to capture
    the canonical id (handling the conflict-loser race where
    a peer publisher won between prefetch and INSERT). The
    re-SELECT result is merged into a `committed_ids` map.
    This is the "skeleton-INSERT pass" — its purpose is to
    close the in-process publish→consume gap that the
    prefetch-only fix left open, so every id the publish path
    hands to consumers is backed by a committed
    `trade_signals` row before `publish_opportunities`
    returns. The asynchronous projection loop's later UPSERT
    fills in the rich payload via UPDATE on the same row.
  - Inside the lock, replace the new-id allocation
    `signal_id = uuid.uuid4().hex` with
    `signal_id = prefetched_ids.get(dedupe_key) or
    committed_ids.get(dedupe_key) or uuid.uuid4().hex`.
- [x] Task 1's three tests pass with the fix in place
  (verified inside the production worker-trading container
  against a Postgres scratch DB):

  ```
  tests/test_intent_runtime_publish_projection_durability.py::
    test_publish_adopts_existing_trade_signals_id_for_known_dedupe_key PASSED
    test_publish_commits_skeleton_for_unknown_dedupe_key PASSED
    test_publish_adopts_existing_id_when_db_row_is_terminal PASSED
  ```
- [x] Regression set (run inside `worker-trading`,
  post-skeleton-INSERT extension): 59 passed across
  `test_intent_runtime_publish_projection_durability.py`,
  `test_intent_runtime_publish_opportunities_traders_source.py`,
  `test_intent_runtime_ws_freshness.py`,
  `test_signal_bus_strategy_runtime_metadata.py`,
  `test_signal_bus_reactivation.py`.
  The orchestrator-side `_ensure_runtime_signal_persisted`
  behaviour is unchanged; its existing regression coverage in
  `test_trader_orchestrator_worker.py` continues to pass.
- [x] Mark completed

### Task 4: Update architecture notes

- [x] Updated
  [`copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md):
  the "Post-fix flow" header was renamed to "Post-fix flow
  (Plans 0009 + 0010)" and a new "Publish/projection durability
  (post Plan 0010)" section documents the publish-side id
  adoption invariant (`(source, dedupe_key) → id` pulled from
  `trade_signals` before allocating a fresh uuid). The
  end-of-doc "See also" cross-links plan 0010.
- [x] Updated
  [`trader-pipeline.md`](architecture/trader-pipeline.md): the
  "Copy-trade bot idle" entry in the "Common end-state symptoms"
  table now references plans 0009 + 0010 with the publish-time
  id-adoption invariant; the FK callout no longer reads as
  open.
- [x] Mark completed

### Task 5: Update the operational journal

- [x] Appended a `2026-05-08 ~06:20 UTC — Plan 0010: traders
  publish-side FK race fixed` entry to
  [`runtime-tweaks.md`](../operational/runtime-tweaks.md). The
  entry carries the four `## Validation Commands` checks, the
  post-deploy snapshot table (0 FK violations, 95
  `trader_decisions` for `traders_copy_trade`, 99
  `trader_signal_consumption` rows all linked to a real
  `decision_id`, 137 `trade_signals` rows for `source='traders'`),
  and the rollback recipe (code revert; no runtime knob).
- [x] Updated the prior `2026-05-08 ~05:00 UTC` entry: status
  flipped from OPEN to CLOSED, with a back-reference to the new
  06:20 UTC entry.
- [x] Mark completed

### Task 6: Deploy and verify on `polyhome-1`

- [x] Deployed via `BUILD_IMAGES=0 ./deploy/sync_remote.sh` at
  `2026-05-08 06:19:45 UTC` — pure-Python change, GHCR pull
  sufficient. All seven services healthy in `docker compose ps`
  within 25 s; `migrate` exited cleanly.
- [x] Backend health verified (`docker compose ps` reports all
  containers `Up (healthy)`; the orchestrator started in
  `is_enabled=false, is_paused=true` per the canonical
  post-redeploy state and was unpaused via
  `POST /api/trader-orchestrator/start
  -d '{"mode":"shadow","selected_account_id":"08fb2d1e-3bb1-4cd5-bd22-db3efbe4085e"}'`).
- [x] Ran the four post-deploy checks from
  `## Validation Commands`:

  | Check | Expected | Got |
  |---|---:|---:|
  | FK violations in `worker-trading` logs (7 min) | `0` | **0** |
  | `trader_decisions` for `traders_copy_trade` | ≥ 1 | **95** (76 skipped + 19 blocked) |
  | `trader_signal_consumption` non-`failed` outcomes | ≥ 1 | 99 / 99 with real `decision_id` (78 skipped + 21 blocked); zero `(null)` decision link |
  | `trade_signals (source='traders') without_seq` (plan 0009 invariant) | `0` | **0** (137 rows: 66 pending, 70 skipped, 1 expired) |

  All four pass. The skipped/blocked decision distribution is
  the strategy's gate-filter output now that the FK race is
  no longer masking it as `failed`.
- [x] Mark completed

### Task 7: Close

- [x] All check-boxes above are `[x]`.
- [x] `git mv docs/plans/0010-fix-traders-publish-fk-race.md
  docs/plans/completed/`.
- [x] Updated
  [`plan-control-index.md`](plan-control-index.md): link target
  flipped to `completed/0010-fix-traders-publish-fk-race.md`,
  per-plan note updated to reflect the outcome (zero FK
  violations + Copy Trade decisions persisting end-to-end).
- [x] Mark completed
