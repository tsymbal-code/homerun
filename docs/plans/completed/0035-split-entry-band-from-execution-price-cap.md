# Plan: Split entry-band cap from execution-price cap in shadow chase-up

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0035` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

The shadow execution path computes `shadow_limit_price` for a
chase-up `taker_limit BUY` by reducing six candidate caps with
`min(...)` at
[`order_manager.py:962–980`](../../backend/services/trader_orchestrator/order_manager.py:962):

```python
explicit_buy_caps = [
    _valid_execution_bound(leg.get("max_execution_price")),
    _valid_execution_bound(metadata_for_caps.get("max_execution_price")),
    _valid_execution_bound(params.get("max_execution_price")),
    _valid_execution_bound(params.get("max_entry_price")),
    _valid_execution_bound(params.get("max_probability")),
    _derive_min_upside_price_cap(params.get("min_upside_percent")),
]
tightest_explicit_cap = min(...)
```

Two of those caps are **entry-band guards**, not execution-price
caps:

- `params.max_probability` — the strategy's entry-band ceiling.
  Used at signal-emission time to refuse opportunities whose
  probability is already too close to 1.0 to leave room for the
  `min_upside_percent` floor. It is NOT an opinion about how high
  the bot is willing to lift its limit on the wire to capture an
  in-band opportunity that has already been emitted.
- `_derive_min_upside_price_cap(min_upside_percent)` — same
  category. `min_upside_percent` is an entry-time floor; the
  derived price cap (`1 / (1 + p/100)`) describes "highest entry
  price at which the upside floor is met," again at signal time.

The remaining four caps (`leg.max_execution_price`,
`metadata.max_execution_price`, `params.max_execution_price`,
`params.max_entry_price`) are **execution-price caps**: the
strategy's explicit instruction "you may chase up to X to fill
me." `tail_end_carry.py:809` writes `max_execution_price = price
+ max(0.015, (1.0 − price) · 0.45)` (≈ 0.9478 for price=0.905) —
a deliberate chase-up target.

`min(...)` over the union collapses the chase-up target. For
the `Sandbox - Tail-End` bot with `max_probability=0.905`, the
realised `shadow_limit_price` is 0.905 even though the strategy
emitted `max_execution_price=0.94775` and the order-book
`best_ask` was in [0.91, 0.92] — so the simulator (correctly,
given that limit) returns `limit_price_not_executable` for 33
of 33 cancelled orders.

[Plan 0033](completed/0033-verify-cox-ph-shadow-fill-pessimism.md)
classified 25/27 evidential cancellations into Bucket A
(config-driven via this same `min(...)` reduction), 0 into
Bucket B (simulator pessimism), 2 into Bucket C (book really
absent). The fix is structural, not a knob tweak.

Done means: `shadow_limit_price` reduces only over execution-
price caps (the four explicit `max_execution_price` /
`max_entry_price` candidates). Entry-band caps are ignored at
the chase-up reduction. The bug is reproduced by a unit test
that fails before the fix and passes after.

## Context / References

- [Architecture: Cox-PH fill simulator, live execution, reconciliation, redeemer](architecture/execution-and-fills.md)
- [Plan 0033 — Verify Cox-PH shadow-fill pessimism (verdict)](completed/0033-verify-cox-ph-shadow-fill-pessimism.md)
- [order_manager.py:225 — `_valid_execution_bound`](../../backend/services/trader_orchestrator/order_manager.py:225)
- [order_manager.py:232 — `_derive_min_upside_price_cap`](../../backend/services/trader_orchestrator/order_manager.py:232)
- [order_manager.py:315 — `_resolve_execution_price_bounds`](../../backend/services/trader_orchestrator/order_manager.py:315)
- [order_manager.py:958–980 — chase-up `min(...)` reduction (the bug)](../../backend/services/trader_orchestrator/order_manager.py:958)
- [tail_end_carry.py:809 — `max_execution_price = target_price`](../../backend/services/strategies/tail_end_carry.py:809)
- [docs/strategies/_common-bot-parameters.md — knob matrix](../strategies/_common-bot-parameters.md)
- [docs/operational/runtime-tweaks.md — Plan 0033 verdict entry](../operational/runtime-tweaks.md)

## Validation Commands

- `bash scripts/run_tests_remote.sh tests/test_execution_session_engine.py tests/test_execution_latency_metrics.py tests/test_trader_order_manager_live.py`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose run --rm --no-deps backend python -c "import py_compile; py_compile.compile(\"/app/backend/services/trader_orchestrator/order_manager.py\", doraise=True); print(\"OK\")"'`

