# Plan: Normal-tier runtime_sequence cursor race for trader signal consumption

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0057` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

The trader orchestrator advances a per-trader cursor over
`trade_signals.runtime_sequence` to pick up new copy-trade
signals. Plan 0053 documented branch (C) of this pattern at the
**fast** tier: when newer signals commit their `runtime_sequence`
before older signals do (because two writers race the
`runtime_sequence` assignment ↔ `commit()` ordering), the
cursor advances past pending rows and the trader **silently
skips them**.

Plan 0054 confirmed the same race **at the normal tier** (where
`Focused - 0x10c95474a8` operates with
`interval_seconds=5`). Post-Plan-0054 evidence
([`work-artifacts/0054-pre-fix-evidence.md`](work-artifacts/0054-pre-fix-evidence.md)
§ "Post-fix" § 5): in a 30-minute window, **41 of 86** lead-wallet
signals never reached `trader_signal_consumption` at all
(`consumed == got_decision == 45`). The worker had idle slack
during this window (`Trader cycle slow=0`, gaps ≥ 30 s collapsed
to 3, firehose load gone) — so the residual is structural, not
load-driven.

"Done" looks like: signal-to-decision coverage for
`Focused - 0x10c95474a8` reaches ≥ 90 % in a 30-min steady-state
window, and the absolute number of signals missing from
`trader_signal_consumption` drops to near-zero (the only
acceptable remainder is signals genuinely produced while the
trader was paused / disabled / killed).

## Context / References

- [`docs/plans/backlog/0053-fast-trader-signal-cache-miss-between-signal-bus-insert-and-runtime-read.md`](backlog/0053-fast-trader-signal-cache-miss-between-signal-bus-insert-and-runtime-read.md)
  — sibling plan covering the **fast-tier** mirror of the same
  race. The fix pattern proposed there (snapshot-cursor +
  bounded re-scan window) is the same shape this plan will
  apply at the normal tier; coordination required.
- [`docs/plans/0054-cap-firehose-emission-load.md`](0054-cap-firehose-emission-load.md)
  — predecessor plan that closed the α (event-loop saturation)
  side of the problem.
- [`docs/plans/work-artifacts/0054-pre-fix-evidence.md`](work-artifacts/0054-pre-fix-evidence.md)
  — pre-fix and post-fix evidence stack. The Post-fix § 5
  paragraph is the canonical proof that the residual is
  cursor-race, not α or β.
- [`backend/workers/trader_orchestrator_worker.py`](../../backend/workers/trader_orchestrator_worker.py)
  — `list_unconsumed_trade_signals` (lines 723–772),
  `cursor_runtime_sequence` updates (multiple sites including
  lines 1453–1459, 4420–4426, 5285–5295), commit boundary at
  the end of `_run_trader_once_inner`.
- [`backend/services/signal_bus.py`](../../backend/services/signal_bus.py)
  — `bump_runtime_sequence` / `runtime_sequence` assignment
  path. The race lives between the
  `bump_runtime_sequence`+`commit` of an older inserter and the
  `bump_runtime_sequence`+`commit` of a newer inserter; if the
  newer one commits first, the cursor will skip the older one.
- [`backend/services/intent_runtime.py`](../../backend/services/intent_runtime.py)
  — sibling read path; the fast-tier mirror of the cursor used
  by the trader orchestrator. Plan 0053 branch (C) operates
  here. Cross-tier helper must serve both.
- [`backend/models/database.py`](../../backend/models/database.py)
  — `TradeSignal.runtime_sequence` column, `TraderSignalCursor`
  table, `TraderSignalConsumption` ledger.

## Validation Commands

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "WITH ours AS (SELECT s.id FROM trade_signals s WHERE s.source='"'"'traders'"'"' AND s.payload_json::text ILIKE '"'"'%0x10c95474a829%'"'"' AND s.created_at > NOW() - interval '"'"'30 minutes'"'"') SELECT COUNT(*) total, COUNT(c.signal_id) FILTER (WHERE c.consumed_at IS NOT NULL) AS consumed, COUNT(d.id) AS got_decision FROM ours o LEFT JOIN trader_signal_consumption c ON c.signal_id=o.id AND c.trader_id='"'"'8c1d3d6561e94c37a81ef351bd5fc071'"'"' LEFT JOIN trader_decisions d ON d.signal_id=o.id AND d.trader_id='"'"'8c1d3d6561e94c37a81ef351bd5fc071'"'"';"'`
- `cd /Users/dtsym/Work/Splunk/_Project-X/homerun && bash scripts/run_tests_remote.sh -q tests/test_trader_orchestrator_signal_loop.py tests/test_trader_signal_consumption.py`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since=30m worker-trading 2>&1 | grep -F "cursor advanced past pending signal" | wc -l'`

