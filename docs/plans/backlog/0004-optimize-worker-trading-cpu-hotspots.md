# Plan: Optimize worker-trading CPU hotspots (deepcopy, oracle history, stability)

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](../README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](../completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](../README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](../plan-control-index.md).
>
> **Status: BACKLOG.** This plan is parked because the upstream
> mitigation (market category filtering — see plan that supersedes
> it) is expected to reduce the same hotspots by shrinking the
> input volume. Pull this plan back into the active queue only if
> a re-profile *after* the filtering plan still shows ≥ 10 % CPU
> in any of these three hotspots.

## Overview

The 2026-05-07 py-spy profile of worker-trading
([architecture note section "Measured CPU profile"](../architecture/worker-trading.md#measured-cpu-profile-2026-05-07))
identified three pure-Python CPU hotspots that together account
for ~34 % of CPU-active time:

1. `copy.deepcopy` called twice on the crypto opportunity payload
   (~15 %).
2. `get_oracle_history` linear scan + bucketing on every call,
   no cache (~14 %).
3. `_compute_stability` nested Python loop over price history
   (~5 %).

Plus a smaller bonus target:

4. `stdlib json` on the dispatch hot path (~4 %).

Each is a 10–50 line localised change. Done = worker-trading CPU
samples show all four hotspots reduced to < 5 % each, with a
re-captured flamegraph as evidence.

This plan is **opportunistic, not architectural**. It does not
touch the GIL, the process model, or the async event loop. It
treats specific algorithmic inefficiencies discovered by
profiling.

## Why this is in backlog

The natural way to reduce these hotspots is to feed less data
into them: a category whitelist at the Polymarket ingest layer
([`scanner.py:937`](../../../backend/services/scanner.py))
multiplies the funnel reduction across **every** downstream
consumer — the same deepcopy now copies a shorter list, the same
oracle lookup runs over fewer assets, the same stability loop
processes fewer rows. Instead of fixing each leaf, fix the root.

If the upstream filter lands and the next profile still shows
material time in any of these three frames, this plan returns to
the active queue with the **same task list** (no re-derivation
needed). The hotspots are stable code, not flaky.

## Context / References

- [Architecture: worker-trading process model + CPU profile](../architecture/worker-trading.md)
- [Plan 0003 — Profile worker-trading hotspots](../completed/0003-profile-worker-trading-hotspots.md)
- [Profiling artefacts](../architecture/worker-trading-profile-2026-05-07.svg)
- [`market_runtime.py:1525-1583`](../../../backend/services/market_runtime.py)
- [`reference_runtime.py:200-240`](../../../backend/services/reference_runtime.py)
- [`market_monitor.py:140-167`](../../../backend/services/market_monitor.py)

## Validation Commands

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest -q backend/tests/services/test_market_runtime.py backend/tests/services/test_reference_runtime.py'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend ruff check backend/services/market_runtime.py backend/services/reference_runtime.py backend/services/market_monitor.py'`
- Re-profile (manual, requires plan-0003 ptrace cap recipe): a
  fresh py-spy capture under steady-state load shows each of the
  four hotspots reduced to < 5 % self-time.

### Task 1: Eliminate the redundant deepcopy in the crypto dispatch path

- [ ] Read [`market_runtime.py:1525-1583`](../../../backend/services/market_runtime.py)
  end-to-end. Confirm that the payload is *not* mutated between
  `_queue_opportunity_dispatch` (line 1533) and
  `_run_opportunity_dispatch_loop` (line 1560). If true, the
  outer deepcopy (1533) is redundant — the dispatcher consumes
  the buffered list under `_pending_opportunity_lock`, then
  immediately wraps it.
- [ ] Replace the line-1533 `copy.deepcopy(payload)` with a
  shallow reference store (`self._pending_opportunity_payload =
  payload`). Keep the line-1560 deepcopy to preserve the
  defensive-copy contract toward event handlers.
- [ ] Add a unit test asserting that mutating the *input* list
  after `_queue_opportunity_dispatch` returns does not affect a
  subsequent dispatched payload (this is the contract we *do*
  want to keep on the dispatch side, not the queue side).
- [ ] If a downstream caller is found that mutates the returned
  payload before dispatch, document the assumption in the
  function docstring and revert the change rather than masking
  the bug.
- [ ] Mark completed

### Task 2: Add a TTL cache to `get_oracle_history`

- [ ] Read [`reference_runtime.py:200-240`](../../../backend/services/reference_runtime.py).
  Note that the function is keyed by
  `(asset, points, max_age_seconds)` and walks `_history` linearly
  every call. Chainlink price ticks arrive at most once per few
  seconds.
- [ ] Add a `functools.lru_cache`-style or hand-rolled TTL cache
  (TTL ≈ 1.0 s, configurable). Cache key:
  `(asset, points, max_age_seconds, last_history_len)`. Including
  `last_history_len` ensures the cache invalidates when a new
  tick arrives without needing a separate timer.
- [ ] Verify with a unit test that two calls with identical
  parameters within the TTL return the same object reference and
  that a new tick (changes `last_history_len`) busts the cache.
- [ ] Confirm `get_oracle_motion_summary` (same module) doesn't
  duplicate the same scan — if it does, factor a shared helper.
- [ ] Mark completed

### Task 3: Vectorise `_compute_stability`

- [ ] Read [`market_monitor.py:140-167`](../../../backend/services/market_monitor.py).
  The current Python double loop computes
  `sum(abs(curr_prices[j] - prev_prices[j]))` over a price-history
  list. Convert to a numpy single-pass computation:
  `np.diff(np.array(price_history), axis=0)` then
  `np.abs(...).sum()`.
- [ ] Make sure the numpy path handles the "fewer than 2
  observations" edge case the same way (return 0.5).
- [ ] Add a unit test that the numpy result equals the Python
  result for several fixture histories (regression guard).
- [ ] Mark completed

### Task 4: Replace stdlib `json` with `orjson` on the dispatch path

- [ ] Identify the exact `json.dump` and `raw_decode` callers
  visible in the 2026-05-07 profile. Most likely candidates:
  the `DataEvent.payload` serialisation in
  [`market_runtime.py:1556-1561`](../../../backend/services/market_runtime.py)
  and the WS message decode loop in `services/ws_feeds.py`.
- [ ] Add `orjson` to the backend's dependency manifest (already
  used in some places? check first to avoid duplication).
- [ ] Replace the hot-path callers only — do **not** do a global
  search-and-replace. orjson's API differs subtly from stdlib
  (`bytes` vs `str`, no `default=` for some types).
- [ ] Add a smoke test that the serialised output is byte-equal
  for a representative payload.
- [ ] Mark completed

### Task 5: Re-capture profile, verify each hotspot is below threshold

- [ ] Re-apply the temporary `cap_add: [SYS_PTRACE]` per
  [plan 0003 Task 2](../completed/0003-profile-worker-trading-hotspots.md).
- [ ] Re-run the 60 s py-spy capture (`--rate 100`, no `--idle`)
  under the same workload as 2026-05-07 (one fast trader,
  Sandbox - Traders Copy Trade active).
- [ ] Compare top-N table to the original. Each of the four
  targeted frames should be < 5 % self-time. If any is not,
  open a sub-issue rather than re-opening this whole plan —
  there's likely a second-order hotspot that needs its own
  treatment.
- [ ] Save the new SVG to
  `docs/plans/architecture/worker-trading-profile-<YYYY-MM-DD>.svg`
  and append a "After plan-0004" subsection to the architecture
  note's "Measured CPU profile" section.
- [ ] Revert the cap_add. Confirm the worker keeps writing
  `trader_orders` after the recreate.
- [ ] Mark completed

### Task 6: Update architecture note + close

- [ ] In [`architecture/worker-trading.md`](../architecture/worker-trading.md),
  in the "Measured CPU profile" section, append a "Post-0004"
  table with the new top-N. State whether the four hotspots
  fell below the 5 % threshold.
- [ ] If the GIL ceiling is *still* the next limit (i.e. CPU is
  now genuinely saturated by many small frames with no single
  dominant one), promote one of Options 1–3 to a follow-up
  plan. Otherwise, the document closes the door on
  GIL-removal as the next step.
- [ ] `git mv docs/plans/backlog/0004-optimize-worker-trading-cpu-hotspots.md
  docs/plans/completed/`.
- [ ] Update [`plan-control-index.md`](../plan-control-index.md):
  link target to `completed/0004-...md`.
- [ ] Mark completed

## Out of scope

- **GIL removal / Python 3.13 free-threaded build / ProcessPool /
  plane split.** Those are Options 1–3 in
  [`architecture/worker-trading.md`](../architecture/worker-trading.md)
  and remain *candidates* if 0004 leaves residual GIL pressure.
- **Reducing the input volume of markets.** That's a different
  plan (upstream category filter). The two are complementary —
  this plan trims the per-item cost; the other trims the item
  count.
- **Caching strategy decisions** beyond the simple TTL-cache for
  `get_oracle_history`. If a more sophisticated cache layer is
  warranted, that's a separate plan.