> The original plan listed `tests/test_strategy_tail_end_carry.py`,
> which doesn't exist (and per `.cursor/rules/strategies.mdc`,
> per-slug strategy tests are forbidden). It also listed
> `ruff check`, but `ruff` isn't installed in the runtime image
> (`docker compose exec -T backend ruff check ...` returns
> `executable file not found`). `py_compile` is the substitute
> syntax check; `test_trader_order_manager_live.py` is the
> direct regression home for this plan's fix.

## Out of scope

This plan does not touch any of the 15 CRITICAL-tier safety
knobs. `max_probability` and `min_upside_percent` are
strategy_params (HIGH/MEDIUM tier per
[knob matrix](../strategies/_common-bot-parameters.md)). The
fix is a code-side semantic split, not a knob value change —
existing operator config carries through unchanged. Behaviour
change: chase-up will now lift `shadow_limit_price` to the
explicit execution-price cap, ignoring the entry-band ceiling
the strategy already cleared at signal-emission time.

This plan also does not touch the live-execution path. The
identical `min(...)` reduction lives only on the shadow branch
at line 958–980; the live branch uses a different
`max_execution_price` / `min_execution_price` pair returned
from `_resolve_execution_price_bounds` (line 883). If
`_resolve_execution_price_bounds` itself mixes the two cap
classes the same way (Task 1 must check), the fix extends
there too — but the test set proves only the shadow path
today.

The `Sandbox - Tail-End` `max_probability=0.97` operator
override applied on 2026-05-10 is left in place. Its effect
will compound with this fix: post-fix, the bot can chase up
to its full `target_price` regardless of the entry-band
ceiling, and the entry-band ceiling itself gates only signal
emission. That's the correct end state.

### Task 1: Audit `_resolve_execution_price_bounds` for the same bug

- [x] Read [`order_manager.py:315`](../../backend/services/trader_orchestrator/order_manager.py:315)
  and surrounding helpers. Determine whether the live-execution
  `max_execution_price` reducer also conflates entry-band caps
  (`max_probability`, `min_upside_percent` cap) with
  execution-price caps. Document the finding inline in the
  plan as a sub-bullet under this task.
  - **Finding: same conflation exists in
    `_resolve_execution_price_bounds` for the BUY branch.**
    Lines 329–336 build a six-element `candidates` list that
    includes `_valid_execution_bound(strategy_params.get(
    "max_probability"))` and
    `_derive_min_upside_price_cap(strategy_params.get(
    "min_upside_percent"))` and reduces with `min(...)` —
    structurally identical to the shadow chase-up reducer at
    lines 965–976. The returned `max_execution_price` is then
    handed to `live_execution_adapter.execute_live_order` (see
    `order_manager.py:1165` and `:1196`), so live orders also
    inherit the entry-band conflation. The SELL branch (lines
    348–354) does not have the bug — it correctly limits to
    execution-price caps only (`min_execution_price`,
    `min_exit_price`, `min_sell_price`).
  - Existing test
    `tests/test_trader_order_manager_live.py::
    test_submit_execution_leg_live_taker_limit_caps_execution_to_dynamic_price_bound`
    pins the bug at line 159: it asserts
    `submit_kwargs["max_execution_price"] == 100.0 / 105.0`,
    where 100/105 is the value derived from
    `min_upside_percent=5.0`. Updating that test is part of
    Task 2's scope.
- [x] If the same conflation exists, the fix in Task 2 must
  apply both at the shadow chase-up site and inside
  `_resolve_execution_price_bounds`. If it doesn't, the fix
  is shadow-only — note that explicitly so reviewers know the
  scope.
  - **Decision:** the fix extends to
    `_resolve_execution_price_bounds` (BUY branch only).
    Shadow chase-up site at lines 965–971 also gets the
    helper. SELL branch is left untouched.
- [x] Mark completed

### Task 2: Introduce `_chase_up_execution_caps()` helper and migrate the shadow site

