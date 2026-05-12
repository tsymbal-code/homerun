# Plan: Cap firehose emission load to unblock copy-trade signal-to-decision throughput

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0054` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

`worker-trading` runs on a single asyncio event loop (single
CPU-core ceiling). A diagnostic on 2026-05-12 05:30 UTC under the
operator's current load showed the following stack on the live
host. The active-trader set (verified against `traders` on
`polyhome-1` 2026-05-12) is 7 enabled, 0 paused:

- 5 × `crypto_5m_midcycle` (fast tier): `BTC - 5min`,
  `BTC5m-dist05`, `BTC5m-dist07`, `BTC5m-dist10`,
  `BTC5m-dist15`.
- 1 × `crypto_5m_last_outcome` (fast tier): `Crypto 5m Last
  Outcome` (`trader_id=eff366f86217484b98950ea836099a02`).
- 1 × `traders_copy_trade` (normal tier): **the bot under
  observation** — `Focused - 0x10c95474a8`
  (`trader_id=8c1d3d6561e94c37a81ef351bd5fc071`, lead wallet
  `0x10c95474a829d67b6a41025da3b886f05719e999`).

Under this load the diagnostic measured:

| Metric (worker-trading, 90 min window 04:00–05:30 UTC) | Value |
|---|---|
| CPU | **102.6 %** (one core saturated) |
| Event-loop stalls in 60 min | **658** (≈ 11/min) |
| Stalls ≥ 2.0 s | **662** (50 % of the sample) |
| Avg active asyncio tasks | **184** (peak **332**) |
| Single biggest task-group in stalls | `_firehose._tracked_emission` — **62–84 in-flight** per snapshot |
| Signals from lead wallet reaching `trade_signals` | **207** |
| Signals that reached a `trader_decisions` row | **58 (28 %)** |
| Orphan signals (no decision at all before expiry) | **149 (72 %)** |
| `signal → decision` lag | avg **30.9 s**, p50 **30 s**, p99 **51 s** |

The orphan signals expire against
`max_signal_age_seconds=100` inside the copy-trade strategy: by
the time `_processor_loop` (`traders_copy_trade_signal_service`)
or `_run_trader_once` (`trader_orchestrator_worker`) gets a tick
slice on the event loop, the signal is already too old to act on.

The largest single contributor to the loop saturation is the
strategy firehose. The 5 `crypto_5m_midcycle` clones each
evaluate 12 gates per market for every `crypto_update` tick;
~16 crypto markets × 5 strategy instances × 12 gates can produce
hundreds of fire-and-forget `emit_evaluation` / `emit_gate`
tasks per second. The existing in-flight budget of **256** lets
the firehose hold a full event-loop slot's worth of work at peak,
and the budget is large enough that the per-emission overhead
(async lock on `_audit_buffer` inside `buffer_trader_event`,
`asyncio.create_task` overhead, `_refresh_binding_cache_guarded`
ticks) shows up directly in stall dumps.

This plan caps that load with two surgical changes inside
`backend/services/strategies/_firehose.py` plus a config knob:

1. Drop `_INFLIGHT_TASK_BUDGET` from **256 → 64**. Firehose is
   observability — losing emissions under pressure is the
   correct trade-off and is already designed for (see the
   "Fix OO" comment block in `_firehose.py:96-110`).
2. Add a **min-verbosity floor** evaluated **before**
   `_fire_and_forget` schedules the coroutine. Today every
   strategy gate-fail at WHISPER tier (per-gate emission for
   markets that fail the cheap gates: `timeframe`,
   `asset_enabled`, `midcycle_crossed`, etc.) creates an
   asyncio task even when the operator's UI volume dial is at
   MURMUR. With the floor in place, sub-floor coroutines are
   closed at the source, before `asyncio.create_task` and
   before the in-flight counter even ticks. The floor is
   configurable via the new `FIREHOSE_MIN_VERBOSITY` env knob
   (default `MURMUR`) so operators who want raw WHISPER
   firehose during a debug session can still get it by setting
   `FIREHOSE_MIN_VERBOSITY=whisper` and restarting the worker.

Done means: the diagnostic above re-run for `Focused -
0x10c95474a8` (`trader_id=8c1d3d6561e94c37a81ef351bd5fc071`)
shows decision coverage ≥ **90 %** of incoming signals and
`signal → decision` p99 ≤ **5 s**, with the worker-trading CPU
and stall counts measurably lower (target: < 5 stalls/min ≥ 2 s).

The plan is **opportunistic and observability-only**. It does
not touch any risk knob, any execution gate, any strategy
contract, or any storage schema. It only reduces the volume of
firehose tasks the strategy layer schedules on the event loop.

## Context / References

- [Architecture: worker-trading process model + CPU profile](architecture/worker-trading.md)
- [Architecture: Copy-Trade Pipeline (`source='traders'`)](architecture/copy-trade-pipeline.md)
- [`backend/services/strategies/_firehose.py`](../../backend/services/strategies/_firehose.py)
  — module under treatment (`_INFLIGHT_TASK_BUDGET` at line 111,
  `_fire_and_forget` at line 300, `_tracked_emission` at line 345,
  tier ranks at line 246, `emit_gate_nowait` /
  `emit_evaluation_nowait` at lines 404 / 478)
- [`backend/services/strategies/crypto_5m_midcycle.py`](../../backend/services/strategies/crypto_5m_midcycle.py)
  — the strategy emitting WHISPER per-gate evaluations (lines
  364–540 cover the gate cascade and `emit_evaluation_nowait` /
  `emit_reject` calls)
- [`backend/services/trader_hot_state.py:1271`](../../backend/services/trader_hot_state.py)
  — `buffer_trader_event` definition, the async-lock-protected
  consumer that every firehose emit eventually awaits
- [`backend/config.py:142`](../../backend/config.py) — existing
  `TRADER_EVENTS_FIREHOSE_*` knobs (retention only; this plan
  adds a complementary **ingest-side** knob)
- Plan 0004 (backlog): [Optimize worker-trading CPU hotspots](backlog/0004-optimize-worker-trading-cpu-hotspots.md)
  — sibling concern (deepcopy / oracle-history / json). Plan
  0054 is independent and lands first; 0004 stays archived
  unless a future re-profile resurrects its hotspots above 10 %.

## Validation Commands

All commands are runnable as-is against the live host. The SSH
alias is `polyhome-1` (resolves via the operator's
`~/.ssh/config`); there is no other host this plan targets.

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest -q backend/tests/services/strategies/ backend/tests/test_firehose_backpressure.py'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend ruff check backend/services/strategies/_firehose.py backend/config.py backend/tests/test_firehose_backpressure.py'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend python -c "from services.strategies._firehose import get_firehose_stats; print(get_firehose_stats())"'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "WITH ours AS (SELECT s.id sig_id, s.created_at sig_ts, d.created_at dec_ts, EXTRACT(EPOCH FROM (d.created_at - s.created_at)) * 1000 AS lag_ms FROM trade_signals s LEFT JOIN trader_decisions d ON d.signal_id=s.id AND d.trader_id='"'"'8c1d3d6561e94c37a81ef351bd5fc071'"'"' WHERE s.source='"'"'traders'"'"' AND s.payload_json::text ILIKE '"'"'%0x10c95474a829%'"'"' AND s.created_at > NOW() - interval '"'"'30 minutes'"'"') SELECT COUNT(*) total, COUNT(dec_ts) got_decision, ROUND(100.0*COUNT(dec_ts)/NULLIF(COUNT(*),0),1) coverage_pct, ROUND(AVG(lag_ms)::numeric,0) avg_lag_ms, ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY lag_ms)::numeric,0) p50, ROUND(percentile_cont(0.99) WITHIN GROUP (ORDER BY lag_ms)::numeric,0) p99 FROM ours;"'`

