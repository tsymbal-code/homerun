# Plan: Upsert in `sync_trader_position_inventory` to eliminate `uq_trader_position_identity` IntegrityError

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0024` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

The `uq_trader_position_identity` constraint covers
`(trader_id, mode, market_id, direction)` regardless of `status`
([`backend/models/database.py:4373-4379`](../../backend/models/database.py:4373)).
`sync_trader_position_inventory`
([`backend/services/trader_orchestrator_state.py:8364-8567`](../../backend/services/trader_orchestrator_state.py:8364))
attempts to INSERT a new `TraderPosition` row whenever its in-memory
`existing_by_identity` lookup misses
([line 8500-8519](../../backend/services/trader_orchestrator_state.py:8500)).
That lookup is built from a snapshot of `trader_positions` at the
top of the function
([line 8463-8473](../../backend/services/trader_orchestrator_state.py:8463));
between the snapshot read and the eventual `flush`/`commit`,
another concurrent caller (or a subsequent commit on a row that
this caller's transaction couldn't see) can insert a row with the
same identity tuple, and the second commit raises `IntegrityError`.

In production this manifests as the
`Sandbox - Traders Copy Trade` bot firing
`uq_trader_position_identity` IntegrityError on copy-trade signals
where the bot had a previous position on the same
`(market_id, direction)` — most often after
`circuit_breaker_safe_exit` force-closed a batch of positions,
leaving stale closed rows that the next signal collides with.
The signal-processing layer counts the resulting "failed signal"
as a loss, which feeds the `halt_on_consecutive_losses` counter
and trips the breaker, which force-closes more positions, which
seeds more collisions. Documented chain of events lives in
[`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
2026-05-10 ~10:12-11:24 UTC entry.

The current production workaround is `halt_on_consecutive_losses=false`,
which is unsafe to keep — once this fix lands, the workaround MUST
be reverted as part of close-out.

### Fix

Replace the per-row `session.add(TraderPosition(...))` INSERT with
a `pg_insert(TraderPosition.__table__).on_conflict_do_update(...)`
keyed on `constraint="uq_trader_position_identity"`. The conflict
branch overwrites the colliding row with the same set of fields the
existing UPDATE branch (line 8522-8534) would have written:
status → ACTIVE, current open_order_count, total_notional_usd,
avg_entry_price, first/last_order_at, closed_at=null, merged
payload_json, updated_at=now. This makes the operation idempotent
at the DB level — the in-memory `existing_by_identity` race
disappears because the DB itself owns the conflict resolution.

The same UPSERT pattern is already used elsewhere in the file
(e.g. [line 6716](../../backend/services/trader_orchestrator_state.py:6716)
for `TraderSignalConsumption`,
[line 6982](../../backend/services/trader_orchestrator_state.py:6982)
for `TraderSignalCursor`) so the change is a localised application
of an established codebase pattern, not a new abstraction.

### What "done" looks like

- `sync_trader_position_inventory` no longer raises
  `IntegrityError` on `uq_trader_position_identity` under any
  ordering of concurrent calls.
- Sandbox bot's IntegrityError stream
  (currently ~2 events / 5 min) drops to zero in the
  worker-trading log over a 30-min observation window post-deploy.
- `halt_on_consecutive_losses=true` is restored on the Sandbox
  bot in the same close-out (currently `false` as workaround).
- New regression tests pin: (a) the reopen-after-close case
  (existing closed row gets reopened by the next signal),
  (b) the concurrent-insert race (two parallel
  `sync_trader_position_inventory` calls on the same identity
  both succeed, second one updates the first's row).
- Architecture note for `trader_orchestrator` (closest match:
  [`docs/plans/architecture/trader-pipeline.md`](architecture/trader-pipeline.md))
  gets a one-paragraph footgun entry referencing this plan.

## Context / References

- Defective INSERT site:
  [`backend/services/trader_orchestrator_state.py:8499-8519`](../../backend/services/trader_orchestrator_state.py:8499).
- Existing UPDATE branch (the on-conflict semantics target):
  [`backend/services/trader_orchestrator_state.py:8522-8534`](../../backend/services/trader_orchestrator_state.py:8522).
- Constraint definition:
  [`backend/models/database.py:4373-4379`](../../backend/models/database.py:4373).
- Established UPSERT pattern in same file:
  [`trader_orchestrator_state.py:6706-6726`](../../backend/services/trader_orchestrator_state.py:6706).
- `pg_insert` import already in scope:
  [`trader_orchestrator_state.py:17`](../../backend/services/trader_orchestrator_state.py:17).
- Caller (where the IntegrityError surfaces):
  [`backend/services/trader_orchestrator/session_engine.py:1242`](../../backend/services/trader_orchestrator/session_engine.py:1242)
  (`_persist_execution_projection`).
- Existing tests for this file:
  [`backend/tests/test_trader_orchestrator_state_signals.py`](../../backend/tests/test_trader_orchestrator_state_signals.py).
- Related runtime journal:
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  2026-05-10 ~10:12-11:24 UTC entry (Phase 0 cascade-failure).

## Validation Commands

- `bash scripts/run_tests_remote.sh tests/test_trader_orchestrator_state_signals.py`
- `bash scripts/run_tests_remote.sh tests/test_execution_session_engine.py`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since=15m worker-trading 2>&1 | grep -c "uq_trader_position_identity"'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "SELECT (risk_limits_json::jsonb -> '\''halt_on_consecutive_losses'\'')::text AS halt FROM traders WHERE id='\''61dcbeb2b9bc42bd9e9635a09ae5e0c3'\''"'`

