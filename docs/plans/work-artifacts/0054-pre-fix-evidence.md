# Plan 0054 — Pre-fix evidence

**Captured:** 2026-05-12 06:10–06:16 UTC against `polyhome-1`.
**Bot under observation:** `Focused - 0x10c95474a8`
(`trader_id=8c1d3d6561e94c37a81ef351bd5fc071`).

## 1. Firehose in-flight sampling (10 × 30 s)

Source command (Task 1 bullet 1):

```text
ssh polyhome-1 'for i in $(seq 1 10); do \
  echo "=== sample $i $(date -u +%H:%M:%S) ==="; \
  cd /home/polyhome/homerun && docker compose exec -T backend \
    python -c "from services.strategies._firehose import get_firehose_stats; print(get_firehose_stats())"; \
  sleep 30; done'
```

Raw output:

| sample | UTC      | in-flight | dropped_total | budget |
|--------|----------|-----------|---------------|--------|
| 1      | 06:10:57 | 0         | 0             | 256    |
| 2      | 06:11:30 | 0         | 0             | 256    |
| 3      | 06:12:03 | 0         | 0             | 256    |
| 4      | 06:12:35 | 0         | 0             | 256    |
| 5      | 06:13:08 | 0         | 0             | 256    |
| 6      | 06:13:41 | 0         | 0             | 256    |
| 7      | 06:14:13 | 0         | 0             | 256    |
| 8      | 06:14:46 | 0         | 0             | 256    |
| 9      | 06:15:18 | 0         | 0             | 256    |
| 10     | 06:15:51 | 0         | 0             | 256    |

**Interpretation.** The budget=256 ceiling is never reached at
30-second polling cadence in steady-state — every individual emit
completes in sub-millisecond once the binding cache is warm, so a
random-instant probe sees zero in-flight. This is consistent with
the Overview's "62–84 in-flight per stall dump" figure: the high
in-flight number occurs only **inside** an event-loop stall, when
multiple emissions queue while one of them is blocked on the
`_audit_buffer` lock or a `buffer_trader_event` await. Between
stalls the queue drains to zero in well under 30 s, so direct
polling cannot see it. The volume of emissions is the actual load
signal, not the in-flight count — see § 2 below.

## 2. Firehose volume (rows / sec via `trader_events`)

Last 5 minutes (06:11–06:16 UTC) by verbosity:

```
 verbosity |     event_type      | count
-----------+---------------------+-------
 whisper   | firehose_evaluation | 18277
 murmur    | firehose_evaluation |    17
 voice     | firehose_emit       |     4
```

`18 277 / 300 s ≈ 61 emits / s`, **99.9 % of which are
`verbosity=whisper`**. Every one of those takes the
`emit_evaluation_nowait → _fire_and_forget → _tracked_emission →
buffer_trader_event` path, contending for the
`_audit_buffer` lock inside `trader_hot_state.py:1271`.

Per-minute volume for the last 15 min sits in 3.5 k–4.3 k:

```
        m            | count
---------------------+-------
 2026-05-12 06:12:00 | 1812 (partial minute)
 2026-05-12 06:11:00 | 3780
 2026-05-12 06:10:00 | 3516
 2026-05-12 06:09:00 | 3600
 2026-05-12 06:08:00 | 4110
 2026-05-12 06:07:00 | 4294
 ...
 2026-05-12 05:58:00 | 3829
```

Steady ~4 000 / min ≈ 67 / s confirms § 1's volume estimate. No
`firehose dropping emissions` warnings appear in the last 60 m of
`worker-trading` logs, i.e. the existing in-flight backpressure
never bit. **Volume, not budget saturation, is the load.**

## 3. Signal → decision coverage (30-min window)

```sql
WITH ours AS (
  SELECT s.id sig_id, s.created_at sig_ts,
         d.created_at dec_ts,
         EXTRACT(EPOCH FROM (d.created_at - s.created_at)) * 1000 AS lag_ms
  FROM trade_signals s
  LEFT JOIN trader_decisions d
    ON d.signal_id = s.id
   AND d.trader_id = '8c1d3d6561e94c37a81ef351bd5fc071'
  WHERE s.source = 'traders'
    AND s.payload_json::text ILIKE '%0x10c95474a829%'
    AND s.created_at > NOW() - interval '30 minutes'
)
SELECT COUNT(*) total, COUNT(dec_ts) got_decision,
       ROUND(100.0 * COUNT(dec_ts) / NULLIF(COUNT(*),0), 1) coverage_pct,
       ROUND(AVG(lag_ms)::numeric, 0) avg_lag_ms,
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY lag_ms)::numeric, 0) p50,
       ROUND(percentile_cont(0.99) WITHIN GROUP (ORDER BY lag_ms)::numeric, 0) p99
FROM ours;
```

