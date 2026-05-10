# Plan: Eliminate fast-trader dedup-spam (signal_cache deep fix)

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0032` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

The fast-tier trader runtime re-evaluates the same `(trader_id,
signal_id)` pair thousands of times per active trading day. On
2026-05-10 the `Sandbox - Tail-End` bot logged ~5 000 `skipped`
decisions per day with the reason `trader_order already exists for
signal <X>`; one signal alone (`2606401e…`) produced 1 490 such
log lines in five hours. Every repeat costs an `evaluate()` call
plus a DB SELECT against `trader_orders` plus a buffered
`trader_decision` row, eating fast-lane budget (`0.2 s` per cycle)
and bloating the audit table.

The duplicates are not a money-bug — `fast_submit` blocks
duplicate submission via the `(trader_id, signal_id)`
idempotency-guard at [fast_submit.py:404](../../backend/services/trader_orchestrator/fast_submit.py:404).
But the wasted CPU has shown up as `Fast evaluate exceeded budget
(0.2s)` failures (12/day on the same trader), and the audit row
volume hides legitimate skip reasons behind the noise.

Three independent contributions cause the spam, and the deep fix
addresses all three:

1. **Cold-start consumed-set hydrates with `[]`.** Every
   restart of `worker-trading` calls
   `cache.hydrate_trader_consumed_ids(trader_id, [])` at
   [fast_trader_runtime.py:919](../../backend/workers/fast_trader_runtime.py:919) —
   so the consumed-set is empty and the trader re-walks every
   pending `trade_signals` row that has a corresponding
   `trader_orders` row already.

2. **Ring overflow wraps consumed signal_ids out of the set.**
   `_MAX_CONSUMED_RING_PER_TRADER = 1_000` at
   [signal_cache.py:80](../../backend/services/signal_cache.py:80).
   An active trader writes ~12 `mark_consumed` per minute, so
   the ring wraps in ~1.4 hours; once wrapped, scanner re-emits
   for "old" signals re-trigger duplicate submission attempts.

3. **`cache.upsert` overwrites snapshots without consulting
   the consumed-set.** Even when a trader has consumed a signal,
   the next Redis `signal_payloads` push lifts the snapshot back
   into `_signals` with a fresh `runtime_sequence`. The
   per-trader `_consumed_set` lookup in `get_unconsumed_signals`
   is the only thing that suppresses re-issuance, and (1)+(2)
   defeat it.

Done means: the same active trader logs ≤ 50 "already exists"
decisions per 24 h (down from 5 000+); restart of
`worker-trading` does not produce a re-walk burst; ring overflow
is no longer reachable in the relevant working window.

## Context / References

- [Trader Pipeline & Diagnostics](architecture/trader-pipeline.md)
- [worker-trading process model](architecture/worker-trading.md)
- [signal_cache.py:80 — `_MAX_CONSUMED_RING_PER_TRADER`](../../backend/services/signal_cache.py:80)
- [signal_cache.py:315 — `upsert`](../../backend/services/signal_cache.py:315)
- [signal_cache.py:354 — `hydrate_trader_consumed_ids`](../../backend/services/signal_cache.py:354)
- [signal_cache.py:398 — `get_unconsumed_signals`](../../backend/services/signal_cache.py:398)
- [fast_trader_runtime.py:919 — `hydrate_trader_consumed_ids(trader_id, [])`](../../backend/workers/fast_trader_runtime.py:919)
- [fast_submit.py:266–411 — `(trader_id, signal_id)` idempotency-guard](../../backend/services/trader_orchestrator/fast_submit.py:266)
- [intent_runtime.py:2543 — re-emit reseats `runtime_sequence`](../../backend/services/intent_runtime.py:2543)

## Validation Commands

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest -q backend/tests/test_signal_cache.py'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest -q backend/tests/test_trader_signal_cursor_scanning.py backend/tests/test_in_process_runtime_delivery.py'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend ruff check backend/services/signal_cache.py backend/workers/fast_trader_runtime.py'`

## Out of scope

This plan does not touch any of the 15 CRITICAL-tier safety
knobs (no `risk_limits`, no `block_new_orders`, no fill-policy
changes). It touches only the in-process consumed-set bookkeeping
and the cold-start hydration query — both internal to
`worker-trading`. No HIGH/MEDIUM-tier `risk_limits` /
`strategy_params` are changed either.

