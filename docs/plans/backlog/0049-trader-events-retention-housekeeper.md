# Plan: Add retention housekeeper for `trader_events` (esp. firehose_evaluation)

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](../README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](../completed/) on close. Every commit produced by
> this plan carries a `Plan: 0049` git trailer (see
> [Commits and traceability](../README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](../plan-control-index.md).
>
> **Status: BACKLOG.** Activate when one of:
> (a) `trader_events` table size exceeds **30 GB** on the
>     `polyhome-1` Postgres instance (current rate observed
>     2026-05-11: ~8.4 GB/day, so this triggers at roughly
>     T+4 days from the Plan 0044 firehose deploy), OR
> (b) `pg_database_size('homerun')` exceeds **40 GB**, OR
> (c) host `/dev/sda1` usage exceeds **40 %**, OR
> (d) operator wants to land it pre-emptively before any of the
>     above thresholds.
>
> The Plan 0044 firehose-binding-cache change (cross-mode telemetry)
> is what unlocked the volume — `firehose_evaluation` rows now land
> for every shadow bot's every tick, ~5 rows/s/asset per bot. This
> plan is the planned follow-up. Do NOT roll back Plan 0044 to
> "fix" the volume — the telemetry is wanted; only retention is
> missing.

## Overview

`trader_events` has no retention. As of 2026-05-11 14:49 UTC:

- Table size: **1.42 GB** (1.07M rows total)
- Last-1h `firehose_evaluation` rate: **262 406 rows / h** ≈
  6.3M rows / day ≈ 8.4 GB / day after Postgres overhead
- At this rate, the table reaches **30 GB after 4 days**,
  **60 GB after a week**, **250 GB after a month**.
- Host `/dev/sda1` is 301 GB total with 266 GB free — the runway
  is **~30 days** at full firehose pressure with no other growth.

Two-tier retention is the right shape:

1. **`firehose_evaluation`** (the bulk, ~99 % of rows): keep **14 days**.
   That covers two full A/B-test windows and the Plan 0046
   backtest's longest practical replay window. Beyond 14 days the
   rows are dead weight — the backtester is the only consumer that
   reaches back that far, and rolling-window stat-arb on stale
   firehose has no business case.
2. **Everything else** (`decision`, `order`, `provider_health`,
   `circuit_breaker`, etc. — low-volume audit trail): keep
   **90 days**. These are diagnostic / regulatory and the volume
   is negligible.

Done state: a background housekeeper running at 6 h cadence (same
shape as `chainlink_feed._housekeeper_loop`) deletes rows older
than each tier's retention; both tiers are DB-backed knobs in
`app_settings`; the table's `created_at` index already exists
(`ix_trader_events_created_at`) so deletes are O(log n) per batch.

## Context / References

- [Architecture: websocket-and-events](../architecture/websocket-and-events.md) §
  "Polymarket WS subscription discipline" — same operator-toggle
  pattern this plan reuses (`scanner_ws_subscribe_enabled`,
  `recorder_subscribe_enabled`)
- [Plan 0044 — Firehose binding cache includes shadow traders](../completed/0044-firehose-binding-cache-include-shadow-traders.md) —
  the change that unlocked the volume
- [Plan 0046 — Offline backtest harness](../completed/0046-offline-backtest-for-crypto-strategies.md) §
  Task 1 housekeeper — pattern to mirror
- [`backend/services/chainlink_feed.py`](../../../backend/services/chainlink_feed.py) §
  `_housekeeper_loop` / `_housekeeper_once` — reference implementation
- [`backend/models/database.py`](../../../backend/models/database.py) —
  `TraderEvent` model, `AppSettings` model
- [`backend/services/strategies/_firehose.py`](../../../backend/services/strategies/_firehose.py) —
  emit path (no change needed; this plan is read-only on emit side)

## Validation Commands

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest backend/tests/test_trader_events_retention.py -q'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend alembic upgrade head'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "select count(*) filter (where event_type='\''firehose_evaluation'\''), count(*) filter (where event_type<>'\''firehose_evaluation'\''), pg_size_pretty(pg_total_relation_size('\''trader_events'\'')) from trader_events"'` —
  before / after a manual housekeeper kick, expect the firehose
  count to drop to ~14 days × 24 h × 262 k/h ≈ 88 M rows in
  steady state

## Task 1: DB-backed retention knobs in `app_settings`

- [ ] Add Alembic migration adding two columns to `app_settings`:
      `trader_events_firehose_retention_days` (`Integer`, default
      14, nullable false) and
      `trader_events_other_retention_days` (`Integer`, default 90,
      nullable false). Migration file lives at
      `backend/alembic/versions/<date>_add_trader_events_retention_knobs.py`.
- [ ] Mirror the columns in
      [`backend/models/database.py`](../../../backend/models/database.py)
      `AppSettings` ORM alongside the other `*_enabled` /
      `*_seconds` knobs.
- [ ] Hook through `apply_search_filters` /
      `apply_runtime_settings_overrides` in
      [`backend/config.py`](../../../backend/config.py) so the
      values are reflected on the in-memory `settings` singleton
      (same pattern as `SCANNER_WS_SUBSCRIBE_ENABLED` from Plan
      0045). Keys: `TRADER_EVENTS_FIREHOSE_RETENTION_DAYS` /
      `TRADER_EVENTS_OTHER_RETENTION_DAYS`.
- [ ] Wire the two fields into the Settings panel
      ([`frontend/src/components/SettingsPanel.tsx`](../../../frontend/src/components/SettingsPanel.tsx))
      under a new "Trader events retention" subsection with a
      visible "Apply requires no restart — housekeeper picks up at
      next 6 h tick" hint.
- [ ] Regression test:
      `backend/tests/test_app_settings_retention_knobs.py` asserts
      defaults, GET round-trip, PUT round-trip, and that
      `apply_search_filters` (called from FastAPI startup) populates
      `config.settings.TRADER_EVENTS_*_RETENTION_DAYS` from the DB
      row.
- [ ] Mark completed

## Task 2: housekeeper background task

- [ ] Add `services/trader_events_retention_service.py` modelled on
      `chainlink_feed._housekeeper_loop` (`:476-541`):
      * `_housekeeper_loop` — async task; first tick after 60 s
        startup delay, then every 6 h
      * `_housekeeper_once` — two `DELETE FROM trader_events
        WHERE created_at < NOW() - INTERVAL '...'` statements, one
        per tier, using the current values of
        `settings.TRADER_EVENTS_FIREHOSE_RETENTION_DAYS` and
        `settings.TRADER_EVENTS_OTHER_RETENTION_DAYS`
      * Each delete is **batched at 50 000 rows / iteration** (use
        `DELETE … WHERE ctid IN (SELECT ctid … LIMIT 50000)` and
        loop until 0 rows affected) so we don't lock the table for
        minutes on the first run that drains a backlog. Pause 100 ms
        between batches.
      * Emits one structured `info` log per run with `rows_deleted`,
        `tier`, `elapsed_ms`. No new firehose row — we are not
        feeding the snake.
- [ ] Start the task from the `worker-news` plane's startup
      lifespan (it has the lightest hot-path budget and an existing
      pattern for low-priority periodic background work — confirm
      from
      [`backend/workers/`](../../../backend/workers/) which file
      owns its lifespan; the housekeeper does not need to run on
      every worker).
- [ ] Idempotency: on startup, if a previous housekeeper run is
      still alive (detected via Redis-key heartbeat
      `trader_events_housekeeper_running` with 1 h TTL), skip this
      cycle and log a warn. Belt-and-suspenders against two
      workers racing.
- [ ] Regression test:
      `backend/tests/test_trader_events_retention.py`:
      * insert 10 firehose rows aged 1-20 days + 5 non-firehose
        rows aged 30-120 days
      * call `_housekeeper_once()`
      * assert rows aged > `firehose_retention_days` for firehose
        are deleted, others preserved
      * assert rows aged > `other_retention_days` for non-firehose
        are deleted, others preserved
      * assert one structured log emitted per tier
- [ ] Mark completed

## Task 3: operational guardrails

- [ ] Document the first-run drain cost: at the moment of activation
      the table has ≥ 14 days × 8.4 GB/day = **~118 GB** of
      firehose backlog if let run unattended. First housekeeper
      kick must therefore prune `firehose_evaluation` in 50 000-row
      batches with pauses to avoid runaway autovacuum or replica
      lag. Add a "First-run" subsection to the strategy doc /
      runbook noting that the first 24 h after enabling the
      housekeeper should be monitored.
- [ ] Add a small CLI helper at
      `scripts/trader_events_housekeeper_dry_run.py` that prints
      `(rows_to_delete, oldest_row, bytes_estimate)` per tier
      **without deleting**. Lets the operator preview before turning
      retention down further.
- [ ] Update
      [architecture/websocket-and-events.md](../architecture/websocket-and-events.md)
      with a "Retention" subsection under "Polymarket WS
      subscription discipline" describing the two tiers, the 6 h
      cadence, and the DB-backed knobs.
- [ ] Mark completed

## Task 4: close

- [ ] Smoke-verify on `polyhome-1`: kick the housekeeper once
      manually (`docker compose exec backend python -c
      "from services.trader_events_retention_service import
      _housekeeper_once; import asyncio; print(asyncio.run(_housekeeper_once()))"`),
      confirm `pg_total_relation_size('trader_events')` drops by
      the expected amount (depending on backlog).
- [ ] Move this plan from `backlog/` to `completed/` and update
      `plan-control-index.md`.
- [ ] Mark completed
