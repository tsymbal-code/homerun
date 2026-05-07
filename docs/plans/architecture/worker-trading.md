# Architecture: worker-trading & the GIL ceiling

The `worker-trading` plane is the hot-path container of Homerun: it
ingests live WebSocket feeds, runs strategies, makes decisions, and
fires orders (real or shadow). On production it consistently
saturates **one** CPU core at 100% — the classical Python GIL
signature. After the 2026-05-07 host upgrade (4→8 vCPU, 7.6→15 GiB
RAM) every other resource constraint disappeared, but
`worker-trading` is still pinned to a single core. This note
explains what runs inside that single process, where the GIL
actually hurts, and what the realistic options are for breaking the
ceiling.

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

## Why a bigger box helped only halfway

The 2026-05-07 resource bump (4→8 vCPU, 7.6→15 GiB RAM) gave
**other** processes (backend, postgres, worker-discovery) more
headroom and wiped out kswapd0 / page-cache thrashing. That
restored normal asyncpg latency and `pg_stat_database.cache_hit_pct`
≥ 99%. Indirectly that helped `worker-trading` (its DB calls
returned faster, freeing the loop sooner).

But the GIL ceiling did not move:

| Metric | Before (4 vCPU/7.6 G) | After (8 vCPU/15 G) |
|---|---:|---:|
| `worker-trading` CPU | 100.83% | 111.81% |
| Max event-loop stall | 16.9 s | 4.1 s |
| `armed_to_now_ms` (signal latency) | 56 257 | 8 634 |
| `Trader cycle slow` events / 15 min | dozens | 0 |
| Idle CPU on host | ~30% (load 1.7/4 vCPU) | ~85% (load 1.9/8 vCPU) |

`worker-trading` still uses ~1.1 cores. The 6.9 cores around it sit
idle from its perspective. Vertical scaling does not fix the
GIL — it just makes the wait shorter.

## Options to lift the GIL ceiling

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

The cheapest meaningful win is **Option 1** — the existing
`ThreadPoolExecutor` already does the right thing semantically,
it's only the GIL that prevents real parallelism. Bump
`PYTHON_VERSION` to 3.13 free-threaded in
[`backend/Dockerfile`](../../../backend/Dockerfile), run a 1-week
soak on staging with production traffic, measure the stall
distribution. If 3.13t holds together — done, no application code
needed.

If after Option 1 the orchestrator latency budget is still tight
(p95 of `ps_strategy_evaluate` > 100 ms with 5+ traders), add
**Option 2** for explicit cross-process parallelism. After both,
revisit whether the residual stalls are still strategy-eval
contention or have shifted to feed parsing — that decides whether
Option 3 is worth the 4–6 weeks.

Option 4 (Cython) is a "do it if you're already extending the
build pipeline for something else" item, not a solo project.

## Out of scope for this note

- **Live-trading semantics.** Everything above applies equally to
  shadow and live; the GIL ceiling is process-level, not
  mode-level.
- **Backend / `worker-news` / `worker-discovery` GIL.** Those
  processes have different workload mixes; backend is mostly I/O
  (a Python 3.13 bump there is even safer), worker-news loads big
  ML models (free-threaded compatibility for sentence-transformers
  needs separate testing), worker-discovery is REST-bound (already
  GIL-friendly). Each merits its own assessment.
- **Vertical scaling further.** Going to 16+ vCPU does nothing for
  `worker-trading` until the GIL is addressed; a bigger host is
  pure waste of budget on its own.

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
