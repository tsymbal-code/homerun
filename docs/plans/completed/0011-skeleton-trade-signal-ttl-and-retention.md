# Plan: Defensive `expires_at` on skeleton-INSERTed `trade_signals` rows + stuck-skeleton retention sweep

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Plan 0010 added a publish-side skeleton-INSERT pass to
[`intent_runtime.publish_opportunities`](../../backend/services/intent_runtime.py)
that synchronously commits a `(source, dedupe_key)`
placeholder row in `trade_signals` **before** the projection
loop enriches it with `payload_json`, `runtime_sequence`,
`expires_at`, etc. The skeleton-INSERT closes the FK race that
plan 0010 was scoped to, and the post-deploy snapshot showed 0
FK violations and 0 orphaned skeletons (see
[runtime-tweaks.md `2026-05-08 ~06:20 UTC` entry](../operational/runtime-tweaks.md)).

**The residual edge case this plan addresses.** The current
skeleton row is committed with `expires_at = NULL`. If the
publish call dies between the skeleton commit and the
projection loop's UPSERT — process kill, connection drop,
unhandled exception in the rest of `publish_opportunities`,
operator-issued `docker compose restart` mid-call — the row
stays in `trade_signals` with `payload_json IS NULL`,
`runtime_sequence IS NULL`, `status='pending'`,
`expires_at IS NULL` **forever**. Three downstream effects:

1. **`(source, dedupe_key)` slot is occupied.** The next
   genuine publish for the same dedupe_key hits the ON
   CONFLICT DO NOTHING branch, re-queries, and adopts the
   stuck row's id. `_strategy_runtime_metadata`'s subsequent
   `upsert_trade_signal` in the projection loop will UPDATE
   the same row in place, which is the self-healing path
   plan 0010 relies on. **In the steady state, the system
   recovers automatically** — that's why post-deploy
   monitoring saw 0 orphans after 5 minutes.
2. **The `trade_signals` table accumulates rows that never
   participate in any decision.** Most legacy ingest paths
   eventually mark rows `expired` via the
   `_run_trade_signal_pruner_loop` in
   `host.py`, but the pruner's eligibility query keys on
   `expires_at < now()`. With `expires_at = NULL` a stuck
   skeleton is invisible to the pruner.
3. **The post-fix invariant query in
   [`copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md)
   has to filter `payload_json is not null` to avoid
   alerting on transient skeletons.** That filter is
   correct for the in-flight case but masks the stuck
   case — there is no clean way to distinguish "skeleton
   in flight, projection hasn't run yet" from "skeleton
   stuck because publish died".

This plan adds two safeguards:

- **(a) Defensive TTL.** Skeleton rows are committed with
  `expires_at = now() + skeleton_ttl_seconds` (default
  300 s). The projection loop's later UPSERT overwrites
  `expires_at` with the strategy's intended value, so the
  TTL only affects rows where projection never ran.
- **(b) Retention sweep.** A small loop in
  `worker-discovery` scans `trade_signals` for rows with
  `payload_json IS NULL AND runtime_sequence IS NULL AND
  status = 'pending' AND created_at < now() - interval
  '<sweep_age>'` and **deletes** them outright (no
  status flip — they never carried any consumer-visible
  state). Default sweep age 1 hour, runs every 15 min.

Done = the invariant query in
[`copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md)
can drop the `payload_json is not null` filter for the
**stuck-skeleton** check (it stays for the in-flight check),
and operators can monitor stuck skeletons via a single
metric: `count(*) where created_at < now() - interval '1
minute' and payload_json is null and status='pending'`.

## Out of scope

- **Removing the skeleton-INSERT entirely.** That was plan
  0010's design; it works. This plan is purely defensive
  hardening.
- **Speeding up the projection loop.** Plan 0004 territory if
  ever needed. Skeleton rows are <500 ms in flight in steady
  state.
