# Plan: Raise the copy-trade signal-processor concurrency ceiling

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0055` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

[Plan 0054](0054-cap-firehose-emission-load.md) tightened the
strategy firehose backpressure and zeroed out the WHISPER-tier
emission storm (246,711 events / 90 min → 0). Event-loop stalls
≥ 2 s fell from ≈ 11/min to 0. But the
**signal-to-decision coverage** for `Focused - 0x10c95474a829`
did not recover (27.9 % pre-fix → 25.2 % post-fix, both under a
busy signal-rate window). Per-minute breakdown of the post-fix
30-min sample shows coverage tracks signal rate linearly:

| Signals / min | Coverage |
|---:|---:|
| 17 | 76 % |
| 28 | 50 % |
| 39 | 41 % |
| 31 | 6 % |
| 48 | 6 % |

The bottleneck has shifted from event-loop saturation
(plan 0054's territory) to
[`backend/services/traders_copy_trade_signal_service.py`](../../backend/services/traders_copy_trade_signal_service.py):
the service runs **8 hardcoded `_processor_loop` coroutines**
draining one `asyncio.Queue(maxsize=20000)`. At signal bursts of
30–48/min (≈ 0.5–0.8 signals/s) the in-strategy gate cascade plus
the DB writes per signal exceeds the 8-coroutine throughput, the
queue depth grows, and signals reach
`max_signal_age_seconds = 100` inside `traders_copy_trade` and
are dropped pre-decision.

This plan raises the ceiling in two iterations:

1. **Quick lift (Iteration 1).** Expose the concurrency as a
   `Settings` field (`TRADERS_COPY_TRADE_PROCESSOR_CONCURRENCY`,
   default 24), hot-reloadable via `app_settings`. Operator can
   then tune it without a code change. Measure: does 24
   processors close the burst gap, or does the loop just hit a
   second-order limit (DB write fanout, gate-cascade CPU per
   signal)?
2. **Structural lift (Iteration 2, gated on Iteration 1
   measurement).** If raising concurrency alone is insufficient
   (CPU pinned at 100 %, processors blocked on DB writes, or
   coverage still < 75 % at peak), promote one of three
   structural fixes — multi-process plane split, separate
   `worker-copy-trade` container, or in-process DB write
   batching — to a dedicated plan and execute it.

Done means: at the 2026-05-12 burst rates (peaks of 48
signals/min for `Focused`), coverage ≥ 75 % steady-state and p99
signal-to-decision lag ≤ 10 s. If Iteration 1 alone does not
hit those numbers, this plan does not close — Task 5 records
the residual and the next plan picks up where Iteration 2
leaves off.

## Secondary failure mode confirmed (2026-05-12): live-mode crypto orders time out

The same `_processor_loop x8` saturation that drops copy-trade
signals **also** causes every `mode=live` crypto order on
production to fail with a CLOB submit timeout, even though the
crypto strategy itself evaluates correctly and selects an entry.
This was discovered during a parallel investigation of why
`BTC5m-dist05` was generating 0 executed orders on prod while
the same strategy + bot config was generating regular
`order BUY EXECUTED` events on stage. The investigation chain:

1. **Stage worked, prod did not.** Operator showed stage events:
   `BUY EXECUTED • Solana Up or Down ... Mode: SHADOW`, regular
   cadence (every 5 min on the binary boundary). On prod the
   same bot — same config, same strategy `crypto_5m_midcycle`,
   same wallet — produced **zero** `trader_orders` in 7 hours.
2. **Bot's evaluate phase succeeds on prod.** The `selected`
   decision is recorded with `reason="Passthrough: detection
   thresholds met"` (the strategy's gate cascade passed). So the
   gates, the WS oracle, the live-market context, the in-strategy
   evaluator — all fine.
3. **The order row that follows is `status=failed` with
   `error_message="submit_execution_leg timed out after 5.0s
   (CLOB degraded)"`.** This is the `_FAST_LEG_SUBMIT_TIMEOUT_SECONDS
   = 5.0` guard in
   [`backend/services/trader_orchestrator/fast_submit.py:560-571`](../../backend/services/trader_orchestrator/fast_submit.py).
4. **CLOB itself is fast.** A direct `curl` from the polyhome-prod
   host to `clob.polymarket.com` for the exact market the order
   was for returns in **80–115 ms**. There is no Polymarket-side
   degradation; the timeout is purely local.