- [x] Add a new helper to
  [`backend/services/trader_orchestrator/order_manager.py`](../../backend/services/trader_orchestrator/order_manager.py),
  near `_valid_execution_bound`:
  ```python
  def _chase_up_execution_caps(
      *,
      leg: dict[str, Any],
      metadata: dict[str, Any],
      params: dict[str, Any],
  ) -> list[float]:
      """Return only the execution-price caps eligible for chase-up.

      Excludes entry-band guards (`max_probability`,
      derived-from-`min_upside_percent`), which apply at
      signal-emission, not at submit. See Plan 0035.
      """
      candidates = [
          _valid_execution_bound(leg.get("max_execution_price")),
          _valid_execution_bound(metadata.get("max_execution_price")),
          _valid_execution_bound(params.get("max_execution_price")),
          _valid_execution_bound(params.get("max_entry_price")),
      ]
      return [cap for cap in candidates if cap is not None]
  ```
- [x] Replace the `explicit_buy_caps` list at
  [`order_manager.py:965–972`](../../backend/services/trader_orchestrator/order_manager.py:965)
  with a call to the new helper. Remove `params.max_probability`
  and `_derive_min_upside_price_cap(params.get("min_upside_percent"))`
  from the chase-up reduction.
- [x] Preserve the existing `tightest_explicit_cap is None →
  shadow_limit_price = 1.0` fallback. The "no caps at all" path
  is the natural market boundary; it stays untouched.
- [x] If Task 1 found the same bug in
  `_resolve_execution_price_bounds`, mirror the change there
  too: split the reducer into entry-band vs execution-price,
  and surface only the execution-price cap into the
  `max_execution_price` return value.
  - Done: BUY branch of `_resolve_execution_price_bounds` now
    delegates to `_chase_up_execution_caps`. Updated existing
    `test_trader_order_manager_live.py::test_submit_execution_leg_live_taker_limit_caps_execution_to_dynamic_price_bound`
    (renamed to `..._to_explicit_execution_price_bound`) to set
    `max_execution_price=0.96` and assert the cap; added a
    sibling test `..._ignores_entry_band_caps_for_max_execution_price`
    that pins the post-fix fallback when only entry-band
    knobs are present (returns the signal price, not
    `100/105`).
- [x] Mark completed

### Task 3: Regression test that fails before the fix

- [x] Add `backend/tests/test_shadow_chase_up_caps.py` (or
  extend an existing shadow-path test file if one is the
  conventional home — search the repo first; do not invent a
  new file when an existing one fits). The test:
  - Builds a synthetic `params` dict with
    `max_probability=0.905`, `min_upside_percent=6`,
    `allow_taker_limit_buy_above_signal=True`.
  - Builds a synthetic `leg` dict with
    `max_execution_price=0.9478`, `price=0.905`, side=`buy`.
  - Calls into the chase-up branch (either via a direct
    helper test of `_chase_up_execution_caps`, or via a
    mocked `submit_execution_leg` exercising the shadow
    branch with a stubbed book where `best_ask=0.92`).
  - Asserts `shadow_limit_price == 0.9478` (post-fix). The
    test must fail on `main` pre-fix (current code gives
    `0.905`) — verify by `git stash` of the production
    change and re-running the test.
  - Added to the existing
    `backend/tests/test_trader_order_manager_live.py`
    (sibling to `test_shadow_buy_with_chase_up_lifts_simulator_limit_so_asks_above_mid_fill`),
    not a new file. Two helper unit tests
    (`test_chase_up_execution_caps_excludes_entry_band_guards`,
    `test_chase_up_execution_caps_returns_empty_when_only_entry_band_caps_present`)
    + one integration test
    (`test_shadow_chase_up_uses_explicit_max_execution_price_over_max_probability`)
    pin the chase-up reduction.
- [x] Add a second test that pins the existing safety
  property: when neither `leg.max_execution_price` nor any
  other execution-price cap is present, `shadow_limit_price`
  falls back to 1.0. This guards against accidental
  regressions in the helper's filter logic.
  - Added as
    `test_shadow_chase_up_falls_back_to_one_when_no_execution_price_cap`.
    Stubbed book with `ask=0.99`; pre-fix `shadow_limit_price`
    collapses to `min(max_probability=0.95, derived(6)≈0.943)`
    and the simulator rejects ask=0.99 → `skipped`. Post-fix
    `shadow_limit_price = 1.0` and the simulator crosses to
    `effective_price=0.99`.
- [x] Pre-fix verification round-trip executed: `git stash`
  of `backend/services/trader_orchestrator/order_manager.py`
  + `./deploy/sync_remote.sh` (rebuild) + targeted pytest
  → 6/6 Plan 0035 tests fail. `git stash pop` + redeploy →
  6/6 pass. Verified 2026-05-10.