- **The trader-cycle 10s timeout that's currently blocking
  Copy-Trade execution on `latency_class=normal`.** That's a
  separate diagnosis (see "Discovered while auditing" entry
  in [runtime-tweaks.md](../operational/runtime-tweaks.md))
  and warrants its own plan.

## Context / References

- [Plan 0010 — Fix `trader_decisions` FK race](completed/0010-fix-traders-publish-fk-race.md)
  — adds the skeleton-INSERT this plan hardens.
- [Architecture: Copy-trade pipeline](architecture/copy-trade-pipeline.md)
  "Operational guidance" — the invariant query that this
  plan tightens.
- [`backend/services/intent_runtime.py:2007-2200`](../../backend/services/intent_runtime.py)
  — current skeleton-INSERT pass (no `expires_at`).
- [`backend/workers/host.py`](../../backend/workers/host.py)
  `_run_trade_signal_pruner_loop` — the existing pruner that
  keys on `expires_at < now()`.
- [`backend/workers/host.py:100-158`](../../backend/workers/host.py)
  — worker-discovery plane composition (where the new
  retention loop attaches).

## Validation Commands

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest -q backend/tests/test_intent_runtime_publish_projection_durability.py backend/tests/test_skeleton_signal_retention.py'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend ruff check backend/services/intent_runtime.py backend/workers/host.py'`
- `ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c \"select count(*) stuck_skeletons from trade_signals where payload_json is null and runtime_sequence is null and status='pending' and created_at < now() - interval '1 minute'\""`
  — expected: 0 in steady state.
- `cd frontend && npm run typecheck` — sanity (this plan does
  not touch the frontend).

### Task 1: Add `skeleton_ttl_seconds` config knob

- [x] In [`backend/config.py`](../../backend/config.py), added
  three knobs adjacent to `INTENT_RUNTIME_DEFERRED_MAX_AGE_SECONDS`:
  `INTENT_RUNTIME_SKELETON_TTL_SECONDS = 300`,
  `INTENT_RUNTIME_SKELETON_RETENTION_INTERVAL_SECONDS = 900`,
  `INTENT_RUNTIME_SKELETON_RETENTION_MAX_AGE_SECONDS = 3600`.
  Each is documented inline with the rationale (TTL = projection
  loop's drain budget; retention interval = orphans are rare so
  every 15 min is enough; max age = wait an hour before
  considering a skeleton truly orphaned).
- [x] Mark completed

### Task 2: Set `expires_at` on every skeleton-INSERTed row

- [x] In
  [`intent_runtime.publish_opportunities`](../../backend/services/intent_runtime.py),
  the skeleton-row dict (line 2143 area where `skeleton_rows`
  is built) now includes `"expires_at": skeleton_expires_at`
  with `skeleton_expires_at = now + timedelta(seconds=skeleton_ttl_seconds)`
  and `skeleton_ttl_seconds = max(60, int(getattr(settings,
  "INTENT_RUNTIME_SKELETON_TTL_SECONDS", 300) or 300))` (60 s
  hard floor matches the retention service's clamp).
- [x] Confirmed by reading
  [`signal_bus.upsert_trade_signal`](../../backend/services/signal_bus.py)
  lines 1590 and 1678: `expires_at` IS in the UPSERT's update
  set in BOTH the SQL-level conditional UPDATE (when
  `runtime_sequence` is supplied) and the ORM mutate fallback.
  The defensive TTL is overwritten by the strategy's intended
  expiry as soon as the projection loop commits.
- [x] Added
  `test_skeleton_row_carries_defensive_expires_at_overwritten_by_projection`
  in
  [`backend/tests/test_intent_runtime_publish_projection_durability.py`](../../backend/tests/test_intent_runtime_publish_projection_durability.py).
  The test pins both invariants:
  1. A fresh skeleton row carries
     `publish_started + (ttl - 5s) <= expires_at <= now + (ttl + 5s)`.
  2. After invoking `upsert_trade_signal(...,
     expires_at=now + 6h, ...)` for the same
     `(source, dedupe_key)`, the strategy's 6h value
     overwrites the 5-min defensive TTL.
  Verified passing in `worker-trading` container.
- [x] Mark completed

### Task 3: Add the stuck-skeleton retention sweep

- [x] Created
  [`backend/services/skeleton_signal_retention.py`](../../backend/services/skeleton_signal_retention.py)
  with `prune_stuck_skeletons(session, *, max_age_seconds: int) -> int`
  that issues the DELETE described, returns the deleted row
  count, and clamps `max_age_seconds` to `>= 60` (`_MIN_MAX_AGE_SECONDS = 60`).
- [x] Wired into
  [`backend/workers/host.py`](../../backend/workers/host.py)
  as `_run_skeleton_signal_retention_loop` (function ~line 501)
  scheduled on the discovery plane only (`if self._plane_name
  == "discovery"`, line ~1010). Stagger 60 s, then iterates at
  `INTENT_RUNTIME_SKELETON_RETENTION_INTERVAL_SECONDS` (default
  900 s) using `INTENT_RUNTIME_SKELETON_RETENTION_MAX_AGE_SECONDS`
  (default 3600 s). Discovery plane is the right home: trading
  already owns the terminal-row pruner, and the orphan-deletion
  path stays off the trader-cycle's 10 s budget.
- [x] Each sweep logs at INFO regardless of `deleted` count
  (`"Stuck-skeleton retention sweep" plane=discovery deleted=N
  max_age_seconds=3600`), so the operator can correlate sweeps
  with publish failures even when no orphan was reaped that
  cycle.
- [x] Added
  [`backend/tests/test_skeleton_signal_retention.py`](../../backend/tests/test_skeleton_signal_retention.py)
  with two tests:
  - `test_prune_stuck_skeletons_deletes_only_aged_orphan_rows`
    inserts (a) a 10 s-old skeleton, (b) a 2 h-old skeleton
    (orphan), (c) a fully-projected row with non-null
    `payload_json` and `runtime_sequence`. Asserts only (b)
    is deleted, the other two survive, and a second call
    deletes 0 (idempotency).
  - `test_prune_stuck_skeletons_bounds_max_age_to_60_seconds`
    asserts a caller-supplied `max_age_seconds=10` is silently
    clamped to 60 s, so a 30-s-old row survives.
  Both tests use `pg_insert` directly (not the ORM) for
  skeleton fixtures so `payload_json` defaults to SQL NULL —
  ORM `payload_json=None` would store JSON `null` because
  `none_as_null` is not set on the column. Verified passing
  in `worker-trading` container.
- [x] Mark completed

### Task 4: Update architecture notes

- [x] In
  [`architecture/copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md)
  "Operational guidance" (step 2), replaced the single post-
  Plan-0010 invariant query with a two-tier monitoring scheme:
  - **Tier 1 (steady-state, production alerting):** existing
    Plan 0009/0010 query with the `payload_json is not null`
    filter to exclude in-flight skeletons; should always
    show `without_seq=0`.
  - **Tier 2 (stuck-skeleton, this plan):** new query
    `count(*) ... where payload_json is null and
    runtime_sequence is null and status='pending' and
    created_at < now() - interval '1 minute'`; should
    always be 0 and points to a dying publish path when not.
  Both queries are now in the journal entry too (Task 5)
  for operator copy-paste.
