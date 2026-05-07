# Plan: Optimize worker-trading CPU hotspots (deepcopy, oracle history, json)

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](../README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](../completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](../README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](../plan-control-index.md).
>
> **Status: BACKLOG (2026-05-07, second archival).** Originally
> backlog'd pending plan 0005. Promoted to ACTIVE on
> 2026-05-07 after plan 0005's post-filter re-profile, then
> **re-archived on 2026-05-07** after plan 0006's lane-off
> re-profile (see
> [`architecture/worker-trading.md`](../architecture/worker-trading.md#after-plan-0006-2026-05-07-crypto-fast-binary-lane-off))
> showed all three remaining hotspots collapse below 1 % of CPU
> when the crypto fast-binary lane is off (the actual operating
> mode — operator trades only Polymarket general markets).
> Resurrect this plan if the operator turns the crypto lane back
> on and a fresh `py-spy` profile shows
> `get_oracle_history` / `copy.deepcopy` returning to ≥ 10 %.

## Overview

After plan 0005's tag-filter landed and trimmed the catalog by
~27 %, the 2026-05-07 post-filter py-spy profile of
`worker-trading`
([architecture note section "After plan 0005"](../architecture/worker-trading.md#after-plan-0005-2026-05-07-tag-whitelist-active))
shows two pure-Python CPU hotspots still dominate:

1. `get_oracle_history` linear scan + bucketing on every call,
   no cache (~36 % combined across multiple inner lines).
2. `copy.deepcopy` chain on the crypto opportunity payload
   (~10.8 % combined).

Plus a smaller bonus target that survives across both profiles:

3. `stdlib json` on the dispatch hot path (`json.dump` +
   `raw_decode` ≈ 3 %).

(`_compute_stability` was the third hotspot in the pre-filter
profile but dropped from ~5 % to <1 % post-filter; see plan
notes — Task 3 below skipped.)

Each remaining hotspot is a 10–50 line localised change. Done =
worker-trading CPU samples show the surviving hotspots reduced
to < 5 % each, with a re-captured flamegraph as evidence.

This plan is **opportunistic, not architectural**. It does not
touch the GIL, the process model, or the async event loop. It
treats specific algorithmic inefficiencies discovered by
profiling.

## Why the upstream filter alone wasn't enough

The natural way to reduce these hotspots was to feed less data
into them: a tag whitelist at the Polymarket ingest layer
([`scanner.py`](../../../backend/services/scanner.py)) multiplies
the funnel reduction across **every** Polymarket-derived
downstream consumer. Plan 0005 delivered that whitelist and
shrank the catalog by ~27 %. But the two surviving hotspots run
on data that **isn't gated by the catalog**: the crypto
fast-binary lane reads its reference series from the **Binance**
WS feed and the Chainlink oracle history, neither of which the
tag filter touches. Their absolute CPU cost was therefore
roughly constant across the two profiles — and rose in *share*
because the catalog-driven hotspots shrank around them. So the
local fixes below are now the primary lever for further CPU
relief.

## Context / References

- [Architecture: worker-trading process model + CPU profile](../architecture/worker-trading.md)
- [Plan 0003 — Profile worker-trading hotspots](../completed/0003-profile-worker-trading-hotspots.md)
- [Plan 0005 — Tag-based market filter at ingest](../completed/0005-tag-based-market-filter-at-ingest.md)
- [Plan 0006 — Crypto fast-binary lane toggle](../completed/0006-crypto-fast-binary-lane-toggle.md)
- [Pre-filter flamegraph](../architecture/worker-trading-profile-2026-05-07.svg)
- [Post-filter flamegraph](../architecture/worker-trading-profile-2026-05-07-post-filter.svg)
- [Lane-off flamegraph](../architecture/worker-trading-profile-2026-05-07-crypto-lane-off.svg)
- [`market_runtime.py:1525-1583`](../../../backend/services/market_runtime.py)
- [`reference_runtime.py:200-240`](../../../backend/services/reference_runtime.py)
- [`market_monitor.py:140-167`](../../../backend/services/market_monitor.py)

## Validation Commands

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest -q backend/tests/services/test_market_runtime.py backend/tests/services/test_reference_runtime.py'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend ruff check backend/services/market_runtime.py backend/services/reference_runtime.py backend/services/market_monitor.py'`
- Re-profile (manual, requires plan-0003 ptrace cap recipe): a
  fresh py-spy capture under steady-state load shows the
  remaining hotspots reduced to < 5 % self-time.

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
  The post-filter profile shows `_oracle_move_from_history` at
  ~6.6 %, which suggests it does walk the same history; expect
  the shared helper to fall out of this cache as a bonus.
- [ ] Mark completed

### Task 3: Vectorise `_compute_stability` — DESCOPED

- [x] **Skipped** — the post-filter profile shows
  `_compute_stability` at <1 % CPU (down from ~5 % pre-filter).
  No remaining work; the hotspot is already below the < 5 %
  target threshold. This task remains in the file as a
  historical record only.
- [x] Mark completed

### Task 4: Replace stdlib `json` with `orjson` on the dispatch path

- [ ] Identify the exact `json.dump` and `raw_decode` callers
  visible in both 2026-05-07 profiles. Most likely candidates:
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
  [plan 0003 Task 2](../completed/0003-profile-worker-trading-hotspots.md)
  / [plan 0005 Task 8](../completed/0005-tag-based-market-filter-at-ingest.md)
  (whichever lands as completed first).
- [ ] Re-run the 60 s py-spy capture (`--rate 100`, no `--idle`)
  under the same workload as 2026-05-07 (one fast trader,
  `Sandbox - Traders Copy Trade` active) **with the same tag
  filter active** (`crypto`, `sports`, `politics`) so the
  comparison stays apples-to-apples vs the post-filter baseline.
- [ ] Compare top-N table to the post-filter baseline. Each of
  the targeted frames (`get_oracle_history`, `copy.deepcopy`
  chain, `json` callers) should be < 5 % self-time. If any is
  not, open a sub-issue rather than re-opening this whole plan —
  there's likely a second-order hotspot that needs its own
  treatment.
- [ ] Save the new SVG to
  `docs/plans/architecture/worker-trading-profile-<YYYY-MM-DD>-post-0004.svg`
  and append a "After plan-0004" subsection to the architecture
  note's "Measured CPU profile" section.
- [ ] Revert the cap_add. Confirm the worker keeps writing
  `trader_orders` after the recreate.
- [ ] Mark completed

### Task 6: Update architecture note + close

- [ ] In [`architecture/worker-trading.md`](../architecture/worker-trading.md),
  in the "Measured CPU profile" section, append a "Post-0004"
  table with the new top-N. State whether the targeted hotspots
  fell below the 5 % threshold.
- [ ] If the GIL ceiling is *still* the next limit (i.e. CPU is
  now genuinely saturated by many small frames with no single
  dominant one), promote one of Options 1–3 (free-threaded
  Python, ProcessPool, plane split) to a follow-up plan.
  Otherwise, the document closes the door on GIL-removal as the
  next step.
- [ ] `git mv docs/plans/0004-optimize-worker-trading-cpu-hotspots.md
  docs/plans/completed/`.
- [ ] Update [`plan-control-index.md`](../plan-control-index.md):
  link target to `completed/0004-...md`.
- [ ] Mark completed

## Out of scope

- **GIL removal / Python 3.13 free-threaded build / ProcessPool /
  plane split.** Those are Options 1–3 in
  [`architecture/worker-trading.md`](../architecture/worker-trading.md)
  and remain *candidates* if 0004 leaves residual GIL pressure.
- **Reducing the input volume of markets.** That was plan 0005
  (upstream tag filter), now landed. The two are complementary —
  this plan trims the per-item cost; that one trimmed the item
  count.
- **Caching strategy decisions** beyond the simple TTL-cache for
  `get_oracle_history`. If a more sophisticated cache layer is
  warranted, that's a separate plan.