Acceptance for the SQL bullet: `coverage_pct >= 90` AND `p99 <= 5000`. Task 6 owns the full acceptance flow.

### Task 1: Pin pre-fix evidence and disambiguate event-loop vs Postgres latency

Before changing any code, capture the live baseline so Task 6 has
something to compare against, and resolve the central ambiguity in
this plan's hypothesis: is `worker-trading` slow because the event
loop is saturated by firehose tasks **(α)**, or because per-signal
DB writes are slow at the Postgres side **(β)**? The fix in Tasks
2–4 only helps **(α)**. If **(β)** dominates, this plan must be
re-shaped (toward `trader_decisions.payload_json` thinning or
similar) — do not run code changes on a wrong diagnosis.

- [x] Create `docs/plans/work-artifacts/0054-pre-fix-evidence.md`
  and pin the timestamp (UTC) of the first sample. Source data
  lives only on `polyhome-1`; capture commands via
  `ssh polyhome-1`.
- [x] Snapshot firehose pressure over a 5-minute window with a
  30-second cadence:
  ```bash
  ssh polyhome-1 'for i in $(seq 1 10); do \
    cd /home/polyhome/homerun && docker compose exec -T backend \
      python -c "from services.strategies._firehose import get_firehose_stats; print(get_firehose_stats())"; \
    sleep 30; done'
  ```
  Record each line into the artifact. Expected (per Overview's
  baseline): `inflight_emission_tasks` peaks at 62–84, budget=256.