Acceptance for the SQL bullet: `consumed >= 0.90 * total` AND the
absolute count of `total - consumed` ≤ 3 (i.e. ≤ 3 signals can
be genuinely racy at the inserter boundary, anything more is a
real miss). Task 5 owns the full acceptance flow.

### Task 1: Pin pre-fix evidence specific to the normal tier

- [ ] Capture a fresh 30-min window of the SQL above for the
  `Focused - 0x10c95474a8` trader
  (`trader_id=8c1d3d6561e94c37a81ef351bd5fc071`). Append to
  `work-artifacts/0057-pre-fix-evidence.md`.
- [ ] Enumerate the **specific missing `runtime_sequence`
  values** in that window (the rows in `trade_signals` for the
  lead wallet that have no matching row in
  `trader_signal_consumption` for this trader). Three were
  documented in Plan 0054 Out of scope: 236689, 236693, 236731.
  Confirm the pattern: each missing sequence is adjacent (≤ 2
  positions) to a sequence that **was** consumed, which is the
  fingerprint of the race.
- [ ] Decide between two fix shapes (write the decision in the
  artifact before coding):
  - **(A) Snapshot-cursor.** Read `cursor_runtime_sequence`,
    then `SELECT … WHERE runtime_sequence > cursor AND
    created_at <= NOW() - <safety_window>` to skip rows that
    might still be inflight on a different writer. Cursor
    advances to the *minimum* `runtime_sequence` seen in the
    last cycle, not the maximum.
  - **(B) Bounded re-scan.** Keep the high-water cursor as-is,
    but on each tick **also** scan
    `runtime_sequence BETWEEN cursor - re_scan_window AND
    cursor` for any row whose
    `(trader_id, signal_id) ∉ trader_signal_consumption`.
    Forward-only watermark, with a small bounded look-back.
  Plan 0053 proposes (B) for the fast tier. This plan should
  pick the same shape unless evidence in the artifact justifies
  otherwise.
- [ ] Mark completed

### Task 2: Implement the fix (shape per Task 1 decision)

- [ ] Single source-of-truth change in
  `trader_orchestrator_worker.py`'s
  `list_unconsumed_trade_signals` (or the equivalent SDK
  function it calls). Same code path serves both fast and
  normal tier — the fix must not regress Plan 0053's
  fast-tier remediation.
- [ ] Re-scan window size is `MAX(60, interval_seconds * 6)`
  seconds for normal tier, `MAX(2, interval_seconds * 3)` for
  fast tier. Justification: at normal tier we tolerate a
  larger look-back because the cycle interval is sparse and a
  duplicate-consumption race is cheap (the
  `trader_signal_consumption (trader_id, signal_id)` unique
  constraint absorbs it idempotently).
- [ ] Defensive de-dup: `INSERT INTO
  trader_signal_consumption … ON CONFLICT (trader_id,
  signal_id) DO NOTHING`. Verify the unique constraint
  already exists; if not, add a migration.
- [ ] Mark completed

### Task 3: Unit tests for the cursor advancement

- [ ] Create
  `backend/tests/test_trader_orchestrator_signal_loop.py` (or
  extend an existing test if one covers the loop).
- [ ] Test: inserter A inserts `runtime_sequence=N`, inserter B
  inserts `runtime_sequence=N+1`, inserter B commits **first**,
  then inserter A commits. Trader orchestrator's next cycle
  must pick up **both** signals.
- [ ] Test: the same row is not double-consumed across two
  consecutive cycles, even with the look-back window active.
- [ ] Test: rows in the look-back window that the trader was
  paused / disabled / killed for are **not** retroactively
  consumed when the trader is re-enabled. (The trader cursor
  is the only authority; the look-back is a race-fix, not a
  resume-from-pause mechanism.)