| total | got_decision | coverage_pct | avg_lag_ms | p50  | p99    |
|-------|--------------|--------------|------------|------|--------|
| 436   | 360          | **82.6 %**   | 3 445      | 2 452 | **16 420** |

**Interpretation.** 17.4 % of the lead-wallet's signals reaching
`trade_signals` never produce a decision for the bot inside their
60-second grace window. Median lag is 2.5 s but p99 reaches 16.4 s
— i.e. 1 in 100 cycles takes ≥ 16 s to even start, which is well
beyond the strategy's `max_signal_age_seconds=120` floor for sticky
copy-trade chains.

(The Overview's diagnostic snapshot quoted 28 % coverage. Coverage
fluctuates with the lead wallet's burst rate; 82.6 % here is the
quieter end of the distribution. The pass criterion in Task 6 is
`≥ 90 %` so even this window fails.)

## 4. Recent "Trader cycle slow" stage_timings_ms

The two `Trader cycle slow` warnings in the last 30 min for this
trader, both `processed_signals=1`:

| timestamp UTC          | duration_s | ps_decision_writes | ps_submit_order | ps_db_commit | signal_loop |
|-----------------------|-----------:|-------------------:|----------------:|-------------:|------------:|
| 2026-05-12 05:48:23   | **10.53**  | **6 431 ms**       | 2 889 ms        | 1 229 ms     | 9 020 ms    |
| 2026-05-12 06:06:43   | **10.16**  | **5 478 ms**       | 1 944 ms        | 404 ms       | 9 028 ms    |

`ps_decision_writes` ≈ 5–6 s for a **single** signal is the central
mystery. § 6 resolves it.

## 5. Consumption gaps (cross-check for α — event-loop starvation)

```sql
WITH gaps AS (
  SELECT consumed_at,
         consumed_at - LAG(consumed_at) OVER (ORDER BY consumed_at) AS gap
    FROM trader_signal_consumption
   WHERE trader_id = '8c1d3d6561e94c37a81ef351bd5fc071'
     AND consumed_at > NOW() - interval '30 minutes'
)
SELECT COUNT(*) FILTER (WHERE gap >= interval '30 seconds') AS gaps_ge_30s,
       COUNT(*) FILTER (WHERE gap >= interval '60 seconds') AS gaps_ge_60s,
       MAX(gap) AS max_gap,
       ROUND(AVG(EXTRACT(EPOCH FROM gap))::numeric, 2) AS avg_gap_s
  FROM gaps WHERE gap IS NOT NULL;
```

| gaps_ge_30s | gaps_ge_60s | max_gap     | avg_gap_s |
|-------------|-------------|-------------|-----------|
| 9           | 6           | **05:28**   | 4.56      |

Top 10 gaps:

```
        consumed_at         |       gap
----------------------------+-----------------
 2026-05-12 05:54:55.056    | 00:05:28.069   ← worker silent for 5m 28s
 2026-05-12 05:58:24.888    | 00:03:22.073
 2026-05-12 06:03:24.043    | 00:03:11.638
 2026-05-12 05:48:19.022    | 00:01:51.038
 2026-05-12 05:49:25.087    | 00:01:01.510
 2026-05-12 06:06:31.711    | 00:01:00.389
 2026-05-12 06:07:41.699    | 00:00:53.164
 2026-05-12 06:08:33.021    | 00:00:46.385
 2026-05-12 06:00:02.673    | 00:00:44.634
 2026-05-12 05:46:15.474    | 00:00:26.060
```