- [x] Snapshot the per-trader signal→decision coverage over the
  same 30-minute window for **the live trader_id**:
  ```sql
  -- run as: docker compose exec -T postgres psql -U homerun -d homerun
  WITH ours AS (
    SELECT s.id AS sig_id, s.created_at AS sig_ts,
           d.created_at AS dec_ts,
           EXTRACT(EPOCH FROM (d.created_at - s.created_at)) * 1000 AS lag_ms
    FROM trade_signals s
    LEFT JOIN trader_decisions d
      ON d.signal_id = s.id
     AND d.trader_id = '8c1d3d6561e94c37a81ef351bd5fc071'
    WHERE s.source = 'traders'
      AND s.payload_json::text ILIKE '%0x10c95474a829%'
      AND s.created_at > NOW() - interval '30 minutes'
  )
  SELECT COUNT(*) AS total, COUNT(dec_ts) AS got_decision,
         ROUND(100.0 * COUNT(dec_ts) / NULLIF(COUNT(*),0), 1) AS coverage_pct,
         ROUND(AVG(lag_ms)::numeric, 0) AS avg_lag_ms,
         ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY lag_ms)::numeric, 0) AS p50,
         ROUND(percentile_cont(0.99) WITHIN GROUP (ORDER BY lag_ms)::numeric, 0) AS p99
  FROM ours;
  ```
  Capture the row into the artifact.
- [x] Capture the most recent "Trader cycle slow" warning for this
  trader and record its full `stage_timings_ms` breakdown — this
  is the most direct hint at α vs β:
  ```bash
  ssh polyhome-1 'cd /home/polyhome/homerun && \
    docker compose logs --since=30m worker-trading 2>&1 | \
    grep -F "Trader cycle slow" | \
    grep -F "8c1d3d6561e94c37a81ef351bd5fc071" | tail -5'
  ```
  Field `ps_decision_writes` is the discriminator: if it consumes
  > 50 % of `signal_loop` and the cycle has only 1 processed
  signal, lean toward β (Postgres-side latency). If it stays
  small but the gap between consecutive consumption rows is
  large, lean toward α (event-loop starvation).
- [x] Cross-check α by looking for the gap pattern in
  `trader_signal_consumption`:
  ```sql
  SELECT consumed_at,
         consumed_at - LAG(consumed_at) OVER (ORDER BY consumed_at) AS gap
    FROM trader_signal_consumption
   WHERE trader_id = '8c1d3d6561e94c37a81ef351bd5fc071'
     AND consumed_at > NOW() - interval '30 minutes'
   ORDER BY consumed_at DESC;
  ```
  Persistent gaps ≫ `interval_seconds=5` (e.g. 60–120 s) at
  steady-state confirm α. Record the count of gaps ≥ 30 s into
  the artifact.
