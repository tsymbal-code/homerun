# Plan: Grace period in expire_source_signals_except

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0052` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

`crypto_5m_last_outcome` emits an Opportunity for each enabled crypto
asset on every 5-minute cycle rollover (Plans 0047, 0051). Live
observation 2026-05-11 16:25–17:25 UTC shows the strategy emitting
on **every** cycle (15 signals over 25 min), but only **5 of 15**
reach the trader as executed orders. The remaining 10 are flipped to
`status='expired'` in `trade_signals` ~2–3 seconds after creation.

Forensics on the 17:15 cycle confirm the cause:

- 3 signals (BTC, SOL, XRP) are INSERT-ed at `17:15:30.101874`.
- All 3 are sweep-expired at `17:15:32.573268` —
  `updated_at = created_at + 2.47 s`.
- No other `source='crypto'` signal is created in that 5-second
  window, so this is not a midcycle batch overriding ours.

The killer is `expire_source_signals_except` in
[`backend/services/signal_bus.py:1816`](../../backend/services/signal_bus.py),
called by `intent_runtime._project_status_batch`
([`intent_runtime.py:3124`](../../backend/services/intent_runtime.py)).
On every projection sweep the function takes the set of dedupe_keys
in the current snapshot and force-expires every `source=<source>`
pending signal **not** in that set. When the projection's snapshot
was captured a fraction of a second BEFORE the strategy INSERT-ed
its row, our fresh signal isn't in `keep_dedupe_keys`, and the
sweep nukes it before the trader's cursor can reach it.

This is a system-level race between INSERT and projection; it is
not a bug in any strategy. The same race protects the scanner
pipeline (markets that drop out of a scan should expire promptly),
but it actively hurts event-driven sources where each emit is
self-contained and not part of a continuous snapshot.

The fix is a single conservative guard inside
`expire_source_signals_except`: refuse to mark `status='expired'`
on rows younger than a configurable grace window (default 60 s).
Scanner-source signals lose at most one sweep cycle of staleness;
event-driven signals get protected from the race entirely.

"Done" = the `executed / (executed + expired)` ratio for
`crypto_5m_last_outcome` over a 30-minute live window after deploy
is **≥ 90%**, vs the observed ~33% baseline (5 / 15) on 2026-05-11
between 17:00 and 17:25 UTC.

## Context / References

- [Architecture: Copy-trade pipeline](architecture/copy-trade-pipeline.md) —
  steps 15-17 cover `signal_bus.upsert_trade_signal` and the
  `trade_signals` projection contract. Same writer-side path our
  signals travel through.
- [Architecture: Trader pipeline & diagnostics](architecture/trader-pipeline.md)
- [Plan 0011: Defensive `expires_at` on skeleton `trade_signals` + retention sweep](completed/0011-skeleton-trade-signal-ttl-and-retention.md) —
  same class of fix on the WRITER side; this plan is the symmetric
  SWEEPER-side guard.
- [Plan 0044: Firehose binding cache must include shadow traders](completed/0044-firehose-binding-cache-include-shadow-traders.md) —
  prior race-condition fix in the signal-emit fan-out path.
- [Plan 0047: Crypto 5m last-outcome-follow strategy](completed/0047-crypto-5m-last-outcome-follow-strategy.md) —
  the strategy whose emit drops surfaced the race.
- [Plan 0051: REST book-fallback for `crypto_5m_last_outcome`](completed/0051-rest-book-fallback-for-crypto-5m-last-outcome.md) —
  fixed the WS-book-cache miss; uncovered the projection-sweep race
  hiding behind it.
- [backend/services/signal_bus.py:1816](../../backend/services/signal_bus.py) —
  `expire_source_signals_except`; sole call site we are
  modifying.
- [backend/services/intent_runtime.py:3124](../../backend/services/intent_runtime.py) —
  `_project_status_batch`; the only caller of
  `expire_source_signals_except`.
- [backend/models/database.py](../../backend/models/database.py) —
  `TradeSignal.created_at` already exists; no migration needed.

## Validation Commands

- `docker compose exec backend pytest -q backend/tests/test_signal_bus_expire_source.py`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose run --rm --no-deps -v /home/polyhome/homerun/backend:/app/backend:ro backend python -m pytest -q tests/test_signal_bus_expire_source.py'`
- `ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c \"SELECT status, COUNT(*) FROM trade_signals WHERE strategy_type='crypto_5m_last_outcome' AND created_at > NOW() - INTERVAL '30 minutes' GROUP BY status ORDER BY status;\""`