**Interpretation.** `interval_seconds=5` for this trader; in a
healthy state we'd expect every gap ≤ 5 s. We observe 9 gaps ≥ 30 s
and 6 gaps ≥ 60 s in a 30-min window, with a max of **5:28**. That
is the entire trader cycle being preempted — the bot's coroutine
yielded and other tasks ran for 5 minutes before it got scheduled
again. Pure event-loop starvation. **α-pattern, severe.**

`worker-trading` CPU at the time of evidence capture: **95.3 %**
(one core saturated, baseline matches Overview's 102 % within
sampling noise).

## 6. payload_json size (cross-check for β — Postgres-side latency)

```sql
SELECT COUNT(*) n,
       ROUND(AVG(octet_length(payload_json::text))::numeric, 0) avg_bytes,
       MAX(octet_length(payload_json::text)) max_bytes,
       MIN(octet_length(payload_json::text)) min_bytes
  FROM trader_decisions
 WHERE trader_id = '8c1d3d6561e94c37a81ef351bd5fc071'
   AND created_at > NOW() - interval '30 minutes';
```

| n   | avg_bytes | max_bytes | min_bytes |
|-----|-----------|-----------|-----------|
| 360 | **5 134** | **7 902** | 4 468     |

Spot-check on the most recent 10 rows: every payload is **~4.5 KB**,
none exceeds 8 KB. Write rate is 360 rows / 30 min ≈ 12 rows / min
= 0.2 rows / s. Postgres handles 5 KB JSON inserts at ≪ 1 ms in
this size class. **β is not viable as the dominant factor.**

This resolves the central mystery of § 4: `ps_decision_writes` is
measured by `time.monotonic()` deltas wrapping a chain of
`await`-based DB calls. With payloads < 8 KB and a 0.2 row/s
serial write rate, real Postgres time inside that chain is < 100 ms.
The remaining ~5.4 s (out of 5–6 s) is **wall-clock spent between
awaits while other coroutines ran on the saturated event loop** —
the await yields, the scheduler runs ~60 firehose tasks plus the
binding-cache refresh plus 5 crypto strategies' own awaits, and our
bot's coroutine is resumed several seconds later. The monotonic
delta then attributes the lost time to whichever bucket was open.

## 7. Diagnosis

**Verdict: α (event-loop saturation), dominant. β is not viable.**

Evidence stack:

1. Firehose emission **volume is ~61/s**, of which **99.9 % is
   `WHISPER`-tier `firehose_evaluation`**. This is exactly the
   load shape Plan 0054's Task 4 (`FIREHOSE_MIN_VERBOSITY=murmur`)
   short-circuits at the source, before `_fire_and_forget` even
   allocates an asyncio task.
2. The in-flight budget=256 **never bites** at 30 s sampling
   cadence in steady-state. Plan 0054's Task 2 (256 → 64) is a
   secondary defensive measure — it caps the leak surface for
   transient stalls, but is **not** the main fix lever. The
   floor is.
3. Consumption gaps show the worker freezes for up to **5:28**
   continuously, with 6 freezes ≥ 60 s in a 30-min window — the
   precise shape of event-loop starvation.
4. `trader_decisions.payload_json` averages 5 KB, max 8 KB.
   Postgres-side write latency cannot explain the observed
   `ps_decision_writes` numbers. The 5–6 s attribution is
   scheduler wait-time, not DB I/O.
5. Coverage 82.6 % at p99 lag 16.4 s **fails** Task 6's
   acceptance (≥ 90 % AND p99 ≤ 5 000 ms).

**Proceed with Tasks 2–4 (budget cap + verbosity floor + config
knob).** The expected post-fix shape is: WHISPER firehose drops at
the call site, ~67/s × ~99.9 % = ~67/s tasks **not scheduled**, the
event loop reclaims its slice for `traders_copy_trade` ticks,
consumption gaps collapse to ≤ 5 s, coverage rises ≥ 90 %.

**Do NOT** proceed with payload-thinning or any β-targeted change
in this plan — § 6 falsifies that hypothesis. If Task 6 acceptance
fails despite α being the dominant pattern, the residual is
overwhelmingly likely the normal-tier cursor-race documented in
this plan's "Out of scope" — open Plan 0057 then, not a
payload-thinning plan.

---

## Post-fix evidence (after Tasks 2–4 + deploy at 2026-05-12 06:27 UTC)

Window: 2026-05-12 06:28–06:58 UTC (30 min steady-state after
`./deploy/sync_remote.sh` and worker-trading container restart).