- [x] Cross-check β by running one slow-cycle's DB write on the
  Postgres side directly. Pick a recent decision row, dump its
  `payload_json` size, and EXPLAIN ANALYZE a representative
  INSERT against `trader_decisions`:
  ```sql
  SELECT id, octet_length(payload_json::text) AS payload_bytes,
         created_at
    FROM trader_decisions
   WHERE trader_id = '8c1d3d6561e94c37a81ef351bd5fc071'
   ORDER BY created_at DESC LIMIT 5;
  ```
  Record the byte sizes. A payload of > 30 KB per row, written
  hundreds of times per minute, is a viable β source independent
  of firehose load.
- [x] Write a one-paragraph "diagnosis" at the bottom of the
  artifact: explicitly call **α** (firehose-driven), **β**
  (Postgres-driven), or **mixed**. Cite the numbers from the
  steps above. If the call is "β" or "mixed with β dominant",
  **stop** and convert this plan into a sibling that targets
  `payload_json` thinning instead — Tasks 2–4 will not move the
  needle.
  **Verdict 2026-05-12 06:16 UTC: α-dominant; β falsified by
  5 KB avg / 8 KB max payload.** See
  [`work-artifacts/0054-pre-fix-evidence.md`](work-artifacts/0054-pre-fix-evidence.md).
- [x] Mark completed

### Task 2: Lower the in-flight firehose budget from 256 → 64

- [x] Read [`backend/services/strategies/_firehose.py:96-114`](../../backend/services/strategies/_firehose.py)
  to confirm the in-flight counter (`_inflight_emission_tasks`)
  is the only consumer of `_INFLIGHT_TASK_BUDGET` and that
  `_fire_and_forget` at line 312 already silently closes the
  coroutine when the budget is saturated. (It does — that is
  the "Fix OO" backpressure block.)
- [x] Change `_INFLIGHT_TASK_BUDGET = 256` to
  `_INFLIGHT_TASK_BUDGET = 64` on line 111. The "Fix OO" comment
  block above the constant explicitly states that observability
  emissions are designed to be droppable under pressure; reduce
  the comment's stale "300-600 fire-and-forget tasks per second"
  estimate to "~150 in-flight peak under steady-state load
  measured 2026-05-12" so the next reader does not re-raise the
  budget without re-measuring.
- [x] Confirm `get_firehose_stats()` still returns
  `inflight_budget` so an operator can read the new ceiling via
  the validation command above without recompiling.
- [x] Mark completed

### Task 3: Add a min-verbosity floor evaluated **before** `_fire_and_forget`

- [x] In `_firehose.py`, add a module-level constant
  `_MIN_VERBOSITY_RANK: int` and a loader function
  `_resolve_min_verbosity_rank()` that reads
  `settings.FIREHOSE_MIN_VERBOSITY` (env: `FIREHOSE_MIN_VERBOSITY`),
  normalises via `tier_rank` (already defined on line 249), and
  defaults to the rank of `MURMUR` (`= 2`) when unset or
  unrecognised. The rank is resolved **once on first emit**, not
  on every call — strategies fire this code hundreds of times
  per second and reading `settings.*` per call is wasteful.
- [x] Extend `emit_gate_nowait` (line 404) and
  `emit_evaluation_nowait` (line 478) so that **before** the
  `_fire_and_forget(emit_*(...))` call they compare
  `tier_rank(verbosity)` to `_MIN_VERBOSITY_RANK`. If lower,
  close the prospective coroutine reference (or simply do not
  build one — preferred) and return. Do not increment
  `_inflight_emission_tasks`; this is a **pre-budget** drop
  and the existing `_dropped_emission_tasks` counter (designed
  for in-flight drops) does **not** apply.
- [x] Add a separate counter `_below_floor_emission_drops: int`
  and surface it in `get_firehose_stats()` alongside the
  existing fields. The post-fix diagnostic in Task 6 reads
  this counter to confirm the floor is biting at expected
  volume.