## Out of scope

### Baseline reference (pinned 2026-05-11 18:08 UTC, snapshotting the 17:00–17:30 window)

- `trade_signals` for `strategy_type='crypto_5m_last_outcome'` in
  `created_at BETWEEN 2026-05-11 17:00 AND 17:30 UTC`:
  **9 executed / 9 expired** (50 % executed) — full row dump in
  [`work-artifacts/0052-pre-fix-evidence.md`](work-artifacts/0052-pre-fix-evidence.md).
- `trader_events.firehose_emit` for the same window: **6 emits per
  asset × 3 assets = 18 emits**, matching the 18 trade_signal rows.
  Every emit becomes a row; the lossy stage is between INSERT and
  trader-cursor pickup.
- The 17:15 cycle is the smoking gun: 3 INSERTs at
  `17:15:30.101874`, all three flipped to `expired` at
  `17:15:32.573268` (`age_at_update_s = 2.47`). No
  `crypto_5m_midcycle` write in that 5 s window, so this is the
  projection sweep, not a sibling-strategy snapshot override.
- Pass criterion: post-deploy 30 min window
  `executed / (executed + expired) ≥ 90 %`.

### Out of scope (continued)

- No change to `intent_runtime._project_status_batch` itself — the
  projection's *snapshot semantics* are correct; we are only
  changing the eviction side-effect's threshold.
- No change to `expire_stale_signals`
  ([signal_bus.py:1780](../../backend/services/signal_bus.py)),
  which expires by `expires_at_passed` and `market_price_stale`.
  That path already requires a non-fresh signal by construction,
  so the grace guard is redundant there.
- No CRITICAL-tier knob touch. The new
  `expire_source_grace_seconds` setting is HIGH-tier — it gates a
  cross-strategy invariant. Disclosed below.
- No change to event-driven vs scanner strategy classification. The
  grace window is uniform across sources because the scanner can
  tolerate ≤ grace_seconds of staleness without behaviour change
  (next scan re-emits the same dedupe_key, refreshing the row).
- No retroactive fix for already-expired signals. Once the deploy
  lands, only future signals are protected.

### Task 1: Capture forensic evidence and pin the baseline

- [x] Run on `polyhome-1` against postgres (one-shot, no commit):
  ```
  SELECT status, COUNT(*) FROM trade_signals
   WHERE strategy_type='crypto_5m_last_outcome'
     AND created_at BETWEEN '2026-05-11 17:00:00' AND '2026-05-11 17:30:00'
   GROUP BY status;
  ```
  Paste the row counts into `## Out of scope` so the post-deploy
  comparison has a fixed reference, not a moving 30-min window.
  → 9 executed / 9 expired (see baseline block above).
- [x] Capture one representative cycle (e.g. 17:15) where all three
  assets' signals expired in lockstep — record signal_ids, created_at,
  updated_at, dedupe_keys — in a new file
  `docs/plans/work-artifacts/0052-pre-fix-evidence.md` so the
  race condition is reproducible from the historical row set.
- [x] Mark completed

### Task 2: Add `min_signal_age_seconds` guard to `expire_source_signals_except`

- [x] In [`backend/services/signal_bus.py`](../../backend/services/signal_bus.py)
  add a module-level `_EXPIRE_SOURCE_GRACE_SECONDS: float = 60.0`
  default, plus a `min_signal_age_seconds: Optional[float]`
  keyword argument on `expire_source_signals_except`. When `None`,
  fall back to the module constant. Document the unit in the
  docstring.
- [x] Extend the SQL `SELECT` in
  `expire_source_signals_except` to add the predicate
  `TradeSignal.created_at <= now - INTERVAL '<grace>s'`. **Plan
  deviation:** the predicate is built from a Python-side
  `cutoff = now - timedelta(seconds=grace)` rather than
  `func.now() - <interval>` so it stays on the same
  `_to_utc_naive(_utc_now())` clock the rest of the function
  already uses (and matches the codebase-wide pattern in
  `trader_orchestrator_state.py` etc.). The predicate is still
  WHERE-clause-side; the only difference vs the plan's wording is
  that the cutoff timestamp is a bind parameter instead of a
  server-side `now()`. On a single-host docker-compose stack the
  two clocks are the same source.
