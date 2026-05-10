# Plan: Verify Cox-PH shadow-fill pessimism before tuning

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0033` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

The `Sandbox - Tail-End` bot wrote 33 `cancelled` `trader_orders`
in 14 days against 9 `executed`. Every single one of the 33
cancellations carries `payload_json#>>'{leg,reason}' =
'limit_price_not_executable'`. The Cox-PH shadow ensemble
returns `fill_probability: 0.0`, `levels_consumed: 0`,
`filled_shares: 0` — i.e., the simulator concluded the
`taker_limit BUY` would not cross the best ask within the
6-second `FAK` window.

Before tuning the simulator (relaxing `fill_probability`
threshold, widening the chase-up cap, or anything else), we have
to **verify the simulator is wrong** — i.e., a real CLOB taker
order at the same instant would have filled. If the simulator is
correct, the fix is on the strategy side (chase-up cap, entry
band, or signal-vs-execution price drift) and the simulator
stays untouched.

The hot suspicion: the bot's config has
`max_probability=0.905`. In
[`order_manager.py:962–976`](../../backend/services/trader_orchestrator/order_manager.py:962),
`shadow_limit_price` is computed as `min(...)` over six caps,
and `params.max_probability` is one of them. So even though the
strategy emits `max_execution_price=0.9478` (the chase-up
target), the simulator clamps the effective limit price down to
`0.905`. If `best_ask` on the NO leg is 0.91 at submit, the
simulator correctly flags `limit_price_not_executable` — but
that's the operator's `max_probability` knob preventing the
chase-up, not the simulator being pessimistic. The 9 executed
orders all had `entry_price ≤ 0.8865`; the 33 cancelled orders
all had `entry_price ∈ {0.900, 0.905}`. Sharp boundary at the
`max_probability` cap.

Done means: a written verdict in `runtime-tweaks.md` and (if
relevant) `architecture/execution-and-fills.md` that says one
of three things, with evidence:

1. **Simulator is correct, config is the gate.** The
   `max_probability=0.905` cap collides with the chase-up
   target during the `min(...)` reduction in
   `_resolve_execution_price_bounds`. Recommendation:
   either raise `max_probability` for this bot (operator
   tune-knob) or split the entry-band cap from the
   execution-price cap in code. No simulator change.

2. **Simulator is too strict.** Real CLOB trades within the
   6-second window happened at prices ≤ the
   simulator-computed `shadow_limit_price`. Recommendation:
   tune the Cox-PH `fill_probability` threshold (CRITICAL
   knob — needs walkthrough).

3. **Mixed.** Some cancellations are config-driven, others
   simulator-driven. Recommendation: split out per-bucket fix.

## Context / References

- [Architecture: Cox-PH fill simulator, live execution, reconciliation, redeemer](architecture/execution-and-fills.md)
- [order_manager.py:880–1069 — shadow execution path](../../backend/services/trader_orchestrator/order_manager.py:880)
- [order_manager.py:962–976 — `shadow_limit_price` cap reduction](../../backend/services/trader_orchestrator/order_manager.py:962)
- [tail_end_carry.py:809 — `max_execution_price = target_price`](../../backend/services/strategies/tail_end_carry.py:809)
- [docs/operational/runtime-tweaks.md — knob walkthrough template](../operational/runtime-tweaks.md#walkthrough-template-for-critical-knob-changes)
- Polymarket CLOB API: `https://clob.polymarket.com/trades?market=<condition_id>` (public, no auth needed for read)

## Validation Commands

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest -q backend/tests/test_execution_session_engine.py backend/tests/test_execution_latency_metrics.py'`

## Out of scope