- [x] Mark completed

### Task 4: Live-data simulation against Plan 0033 artefacts

- [x] Take the 25 Bucket-A rows from
  [`docs/plans/work-artifacts/0033-bucket-classification.md`](work-artifacts/0033-bucket-classification.md)
  and run them through the post-fix `_chase_up_execution_caps`
  in a one-shot script (
  `scripts/simulate_0035_chase_up_caps.py`, deletable after
  use). For each row, compute the post-fix
  `shadow_limit_price` and compare against the recorded
  `best_ask` in `0033-book-snapshot-join.csv`. Count rows
  where `post_fix_shadow_limit ≥ best_ask` — these are the
  fills that the fix recovers.
  - Done. Script
    [`scripts/simulate_0035_chase_up_caps.py`](../../scripts/simulate_0035_chase_up_caps.py)
    imports the production helper and replays each row.
    Output (run inside the backend container against the
    `0033-book-snapshot-join.csv` artefact):
    ```
    Bucket A (config-gated):      24 / 24 recovered
    Bucket C (book above target):  0 /  2 recovered
    Indeterminate (no snapshot):   7
    Total evidenced:              24 / 26 recovered
    ```
  - The canonical CSV has 24 (not 25) Bucket-A rows; one
    row the bucket-classification.md narrative attributed to
    Bucket A is `no_book_snapshot` in the CSV. Recovery rate
    against actual evidence is 100 % for Bucket A.