- [x] Preserve the existing behaviour exactly when the caller
  passes `min_signal_age_seconds=0.0` so emergency paths can opt
  out (none currently do; this is forward-defensive).
- [x] Mark completed

### Task 3: Tests

- [x] Create
  `backend/tests/test_signal_bus_expire_source.py` with the
  following cases (one test per behaviour):
  - `test_expire_skips_signals_younger_than_grace` — INSERT one
    pending row with `created_at = now - 5s`; call
    `expire_source_signals_except(source='crypto',
    keep_dedupe_keys=set())` with default grace. Row must stay
    `pending`.
  - `test_expire_keeps_signals_older_than_grace` — INSERT one
    pending row with `created_at = now - 120s`; same call as
    above. Row must flip to `expired`.
  - `test_expire_respects_keep_set_irrespective_of_age` — INSERT
    one row with `created_at = now - 120s` and the same dedupe_key
    in `keep_dedupe_keys`. Row must stay `pending` (the keep set
    overrides age).
  - `test_explicit_zero_grace_restores_legacy_behavior` — INSERT a
    1 s-old row, call with `min_signal_age_seconds=0.0`, row
    expires.
  - One extra `test_module_constant_is_60_seconds` pin so a future
    accidental edit of the default surfaces in tests, not in
    review of a one-line diff.
- [x] Use the same in-memory postgres test fixture pattern as
  [`backend/tests/test_strategy_catalog_seed_create_only.py`](../../backend/tests/test_strategy_catalog_seed_create_only.py)
  via `tests/postgres_test_db.build_postgres_session_factory`. No
  manual SQL — drive everything through the SQLAlchemy
  `TradeSignal` model and the `signal_bus` public API.
- [x] Mark completed

### Task 4: Wire the default value through

- [x] Confirm `intent_runtime._project_status_batch` does NOT
  override the kwarg at the call site
  ([intent_runtime.py:3124](../../backend/services/intent_runtime.py)).
  The default-on-None path means the projection sweep picks up the
  60 s grace transparently — no caller-side change needed.
  Verified: the call site passes `source`, `keep_dedupe_keys`,
  `signal_types`, `strategy_types`, `commit=False` — no
  `min_signal_age_seconds`.
- [x] If any other caller is introduced before this plan lands
  (search via `git grep expire_source_signals_except`), audit it
  for compatibility. Audit result: only one production caller
  (`backend/services/intent_runtime.py:3124`), one mock site in
  tests (`test_intent_runtime_ws_freshness.py:492`, replaces the
  function with a mock — kwarg-default-additive change is
  signature-compatible).
- [x] Mark completed

### Task 5: Live verification on polyhome-1

- [ ] `./deploy/sync_remote.sh` from the local checkout.
- [ ] No strategy-row mutation needed — this change is contained
  in `signal_bus.py` and re-deploys as part of the standard image
  rebuild. Plan 0051's `reset-to-factory` workflow is **not**
  relevant here because there is no strategy `source_code` to
  re-snapshot.
- [ ] `docker compose restart worker-trading backend` so both
  consumers of `signal_bus` reload.
- [ ] Wait 30 minutes (six 5 m cycles per asset × 3 assets =
  18 potential signals). Then run the third Validation Command and
  compare to the Task 1 baseline. Pass criterion:
  `executed / (executed + expired) ≥ 90%`.
- [ ] If the ratio stays below 90 %, capture the post-deploy
  `trade_signals` lifecycle (status, created_at, updated_at per
  signal_id) into `work-artifacts/0052-post-fix-evidence.md` and
  attach the gap to the plan close notes — the most likely cause
  would then be a SECOND expiry path we missed in
  `expire_stale_signals` or `intent_runtime`.
- [ ] Mark completed

### Task 6: Doc + close-out

- [ ] Update [`docs/plans/architecture/copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md):
  add a short note under the signal-projection / `upsert_trade_signal`
  section describing the new grace guard and citing this plan.
  One paragraph max — the authoritative behaviour lives in
  the docstring on `expire_source_signals_except`.
- [ ] Update [`docs/strategies/crypto-5m-last-outcome.md`](../strategies/crypto-5m-last-outcome.md):
  remove or revise any line that implies signals can be silently
  dropped between emit and trader; add a one-line link to this
  plan under "Посилання".
- [ ] Move this plan to `docs/plans/completed/` and update the
  link in `plan-control-index.md`.
- [ ] Mark completed