5. **The 5 s budget is eaten by event-loop stalls inside
   `worker-trading`.** Watchdog samples at the same minute as the
   failed order showed `stall_seconds: 0.5–2.0` recurring every
   30–60 s, with task-group histograms dominated by
   `traders_copy_trade_signal_service.py:_processor_loop:163 x8`,
   `protocol.py:close_connection/transfer_data x4–6`, and the 5
   `fast_trader_runtime.py:run:228` crypto-clone cycles. Three
   sequential 1–2 s stalls inside the `wait_for(submit_execution_leg)`
   await window deterministically blow past 5 s; the POST
   completes upstream, but the response handler does not get
   loop time to drain the socket before the timer fires.
6. **Why stage doesn't hit this.** Stage bot runs in
   `mode=shadow`. In `order_manager.py:912` the shadow branch
   replaces `submit_execution_leg`'s CLOB path with
   `_resolve_shadow_book_and_tape` + Cox-PH fill simulator —
   **no network I/O, no `wait_for` against a 5 s timeout**.
   Event-loop saturation degrades shadow-mode latency but
   does not zero out shadow-mode throughput; it deterministically
   zeroes out live-mode throughput as soon as stalls cross the
   5 s threshold.

**Implication for this plan.** The same fix that lifts copy-trade
coverage (cutting the `_processor_loop` count's monopoly on
loop time, or splitting copy-trade onto a separate plane) also
unblocks every live-mode crypto order on prod. Iteration 1's
concurrency lift to 24 is the **first** lever; if it works on
copy-trade but live-mode crypto orders still time out, that
single fact is enough to short-circuit Task 4's triage to
Iteration 2B (plane split). The two coverage problems share one
root cause and one fix.

Concretely: in Task 3, when measuring Iteration 1, **also**
record live-mode crypto order outcomes for the 5 crypto clones
over the same 30-min window:

```sql
SELECT t.name,
       COUNT(*) FILTER (WHERE o.status = 'executed') AS executed,
       COUNT(*) FILTER (WHERE o.status = 'failed' AND
                              o.error_message LIKE '%CLOB%timeout%')
         AS clob_timeouts,
       COUNT(*) AS total
FROM trader_orders o
JOIN traders t ON t.id = o.trader_id
WHERE t.name LIKE 'BTC%5min%' OR t.name LIKE 'BTC5m-%'
  AND o.created_at > now() - interval '30 min'
GROUP BY t.name ORDER BY t.name;
```

Acceptance addendum for Task 3: `clob_timeouts = 0` (or at least
≤ 1 over 30 min) across the 5 crypto clones. If
`clob_timeouts > 1` while copy-trade coverage simultaneously
≥ 75 %, that asymmetry is itself diagnostic — copy-trade lives
on the queue path, crypto lives on the per-trader fast-cycle
path, so if one recovered and the other did not, Task 4 should
weigh the per-trader fast-cycle path's task budget as a separate
hotspot, not just the copy-trade processor queue.

## Context / References

- [Plan 0054 — Cap firehose emission load](0054-cap-firehose-emission-load.md)
  (immediate predecessor; landed firehose-side relief but
  left copy-trade processor coverage unaddressed)
- [Architecture: Copy-Trade Pipeline (`source='traders'`)](architecture/copy-trade-pipeline.md)
- [Architecture: worker-trading process model + CPU profile](architecture/worker-trading.md)
  — "After plan 0054" section captures the 2026-05-12 baseline
- [`backend/services/traders_copy_trade_signal_service.py:79`](../../backend/services/traders_copy_trade_signal_service.py)
  (`_processor_concurrency = 8` — the knob)
- [`backend/services/traders_copy_trade_signal_service.py:113-119`](../../backend/services/traders_copy_trade_signal_service.py)
  (`for index in range(self._processor_concurrency)` —
  task spawn loop)
- [`backend/services/traders_copy_trade_signal_service.py:158`](../../backend/services/traders_copy_trade_signal_service.py)
  (`_processor_loop` — drain queue → `_process_wallet_trade_event`)
- [`backend/services/strategies/traders_copy_trade.py`](../../backend/services/strategies/traders_copy_trade.py)
  — `max_signal_age_seconds=100` lives inside the strategy's
  gate cascade; that is the floor below which a delayed signal
  becomes a dropped signal
- [`backend/services/trader_orchestrator/fast_submit.py:560-571`](../../backend/services/trader_orchestrator/fast_submit.py)
  — `_FAST_LEG_SUBMIT_TIMEOUT_SECONDS = 5.0` and the
  `wait_for(submit_execution_leg(...))` call. The 5 s budget is
  the deadline that event-loop stalls breach for `mode=live`
  crypto orders.
- [`backend/services/trader_orchestrator/order_manager.py:912`](../../backend/services/trader_orchestrator/order_manager.py)
  — `if mode_key == "shadow":` branch that bypasses CLOB. This
  is why stage (shadow) survives the same event-loop pressure
  that breaks prod (live).

