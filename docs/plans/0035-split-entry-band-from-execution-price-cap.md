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

- `bash scripts/run_tests_remote.sh tests/test_execution_session_engine.py tests/test_execution_latency_metrics.py tests/test_strategy_tail_end_carry.py`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend ruff check backend/services/trader_orchestrator/order_manager.py'`

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

- [ ] Read [`order_manager.py:315`](../../backend/services/trader_orchestrator/order_manager.py:315)
  and surrounding helpers. Determine whether the live-execution
  `max_execution_price` reducer also conflates entry-band caps
  (`max_probability`, `min_upside_percent` cap) with
  execution-price caps. Document the finding inline in the
  plan as a sub-bullet under this task.
- [ ] If the same conflation exists, the fix in Task 2 must
  apply both at the shadow chase-up site and inside
  `_resolve_execution_price_bounds`. If it doesn't, the fix
  is shadow-only — note that explicitly so reviewers know the
  scope.
- [ ] Mark completed

### Task 2: Introduce `_chase_up_execution_caps()` helper and migrate the shadow site

- [ ] Add a new helper to
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
- [ ] Replace the `explicit_buy_caps` list at
  [`order_manager.py:965–972`](../../backend/services/trader_orchestrator/order_manager.py:965)
  with a call to the new helper. Remove `params.max_probability`
  and `_derive_min_upside_price_cap(params.get("min_upside_percent"))`
  from the chase-up reduction.
- [ ] Preserve the existing `tightest_explicit_cap is None →
  shadow_limit_price = 1.0` fallback. The "no caps at all" path
  is the natural market boundary; it stays untouched.
- [ ] If Task 1 found the same bug in
  `_resolve_execution_price_bounds`, mirror the change there
  too: split the reducer into entry-band vs execution-price,
  and surface only the execution-price cap into the
  `max_execution_price` return value.
- [ ] Mark completed

### Task 3: Regression test that fails before the fix

- [ ] Add `backend/tests/test_shadow_chase_up_caps.py` (or
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
- [ ] Add a second test that pins the existing safety
  property: when neither `leg.max_execution_price` nor any
  other execution-price cap is present, `shadow_limit_price`
  falls back to 1.0. This guards against accidental
  regressions in the helper's filter logic.
- [ ] Mark completed

### Task 4: Live-data simulation against Plan 0033 artefacts

- [ ] Take the 25 Bucket-A rows from
  [`docs/plans/work-artifacts/0033-bucket-classification.md`](work-artifacts/0033-bucket-classification.md)
  and run them through the post-fix `_chase_up_execution_caps`
  in a one-shot script (
  `scripts/simulate_0035_chase_up_caps.py`, deletable after
  use). For each row, compute the post-fix
  `shadow_limit_price` and compare against the recorded
  `best_ask` in `0033-book-snapshot-join.csv`. Count rows
  where `post_fix_shadow_limit ≥ best_ask` — these are the
  fills that the fix recovers.
- [ ] Append the count to
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  as an addendum under the Plan 0033 verdict entry. Format:
  `### 2026-MM-DD — Plan 0035 cap-split simulation
  (X / 25 Bucket-A rows recovered)`.
- [ ] Mark completed

### Task 5: Production rollout and validation

- [ ] Deploy via `./deploy/sync_remote.sh`. Watch
  `worker-trading` logs for one full
  scanner-cycle window (~5 min) and confirm no new exception
  classes appear. The chase-up path runs on every shadow BUY
  with `allow_taker_limit_buy_above_signal=True`, so coverage
  is immediate.
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
- [ ] If `dropped_by_chase_cap` did NOT drop, leave the plan
  open and add a Task 6 with the diagnosis. Common
  alternatives: the live-execution path conflates the same
  caps and Task 1 missed it; the order-book on relevant
  markets is genuinely deeper than the chase-up target.
- [ ] Mark completed

### Task 6: Update architecture notes and close

- [ ] Update
  [`docs/plans/architecture/execution-and-fills.md`](architecture/execution-and-fills.md)
  with one paragraph under "Chase-up cap reduction" describing:
  (a) entry-band caps (`max_probability`,
  `min_upside_percent`-derived) are NOT considered at chase-up,
  (b) only `max_execution_price` / `max_entry_price` are, and
  (c) the live-execution path's behaviour here (per Task 1
  finding). Refresh the `Last verified` marker.
- [ ] If
  [`docs/plans/architecture/trader-pipeline.md`](architecture/trader-pipeline.md)
  also mentions the chase-up reducer (search for
  `max_probability` and `chase`), update it to match. Refresh
  `Last verified`.
- [ ] Mark completed
