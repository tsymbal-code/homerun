# Plan: Auto-resync SYSTEM strategies from disk on every container boot

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0050` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Strategies execute from `strategies.source_code` (Postgres) — the
worker-trading process `exec()`-s that string at boot
([`strategy_loader.py`](../../backend/services/strategy_loader.py))
and never re-reads the disk file. On-disk `.py` files under
`backend/services/strategies/` are only the **seed source** —
they reach the DB exactly once via
`ensure_system_opportunity_strategies_seeded()` on a fresh
install, or manually via `reset_strategy_to_factory(slug)` from
the Strategy Manager UI.

The footgun: `./deploy/sync_remote.sh` rsyncs new code to the
host but does **not** touch the DB. Worker-trading restarts (or
reloads), picks up the OLD `source_code` from Postgres, and
silently runs stale logic — even though the file on disk is new.
This has bitten the operator at least once in this session
(Plan 0047 follow-up touched `crypto_5m_last_outcome.py`; the
strategy did not pick up the change until manual reset).

Done state: every backend process — `backend` (FastAPI) and each
`worker-*` plane — runs an **auto-resync pass at boot**, right
between `ensure_all_strategies_seeded()` and
`strategy_loader.refresh_all_from_db()`. The pass walks every
SYSTEM strategy seed, compares md5(disk source) vs md5(DB
source), and calls `reset_strategy_to_factory(slug)` on
mismatch. User-authored strategies (`is_system=false`) are
**never touched** — that's their own edit. A `trader_events` row
with `event_type='strategy_resync'` is written every boot so the
Strategy Manager UI can show a "Last system resync: ... ago — N
updated" banner.

Constraints:

- Must run **before** `strategy_loader.refresh_all_from_db()` —
  otherwise the loader caches stale code and the resync only
  takes effect on the NEXT boot.
- Must work on the `backend` plane (`backend/main.py`) and on
  every worker plane (`backend/workers/host.py`). Both startup
  paths call `ensure_all_strategies_seeded` already — same
  insertion point.
- Must be idempotent — boot loops would otherwise create spam
  rows.
- Failure of the resync must NOT block startup. Log the error
  and continue with stale code (same fail-open posture as the
  surrounding seed/refresh block).

## Context / References

- [`backend/services/opportunity_strategy_catalog.py:1593-1628`](../../backend/services/opportunity_strategy_catalog.py) —
  existing `reset_strategy_to_factory(slug)` — the reset
  primitive this plan composes
- [`backend/services/opportunity_strategy_catalog.py:1287-1316`](../../backend/services/opportunity_strategy_catalog.py) —
  `build_system_opportunity_strategy_rows()` — source of truth
  for what disk-side code looks like
- [`backend/main.py:405-420`](../../backend/main.py) — backend
  FastAPI lifespan startup; insertion point ➀
- [`backend/workers/host.py:1078-1106`](../../backend/workers/host.py) —
  worker host startup; insertion point ➁
- [`backend/services/strategy_loader.py`](../../backend/services/strategy_loader.py) —
  the consumer that must see post-resync source_code
- [`backend/models/database.py`](../../backend/models/database.py) —
  `Strategy.is_system`, `Strategy.version`, `TraderEvent` schema
- [`frontend/src/components/UnifiedStrategiesManager.tsx`](../../frontend/src/components/UnifiedStrategiesManager.tsx) —
  banner host
- [`agents.md`](../../agents.md) § "Strategies live in the
  database, not on disk" — to be amended

## Validation Commands

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest backend/tests/test_resync_system_strategies.py -q'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend ruff check backend/services/opportunity_strategy_catalog.py backend/main.py backend/workers/host.py backend/api/routes_strategies.py'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose restart backend worker-trading worker-news worker-discovery'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since=2m backend worker-trading worker-news worker-discovery 2>&1 | grep -E "resynced system strategy|System strategy resync complete"'`
- `ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/api/strategies/system-resync/last' | jq`

### Task 1: backend resync function + structured event

- [x] Add `resync_system_strategies_with_disk(session)` to
      [`backend/services/opportunity_strategy_catalog.py`](../../backend/services/opportunity_strategy_catalog.py)
      after `reset_strategy_to_factory`. Contract:
      * Compute `build_system_opportunity_strategy_rows()` (disk
        side) and one SELECT for all matching slugs (DB side).
      * For each pair: skip if `current is None` (will be
        created by ensure-seeded), skip if `current.is_system =
        False` (user-authored), skip if `md5(db.source_code) ==
        md5(disk.source_code)` — unchanged.
      * On mismatch: call `reset_strategy_to_factory(session,
        slug)` and append to a `resynced` list with
        `{slug, db_md5_before, disk_md5, len_delta}`.
      * Catch per-slug exceptions; collect into `errors`; do
        NOT raise — caller is fail-open at boot.
      * Return `{ran_at, resynced, unchanged_count,
        skipped_user_authored, errors, total_seeds}`.