This plan **measures** simulator behaviour — it does not change
the simulator, the strategy code, or any runtime knob. Any tune
that emerges falls under a follow-up plan and (if it touches a
CRITICAL knob) carries the
[walkthrough template](../operational/runtime-tweaks.md#walkthrough-template-for-critical-knob-changes).

The plan also does not address the dedup-spam (1490-repeats per
signal) issue surfaced in the same operator session — that is
tracked as
[Plan 0032](0032-eliminate-fast-trader-dedup-spam.md). The two
plans can be worked independently.

### Task 1: Pull the cancelled-order forensic sample

- [x] On `polyhome-1`, query for the 33 `cancelled`
  `trader_orders` for the `Sandbox - Tail-End` bot, extracting
  the data we need to reconstruct each submission. Saved as
  [`docs/plans/work-artifacts/0033-tailend-cancelled-orders-2026-05-10.csv`](work-artifacts/0033-tailend-cancelled-orders-2026-05-10.csv).
  Notes from execution:
  - The plan's draft JSON paths were missing the `ensemble`
    intermediate level under `shadow_simulation`. The actual
    paths (used in the dump) are
    `payload_json#>>'{leg,shadow_simulation,ensemble,realistic,estimate,limit_price}'`
    etc. The leg itself does not carry an explicit
    `max_execution_price` key for this bot — the chase-up
    target lives in `strategy_context.max_entry_price`
    (= 0.94775 for `signal=0.905`, the `+5 %` derivation).
- [x] Mark completed

### Task 2: Pull the matching CLOB trades for each cancelled order

- [x] Resolve `condition_id` per `market_id`. The plan's draft
  pointed at a `markets` table that does not exist on this
  deployment; the local `cached_markets` is keyed by
  `condition_id` and only carries currently-active markets (12
  of our 33 hits — closed sports markets get pruned). Used the
  public Gamma endpoint instead:
  `GET https://gamma-api.polymarket.com/markets/{market_id}`
  (singular form — returns closed markets too, while the plural
  `?id=` form silently filters them out). All 33 condition IDs
  resolved.
- [x] For each `(condition_id, token_id, submitted_at)`,
  fetch CLOB trades from the public **data-api** endpoint:
  `https://data-api.polymarket.com/trades?market=<condition_id>&takerOnly=true&filterType=CASH&filterAmount=1`
  (the `clob.polymarket.com/trades` path returns 401 for
  unauthenticated callers, contrary to the plan's draft).
  Paginated via `&offset=` until end-of-stream. Filter to
  `[submitted_at - 1s, submitted_at + 6s]` and matching
  `asset == token_id`.
- [x] For each cancelled order, computed
  `min_taker_buy_price`, `vol_total_taker_buy`,
  `vol_at_or_below_shadow`, `vol_at_or_below_ctx_max`,
  `would_fill_at_shadow`, `would_fill_at_ctx_max`,
  `has_partial_at_shadow`, `has_partial_at_ctx_max`. Result:
  32 of 33 windows have **zero** taker activity — Tail-End
  picks markets in the long thin tail of Polymarket's volume
  distribution. The single corroborating window (market 2125964
  BTC > $80k) saw a public taker BUY at 0.91 inside the FAK
  window — exactly above `shadow_limit=0.895` and below
  `ctx_max_entry=0.94225`. Saved as
  [`docs/plans/work-artifacts/0033-tailend-clob-window-trades.csv`](work-artifacts/0033-tailend-clob-window-trades.csv).
  Helper scraper:
  [`docs/plans/work-artifacts/0033-fetch-clob-window.py`](work-artifacts/0033-fetch-clob-window.py).
- [x] **Stronger evidence path** added on top of the plan:
  joined each cancellation against
  `market_microstructure_snapshots` for a `±15 s` nearest
  snapshot. This gives the *exact same book* the simulator
  consumed at decision time — far more informative than CLOB
  taker-trade reconstruction in markets where no organic taker
  activity occurs. 27 of 33 rows had a snapshot within the
  window; for those the verdict is unambiguous. Saved as
  [`docs/plans/work-artifacts/0033-book-snapshot-join.csv`](work-artifacts/0033-book-snapshot-join.csv)
  (re-runnable from
  [`docs/plans/work-artifacts/0033-bucket-classification.sql`](work-artifacts/0033-bucket-classification.sql)).
- [x] Mark completed

### Task 3: Classify each cancellation into bucket A / B / C

- [x] Classified all 33 rows. Final counts (with full
  per-row table in
  [`docs/plans/work-artifacts/0033-bucket-classification.md`](work-artifacts/0033-bucket-classification.md)):

  | Bucket | Count | Share of evidenced rows | Share of all 33 |
  |---|---:|---:|---:|
  | **A — config-driven** (book ask in `(max_probability, ctx_max_entry]`) | 25 | 92.6 % | 75.8 % |
  | **B — simulator pessimism** | 0 | 0.0 % | 0.0 % |
  | **C — book really wasn't there** | 2 | 7.4 % | 6.1 % |
  | **Indeterminate** (no snapshot ±15s, no public CLOB taker BUY) | 6 | — | 18.2 % |

  > 25 / 27 evidenced = 92.6 % in Bucket A → Verdict 1.

  Per-band slice and the 2 Bucket-C details (both spread
  blowouts to ≥ 425 bps) are in the artifact.
- [x] Mark completed

### Task 4: Write the verdict

- [x] Appended `### 2026-05-10 ~16:30 UTC — Tail-End
  cancelled-order verdict (Plan 0033)` to
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md).
  Entry contains: bucket counts (per-band slice + aggregate),
  the 4-line `min(...)` cap-reduction quote from
  [`order_manager.py:962-980`](../../backend/services/trader_orchestrator/order_manager.py:962),
  Verdict 1, the operator-config recommendation
  (`max_probability` 0.905 → ≥ 0.97, with a CRITICAL-knob
  walkthrough required at apply time), and the alternative
  code-refactor recommendation (split entry-band cap from
  execution-price cap).
- [x] Verdict is 1 → no update needed for
  [`execution-and-fills.md`](architecture/execution-and-fills.md).
  The simulator was vindicated; nothing to flag as
  "potentially-too-strict".
- [x] Mark completed

### Task 5: Spawn follow-up if simulator change is warranted

- [x] Verdict is 1. **No follow-up plan opened.** The
  recommendation is a one-line config tweak (raise
  `max_probability` for `Sandbox - Tail-End` from 0.905 to
  ≥ 0.97 via the Bots UI). The decision is recorded in the
  Task 4 entry under
  [`runtime-tweaks.md`](../operational/runtime-tweaks.md)
  ("Recommendation (operator action — no follow-up plan
  needed)"). When and if the operator instead chooses the
  code-refactor path (split `max_probability` entry-band cap
  from the chase-up execution-price cap in
  `order_manager._resolve_execution_price_bounds` + the
  chase-up branch at lines 962-980), that becomes its own
  plan with the CRITICAL-knob walkthrough.
- [x] Mark completed

### Task 6: Close-out

- [x] Run all Validation Commands. Test invocation in the plan
  used the wrong path (`backend/tests/...` from the host) — the
  backend image excludes `tests/` deliberately
  (`backend/.dockerignore`); the canonical wrapper is
  `bash scripts/run_tests_remote.sh tests/test_execution_session_engine.py tests/test_execution_latency_metrics.py`.
  Result: **27 passed, 3 warnings in 2.71s**.
- [x] `git mv docs/plans/0033-verify-cox-ph-shadow-fill-pessimism.md docs/plans/completed/`.
- [x] Update the row in
  [`plan-control-index.md`](plan-control-index.md) to point at
  `completed/`.
- [x] Mark completed