## Validation Commands

- `ssh polyhome-prod 'cd /home/polyhome/homerun && docker compose exec -T backend pytest -q backend/tests/services/test_traders_copy_trade_signal_service.py'`
- `ssh polyhome-prod 'cd /home/polyhome/homerun && docker compose exec -T backend ruff check backend/services/traders_copy_trade_signal_service.py backend/config.py backend/api/settings_helpers.py'`
- 30-min post-deploy SQL: `signal → decision` coverage and p99
  lag for `trader_id = a1be4ce19a194a489e75e85d706d438e`
  (same query as plan 0054 Task 4); acceptance ≥ 75 % coverage
  AND p99 ≤ 10 s.

### Task 1: Expose `_processor_concurrency` as a `Settings` field

- [ ] Add `TRADERS_COPY_TRADE_PROCESSOR_CONCURRENCY: int = 24`
  to [`backend/config.py`](../../backend/config.py) near the
  existing `TRADER_EVENTS_*` block. Range note in the
  docstring: 1–64 (above 64 hits asyncpg pool exhaustion
  before the gate cascade).
- [ ] Wire the field into the DB-overrides `_apply` list in the
  same file so `app_settings.traders_copy_trade_processor_concurrency`
  is read at startup. Reuse the existing
  `_resolve_runtime_override` helper — this is a hot-reloadable
  knob, not an env-only one (unlike plan 0054's
  `FIREHOSE_MIN_VERBOSITY` which intentionally requires a
  restart).
- [ ] Add the matching column to `app_settings` via a new
  Alembic migration in `backend/alembic/versions/` (the
  next sequence after the latest under
  `git ls-files backend/alembic/versions/`). Use the
  `_column_names` idempotency guard pattern from
  [`.cursor/rules/migrations.mdc`](../../.cursor/rules/migrations.mdc).
- [ ] In
  [`backend/services/traders_copy_trade_signal_service.py:79`](../../backend/services/traders_copy_trade_signal_service.py),
  replace `self._processor_concurrency = 8` with
  `self._processor_concurrency = max(1, int(settings.TRADERS_COPY_TRADE_PROCESSOR_CONCURRENCY))`.
  The value is read in `__init__` — for hot-reload to take
  effect a worker restart is required. Document that in the
  docstring; do **not** re-read inside the spawn loop (the
  reload semantics are the same as the rest of the service's
  `_*_interval_seconds` fields).
- [ ] Mark completed

### Task 2: Surface the knob in the operator UI

- [ ] Add the field to the existing
  [`backend/api/settings_helpers.py`](../../backend/api/settings_helpers.py)
  mapping that materialises Settings fields into the
  UI-visible registry. Group: `Traders` /
  `Copy-Trade Pipeline`. Label: "Copy-trade processor
  concurrency". Help text: pointer at this plan.
- [ ] Mark completed

### Task 3: Deploy and measure Iteration 1

- [ ] Deploy via `./deploy/sync_remote.sh`. After deploy, set
  the new knob to 24 through the Settings UI (or via psql:
  `UPDATE app_settings SET traders_copy_trade_processor_concurrency = 24 WHERE id = 'default'`).
  Restart worker-trading to apply
  (`ssh polyhome-prod 'cd /home/polyhome/homerun && docker compose restart worker-trading'`).
- [ ] Wait 30 min for a steady-state sample matching the
  plan 0054 measurement window (busy signal rate from the
  `Focused - 0x10c95474a829` leader). Run the plan 0054 Task 4
  SQL block. Record total, got_decision, coverage_pct, avg
  lag, p50, p99.
- [ ] Run `docker stats --no-stream homerun-worker-trading`
  five times over 5 min and record the CPU range. If CPU is
  pinned at > 130 % and coverage is still < 75 %, the bottleneck
  is no longer asyncio-internal — it is processor work itself.
  Proceed to Task 4.
- [ ] **Also measure the live-mode crypto path** (see
  "Secondary failure mode confirmed" above). Run the
  `trader_orders` SQL block from that section over the same
  30-min window. Acceptance addendum: `clob_timeouts ≤ 1`
  across the 5 crypto clones AND at least one `executed`
  crypto order. If copy-trade coverage recovered but crypto
  `clob_timeouts` is still high, do **not** close — that
  asymmetry tells Task 4 the per-trader fast-cycle path needs
  its own attention separate from the queue path.
- [ ] If coverage ≥ 75 % AND p99 ≤ 10 s AND crypto
  `clob_timeouts ≤ 1`, skip Task 4 — Iteration 1 is sufficient.
  Note all three numbers in Task 5 and close the plan.