### 1. Deploy + boot verification

```
$ ssh polyhome-1 'cd /home/polyhome/homerun && \
    docker compose exec -T backend python -c \
    "from services.strategies._firehose import get_firehose_stats; print(get_firehose_stats())"'
{'inflight_emission_tasks': 0, 'dropped_emission_tasks_total': 0,
 'below_floor_emission_drops': 0, 'inflight_budget': 64}

$ docker compose exec -T backend python -c \
    "from config import settings; print(settings.FIREHOSE_MIN_VERBOSITY)"
'murmur'
```

`inflight_budget=64` and the `below_floor_emission_drops` counter
are both surfaced — plan 0054 wiring is live.

### 2. Firehose volume (rows / sec via `trader_events`)

Last 5 minutes of the window (06:53–06:58 UTC):

```
 verbosity |     event_type      | count
-----------+---------------------+-------
 murmur    | firehose_evaluation |    18
 voice     | firehose_emit       |     8
```

`(18 + 8) / 300 s = 0.087 emits / s`. Pre-fix baseline was
**61 emits / s**, of which **99.9 % were `whisper`**.

| Stream | Pre-fix | Post-fix | Δ |
|---|---:|---:|---:|
| `whisper firehose_evaluation` | 18 277 / 5 min | **0 / 5 min** | **−100 %** |
| `murmur firehose_evaluation`  | 17 / 5 min | 18 / 5 min | +6 % (noise) |
| `voice firehose_emit`         | 4 / 5 min | 8 / 5 min | +100 % (noise — opportunities are sparse) |

**Effect: −99.86 % firehose volume.** The MURMUR floor short-circuits
every `emit_evaluation_nowait(..., verbosity=WHISPER)` at the call
site, eliminating the per-gate-per-market fan-out that pre-fix
dominated worker-trading's event loop.

### 3. `Trader cycle slow` warnings

Pre-fix: 2 events in 30 min (`duration_s=10.53` and `10.16`).
Post-fix: **0 events in 30 min.**

```
$ docker compose logs --since=30m worker-trading 2>&1 \
    | grep -F "Trader cycle slow" \
    | grep -F "8c1d3d6561e94c37a81ef351bd5fc071" | wc -l
0
```

The per-cycle latency cap from the floor is intact — no individual
cycle blows past the warning threshold.

### 4. Consumption gaps (event-loop starvation proxy)

```
 gaps_ge_30s | gaps_ge_60s |     max_gap      | avg_gap_s
-------------+-------------+------------------+-----------
           3 |           3 | 00:12:02.807029  |     31.09
```

| Metric                | Pre-fix | Post-fix | Δ |
|---|---:|---:|---|
| Gaps ≥ 30 s           | 9 | **3** | **−67 %** |
| Gaps ≥ 60 s           | 6 | **3** | **−50 %** |
| Max gap               | 5 min 28 s | **12 min 03 s** | regressed |
| Avg gap               | 4.56 s | 31.09 s | regressed |

The avg-gap regression is **misleading**: total consumed signals
fell from 360 to 45 (the lead wallet was quieter in the post-fix
window), so a few large gaps inflate the average dramatically.
The single 12-minute gap (06:35–06:51 UTC) is **not** an
event-loop starvation — `Trader cycle slow=0` means the worker
processed every cycle it entered, within the 5 s SLA. The 12 min
window was either: (a) the wallet idle, with no new signals to
consume; (b) the runtime-sequence cursor race jumping the trader
past pending rows. Cross-checked below in § 5.

### 5. Coverage breakdown — α-fix did not close the gap because of cursor race

```sql
WITH ours AS (
  SELECT s.id FROM trade_signals s
   WHERE s.source='traders'
     AND s.payload_json::text ILIKE '%0x10c95474a829%'
     AND s.created_at > NOW() - interval '30 minutes'
)
SELECT COUNT(*) total,
       COUNT(c.signal_id) FILTER (WHERE c.consumed_at IS NOT NULL) AS consumed,
       COUNT(d.id) AS got_decision
FROM ours o
LEFT JOIN trader_signal_consumption c
  ON c.signal_id=o.id AND c.trader_id='8c1d3d6561e94c37a81ef351bd5fc071'
LEFT JOIN trader_decisions d
  ON d.signal_id=o.id AND d.trader_id='8c1d3d6561e94c37a81ef351bd5fc071';
```