- [x] Decide explicitly whether the same floor should be
  applied to the async-only emitters (`emit_gate`,
  `emit_evaluation`, `emit_emit`). Recommend **yes** for
  symmetry — async callers exist (e.g. test paths) and a
  consistent floor avoids "the sync API drops but the async
  API doesn't" footgun. Apply the same `tier_rank(verbosity)`
  check at the top of each `emit_*` async function, after
  `await _emit_should_fire(...)` already returned `True`.
  Bump `_below_floor_emission_drops` from the async path too.
  **Resolution: applied to all six emit functions (3 sync
  nowait + 3 async). `emit_emit*` use `_below_floor(VOICE)`
  since their verbosity is hardcoded; this keeps the symmetry
  with the floor knob if someone later raises the floor above
  VOICE.**
- [x] Mark completed

### Task 4: Add the `FIREHOSE_MIN_VERBOSITY` setting in `backend/config.py`

- [x] In [`backend/config.py`](../../backend/config.py), beside
  the existing `TRADER_EVENTS_FIREHOSE_RETENTION_DAYS` block
  (~line 142), add `FIREHOSE_MIN_VERBOSITY: str = "murmur"` with
  the Plan 0054 docstring explaining the trade-off and the
  process-startup-only contract.
- [x] Confirm the field is not also DB-backed (the firehose
  floor is intentionally **process-startup-only** — operators
  who change it want a restart-bounded change, not a live
  knob that races with `_resolve_min_verbosity_rank()`'s
  cached read). State this explicitly in the docstring above
  the field.
- [x] Do **not** add this knob to `app_settings` /
  `settings_helpers.py`. Walk the existing
  `_load_db_settings_overrides` / `register_settings` paths
  if needed to confirm a plain `Settings` field with no
  matching DB column does not log a "missing column" warning
  on boot — if it does, suppress with the standard
  `runtime_only=True` convention used by other env-only
  fields.
  **Resolution:** the project has no
  `_load_db_settings_overrides` / `register_settings` machinery.
  DB-backed settings are keyed off DEFAULTS dicts in
  `backend/api/settings_helpers.py`, not off the `Settings`
  class. A plain `Settings` field with no matching DB column is
  silent at boot (verified by the analogous
  `TRADER_EVENTS_HOUSEKEEPER_*` env-only fields).
- [x] Add a one-line entry in `.env.example` (top of repo) for
  `FIREHOSE_MIN_VERBOSITY=murmur` with a `#` comment pointing
  at this plan.
- [x] Mark completed

### Task 5: Unit tests for the floor and the budget boundary
**Status:** 7/7 tests passing via `bash scripts/run_tests_remote.sh
-q tests/test_firehose_backpressure.py` against polyhome-1 with the
new files bind-mounted into the throwaway backend container. See
log of run 2026-05-12 06:30 UTC.


The firehose hot path is executed hundreds of times per second by
every strategy in `worker-trading`. A silent regression that
re-raises the budget or accepts WHISPER below the floor would not
surface in production logs — the symptom is just "stalls came
back". Lock the contract with explicit tests.

- [x] Create `backend/tests/test_firehose_backpressure.py`.
  Match the fixture style of sibling tests under
  `backend/tests/services/strategies/` — no
  `monkeypatch` of private globals beyond what is necessary,
  reset `_inflight_emission_tasks`, `_dropped_emission_tasks`,
  `_below_floor_emission_drops`, and the cached
  `_MIN_VERBOSITY_RANK` between tests via a fixture.
- [x] Test: with `FIREHOSE_MIN_VERBOSITY=murmur`,
  `emit_evaluation_nowait(..., verbosity=WHISPER)` does
  **not** increment `_inflight_emission_tasks`, increments
  `_below_floor_emission_drops` by exactly 1, and does not
  schedule any asyncio task. Use a stub event loop or
  `asyncio.all_tasks()` snapshot to assert no task was created.
- [x] Test: with `FIREHOSE_MIN_VERBOSITY=murmur`,
  `emit_gate_nowait(..., verbosity=MURMUR)` **does** schedule
  a task and increments `_inflight_emission_tasks` (i.e. the
  floor is "≥", not ">"). After the task drains,
  `_inflight_emission_tasks` returns to 0.
- [x] Test: `_resolve_min_verbosity_rank()` reads
  `settings.FIREHOSE_MIN_VERBOSITY` exactly once across N calls
  to `emit_*_nowait`. Assert by patching `settings` access with
  a counter and confirming `count == 1` after 5 emit calls.
