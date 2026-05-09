# Architecture: worker-trading process model and CPU profile

> **Status note (2026-05-07).** An earlier version of this note
> framed worker-trading as **GIL-bound**, with four hypothesised
> hotspots (strategy eval, WS JSON parsing, Cox-PH inference,
> copy-trade processor). A live `py-spy` profile (see "Measured
> CPU profile" section below) **partially refuted** that framing:
> with `--idle` sampling, ~90 % of "100 % CPU" turned out to be
> idle ThreadPool workers, and the actual top-3 hotspots are
> algorithmic (`copy.deepcopy` ×2, uncached `get_oracle_history`,
> nested-loop `_compute_stability`) rather than GIL contention.
> The "Options to lift the GIL ceiling" section retains its
> original analysis because those options remain *contingent
> candidates* — they become relevant only if the algorithmic
> hotspots are addressed first and the next profile shows
> genuinely GIL-bound behaviour. Treat references to "GIL
> ceiling" / "GIL signature" in this document as the
> pre-profile hypothesis. The measured ground truth is in the
> "Measured CPU profile" section.

The `worker-trading` plane is the hot-path container of Homerun: it
ingests live WebSocket feeds, runs strategies, makes decisions, and
fires orders (real or shadow). On production `docker stats` shows
one CPU core at ~100 %, but as the 2026-05-07 profile clarifies,
most of that reading is idle thread-pool workers waiting on
queues; real CPU-active work is closer to 10 % of one core. After
the 2026-05-07 host upgrade (4→8 vCPU, 7.6→15 GiB RAM) every other
resource constraint disappeared. This note explains what runs
inside the single worker-trading process, the measured CPU
profile, and the realistic next steps if scaling the plane
becomes necessary.

For the broader topology see [System Overview](system-overview.md).
For the stage-by-stage signal flow see
[Trader Pipeline](trader-pipeline.md). This note is specifically
about the **container's process model and CPU usage**, not the
business pipeline.

## Purpose

This note answers four questions:

1. What loops, runtimes and services are co-resident in
   `worker-trading`?
2. Which of them are CPU-bound (subject to GIL contention) vs
   I/O-bound (asyncpg / WS / HTTP — GIL-friendly)?
3. Where is the actual CPU spent during the 4 s event-loop stalls
   we observe (`event_loop_watchdog`)?
4. What are the options for getting `worker-trading` to use more
   than one core, ranked by cost and risk?

## Process model

`worker-trading` is **one Python process**, started by
`python -m workers.host trading`. The plane configuration lives in
[`backend/workers/host.py:100-158`](../../../backend/workers/host.py).
Inside that process there is **one asyncio event loop** that
co-schedules everything in the table below. Nothing in the plane
runs in a subprocess.

| Component | Role | CPU character | GIL impact |
|---|---|---|---|
| `market_universe_worker` | Maintains live tradable-markets cache | I/O + cache reads | LOW |
| `scanner_worker` | Detects opportunities via `strategy.detect()` | Pattern match, ranking, filtering | MEDIUM |
| `scanner_slo_worker` | SLO tracker on scanner output | Aggregations | LOW |
| `search_index_worker` | Tokenises markets for the Cmd+K search | Tokenisation, indexing | MEDIUM |
| `events_worker` | Consumes the in-process `event_bus` | Callback dispatch | LOW |
| `trader_reconciliation_worker` | Polls live positions every 30 s | DB reads + REST | LOW |
| `fast_trader_runtime` | Single-leg execution for `latency_class=fast` traders | Decision path + submit | MEDIUM |
| `redeemer_worker` | Claims winnings on resolved markets | DB writes | LOW |
| `fill_simulator_refresh_worker` | Reloads Cox-PH model + empirical-constants caches | Model deserialisation (rare) | LOW |
| `trader_orchestrator` (general) | Per-signal decision loop for normal traders | **Strategy eval + risk + submit** | **HIGH** |
| `trader_orchestrator_crypto` | Same loop, crypto fast-binary lane | Same as above | **HIGH** |
| `feed_manager` | Polymarket CLOB + Kalshi WS feeds | JSON parsing | MEDIUM |
| `polymarket_user_feed` | One WS per tracked wallet | JSON parsing + dict updates | MEDIUM |
| `binance_feed` | Combined-stream WS for BTC/ETH/SOL/XRP | JSON parsing per tick | LOW–MEDIUM |
| `wallet_state_cache` | In-memory cache of wallet orders/fills | Dict mutations | LOW |
| `traders_copy_trade_signal_service` | Bridges wallet trades → copy-trade signals | Validation, signal synthesis, DB writes | MEDIUM |
| `event_loop_watchdog` | Stall detector (~50 ms probes) | Asyncio sleep + task census | LOW |

The combined heap is ~2 GiB RSS. Cross-plane state never lives in
this process — discovery, news and ML planes each carry their own
caches, and DB rows are the canonical handoff (see
[System Overview](system-overview.md), section "Cross-plane
communication").

## Where the CPU actually goes

Two instrumentation surfaces tell us:

- `event_loop_watchdog` ([services/event_loop_watchdog.py:29-165](../../../backend/services/event_loop_watchdog.py))
  fires WARNING when the loop's wakeup latency exceeds **250 ms**.
  On stall it dumps `asyncio.all_tasks()` grouped by topmost stack
  frame.
- `Trader cycle slow` log (emitted from
  [`workers/trader_orchestrator_worker.py:7944`](../../../backend/workers/trader_orchestrator_worker.py))
  carries a per-stage timing breakdown: `ps_strategy_evaluate`,
  `ps_risk_eval_setup`, `ps_db_commit`, `ps_submit_order`,
  `ps_decision_writes`, `ps_unaccounted`.

Combining both, four CPU hotspots dominate worker-trading:

### A. Strategy evaluation (`ps_strategy_evaluate` + `ps_unaccounted`)

```python
# trader_orchestrator_worker.py:193
_STRATEGY_EVAL_POOL = ThreadPoolExecutor(max_workers=8,
                                         thread_name_prefix="strategy-eval")
# trader_orchestrator_worker.py:6530
result = await loop.run_in_executor(_STRATEGY_EVAL_POOL,
                                    strategy.evaluate, ...)
```

The orchestrator already moves `strategy.evaluate` off the main
event loop into an 8-thread pool. **But Python threads still
serialise behind the GIL** — when 4–8 strategies evaluate
simultaneously (one per active trader), the threads do *not* run in
parallel. They take turns under the lock, and the main loop sits in
`asyncio.wait_for(decision_future)` waiting for the slowest one to
release. This is the dominant source of `ps_unaccounted` time
(production has shown 55–66 s of unaccounted time in soak tests).

A single strategy eval is 10–50 ms of pure Python (some use numpy:
zscore, rolling stats, correlation). At 5–10 active traders × 2–5
evals/s = 50–250 ms/s of pure-CPU work. With one core that's already
~25% of the budget, plus the GIL-contention overhead.

### B. WebSocket JSON parsing

```
~5–7 long-lived WS connections (Polymarket CLOB + per-wallet user feeds + Kalshi + Binance)
~200–300 json.loads()/s peak (CLOB depth updates dominate)
Pure-Python json (no orjson, no msgpack)
```

See [`ws_feeds.py:926, 1312`](../../../backend/services/ws_feeds.py),
[`polymarket_user_feed.py:364`](../../../backend/services/polymarket_user_feed.py),
[`binance_feed.py:218`](../../../backend/services/binance_feed.py).
Each parse is ~1–5 µs for small messages, up to ~50 µs for large
depth updates. Aggregate ~0.5–1.5 ms per 100 ms cycle = 5–15 % of
one core. Not catastrophic alone, but it competes with strategy
evaluation for the same lock.

### C. Cox-PH fill inference (`services/fill_simulator/cox_inference.py:71-101`)

Pure-math hot loop: 15 covariates, log/exp/multiply per evaluation.
~15–40 µs per call. Called ~5–20 times per signal (once per
execution-policy variant + once per risk-gate scenario). Aggregate
~1–2 ms per cycle. Modest in isolation but multiplicative.

### D. `traders_copy_trade_signal_service._processor_loop`

```python
# traders_copy_trade_signal_service.py:79
self._processor_concurrency = 8       # 8 always-on async tasks
self._queue = asyncio.Queue(maxsize=20000)
self._scope_refresh_interval_seconds = 15
```

Eight concurrent asyncio tasks always present, each pulling from a
queue and processing one wallet trade event at a time. Per event:
~5–20 ms CPU (token-cache lookup, validation, signal synthesis,
config validation, dedupe-key generation), then async DB write.
Most stalls in production have all 8 of these tasks visible at the
top of `event_loop_watchdog` task census — they're not the dominant
CPU but they're omnipresent and contend for the same lock.

### What's NOT a CPU hotspot

- **DB I/O via asyncpg.** Releases the GIL on socket reads/writes;
  asyncpg uses the binary protocol so there's no SQL-text
  serialisation overhead. Even `ps_decision_writes` ~5 s (which we
  saw historically) was wait-time, not Python CPU.
- **Order submission to Polymarket CLOB / Kalshi REST.** Pure HTTP;
  GIL released on the socket call.
- **Redis pub/sub bridging.** Same — socket I/O.

## Measured CPU profile (2026-05-07)

A 60-second `py-spy` sample on the live `worker-trading` process
under steady-state load (one active fast trader,
`Sandbox - Traders Copy Trade`, ~1 selected/5 min, ~63 decisions/min)
revealed that **the four hypotheses above are largely wrong about
which code paths actually burn CPU**. The dominant hotspots are
algorithmic, not GIL-bound.

Methodology:

- `py-spy 0.4.2`, sample rate 100 Hz, duration 60 s, no `--idle`
  (CPU-active samples only). 7 221 samples captured.
- Container had `CAP_SYS_PTRACE` added temporarily via plan 0003.
- Companion `--idle` capture (157 800 samples) confirmed that
  ~90 % of "100 % CPU" reading is actually idle ThreadPool workers
  in `concurrent/futures/thread.py:90` waiting on a queue. Real
  CPU-active work is ~10 % of one core, not 100 %.

Top self-time hotspots (CPU-active sampling):

| Rank | Hotspot | File:line | % CPU |
|---|---|---|---:|
| 1 | `copy.deepcopy` chain (caller `_run_opportunity_dispatch_loop` + `_queue_opportunity_dispatch`) | [`market_runtime.py:1533, 1560`](../../../backend/services/market_runtime.py) | **~15 %** |
| 2 | `get_oracle_history` (linear scan + bucketing on every call, no cache) | [`reference_runtime.py:215-238`](../../../backend/services/reference_runtime.py) | **~14 %** |
| 3 | `_compute_stability` (nested Python loop over price history) | [`market_monitor.py:152-157`](../../../backend/services/market_monitor.py) | **~5 %** |
| 4 | `json.dump` + `json.raw_decode` (stdlib pure-Python json) | stdlib | **~4 %** |
| 5 | `_oracle_move_from_history` | [`reference_runtime.py:298-300`](../../../backend/services/reference_runtime.py) | **~2.5 %** |
| 6 | `_rebuild_realtime_graph` | [`scanner.py:664`](../../../backend/services/scanner.py) | **~3 %** |

**Confirmed vs refuted hypotheses (A–D from above):**

- **A. Strategy evaluation (`ps_strategy_evaluate`).** Refuted as
  the dominant hotspot. Strategy genexpr / evaluate frames
  appear (e.g. `<genexpr> (<strategy:negrisk>:655)` at 0.14 %)
  but cumulatively well under 5 % of CPU. The `_STRATEGY_EVAL_POOL`
  was idle in the `--idle` capture for 90 % of the wall-clock
  time, indicating eval is not the bottleneck on this workload.
- **B. WebSocket JSON parsing.** Partially confirmed. `raw_decode`
  + ssl read = ~3 %, modest. Replacing stdlib `json` with `orjson`
  is a small but easy win.
- **C. Cox-PH fill inference.** Refuted on this workload. No
  `cox_inference.py` frames in the top 30. Likely because the
  trained model row doesn't exist yet (bootstrap phase) — but
  the *aspirational* hypothesis that this is a hot path doesn't
  hold once we measure.
- **D. Copy-trade processor.** Partially refuted. The `_processor_loop`
  appears in the idle capture (8 always-on tasks, as documented),
  but in the CPU capture they account for under 2 %.

**The actual top finding** is `copy.deepcopy` of the crypto
opportunity payload, called **twice** on the same nested
list/dict structure: once in `_queue_opportunity_dispatch`
(line 1533) when buffering, and again in
`_run_opportunity_dispatch_loop` (line 1560) when wrapping into
a `DataEvent`. This is pure-Python recursive structure copying
running on every crypto-feed update tick (Binance: 4 symbols,
~10 Hz aggregate). Eliminating one of the two deepcopy passes is
a ~10–15 lines fix with no architectural risk.

The second-largest finding is `get_oracle_history`: every call
walks the full Chainlink history, filters by cutoff, and rebuilds
buckets. There is no memoisation; the function is called
repeatedly per crypto opportunity dispatch with overlapping
parameters. A 1–3 second TTL cache or an incremental
data-structure would cut this hotspot by ≥ 80 %.

**Implication for the four options below.** Two paths reduce
the same hotspots without touching the GIL: shrinking the input
volume of markets at the ingest layer (a category whitelist —
the higher-leverage move, supersedes the local optimisation
plan) **or** local edits on each hotspot (deepcopy halving,
oracle-history caching, stability vectorisation). The local
optimisation plan is parked in
[`backlog/0004-optimize-worker-trading-cpu-hotspots.md`](../backlog/0004-optimize-worker-trading-cpu-hotspots.md)
pending a re-profile after the upstream-filter plan lands.
Options 1–3 below (free-threaded Python, ProcessPoolExecutor,
plane split) remain **contingent candidates** — they only
become relevant if a future profile (post-filter, post-local-fix)
shows genuinely GIL-bound behaviour.

Source data:

- Flamegraph: [`worker-trading-profile-2026-05-07.svg`](worker-trading-profile-2026-05-07.svg)
- Raw stacks (CPU-only): preserved at
  `/tmp/worker-trading-cpu.txt` on the operator's machine; can
  be re-derived with `py-spy record --format raw` per plan 0003
  Task 3.

### After plan 0005 (2026-05-07, tag-whitelist active)

A second 60 s `py-spy` capture was taken with the new tag-based
ingest filter active (`market_filter_tags=['crypto', 'sports',
'politics']`, cutting the catalog from ~19 966 to ~14 604 markets
per cycle, ~27 % volume reduction). Same active trader as the
baseline (`Sandbox - Traders Copy Trade`, fast latency class),
6 866 CPU-active samples captured.

Top self-time hotspots (CPU-active sampling, post-filter):

| Rank | Hotspot | File:line | % CPU |
|---|---|---|---:|
| 1 | `get_oracle_history` (lines 217+220+231+232+233 combined) | [`reference_runtime.py:217-238`](../../../backend/services/reference_runtime.py) | **~36 %** |
| 2 | `copy.deepcopy` chain (line-118 leaf + helpers) | [`copy.py`](https://docs.python.org/3/library/copy.html) (Python stdlib) called from [`market_runtime.py:1533, 1560`](../../../backend/services/market_runtime.py) | **~10.8 %** |
| 3 | `_oracle_move_from_history` (lines 298+300+301) | [`reference_runtime.py:298-301`](../../../backend/services/reference_runtime.py) | **~6.6 %** |
| 4 | `_rebuild_crypto_rows_from_cache` | [`market_runtime.py:1765`](../../../backend/services/market_runtime.py) | **~2.9 %** |
| 5 | `pydantic.model_validate` (call site = recorder ingestion) | pydantic stdlib | **~3.6 %** |
| 6 | `json.raw_decode` + `json.dump` (stdlib pure-Python json) | stdlib | **~3.1 %** |

Comparison vs. 2026-05-07 baseline:

| Hotspot | Baseline | Post-filter | Δ |
|---|---:|---:|---|
| `copy.deepcopy` (sum) | ~15 % | ~10.8 % | **−28 %** |
| `_compute_stability` | ~5 % | <1 % (out of top-25) | **−80 %** |
| `_rebuild_realtime_graph` | ~3 % | <1 % | dropped |
| `get_oracle_history` (sum) | ~14 % | **~36 %** | up in *share* (held in absolute, so its share rose because other paths shrank) |
| `_oracle_move_from_history` | ~2.5 % | ~6.6 % | same dynamic — share rose because the denominator shrank |

**Why `get_oracle_history` rose in share.** The tag filter
prunes the *Polymarket* catalog (Polymarket markets and events
are filtered by tag union), but the crypto-fast-binary lane in
`market_runtime.py` reads its reference series directly from the
**Binance** WS feeds + Chainlink oracle history, neither of
which are gated by the catalog. So the two `reference_runtime`
hotspots are essentially constant in absolute terms; the
fraction of total CPU rose because the catalog-driven hotspots
(`_compute_stability`, `_rebuild_realtime_graph`,
half of `deepcopy`) shrank.

**Decision.** Two hotspots remain ≥ 10 % of CPU after the tag
filter (`get_oracle_history` ~36 %, `copy.deepcopy` ~10.8 %), so
the local-fix plan
[`backlog/0004-optimize-worker-trading-cpu-hotspots.md`](../backlog/0004-optimize-worker-trading-cpu-hotspots.md)
is **pulled back into the active queue** (see
[`plan-control-index.md`](../plan-control-index.md)). `_compute_stability`
already dropped under 1 % so its scope can be reduced or removed
from that plan; `get_oracle_history` TTL caching and one-pass
`deepcopy` remain.

Source data (post-filter):

- Flamegraph: [`worker-trading-profile-2026-05-07-post-filter.svg`](worker-trading-profile-2026-05-07-post-filter.svg)
- Raw stacks: regenerable with the same `py-spy record --format
  raw --duration 60 --rate 100 --pid <worker-pid>` command per
  plan 0003 Task 3 (the temporary `cap_add: [SYS_PTRACE]` was
  re-applied for the capture and reverted immediately after —
  see plan 0005 Task 8 for the exact sequence).

### After plan 0006 (2026-05-07, crypto fast-binary lane off)

A third 60 s `py-spy record --rate 100` capture was taken with
the **crypto fast-binary lane disabled** through the new toggle
in `Settings → Scanner` (worker_control(crypto).is_paused=True
via `POST /api/workers/crypto/pause`). Same fast trader still
running (`Sandbox - Traders Copy Trade`). Tag whitelist remained
on (`['sports']`). 2 437 CPU-active samples in the raw export.

Top self-time hotspots (lane off):

| Rank | Hotspot | File:line | % CPU |
|---|---|---|---:|
| 1 | `_worker` (ThreadPoolExecutor) | [`concurrent/futures/thread.py:90`](https://docs.python.org/3/library/concurrent.futures.html) | **9.8 %** |
| 2 | `_normalize_history_points` | [`shared_state.py:275`](../../../backend/services/shared_state.py) | 4.4 % |
| 3 | `pydantic.model_dump` | pydantic stdlib | 3.8 % |
| 4 | `json.raw_decode` | json stdlib | 3.7 % |
| 5 | asyncio `selector_events.write` | stdlib | 3.0 % |
| 6 | asyncio `_read_ready__get_buffer` | stdlib | 2.2 % |
| 7 | `ssl.read` | stdlib | 2.0 % |
| 8 | websockets `permessage_deflate.decode` | stdlib | 1.2 % |
| 9 | `_ensure_hot_subscriptions` | [`intent_runtime.py:1083`](../../../backend/services/intent_runtime.py) | 0.8 % |
| 10 | `_deepcopy_dict` | stdlib | 0.7 % |

Comparison vs. the post-filter point:

| Hotspot | Post-filter | Lane off | Δ |
|---|---:|---:|---|
| `get_oracle_history` (sum) | **~36 %** | < 1 % (out of top-25) | **−97 %** |
| `_oracle_move_from_history` | **~6.6 %** | < 1 % | **−85 %+** |
| `_rebuild_crypto_rows_from_cache` | ~2.9 % | < 1 % | dropped |
| `copy.deepcopy` (sum) | ~10.8 % | ~0.7 % | **−93 %** |
| `_compute_stability` | < 1 % | < 1 % | unchanged |

**Decision.** All three Plan 0004 hotspots
([`docs/plans/backlog/0004-optimize-worker-trading-cpu-hotspots.md`](../backlog/0004-optimize-worker-trading-cpu-hotspots.md))
collapsed below 1 % of CPU after the lane was disabled. `get_oracle_history`
disappears almost entirely (it runs in the crypto-lane payload
rebuild path), and the catalog-driven `copy.deepcopy` chain runs
only on `get_crypto_markets()` which is now no-op while the lane
is off. **Plan 0004 is therefore archived without further work**
when the operator keeps the crypto lane off.

If the operator re-enables the crypto lane (e.g. trading a
crypto-fast-binary strategy), the post-filter hotspot
distribution will return and Plan 0004 should be revisited then.
Track this in [`plan-control-index.md`](../plan-control-index.md).

The current top of the profile is dominated by stdlib I/O
machinery (asyncio selector loops, ssl/websockets decode,
pydantic + json serialisation). None of these is a meaningful
single-file fix; if `worker-trading` saturates again, the next
escalation is the multi-process split path described in the
"Options if the CPU plane saturates after upstream + local
fixes" section below, not another local-hotspot pass.

Source data (lane off):

- Flamegraph: [`worker-trading-profile-2026-05-07-crypto-lane-off.svg`](worker-trading-profile-2026-05-07-crypto-lane-off.svg)
- Raw stacks: same `py-spy` recipe as above; the temporary
  `cap_add: [SYS_PTRACE]` was re-applied for the capture and
  reverted immediately after (plan 0006 Task 7).

## Why a bigger box helped only halfway

The 2026-05-07 resource bump (4→8 vCPU, 7.6→15 GiB RAM) gave
**other** processes (backend, postgres, worker-discovery) more
headroom and wiped out kswapd0 / page-cache thrashing. That
restored normal asyncpg latency and `pg_stat_database.cache_hit_pct`
≥ 99%. Indirectly that helped `worker-trading` (its DB calls
returned faster, freeing the loop sooner).

But the single-core CPU reading did not move (and as the profile
later showed, that reading was largely idle threads — see
caveat at the top of this note):

| Metric | Before (4 vCPU/7.6 G) | After (8 vCPU/15 G) |
|---|---:|---:|
| `worker-trading` CPU | 100.83% | 111.81% |
| Max event-loop stall | 16.9 s | 4.1 s |
| `armed_to_now_ms` (signal latency) | 56 257 | 8 634 |
| `Trader cycle slow` events / 15 min | dozens | 0 |
| Idle CPU on host | ~30% (load 1.7/4 vCPU) | ~85% (load 1.9/8 vCPU) |

`worker-trading` still uses ~1.1 cores. The 6.9 cores around it sit
idle from its perspective. Vertical scaling alone does not fix
the within-process limit — it just makes the wait shorter.

## Options if the CPU plane saturates after upstream + local fixes

> **Read order.** These options were written when GIL contention
> was the leading hypothesis. The 2026-05-07 profile reframed
> that picture (see "Measured CPU profile" above). Treat the
> options below as **contingent**: they're the right toolbox
> *if* a future profile — captured *after* the upstream
> category-filter plan and the local-optimisation plan
> ([`backlog/0004-...`](../backlog/0004-optimize-worker-trading-cpu-hotspots.md)) —
> shows the worker genuinely CPU-bound across many small frames
> with no single dominant hotspot. Until then, do not treat
> these options as the next step.

Four options, ranked by effort/reward.

### Option 1 — Python 3.13 free-threaded build (`--disable-gil`)

**Effort: 1–2 days of testing.** **Risk: LOW–MEDIUM.** **Expected
stall reduction: 50–80 %.**

Python 3.13 (October 2024) ships an optional free-threaded build.
It removes the GIL globally; the existing `ThreadPoolExecutor` for
strategy eval becomes truly parallel without any code change.

What changes:

```dockerfile
# backend/Dockerfile:18
ARG PYTHON_VERSION=3.12   # current
# →
ARG PYTHON_VERSION=3.13   # new, plus install free-threaded variant
```

Plus a venv build flag and a CI matrix that tests against both
single-threaded 3.13 and free-threaded 3.13t for fallback.

Risks:

- **C-extension compatibility.** `numpy`, `scipy`, `asyncpg`,
  `cryptography`, `lxml` all have 3.13 wheels by mid-2025. A
  smaller dependency without a free-threaded wheel will block the
  bump. Mitigation: pre-flight `pip install` test in CI before
  cutting the change.
- **Single-thread overhead.** Removing the GIL adds ~15–40 %
  overhead to single-threaded codepaths (atomic refcount ops). On a
  CPU-bound, multi-threaded service that's a net win; on the
  feed-parsing hot path (single-threaded JSON parse) it's a small
  loss. Net effect on production workload: positive.
- **Ecosystem maturity.** The free-threaded build is "experimental
  but stable" as of 3.13.1; some libraries explicitly opt in via
  `Py_GIL_DISABLED` macro. Risk of subtle thread-safety bugs in
  third-party C extensions.

This is the highest-leverage option: zero code change, the existing
`_STRATEGY_EVAL_POOL` (8 threads) becomes real 8-way parallelism on
an 8-core box.

### Option 2 — `ProcessPoolExecutor` for strategy evaluation

**Effort: 2–3 weeks.** **Risk: LOW.** **Expected stall reduction:
30–50 %.**

Replace the `ThreadPoolExecutor` with a `ProcessPoolExecutor`.
Each subprocess has its own GIL → they actually run in parallel
on multiple cores. 2 processes is enough — more burns memory and
serialisation overhead.

```python
# trader_orchestrator_worker.py:193
from concurrent.futures import ProcessPoolExecutor
_STRATEGY_EVAL_POOL = ProcessPoolExecutor(max_workers=2,
                                          initializer=_init_worker)
```

Blockers:

1. **Pickleability.** Strategy classes loaded from DB source code
   need stable `__getstate__/__setstate__`. Custom strategies with
   closures or lambdas will fail. Fix: enforce factory functions or
   pickleable adapter.
2. **Serialisation cost.** Signal payload + trader context
   pickle-roundtrip is ~5–10 ms per eval. Acceptable when total
   eval is 20–100 ms.
3. **Worker initialiser.** Each subprocess must `import` the
   strategy module and warm caches on startup; needs a small
   bootstrap function (~20 lines).

This stacks well with Option 1. With Python 3.13 free-threaded
*and* `ProcessPoolExecutor`, you get true parallelism even if one
of the two paths regresses.

### Option 3 — Multi-process plane split (orchestrator vs feeds)

**Effort: 4–6 weeks.** **Risk: MEDIUM.** **Expected stall
reduction: 50–70 %.**

Split `worker-trading` into two containers:

```
worker-trading-core    → trader_orchestrator + fast_trader_runtime + risk + submit
worker-trading-feeds   → feed_manager + polymarket_user_feed + binance_feed
                         + wallet_state_cache + traders_copy_trade_signal_service
```

Two separate event loops, two separate GILs. The orchestrator no
longer competes with WS JSON parsing or copy-trade validation.

Blockers in current code:

1. **Shared singletons.** `event_bus` (in-memory pub/sub at
   [`event_bus.py:102`](../../../backend/services/event_bus.py))
   and `wallet_state_cache` are module-level globals shared between
   feed code and orchestrator code. Splitting requires replacing
   in-process callbacks with Redis pub/sub for cross-container
   events. ~50–100 lines per consumer.
2. **In-memory caches with low-ms read SLA.** `feed_manager._price_cache`,
   `wallet_state_cache`, `trader_hot_state._snapshots` — the
   orchestrator reads these on every cycle. Moving them across a
   process boundary adds 5–20 ms per read. Mitigation: keep a thin
   read-through cache in the orchestrator side, refreshed via Redis
   keyspace notifications or an HTTP endpoint.
3. **Live-market revalidation.** `live_market_revalidation` gate
   reads `market_data_age_ms` from the in-memory feed cache; that
   must become a thin HTTP call or stay readable from a shared
   Redis copy.

This is the highest-impact long-term option but the most invasive.
Worth doing only after Options 1 and 2 are in place, and only if
the GIL ceiling is *still* binding.

### Option 4 — Cython / Rust extension for Cox-PH inner loop

**Effort: 1–2 weeks.** **Risk: LOW.** **Expected stall reduction:
5–10 %.**

The Cox-PH `evaluate()` method
([`cox_inference.py:82-101`](../../../backend/services/fill_simulator/cox_inference.py))
is pure math: 15 covariates, log/exp/mul. Trivial to rewrite as a
Cython `cdef class` or a Rust `pyo3` extension. Releases the GIL
inside the inner loop.

Worth doing only as part of a broader extension push (e.g.,
strategy hot paths, statistics aggregation). On its own the
absolute time saved is small (1–2 ms per cycle), but it scales
nicely with order volume and proves out the toolchain for further
extensions.

## Summary table

| Option | Effort | Risk | Stall reduction | Code-change cost |
|---|---|---|---:|---|
| 1. Python 3.13 free-threaded | 1–2 days | LOW–MEDIUM | 50–80% | None (Dockerfile bump) |
| 2. `ProcessPoolExecutor` for strategies | 2–3 weeks | LOW | 30–50% | Strategy serialisation, ~200 lines |
| 3. Plane split (orchestrator vs feeds) | 4–6 weeks | MEDIUM | 50–70% | Singleton refactor, IPC, ~500 lines |
| 4. Cython Cox-PH | 1–2 weeks | LOW | 5–10% | New build step + `.pyx` wrapper |
| **1 + 2 (combo)** | **3 weeks** | **LOW** | **60–90%** | Best risk/reward stack |
| 1 + 2 + 3 (full) | 6 weeks | MEDIUM | 80–95% | Long-term ceiling |

## Recommendation

**Updated 2026-05-07 after profile.** The original recommendation
("start with Option 1 — Python 3.13 free-threaded build")
assumed worker-trading was GIL-saturated. The profile showed
that's not the current state — real CPU-active work is ~10 % of
one core, dominated by three algorithmic hotspots. The revised
ordering:

1. **Reduce input volume first — DONE.** Plan 0005
   ([`completed/0005-tag-based-market-filter-at-ingest.md`](../completed/0005-tag-based-market-filter-at-ingest.md))
   added an OR-logic tag whitelist at the Polymarket ingest
   layer (`scanner._apply_market_tag_whitelist`); the
   post-filter profile is in
   ["After plan 0005" above](#after-plan-0005-2026-05-07-tag-whitelist-active).
   `_compute_stability` and `_rebuild_realtime_graph` dropped
   from the top-25 entirely; `copy.deepcopy` shrank ~28 %; the
   crypto-fast-binary reference path
   (`get_oracle_history`/`_oracle_move_from_history`) was
   not addressable from the catalog filter and remained the
   dominant hotspot until step 1.5.
1.5. **Disable parallel ingest lanes the operator does not need
   — DONE.** Plan 0006
   ([`completed/0006-crypto-fast-binary-lane-toggle.md`](../completed/0006-crypto-fast-binary-lane-toggle.md))
   surfaced an on/off toggle for the crypto fast-binary scanner
   in `Settings → Scanner`, plugged the two paths the existing
   `worker_control(crypto)` row did not cover (startup refresh,
   reactive Binance-tick rebuild), and shipped cache
   invalidation on the active↔off transition. With the lane off,
   `get_oracle_history` and `_oracle_move_from_history` collapse
   from ~42 % combined to < 2 %; `copy.deepcopy` collapses from
   ~10.8 % to ~0.7 %. See
   ["After plan 0006" above](#after-plan-0006-2026-05-07-crypto-fast-binary-lane-off).
2. **Local opportunistic fixes second — ARCHIVED.** Plan 0004
   ([`backlog/0004-optimize-worker-trading-cpu-hotspots.md`](../backlog/0004-optimize-worker-trading-cpu-hotspots.md))
   targeted exactly the hotspots that step 1.5 made disappear.
   With the lane off all three Plan 0004 hotspots are < 1 % of
   CPU, so no local-fix work is needed. The plan is parked; if
   the operator later turns the crypto lane back on (e.g. trades
   a crypto-fast-binary strategy) and the post-filter
   distribution returns, Plan 0004 should be revived.
3. **Architectural options (1–4 above) third** — only if a
   re-profile after step 2 shows genuinely GIL-bound behaviour.
   At that point Option 1 (3.13 free-threaded) remains the
   cheapest meaningful win; Option 2 (ProcessPool) is the
   natural follow-up.

Option 4 (Cython) is a "do it if you're already extending the
build pipeline for something else" item, not a solo project.

## Out of scope for this note

- **Live-trading semantics.** Everything above applies equally to
  shadow and live; the per-process CPU model is mode-agnostic.
- **Backend / `worker-news` / `worker-discovery` profiling.**
  Those processes have different workload mixes; each merits its
  own profile if a similar question arises (backend is mostly
  I/O, worker-news loads big ML models, worker-discovery is
  REST-bound). They are not covered by the 2026-05-07 profile.
- **Vertical scaling further.** Going to 16+ vCPU does little
  for `worker-trading` while the algorithmic hotspots dominate;
  a bigger host alone is a poor budget allocation until the
  upstream-filter and local-fix plans land.

## Reading list

- `backend/workers/host.py:100-158` — trading-plane configuration.
- `backend/workers/trader_orchestrator_worker.py:193, 6500-7944` —
  hot loop, `_STRATEGY_EVAL_POOL`, stage timing emission.
- `backend/services/trader_orchestrator/session_engine.py` — execution
  session orchestration.
- `backend/services/ws_feeds.py:926, 1312` — JSON parse hotspots.
- `backend/services/polymarket_user_feed.py:364` — same.
- `backend/services/binance_feed.py:218` — same.
- `backend/services/traders_copy_trade_signal_service.py:57-259` —
  the always-on 8-task processor.
- `backend/services/event_loop_watchdog.py:29-165` — 250 ms stall
  threshold, task census on breach.
- `backend/services/fill_simulator/cox_inference.py:71-101` —
  Cox-PH inner loop, candidate for Option 4.
- `backend/Dockerfile` — `PYTHON_VERSION` arg, candidate for
  Option 1.
- [Trader Pipeline](trader-pipeline.md) — sibling note covering the
  business-side data flow rather than the process model.

Last verified: 2026-05-08