- [x] Updated the Conclusion section to call out Plan 0011's
  contribution (defensive TTL + discovery-plane retention sweep)
  alongside Plans 0009 and 0010.
- [x] Updated Open questions to list a third post-fix invariant
  (`stuck_skeletons = 0` from the Tier 2 query) alongside the
  existing two.
- [x] Cross-referenced plan 0011 from the See also section.
  (Note: the plan's task wording mentioned "Sibling toggles /
  hardening" but no such section exists in
  `copy-trade-pipeline.md`; the cross-link in See also is the
  natural home for plan-level references and matches how
  Plans 0008/0009/0010 are linked.)
- [x] Mark completed

### Task 5: Update the operational journal

- [x] Appended `2026-05-08 ~10:30 UTC — Plan 0011: defensive
  expires_at on skeleton rows + stuck-skeleton retention sweep`
  entry to
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md).
  Documents the chosen defaults (300 s / 900 s / 3600 s with
  per-knob rationale), the Tier 1 + Tier 2 monitoring queries
  from Task 4, and a rollback recipe (delete-orphans SQL +
  one-line revert in `intent_runtime.py`).
- [x] Mark completed

### Task 6: Deploy and verify on `polyhome-1`

- [x] Deployed via `BUILD_IMAGES=1 ./deploy/sync_remote.sh`.
  The first attempt with `BUILD_IMAGES=0` did not pick up
  changes because the worker containers run from baked-in
  image code (no bind mount for `backend/`); a rebuild was
  required.  Stack came back up at 2026-05-08 ~07:33 UTC
  with all 7 containers reporting Up healthy.