- [ ] Mark completed

### Task 4: Coordinate with Plan 0053 (fast tier)

- [ ] If Plan 0053 has already shipped its fast-tier branch (C)
  fix, confirm this plan's normal-tier change reuses the same
  helper and re-scan-window knob (with two values: fast vs
  normal). If 0053 has **not** shipped, this plan ships the
  cross-tier helper and 0053 reduces to a stub that just sets
  the fast-tier window.
- [ ] Cross-check `docs/plans/backlog/0053-...md` Status
  (`BACKLOG` → `active`?) and update the relationship in the
  Plan 0053 file if 0057 supersedes it.
- [ ] Mark completed

### Task 5: Live validation on `Focused - 0x10c95474a8`

- [ ] After deploying via `./deploy/sync_remote.sh`, wait 30 min.
  Capture the post-fix SQL row in the artifact under a
  `## Post-fix` section.
  Acceptance: `consumed >= 0.90 * total` AND `(total -
  consumed) <= 3`.
- [ ] Re-check the consumption-gap query
  (`gaps_ge_30s`, `max_gap`) — expect both to fall meaningfully
  (the cursor race is responsible for a non-trivial share of
  the long gaps observed in Plan 0054's post-fix § 4).
- [ ] If acceptance holds, the bottleneck story documented in
  Plan 0054 + this plan is complete. If acceptance fails, the
  residual is almost certainly the fundamental per-process
  GIL limit (Options 1–3 in
  [`architecture/worker-trading.md`](architecture/worker-trading.md));
  open a follow-up plan for the process-model fix
  (use the next free number — 0056 is taken by
  [`completed/0056-branch-derived-deploy-targets.md`](completed/0056-branch-derived-deploy-targets.md)).
- [ ] Mark completed

### Task 6: Document the change in the trader-pipeline architecture note

- [ ] In
  [`docs/plans/architecture/trader-pipeline.md`](architecture/trader-pipeline.md)
  (or the closest matching note — pick the one that already
  documents `runtime_sequence` and the cursor mechanism),
  append a "Plan 0057 — Normal-tier cursor race" subsection
  with the before/after numbers and a one-paragraph
  description of the race + the chosen fix shape.
- [ ] Bump `Last verified: YYYY-MM-DD` to today (UTC) on that
  note.
- [ ] Mark completed

### Task 7: Close out

- [ ] Run every command listed in `## Validation Commands`. All
  three must succeed; the SQL bullet must meet Task 5's
  acceptance thresholds.
- [ ] `git log --grep='Plan: 0057'` must list the full commit
  set produced by this plan.
- [ ] Run the
  [`plan-validator`](../../.claude/agents/plan-validator.md)
  agent against this file.
- [ ] `git mv docs/plans/0057-normal-tier-runtime-sequence-cursor-race.md
  docs/plans/completed/`.
- [ ] Update [`plan-control-index.md`](plan-control-index.md):
  flip the row's link target to
  `completed/0057-normal-tier-runtime-sequence-cursor-race.md`
  and append a one-paragraph "Per-plan note" with the
  before/after numbers.
- [ ] **Now also flip Plan 0054 to completed.** Plan 0054's
  technical scope was done at the time of writing this plan;
  it only stayed active to mark the acceptance shortfall. Move
  it once Plan 0057 closes:
  `git mv docs/plans/0054-cap-firehose-emission-load.md
  docs/plans/completed/`, and update the index row accordingly.
- [ ] Mark completed

## Out of scope

- **Reworking the inserter side** to make `runtime_sequence`
  assignment atomic with the transaction commit. The cleanest
  fix is consumer-side (snapshot cursor / bounded re-scan), and
  the inserter side has a large blast radius (every signal
  source, including the news plane and discovery plane). Defer
  until/unless Plan 0057's consumer-side fix proves
  insufficient.
- **Removing the `runtime_sequence` column entirely** and
  switching to created_at-based cursoring. Tempting but risky —
  `created_at` has millisecond resolution and the race fingerprint
  would shift, not vanish.
- **Multi-process worker-trading** (Options 1–3 in
  [`architecture/worker-trading.md`](architecture/worker-trading.md)).
  Open a separate follow-up plan if Task 5 acceptance fails
  (0056 is taken; use the next free number).