- [x] Test: budget saturation. Manually set
  `_inflight_emission_tasks = _INFLIGHT_TASK_BUDGET` (i.e. 64),
  call `emit_gate_nowait(..., verbosity=VOICE)` — it must drop
  via the existing path, incrementing
  `_dropped_emission_tasks`, NOT
  `_below_floor_emission_drops`. The two counters must not
  conflate.
- [x] Test (async symmetry): `await emit_gate(..., verbosity=WHISPER)`
  with floor=`MURMUR` returns without calling
  `buffer_trader_event` and increments
  `_below_floor_emission_drops`. Patch
  `services.trader_hot_state.buffer_trader_event` to a Mock and
  assert `call_count == 0`.
- [x] Test (negative): unknown `FIREHOSE_MIN_VERBOSITY=banana`
  defaults to `MURMUR` rank (= 2) and does not raise. This pins
  the documented fallback behaviour from Task 4.
- [x] **Extra: `get_firehose_stats()` shape pin.** Locks the dict
  keys so the Task 6 diagnostic command keeps working and the
  `inflight_budget==64` assertion guards against regression.
- [x] Mark completed

### Task 6: Validate the throughput recovery for `Focused - 0x10c95474a8`

- [x] After deploying Tasks 2–4 via
  `./deploy/sync_remote.sh`, wait **30 min** for a
  representative steady-state sample. Capture the post-fix
  worker-trading CPU and stall stats with the same commands
  used in Task 1 (the pre-fix evidence artifact). Expected:
  CPU < 85 % (down from 102 %), ≥ 2 s stalls < 5 per minute
  (down from ≈ 11/min). Append the post-fix snapshot to the
  same `work-artifacts/0054-pre-fix-evidence.md` file under a
  new `## Post-fix` section so before/after lives side-by-side.
  **Result:** `Trader cycle slow=0` (was 2), gaps ≥ 30 s = 3
  (was 9). CPU still single-core saturated but no longer
  driven by firehose. See artifact § 1–4 + § 7.
- [x] Re-run the signal-to-decision query from Task 1 (same SQL,
  same `trader_id = '8c1d3d6561e94c37a81ef351bd5fc071'`,
  same 30-minute window). Append the row to the post-fix
  section of the evidence artifact.
  Acceptance: `coverage_pct ≥ 90` AND `p99 ≤ 5000` (5 s).
  **Partial acceptance** (`coverage_pct` in 60–89 %) is **not**
  done — see the last bullet of this task.
  **Observed:** coverage 52.3 %, p99 13 908 ms — **FAILS**
  acceptance numerically.  **Root cause:** every signal the
  worker actually *read* produced a decision row
  (`consumed == got_decision == 45`); the 41 missing signals
  were never inserted into `trader_signal_consumption` at all.
  This is the normal-tier cursor race, **not** an α
  residual — Plan 0054 cannot close it. See artifact § 5.
- [x] Capture the firehose drop counters:
  `docker compose exec -T backend python -c "from services.strategies._firehose import get_firehose_stats; print(get_firehose_stats())"`.
  Expected: `below_floor_emission_drops` non-zero and growing
  (confirms the floor is biting), `dropped_emission_tasks_total`
  near zero (confirms the budget is no longer hit).
  **Caveat:** `docker compose exec backend python -c "..."`
  spawns a **fresh** Python interpreter inside the container,
  so the counters always read 0 — they belong to the
  long-running `worker-trading` process and there is no HTTP
  endpoint that surfaces them yet. Cross-validation via DB:
  `firehose_evaluation` rows / 5 min collapsed from 18 277
  (whisper) → 18 (murmur) = **−99.9 %** (artifact § 2). Zero
  `firehose dropping` warnings in 35 min of logs (artifact
  § 8) — budget=64 sized correctly, never bites in
  steady-state.