- [ ] Mark completed

### Task 4: Decide whether Iteration 2 is needed; if yes, scope it

- [ ] Triage the residual hotspot. Capture a fresh
  diagnostic — the relevant questions are:
  - Are processors blocked on **DB writes** (`pg_stat_activity`
    shows many `idle in transaction` rows tagged
    `traders_copy_trade_signal_service`)? → Iteration 2A is
    DB-side: batch the `trader_decisions` UPSERT through a
    small write-coalescing buffer (50 ms window or 16 rows,
    whichever first).
  - Are processors **CPU-bound** in the gate cascade
    (`py-spy` shows `traders_copy_trade._evaluate` /
    `_apply_gates` dominating)? → Iteration 2B is plane-side:
    split `worker-trading` into `worker-trading-strategies`
    + `worker-trading-copy-trade` containers with a shared
    Redis pub/sub bus for `trade_signals`.
  - Are processors **starved for permits** in another
    semaphore (`_overflow_direct_process_semaphore = 16`,
    asyncpg pool size, `_credentials_lock`)? → Iteration 2C
    is the boring-knob route: raise the matching ceiling
    and re-measure.
- [ ] Write up the chosen Iteration 2 path as a new plan
  (`0056-<verb>-<subject>.md`) with its own Tasks 1–N and
  Validation Commands. Do **not** add it as a Task here —
  Iteration 2 has enough scope to warrant its own
  Ralphex plan.
- [ ] Append the new plan's row to
  [`plan-control-index.md`](plan-control-index.md). Mark this
  plan's Iteration 1 as the prerequisite.
- [ ] Mark completed

### Task 5: Update docs and close

- [ ] Append a "Plan 0055 — Copy-trade processor concurrency
  raised" subsection under "Measured CPU profile" in
  [`docs/plans/architecture/worker-trading.md`](architecture/worker-trading.md).
  Before / after table identical in shape to plan 0054's
  Task 5 entry. State whether Iteration 1 was sufficient or
  Iteration 2 (which plan) is now active.
- [ ] Bump `Last verified` on `worker-trading.md` to the
  Task 3 measurement date (UTC).
- [ ] Append an entry to
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  noting the new knob, its current value, and that the
  default (24) is the floor needed at the 2026-05-12 leader
  signal rate. The entry serves as a rollback recipe if 24
  itself causes a regression (revert via
  `UPDATE app_settings SET traders_copy_trade_processor_concurrency = 8`
  + restart).
- [ ] If Task 3 acceptance held: `git mv
  docs/plans/0055-copy-trade-processor-concurrency-ceiling.md
  docs/plans/completed/`. Update
  [`plan-control-index.md`](plan-control-index.md) link
  target.
- [ ] If Task 3 acceptance did NOT hold: leave this plan
  active and confirm 0056 (from Task 4) is in the index.
- [ ] Mark completed

## Out of scope

- **Reverting plan 0054.** This plan layers on top of
  0054, not in place of it. The firehose floor + budget
  fix is what made the post-fix latency numbers
  measurable in the first place.
- **Disabling the 5 `crypto_5m_midcycle` clones.** Operator
  explicitly declined that path on 2026-05-12. Plan 0055
  shares the same constraint.
- **Touching `max_signal_age_seconds=100` inside
  `traders_copy_trade`.** That gate is correct — it
  protects the bot from executing on stale signals. The
  fix is to deliver the signal in time, not to relax the
  gate.
- **Raising `_FAST_LEG_SUBMIT_TIMEOUT_SECONDS` from 5 s to
  paper over the live-mode crypto timeout.** Considered and
  rejected on 2026-05-12. The 5 s budget is the right one —
  Polymarket CLOB returns in 80–115 ms when the local loop
  has cycles to read the response. Raising the timeout
  extends the per-trader submit lock (held inside
  `fast_submit.execute_fast_signal` until the CLOB call
  returns), which just shifts congestion from a visible
  failure to invisible serialisation of every same-trader
  signal behind a stalled one. Fix the loop, not the timer.
- **Flipping the crypto bots to `mode=shadow` to mirror
  stage.** That is the operator's tactical fallback, not a
  resolution — production needs the live path working. It
  is documented in `docs/operational/runtime-tweaks.md` as
  a rollback recipe, not as the answer.
- **Iteration 2 itself.** Scoping it lives in Task 4; the
  actual execution is plan 0056 (or whatever number Task 4
  allocates).
- **CRITICAL or HIGH-tier risk knobs.** None are touched
  by Iterations 1 or 2.