This plan also does not address the Cox-PH shadow simulator
pessimism question raised in the same operator session — that
is tracked separately in
[Plan 0033](0033-verify-cox-ph-shadow-fill-pessimism.md).

### Task 1: Add the consumed-set hydration query

- [x] In `backend/services/trader_orchestrator_state.py` (or a
  fitting helper module — not in `signal_cache.py`, which must
  stay free of DB layer imports), add an async function
  `fetch_recent_consumed_signal_ids(trader_id, *, hours=48,
  limit=50_000)` that runs:
  ```sql
  SELECT signal_id
  FROM trader_signal_consumption
  WHERE trader_id = :trader_id
    AND consumed_at >= now() - interval ':hours hours'
  ORDER BY consumed_at DESC
  LIMIT :limit
  ```
  The query must use the existing `trader_signal_consumption`
  table; do not add an index — the existing
  `(trader_id, signal_id)` PK + the implicit consumed_at
  ordering are good enough at 50k rows.
- [x] Cap rows at `limit` (50 000) so a misconfigured high-
  consumption trader cannot blow up worker memory at start-up.
  Document the cap in the docstring with the post-(2) ring
  size as the upper bound.
- [x] Return `list[str]` (not a set) — `hydrate_trader_consumed_ids`
  takes an `Iterable[str]` and the dedup happens inside the cache.
- [x] Add a unit test in
  `backend/tests/test_trader_orchestrator_state.py` that seeds
  `trader_signal_consumption` with mixed `consumed_at`
  timestamps (some inside, some outside the window) and
  asserts the function returns only the in-window IDs in
  descending order. Also assert the `limit` cap.
- [x] Mark completed

### Task 2: Wire the hydration query into the cold-start path

- [x] In
  [`backend/workers/fast_trader_runtime.py`](../../backend/workers/fast_trader_runtime.py)
  near line 919, replace the unconditional
  `cache.hydrate_trader_consumed_ids(trader_id, [])` with a
  call to `fetch_recent_consumed_signal_ids` followed by
  `cache.hydrate_trader_consumed_ids(trader_id, ids)`. The
  query lives inside the existing `FastAsyncSessionLocal()`
  block — no new session checkout.
- [x] Wrap the query in a `try`/`except` that swallows DB
  errors and falls back to `[]` (current behaviour) — the
  hydration is an optimisation, never a correctness gate.
  Log the failure at `warning` so it shows up in the
  worker-trading log without flooding.
- [x] Time the query and add the duration to
  `self._last_stage_timings_ms["coldstart_consumed_hydrate"]`
  so the operator can see the cost in the existing trader
  diagnostics view.
- [x] Add a regression test in
  `backend/tests/test_trader_orchestrator_worker.py` (or a
  fast-runtime-specific file if one already exists) that
  spins up a stub `cache` and asserts:
  (a) the cold-start hydration call passes the query result
  through, and (b) when the query raises, the cold-start
  still proceeds with `[]`.
- [x] Mark completed

### Task 3: Raise the consumed-ring cap and convert to set-only

- [x] In
  [`backend/services/signal_cache.py`](../../backend/services/signal_cache.py)
  remove `_MAX_CONSUMED_RING_PER_TRADER` and the `deque`-based
  ring entirely. Replace `_consumed_ids: dict[str, deque[str]]`
  + `_consumed_set: dict[str, set[str]]` with a single
  `_consumed_set: dict[str, set[str]]`.
- [x] Add a per-trader hourly background prune: every 1 hour
  (cheap), drop entries from `_consumed_set[trader_id]` whose
  signal_id is no longer present in `_signals` AND has
  `updated_at` older than 24 h (terminal-state cutoff). This
  caps long-term memory growth without re-introducing the wrap
  bug. Trigger the prune lazily inside `mark_consumed` when the
  per-trader set crosses 50 000 entries — no separate timer task.
- [x] Drop the `_MAX_CONSUMED_RING_PER_TRADER` references
  elsewhere (search the repo) and adjust callers that touch
  the ring directly (`hydrate_trader_consumed_ids` no longer
  needs the `deque`).