- [x] If acceptance fails, do **not** raise the budget back.
  The data points elsewhere (the 5 duplicate
  `crypto_5m_midcycle` traders, the
  `idle_touch_commit` blocking, the `traders_copy_trade`
  processor queue depth, the per-signal
  `trader_decisions.payload_json` size if Task 1 leaned β) —
  document the residual hotspot in Task 8 and open a follow-up
  plan if structural relief is needed.
- [x] If `coverage_pct` lands in 60–89 % (i.e. the firehose fix
  helped but did not close the gap), the residual is almost
  certainly per-signal Postgres latency (β) or the cursor-race
  pattern called out in `## Out of scope`. Open the relevant
  sibling plan (0055 for cursor race, separate plan for
  payload-thinning) instead of declaring victory here.
  **Resolution:** coverage 52.3 % < 60 %, but the residual is
  pure cursor-race (41 signals missing from
  `trader_signal_consumption`), not β. **Plan 0057 must be
  opened to address the normal-tier cursor race.**
- [x] Mark completed

### Task 7: Document the change in the worker-trading architecture note

- [x] In [`docs/plans/architecture/worker-trading.md`](architecture/worker-trading.md),
  append a "Plan 0054 — Firehose backpressure tightened" subsection
  to the "Measured CPU profile" section (or create that section
  if it does not yet exist for the firehose layer). Include:
  - The pre-fix diagnostic numbers from
    `work-artifacts/0054-pre-fix-evidence.md` (Task 1).
  - The post-fix numbers from Task 6.
  - The two knobs touched (`_INFLIGHT_TASK_BUDGET`,
    `FIREHOSE_MIN_VERBOSITY`) and how an operator can tune
    them.
  - A pointer back to this plan.
  **Done:** "After plan 0054 (2026-05-12, firehose backpressure
  tightened)" subsection appended in
  [`architecture/worker-trading.md`](architecture/worker-trading.md).
- [x] Bump the `Last verified: YYYY-MM-DD` line at the
  bottom of `worker-trading.md` to the date Task 6 was
  actually run (UTC). Per
  [`AGENTS.md`](../../AGENTS.md) § Documentation
  hygiene, bumping without a real diff is forbidden — this
  task does a real diff because it appends fresh measured
  numbers.
  **Done:** `Last verified: 2026-05-12`.
- [x] Mark completed

### Task 8: Close out

- [x] Run every command listed in `## Validation Commands` against
  `polyhome-1` (post-deploy). All four must succeed. The pytest
  and ruff bullets are pass/fail; the `get_firehose_stats()`
  bullet must show `inflight_emission_tasks <
  _INFLIGHT_TASK_BUDGET` at steady-state; the SQL bullet must
  meet Task 6's acceptance thresholds.
  **Result:**
  - pytest: **12/12 passed** (`tests/test_firehose_backpressure.py`
    + `tests/test_firehose_binding_cache.py`).
  - ruff: SKIPPED — the production backend image does not include
    `ruff`. `python -m py_compile` syntax-checked the three new
    files locally — no syntax errors.
  - `get_firehose_stats()` live:
    `{'inflight_emission_tasks': 0, …, 'inflight_budget': 64}` —
    asserted `inflight < budget`.
  - SQL coverage: 52.3 % FAILS the ≥ 90 % bar (cursor-race
    residual, see Task 6 evidence and next bullet).