- [x] Persist the summary as one row in `trader_events`:
      `event_type="strategy_resync"`, `severity="info"`,
      `source="opportunity_strategy_catalog"`,
      `trader_id=None`, payload_json=summary. Use the existing
      `buffer_trader_event` / direct insert path used elsewhere
      in the catalog. If 0 rows resynced, still emit the row
      with empty `resynced=[]` so the UI banner can show
      "Last resync: now — 0 changes".
- [x] Emit one structured `logger.info` per resynced slug
      ("resynced system strategy from disk slug=… db_md5_before=…
      disk_md5=… len_delta=…") and one summary
      `logger.info("System strategy resync complete",
      resynced=N, unchanged=M, skipped=K, errors=E)`.
- [x] Mark completed

### Task 2: wire resync into both startup paths

- [x] Insert the resync call at
      [`backend/main.py:411`](../../backend/main.py:411) between
      `ensure_all_strategies_seeded(session)` and
      `_loader.refresh_all_from_db(session=session)`. Wrap with
      try/except — failure must not abort startup, log a
      `warning`, continue with stale code.
- [x] Insert the same call at
      [`backend/workers/host.py:1084`](../../backend/workers/host.py:1084)
      between `ensure_all_strategies_seeded(session)` and
      `strategy_loader.refresh_all_from_db(...)`. Same
      try/except posture.
- [x] In both spots: log the summary using `plane` /
      `process` context tags so per-plane logs are
      distinguishable.
- [x] Mark completed

### Task 3: API endpoint + UI banner

- [x] Add `GET /api/strategies/system-resync/last` to
      [`backend/api/routes_strategies.py`](../../backend/api/routes_strategies.py)
      returning the most recent `trader_events` row with
      `event_type='strategy_resync'` (one DB query, `LIMIT 1`,
      `ORDER BY created_at DESC`). Response shape:
      `{available: bool, ran_at: iso, resynced: [...],
      unchanged_count: int, skipped_user_authored: [...],
      errors: [...], total_seeds: int}`. When no row exists yet
      (fresh install before first boot), return
      `{available: false}`.
- [x] Add `getLastSystemResync()` to
      [`frontend/src/services/apiStrategies.ts`](../../frontend/src/services/apiStrategies.ts)
      (or wherever sibling strategy API helpers live —
      grep for an existing pattern; if there isn't one, place
      it next to the most-used strategy fetcher).
- [x] Add a dismissible info-banner at the top of
      [`UnifiedStrategiesManager.tsx`](../../frontend/src/components/UnifiedStrategiesManager.tsx):
      "Last system resync: {relative time} — {N} strategies
      updated, {M} unchanged, {K} skipped (user-authored),
      {E} errors". Banner colour: blue (info), red when
      `errors.length > 0`. Includes an expandable
      `<details>` with the list of resynced slugs and their
      md5-before/after.
- [x] Mark completed

### Task 4: regression tests

- [x] Create `backend/tests/test_resync_system_strategies.py`:
      * **test_resync_unchanged_when_md5_matches** — DB and
        disk agree; assert `resynced=[]`,
        `unchanged_count=len(seeds)`.
      * **test_resync_updates_when_disk_differs** — patch one
        seed in `build_system_opportunity_strategy_rows` to
        return a modified `source_code`; insert old code into
        DB; run resync; assert `resynced` contains that slug
        and `Strategy.source_code` in DB now equals the new
        disk value; assert `Strategy.version` incremented by 1.
      * **test_resync_skips_user_authored** — DB row with
        `is_system=False` and code differing from disk; assert
        slug appears in `skipped_user_authored`, DB row
        untouched.
      * **test_resync_emits_trader_event_row** — assert that
        after a run, one row with `event_type='strategy_resync'`
        exists in `trader_events` with payload matching the
        return value.
      * **test_resync_fail_open_on_single_slug_error** — patch
        one slug's reset to raise; assert other slugs still
        process, error appears in `errors` list, no exception
        escapes.
- [x] Mark completed

### Task 5: docs + close

- [x] Update [`agents.md`](../../agents.md) — the existing
      "strategies live in DB, not on disk" footgun note now
      contradicted by this plan. Rewrite that paragraph: the DB
      is still the runtime source of truth, but disk → DB sync
      is now automatic on every boot for SYSTEM strategies.
      Mention the manual reset is still the path for
      user-authored strategies.
- [x] Update [`docs/plans/architecture/backend-architecture.md`](architecture/backend-architecture.md)
      § strategy loading flow with the new resync step.
- [x] Bump `Last verified:` on any architecture note that
      describes strategy loading.
- [x] Move this plan from active (top-level) to
      [`completed/`](completed/) and update
      [`plan-control-index.md`](plan-control-index.md).
- [x] Mark completed