- [x] Append the count to
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  as an addendum under the Plan 0033 verdict entry. Format:
  `### 2026-MM-DD — Plan 0035 cap-split simulation
  (X / 25 Bucket-A rows recovered)`.
  - Appended as `### 2026-05-10 — Plan 0035 cap-split
    simulation (24 / 24 Bucket-A rows recovered)` (the
    plan's "X / 25" was a 1-off; explained inline).
- [x] Mark completed

### Task 5: Production rollout and validation

- [x] Deploy via `./deploy/sync_remote.sh`. Watch
  `worker-trading` logs for one full
  scanner-cycle window (~5 min) and confirm no new exception
  classes appear. The chase-up path runs on every shadow BUY
  with `allow_taker_limit_buy_above_signal=True`, so coverage
  is immediate.
  - Deployed 2026-05-10. All 8 containers came up healthy
    (`docker compose ps`: backend, frontend, postgres,
    redis, worker-discovery, worker-news, worker-trading
    Up + healthy). worker-trading log scan over the first
    ~3 minutes:
    `docker compose logs --since=3m worker-trading | grep
    -E '"level": "(ERROR|CRITICAL)"' | grep -v "Missing
    Polymarket"` → empty. Only the pre-existing
    `live_execution_service: Missing Polymarket API
    credentials` warning (predates this plan; unrelated).
    No new exception classes.
  - Confirmed bot config still in operator's intended
    state: `max_probability=0.97`,
    `min_upside_percent=6`,
    `allow_taker_limit_buy_above_signal=true` (queried
    `traders.source_configs_json` for trader
    `388da687054c4b4a858ea152fff04900`). Post-fix, the
    chase-up reducer will lift `shadow_limit_price` to
    `target_price = max_execution_price = max_entry_price`
    written by `tail_end_carry.py:809-810`; the
    `max_probability=0.97` setting now gates only signal
    emission (its correct semantic).
- [ ] After 24 h post-deploy steady state, run:
  ```sql
  SELECT
      date_trunc('day', created_at) AS day,
      COUNT(*) FILTER (WHERE status = 'executed') AS executed,
      COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled,
      COUNT(*) FILTER (WHERE status = 'cancelled'
                          AND payload_json#>>'{leg,reason}' = 'limit_price_not_executable') AS dropped_by_chase_cap
  FROM trader_orders
  WHERE trader_id = '388da687054c4b4a858ea152fff04900'
    AND created_at >= now() - interval '7 days'
  GROUP BY 1 ORDER BY 1 DESC;
  ```
  Verify `dropped_by_chase_cap` for the post-deploy day(s) is
  meaningfully lower than the pre-deploy 14-day baseline of
  33/14 ≈ 2.4/day. Account for the operator's
  `max_probability=0.97` knob change (2026-05-10) when reading
  the baseline — the per-day dropped count was already trending
  down before this plan landed.
  - **Pre-deploy baseline captured at deploy time
    (2026-05-10 ~14:05 UTC):**

    | Day | executed | cancelled | dropped_by_chase_cap |
    |---|---:|---:|---:|
    | 2026-05-07 | 0 | 7 | 7 |
    | 2026-05-08 | 0 | 3 | 3 |
    | 2026-05-09 | 2 | 13 | 13 |
    | 2026-05-10 (partial, pre-deploy) | 7 | 10 | 10 |

    100 % of cancellations on this bot are
    `limit_price_not_executable` — the cap-collapse pattern.
    Total: 33 dropped over 4 days = 8.25/day (the plan's
    quoted "33/14 ≈ 2.4/day" includes days where the bot
    wasn't yet active; the active-window rate is the
    relevant comparison).
  - **24 h SQL re-run handed off to**
    [Plan 0037 — Verify Plan 0035 chase-cap drop on 2026-05-11](0037-verify-plan-0035-chase-cap-drop-2026-05-11.md).
    That plan owns the `dropped_by_chase_cap` measurement and
    the verdict (drop confirmed → close 0037; drop not
    observed → re-open Plan 0035 with a Task 7 diagnosis).
- [x] If `dropped_by_chase_cap` did NOT drop, leave the plan
  open and add a Task 6 with the diagnosis. Common
  alternatives: the live-execution path conflates the same
  caps and Task 1 missed it; the order-book on relevant
  markets is genuinely deeper than the chase-up target.
  - Conditional follow-up — owned by Plan 0037 (above).
    No diagnosis needed today: the immediate post-deploy
    log scan was clean and the simulation against Plan 0033
    evidence recovered 24/24 Bucket-A rows. The 24 h field
    measurement is the final confirmation gate.
- [x] Mark completed

### Task 6: Update architecture notes and close

- [x] Update
  [`docs/plans/architecture/execution-and-fills.md`](architecture/execution-and-fills.md)
  with one paragraph under "Chase-up cap reduction" describing:
  (a) entry-band caps (`max_probability`,
  `min_upside_percent`-derived) are NOT considered at chase-up,
  (b) only `max_execution_price` / `max_entry_price` are, and
  (c) the live-execution path's behaviour here (per Task 1
  finding). Refresh the `Last verified` marker.
  - Added "Chase-up cap reduction" subsection to the
    "Known footguns" block (the natural home — the previous
    bullet documents `limit_price_not_executable`, the new
    one explains why chase-up didn't rescue it pre-fix).
    Covers the helper, the four execution-price cap
    sources, the two entry-band caps that are now excluded,
    both reduction sites (shadow + live BUY branch), the
    SELL-branch no-op, and the regression-test addresses.
    `Last verified` bumped to 2026-05-10 with a Plan 0035
    note prepended to the chronology.
- [x] If
  [`docs/plans/architecture/trader-pipeline.md`](architecture/trader-pipeline.md)
  also mentions the chase-up reducer (search for
  `max_probability` and `chase`), update it to match. Refresh
  `Last verified`.
  - Updated the `allow_taker_limit_buy_above_signal` row of
    the Step 5 risk-limit table to point at
    `_chase_up_execution_caps` and the entry-band-vs-
    execution-price split, with a back-reference to this
    plan and to the new `execution-and-fills.md` subsection.
    `Last verified` bumped to 2026-05-10 with a Plan 0035
    note prepended.
- [x] Mark completed

### Task 7: Close-out

- [x] Run all Validation Commands locally; all pass.
  - `bash scripts/run_tests_remote.sh tests/test_execution_session_engine.py tests/test_execution_latency_metrics.py tests/test_trader_order_manager_live.py`
    → 49 passed.
  - `py_compile` on `order_manager.py` → OK.
- [x] Open follow-up plan
  [`0037-verify-plan-0035-chase-cap-drop-2026-05-11.md`](0037-verify-plan-0035-chase-cap-drop-2026-05-11.md)
  for the 24 h SQL field measurement (filename carries the
  earliest-run date per operator request).
- [ ] `git log --grep='Plan: 0035'` shows the full commit set
  (verified by the operator at commit time).
- [x] `git mv docs/plans/0035-split-entry-band-from-execution-price-cap.md docs/plans/completed/`.
- [x] Update the row in `plan-control-index.md` to point at
  `completed/`; add a row for Plan 0037.
- [x] Mark completed