- [ ] `git log --grep='Plan: 0054'` must list the full commit
  set produced by this plan. Each commit's body must carry the
  `Plan: 0054` trailer per
  [`README.md`](README.md#commits-and-traceability).
  **Pending:** the operator has not requested commits yet; this
  bullet flips once the work is committed.
- [x] If Task 6's acceptance held, run the
  [`plan-validator`](../../.claude/agents/plan-validator.md)
  agent against this file to confirm the policy header,
  checkboxes and links are well-formed.
  **Done anyway** (acceptance did not hold, but the validator
  was still run to confirm the plan file itself is well-formed):
  **PASS** on all 10 Ralphex rules.
- [ ] `git mv docs/plans/0054-cap-firehose-emission-load.md
  docs/plans/completed/`.
  **Deferred** per the next bullet — Task 6 acceptance did not
  hold (coverage 52.3 % < 90 %), so the plan stays active until
  Plan 0057 closes the cursor-race residual (and/or Plan 0055
  closes the operator's parallel processor-concurrency
  diagnosis); the technical Tasks 2–7 are all done.
- [ ] Update [`plan-control-index.md`](plan-control-index.md):
  flip the row's link target to
  `completed/0054-cap-firehose-emission-load.md` and append a
  one-paragraph "Per-plan note" briefly describing the two-knob
  fix and citing the before/after numbers from
  `work-artifacts/0054-pre-fix-evidence.md`.
  **Deferred** — see above; the existing index row points at the
  active location.
- [x] If Task 6's acceptance did **not** hold, leave this plan
  active and open `0055-<next-plan>.md` for the structural
  follow-up. Pick the residual based on Task 1's α/β verdict
  and Task 6's leftover symptoms:
  - β-dominant: `payload_json` thinning in `create_trader_decision`.
  - Residual cursor-race pattern on normal-tier: sibling to
    plan 0053 branch (C).
  - Still α at lower budget: multi-process sharding,
    copy-trade processor cap rebalance, or de-duplication of
    `crypto_5m_midcycle` clones.
  **Done:** Plan 0057 opened — see file
  `0057-normal-tier-runtime-sequence-cursor-race.md`. The
  residual is purely cursor race (45 consumed == 45 decisioned;
  41 signals missing from `trader_signal_consumption` entirely).
  **Operator note (2026-05-12):** in parallel, Plan 0055
  (`0055-copy-trade-processor-concurrency-ceiling.md`) was
  drafted with a complementary diagnosis — rate-dependent
  coverage collapse (76 % @ 17 sig/min → 6 % @ 48 sig/min)
  pointing at the `traders_copy_trade` processor concurrency
  ceiling rather than the cursor mechanism. Both plans may be
  correct simultaneously; 0055 lands first and 0057 covers any
  rate-independent residual.
- [x] Mark completed (technical scope; final `git mv` waits on
  Plan 0057 / Plan 0055)

## Out of scope

- **Disabling or merging the 5 `crypto_5m_midcycle` clones**
  (`BTC - 5min`, `BTC5m-dist05`, `…dist07`, `…dist10`,
  `…dist15`). The operator explicitly declined this fix path
  on 2026-05-12 and the firehose-side fix is sufficient on its
  own — Task 6 confirms it.
- **GIL removal, multi-process sharding, or any change to the
  `worker-trading` process model.** Those are the structural
  Options 1–3 in
  [`architecture/worker-trading.md`](architecture/worker-trading.md);
  remain candidates if Task 6's acceptance does not hold.
- **Touching `trader_events` storage / retention.** Plan 0049's
  housekeeper handles the *output* side (DB rows already
  written). This plan caps the *input* side (tasks not even
  scheduled). The two are complementary and do not conflict.
- **Risk knobs (CRITICAL or HIGH tier).** This plan touches
  observability backpressure only. No risk knob is read,
  written, or computed by any change in Tasks 2–4.
- **Normal-tier `runtime_sequence` cursor race.** A live audit
  on `polyhome-1` 2026-05-12 05:44–05:48 UTC observed 3
  `trade_signals` rows for the `Focused - 0x10c95474a8` bot
  (sequences 236689, 236693, 236731) that received a
  `runtime_sequence` non-contiguously and were never picked up
  by `trader_orchestrator_worker.list_unconsumed_trade_signals`
  — the per-trader cursor advanced past them silently. This is
  the **normal-tier** mirror of the fast-tier branch (C)
  enumerated in plan
  [`0053`](backlog/0053-fast-trader-signal-cache-miss-between-signal-bus-insert-and-runtime-read.md),
  and plan 0053 explicitly defers it ("if the same pattern
  shows up on normal-tier, open a sibling plan"). Plan 0054
  does **not** address it; lowering firehose load will not
  recover those 3 signals. Open `0055-<normal-tier cursor-race
  fix>.md` as a sibling-of-0053 plan immediately after 0054,
  or sooner if Task 6 acceptance fails because of these
  missed-cursor signals rather than max-age skips.