## Out of scope

- **Changing the `uq_trader_position_identity` constraint shape**
  (e.g. partial unique index `WHERE status='open'`). UPSERT
  fixes the race without altering the constraint contract that
  other code may rely on. If a future need arises (e.g. wanting
  to keep historical position rows distinct from a re-opened
  one), revisit then with a separate Alembic plan.
- **Changing how the consecutive-loss counter classifies failed
  signals.** That is a separate plan ("don't count
  IntegrityError as a loss") which becomes less urgent once
  this UPSERT eliminates the dominant source of those errors.
  Worth filing if the operator wants belt-and-braces.
- **Refactoring `sync_trader_position_inventory`'s read-then-write
  pattern.** The function still does a SELECT first to compute
  delta counts (inserts/updates/closures) for telemetry. Only
  the write step changes to UPSERT.
- **Optimising for batch UPSERT.** Each conflicting row goes
  through its own UPSERT statement (one per bucket). Could be
  bulk in the future; current per-row pattern matches the
  surrounding code.

### Task 1: Replace INSERT with UPSERT in `sync_trader_position_inventory`

The current code has two branches inside the `for identity, bucket in
grouped.items():` loop — INSERT (line 8500-8519) when the lookup
misses, UPDATE (line 8522-8534) when it hits. Collapse both into a
single `pg_insert().on_conflict_do_update(...)` so the DB owns the
conflict resolution.

- [x] In
      [`backend/services/trader_orchestrator_state.py:8480-8534`](../../backend/services/trader_orchestrator_state.py:8480)
      replace the `if row is None: ... else: ...` split inside the
      loop with a single UPSERT call. Pseudocode:
      ```python
      values = {
          "id": _new_id(),  # placeholder; ON CONFLICT keeps existing id
          "trader_id": trader_id,
          "mode": str(bucket["mode"]),
          "market_id": str(bucket["market_id"]),
          "market_question": bucket.get("market_question"),
          "direction": str(bucket.get("direction") or ""),
          "status": ACTIVE_POSITION_STATUS,
          "open_order_count": int(bucket.get("open_order_count") or 0),
          "total_notional_usd": float(bucket.get("total_notional_usd") or 0.0),
          "avg_entry_price": avg_entry_price,
          "first_order_at": bucket.get("first_order_at"),
          "last_order_at": bucket.get("last_order_at"),
          "closed_at": None,
          "payload_json": position_payload,  # for INSERT path
          "created_at": now,
          "updated_at": now,
      }
      stmt = pg_insert(TraderPosition.__table__).values(**values)
      # On conflict, merge payload_json with existing row's payload
      stmt = stmt.on_conflict_do_update(
          constraint="uq_trader_position_identity",
          set_={
              "market_question": stmt.excluded.market_question,
              "status": stmt.excluded.status,
              "open_order_count": stmt.excluded.open_order_count,
              "total_notional_usd": stmt.excluded.total_notional_usd,
              "avg_entry_price": stmt.excluded.avg_entry_price,
              "first_order_at": stmt.excluded.first_order_at,
              "last_order_at": stmt.excluded.last_order_at,
              "closed_at": None,
              "payload_json": func.coalesce(TraderPosition.__table__.c.payload_json, sa.cast({}, JSON)).op("||")(stmt.excluded.payload_json),
              "updated_at": stmt.excluded.updated_at,
          },
      )
      await session.execute(stmt)
      ```
      The `payload_json` merge mirrors the in-Python merge the
      old UPDATE branch did at line 8530-8532
      (`existing.update(position_payload)`). Use the existing
      JSONB `||` operator pattern from elsewhere in the file if
      one exists; otherwise fall back to a SELECT-then-merge.
      Keep telemetry counters (`inserts`/`updates`/`closures`)
      working — derive `inserts` from "was identity in
      `existing_by_identity`?" since the SELECT already happens
      anyway.
- [x] The closure loop at
      [line 8537-8551](../../backend/services/trader_orchestrator_state.py:8537)
      stays unchanged — that's pure Python attribute mutation
      on already-loaded ORM objects, no INSERT involved.
- [x] Inline 5-line comment block above the new UPSERT
      explaining: "Idempotent at the DB level — concurrent
      callers no longer race on the in-memory
      `existing_by_identity` snapshot. Conflict resolution
      mirrors the previous UPDATE branch (re-open closed
      positions, refresh sizing/timing fields, merge
      payload_json)."
- [x] Mark completed

### Task 2: Regression tests in `test_trader_orchestrator_state_signals.py`

Two scenarios pin the fix.

- [x] Add `test_sync_trader_position_inventory_reopens_existing_closed_row`:
      seed a `trader_positions` row with `status=closed_loss` for
      identity `(trader, shadow, 0xMKT, buy_no)`. Seed a fresh
      `trader_orders` row with same identity, status=open. Call
      `sync_trader_position_inventory(session, trader_id, mode='shadow', commit=True)`.
      Assert: the same row is now `status=open`,
      `open_order_count=1`, `closed_at=NULL`, payload_json
      includes `sync_source='order_inventory'`. Assert no
      duplicate row was created (`COUNT(*)=1` for the identity
      tuple).
- [x] Add `test_sync_trader_position_inventory_concurrent_insert_does_not_raise`:
      pre-insert a `trader_positions` row directly via raw SQL
      after `sync_trader_position_inventory` builds its
      `existing_by_identity` snapshot but before it commits.
      One way: monkeypatch `existing_by_identity` lookup to
      always return None (simulating the snapshot missing the
      row). Confirm the call completes without IntegrityError
      and the existing row gets updated rather than failing.
- [x] Add `test_sync_trader_position_inventory_payload_json_merges_on_conflict`:
      seed an existing row with
      `payload_json={"strategy_exit_config": {"foo": 1}, "sync_source": "manual"}`.
      Run sync with new bucket carrying
      `payload_json={"sync_source": "order_inventory", "open_order_ids": ["abc"]}`.
      Assert the resulting row's payload_json contains both the
      old `strategy_exit_config` (preserved) and the new
      `open_order_ids` (added), with `sync_source` overwritten
      to `"order_inventory"`.
- [x] Run validation:
      `bash scripts/run_tests_remote.sh tests/test_trader_orchestrator_state_signals.py`.
- [x] Mark completed

### Task 3: Deploy, restore `halt_on_consecutive_losses=true`, verify, close out

- [x] Pre-deploy: count `uq_trader_position_identity` log entries
      in last 15 min on Sandbox bot (recent baseline ~2/5min):
      ```bash
      ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since=15m worker-trading 2>&1 | grep -c "uq_trader_position_identity"'
      ```
      Record count.
- [x] Run `./deploy/sync_remote.sh`. Confirm clean restart.
- [x] Wait 5 min for the new code to handle a few cycles, then
      re-count IntegrityError occurrences (same grep, last 5
      min). Expected: **0**. Record count.
- [x] Restore `halt_on_consecutive_losses=true` on the Sandbox
      bot via `PUT /api/traders/{id}` (full risk_limits dict
      with the flag flipped). Audit the revision:
      ```bash
      ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "SELECT operator, reason, created_at FROM trader_config_revisions WHERE trader_id='\''61dcbeb2b9bc42bd9e9635a09ae5e0c3'\'' ORDER BY created_at DESC LIMIT 1"'
      ```
      Confirm a new revision was recorded with the restore reason.
- [x] Wait another 30 min and confirm bot has not auto-paused
      via circuit_breaker. Check
      `trader_events WHERE event_type LIKE 'circuit_breaker%' AND created_at > <deploy_time>`
      → expect zero rows.
- [x] Update
      [`docs/plans/architecture/trader-pipeline.md`](architecture/trader-pipeline.md)
      `## Known footguns`: add a short paragraph noting that
      `sync_trader_position_inventory` now uses UPSERT, the
      historical IntegrityError pattern is gone, and the
      consecutive-loss counter no longer gets infrastructure
      noise from this source. Bump `Last verified:` to deploy
      date.
- [x] Append a dated entry to
      [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
      recording: pre/post IntegrityError counts, the
      `halt_on_consecutive_losses=true` restore, rollback
      recipe (`git revert <SHA>` + redeploy → reverts to
      INSERT-then-pray pattern; would also need to flip
      `halt=false` again as the workaround).
- [x] `git mv docs/plans/0024-upsert-trader-position-inventory.md docs/plans/completed/`.
- [x] Update [`plan-control-index.md`](plan-control-index.md)
      to point at `completed/` path.
- [x] `git log --grep='Plan: 0024'` shows the full commit chain.
- [x] Mark completed