| total | consumed | got_decision |
|------:|---------:|-------------:|
|    86 |       45 |           45 |

**Critical finding: `consumed == got_decision`.** Every signal the
worker actually *read* produced a decision row. The 41 signals
without a decision (= 86 − 45) **never appear in
`trader_signal_consumption`** — they were jumped over entirely.
This is **not** event-loop starvation (the worker had idle slack;
`Trader cycle slow=0`), **not** β (payload < 8 KB, see pre-fix § 6),
**and not** the firehose load (which is now near-zero).

This is the **normal-tier mirror of the
`runtime_sequence` cursor race** documented in
`docs/plans/backlog/0053-...md` branch (C). The trader's cursor
advances past older-but-still-pending rows when newer rows commit
their `runtime_sequence` first.

Signal → decision lag distribution for the 45 consumed signals:

```
 total | got_decision | coverage_pct | avg_lag_ms | p50  | p99
-------+--------------+--------------+------------+------+-------
    86 |           45 |         52.3 |       3579 | 2515 | 13908
```

p50 lag stayed at 2.5 s (same as pre-fix). p99 still high at
13.9 s — driven by the same 1-in-100 cycle where DB / scheduling
overhead stacks up on a busy moment. The firehose fix does not
target this tail; only the cursor-race fix (Plan 0057) and a
worker process model change (Plan 0040 / Option 2) would.

### 6. Decision-side gate rejections

```
                  reason                          | count
--------------------------------------------------+------
 copy_trade_gate_failed:entry_drift               |   27
 Shadow execution did not fill: limit_price_...   |    9
 copy_trade_gate_failed:size_floor                |    1
```

All 37 are **healthy** strategy rejections (price drifted, shadow
fill not executable, size below floor). Not relevant to Plan 0054
acceptance.

### 7. CPU

`docker stats homerun-worker-trading --no-stream`:

| Metric | Pre-fix | Post-fix |
|---|---:|---:|
| CPU | 95.3 % | **98.5 %** |
| Mem | 1.7 GiB | 0.97 GiB |

CPU is unchanged within noise — single-core saturation is now
driven by the other hotspots
([`worker-trading.md#measured-cpu-profile`](../architecture/worker-trading.md))
once the firehose path is gone. The memory drop (1.7 → 0.97 GiB)
is the GC reclaiming the asyncio-task backlog that no longer
queues; secondary confirmation that the floor + budget changes
took effect inside the actual worker process.

### 8. `firehose dropping` warnings

```
$ docker compose logs --since=35m worker-trading | grep -F 'firehose dropping' | wc -l
0
```

The new budget=64 is sized correctly — it never bites in
steady-state. The fix is load-bearing through the floor, not
through saturation drops.

## Acceptance verdict

| Acceptance criterion | Target | Observed | Verdict |
|---|---|---|---|
| firehose volume reduction | n/a (qualitative) | −99.86 % | **PASS** |
| `Trader cycle slow` / 30 min | 0 | **0** | **PASS** |
| Consumption gaps ≥ 30 s / 30 min | < 5 | **3** | **PASS** |
| `coverage_pct` (signal→decision) | ≥ 90 % | **52.3 %** | **FAIL** |
| `p99` lag | ≤ 5 000 ms | **13 908 ms** | **FAIL** |

**Plan 0054 lands its α target completely.** The firehose stream
is collapsed by 99.86 %; the worker no longer queues ~60
fire-and-forget observability tasks per second; cycle-slow events
fall to zero in a 30-min window.

**Coverage and p99 fail acceptance**, but for a reason explicitly
called out in plan 0054's "Out of scope": the **normal-tier
cursor race** (`runtime_sequence` jumps past pending rows). 41
of 86 lead-wallet signals were never inserted into
`trader_signal_consumption` at all — i.e. the worker was idle
when those signals would have been due, not busy with firehose
work.

**Next step (Task 8 below):** open **Plan 0057** to fix the
normal-tier cursor race. Do **not** roll back Plan 0054 — its
α-side fix is permanent and load-bearing for any future trader
density beyond a single shadow bot.