- [x] Update the existing tests in
  `backend/tests/test_signal_cache.py` that rely on the ring's
  bounded eviction (lines 320–340-ish) — assert the new
  unbounded set behaviour instead, and add a test for the
  50 000-entry lazy prune that confirms a stale signal_id
  (terminal status, no longer in `_signals`) is dropped while
  a fresh consumed signal is retained.
- [x] Mark completed

### Task 4: Make `cache.upsert` cheap when the signal is already consumed by every interested trader

- [x] In `signal_cache.py:315 (upsert)`, before writing the
  snapshot to `self._signals[snapshot.id]`, compute
  `interested_traders = self._consumed_set` keys (cheap —
  process-wide, < 100 entries). If **every** trader's
  consumed-set already contains `snapshot.id`, skip the
  upsert outright. Increment a new
  `self._upserts_skipped_consumed_overlap` counter and surface
  it in `status_snapshot()`.
- [x] This is a strict optimisation — wrong-but-conservative
  is fine: when in doubt, write the snapshot. The skip is
  safe only when ALL known traders have consumed; a brand-new
  trader joining mid-day still gets the upsert because its
  consumed-set is initially empty (Task 2 will hydrate it).
- [x] Add a unit test in `test_signal_cache.py` that seeds two
  traders, marks the signal consumed for both, then upserts a
  newer-`runtime_sequence` snapshot and asserts the snapshot
  in `_signals` is unchanged AND the
  `upserts_skipped_consumed_overlap` counter incremented.
- [x] Mark completed

### Task 5: Bump diagnostics and document the new behaviour

- [x] Add the new metrics
  (`upserts_skipped_consumed_overlap`,
  `consumed_set_size_per_trader`,
  `consumed_set_lazy_prunes_total`) to
  `signal_cache.status_snapshot()` so the existing
  `/api/diagnostics` endpoint surfaces them without code
  changes elsewhere.
- [x] Update
  [`docs/plans/architecture/trader-pipeline.md`](architecture/trader-pipeline.md)
  with a short section "Per-trader consumed-set" describing:
  (a) cold-start now hydrates from `trader_signal_consumption`
  for the last 48 h, (b) the in-process set is unbounded with
  lazy prune, (c) `cache.upsert` skips work when every known
  trader already consumed. Refresh the `Last verified` marker.
- [x] Update
  [`docs/plans/architecture/worker-trading.md`](architecture/worker-trading.md)
  if it mentions the ring cap or the empty-list hydrate
  (search for `MAX_CONSUMED_RING` and `hydrate_trader_consumed`).
  Refresh the `Last verified` marker.

  *Verified by `rg`: `worker-trading.md` does not mention the
  ring cap, the deque, the empty-list hydrate, or the
  `_consumed_set` directly — those concepts are covered in the
  trader-pipeline note. No edit required; `Last verified`
  marker untouched.*
- [x] Mark completed

### Task 6: Production validation on `polyhome-1`

- [ ] Deploy via `./deploy/sync_remote.sh` and watch
  `worker-trading` logs for one full restart cycle. Confirm
  the new `coldstart_consumed_hydrate` timing appears in the
  trader stage-timings panel (~tens of ms expected for a
  trader with thousands of recent consumed signals).
- [ ] After 1 hour of post-deploy steady state, run:
  ```sql
  SELECT date_trunc('hour', created_at) AS h,
         COUNT(*) FILTER (WHERE reason LIKE 'trader_order already exists%') AS dupe_n
  FROM trader_decisions
  WHERE trader_id = '388da687054c4b4a858ea152fff04900'
    AND created_at >= now() - interval '6 hours'
  GROUP BY 1 ORDER BY 1 DESC;
  ```
  Verify the post-deploy hours show ≤ 50/hour (was 200–400/hour
  for `Sandbox - Tail-End` on 2026-05-09 / 05-10).
- [ ] Verify `Fast evaluate exceeded budget (0.2s)` failures for
  the same trader drop to ≤ 1/day (was 12/day on 2026-05-10).
- [ ] If any of the above doesn't hold, leave the plan open and
  add a Task 7 with the diagnosis. Don't mark completed
  prematurely.
- [ ] Mark completed