- [x] Confirmed 0 new tracebacks in `worker-discovery` /
  `worker-trading` logs (excluding the pre-existing
  `Missing Polymarket API credentials` warnings).  The
  retention loop logged its first iteration at 07:34:07 UTC
  (`deleted=0 max_age_seconds=3600`) — proving the sweep is
  alive on the discovery plane.
- [x] Manually injected a stuck skeleton at 07:35:21 UTC
  via psql: `(source='__plan_0011_orphan__', dedupe_key
  starts with 'plan-0011-orphan-live-', created_at = now()
  - interval '2 hours')`.  The synthetic source ensures no
  consumer promotes the row away from skeleton shape (an
  earlier `source='traders'` test fixture was processed by
  the orchestrator into a real signal with non-null
  `payload_json` and `runtime_sequence`, which is correct
  but no longer matches the orphan filter).
- [x] Verified end-to-end deletion via the live loop:
  - Manual sweep call (for fast confirmation):
    `prune_stuck_skeletons(session, max_age_seconds=3600)`
    returned `deleted=1`, dedupe_key count went 1 → 0.
  - Live loop iteration at 07:49:07 UTC (the next 15-min
    cycle after injection): logged
    `"Stuck-skeleton retention sweep" deleted=1
    max_age_seconds=3600`, `select count(*) from trade_signals
    where dedupe_key like 'plan-0011-orphan-live-%'` returned
    0.  Total wait: 14 minutes (within the plan's ≤15 min
    budget).
- [x] Post-deploy invariant snapshot (20-minute window):

  | Invariant | Value | Status |
  |---|---:|---|
  | FK violations (`trader_decisions_signal_id_fkey`) | 0 | OK (Plan 0010 still holds) |
  | Tier 1: `traders_copy_trade` rows with `without_seq != 0` | 0 / 264 | OK |
  | Tier 2: stuck skeletons (excluding test fixtures) | 0 | OK |
  | Defensive TTL coverage on new `traders` skeletons | 331 / 331 | OK |
  | `trader_decisions` outcomes | 13 selected, 158 skipped, 48 blocked | Healthy gate filtering |

- [x] Mark completed

### Task 7: Close

- [x] All check-boxes above are `[x]`.
- [x] `git mv docs/plans/0011-skeleton-trade-signal-ttl-and-retention.md
  docs/plans/completed/`.
- [x] Updated [`plan-control-index.md`](plan-control-index.md):
  link target now points to
  `completed/0011-skeleton-trade-signal-ttl-and-retention.md`,
  with a per-plan note summarizing the verified outcomes
  (defensive TTL stamped on 331/331 skeletons, retention
  sweep DELETE'd 1 injected orphan within 14 min, both Tier
  1 and Tier 2 invariants at 0).
- [x] Flipped the runtime-tweaks `2026-05-08 ~10:30 UTC`
  entry from OPEN to CLOSED with the post-deploy snapshot
  inline.
- [x] Mark completed
