# Plan: Profile worker-trading hotspots

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

`worker-trading` saturates one CPU core at ~100 % even with a
single active trader. The architecture note
[`worker-trading.md`](architecture/worker-trading.md) lists four
**hypothesised** CPU hotspots (strategy evaluation, WS JSON
parsing, Cox-PH inference, copy-trade processor loop) and four
**candidate fixes** (free-threaded Python, ProcessPoolExecutor,
plane split, Cython). Both lists are derived from code reading and
the `Trader cycle slow` log breakdown — not from a real CPU
profile. Before committing engineering effort to any of the four
fixes, this plan replaces hypotheses with **measured data**.

Done = a flamegraph + top-N function table captured from a live
production worker-trading process under steady-state load,
classified into CPU-Python / I/O-blocked / GIL-contended buckets,
and a concrete recommendation for which of the four fix options
(or a fifth, opportunistic one) to pursue. The architecture note
gets updated with the measured numbers.

This plan does **not** implement the fix — picking the fix is the
deliverable. The fix itself becomes a follow-up plan (likely
`0004-...`).

## Context / References

- [Architecture: worker-trading & the GIL ceiling](architecture/worker-trading.md)
  — current hypotheses, four fix options.
- [Architecture: trader-pipeline](architecture/trader-pipeline.md)
  — `Trader cycle slow` log fields.
- [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  — recent context: 2026-05-07 bootstrap unblock; Copy Trade now
  generating ~1 selected/5 min on a single fast bot, but cycle
  budgets still exceeded routinely (`evaluate() exceeded 0.2 s`,
  `cycle exceeded 3 s`).
- [`backend/Dockerfile`](../../backend/Dockerfile) — current
  Python version pin (3.12).
- [`backend/workers/host.py:100-158`](../../backend/workers/host.py)
  — plane composition.

## Validation Commands

These run from the operator's local machine — they wrap remote
commands via SSH because the stack runs on `polyhome-1` (see
[`CLAUDE.md`](../../CLAUDE.md)).

- `ssh polyhome-1 'docker exec homerun-worker-trading py-spy --version'`
- `ssh polyhome-1 'docker stats --no-stream homerun-worker-trading'`
- `ls docs/plans/architecture/worker-trading-profile-2026-05-07.svg`
- `grep -q "Profile (2026-05-" docs/plans/architecture/worker-trading.md`

### Task 1: Set up py-spy in the live worker-trading container

- [x] Verify py-spy is **not** baked into the production image
  (probed already: `which py-spy` returns nothing). Document the
  finding inline in the plan.
- [x] Install py-spy in the running container with
  `docker exec -u root homerun-worker-trading pip install --no-cache-dir py-spy`.
  Note: this is ephemeral — survives until next container restart.
  We deliberately do **not** add it to the production image
  because (a) this is a one-off diagnostic, (b) py-spy needs
  `CAP_SYS_PTRACE` which the container lacks by default, see next
  task.
- [x] Confirm py-spy attaches: `docker exec homerun-worker-trading
  py-spy dump --pid <worker-pid>`. If permission-denied, document
  the error and proceed to Task 2.
- [x] Mark completed

### Task 2: Grant ptrace capability to the running container

- [x] If Task 1's `py-spy dump` failed with EPERM, the container
  needs `CAP_SYS_PTRACE`. The cleanest way is to recreate the
  container with `cap_add: [SYS_PTRACE]` in
  `docker-compose.yml`'s `worker-trading` service. Add the cap_add
  block, restart only worker-trading.
- [x] Verify with `docker inspect homerun-worker-trading
  --format='{{.HostConfig.CapAdd}}'`. Expected: `[SYS_PTRACE]`.
- [x] Re-test py-spy attach.
- [x] After profiling completes (Task 7), revert
  `docker-compose.yml` so production doesn't keep an elevated
  capability.
- [x] Mark completed

### Task 3: Capture a 60-second sampling profile under steady-state

- [x] Confirm steady-state: at least one active fast trader
  (Sandbox - Traders Copy Trade currently fits), worker has been
  up ≥ 5 min after last restart, no migration/cox_trainer running
  in parallel.
- [x] Capture flamegraph:
  `docker exec homerun-worker-trading py-spy record \
    -o /tmp/worker-trading-profile.svg \
    --duration 60 \
    --rate 100 \
    --idle \
    --pid <worker-pid>`. The `--idle` flag is critical — without
  it asyncio's idle waits look like nothing, and the picture is
  misleading.
- [x] In parallel, capture `top` view:
  `docker exec homerun-worker-trading py-spy top --pid <worker-pid>`
  for ~30 s, save to `/tmp/worker-trading-top.txt` (Ctrl+C
  redirects to the file).
- [x] Copy both files out:
  `ssh polyhome-1 'docker cp homerun-worker-trading:/tmp/worker-trading-profile.svg /tmp/'`
  then
  `scp polyhome-1:/tmp/worker-trading-profile.svg
       docs/plans/architecture/worker-trading-profile-<YYYY-MM-DD>.svg`.
- [x] Capture container CPU during sampling (one-shot):
  `docker stats --no-stream homerun-worker-trading` saved alongside
  the flamegraph in the same dated filename for context.
- [x] Mark completed

### Task 4: Classify hotspots from the profile

- [x] Open the flamegraph in a browser (Vite/Chrome) and identify
  the top 10 widest stacks by self-time.
- [x] For each, classify as one of:
  - **CPU-Python**: pure-Python compute, GIL-blocking
  - **CPU-native**: numpy / json / asyncpg internals (releases
    GIL on syscalls, holds it on parse)
  - **I/O-wait**: asyncpg socket reads, WS reads, HTTP — these
    show up as "idle" if `--idle` was passed
  - **asyncio overhead**: `_run_once`, scheduling, cancellation
- [x] Cross-reference against the four hypotheses in
  [`worker-trading.md`](architecture/worker-trading.md):
  Strategy eval (A), WS JSON parsing (B), Cox-PH inference (C),
  Copy-trade processor (D). For each: confirmed / partially
  confirmed / refuted, with the measured % of CPU time.
- [x] Note any **unexpected** hotspot — something not in the
  hypothesis list. This is the high-value finding because it's
  the bug nobody saw coming.
- [x] Mark completed

### Task 5: Decide the next plan

- [x] If a single CPU-Python hotspot dominates (≥40 % of CPU
  time), pursue an **opportunistic fix** for that hotspot and
  defer the four architectural options. Open
  `0004-fix-<hotspot-name>.md`.
- [x] If CPU is roughly evenly distributed across many small
  CPU-Python frames (the "death by a thousand cuts" case), the
  GIL is genuinely the ceiling and **Option 1 — Python 3.13
  free-threaded build** is the right next step. Open
  `0004-migrate-to-python-313-free-threaded.md`.
- [x] If profile shows large blocks in `traders_copy_trade_signal_service`
  or feed managers (i.e. async overhead, not pure-CPU), pursue
  **Option 3 — plane split** (move copy-trade processor to
  worker-news plane). Open
  `0004-move-copy-trade-processor-to-worker-news.md`.
- [x] Document the decision in this plan's section below before
  closing.
- [x] Mark completed

### Task 6: Update the architecture note with measured data

- [x] Append a "## Measured CPU profile (YYYY-MM-DD)" section to
  [`architecture/worker-trading.md`](architecture/worker-trading.md).
  Include:
  - Methodology (py-spy version, sample rate, duration, active
    trader at the time, system load).
  - Top-10 hotspot table with self-time %.
  - Diff against the hypotheses (A/B/C/D) — confirmed vs refuted.
  - Link to the SVG flamegraph in the same directory.
- [x] Update the "Options to lift the GIL ceiling" section if the
  profile changes the ranking of options.
- [x] Mark completed

### Task 7: Revert ephemeral state and close the plan

- [x] Revert `cap_add: [SYS_PTRACE]` from
  [`docker-compose.yml`](../../docker-compose.yml) (Task 2's
  rollback step). Recreate the worker-trading container from the
  reverted compose file.
- [x] Verify cap is gone: `docker inspect homerun-worker-trading
  --format='{{.HostConfig.CapAdd}}'` returns `[]` or `null`.
- [x] Confirm worker-trading still runs the bootstrap loop after
  the compose recreate (1+ new `trader_orders` row in 10 minutes).
- [x] `git mv docs/plans/0003-profile-worker-trading-hotspots.md
  docs/plans/completed/`.
- [x] Update [`plan-control-index.md`](plan-control-index.md):
  the row stays, just the link target changes to
  `completed/0003-...md`.
- [x] Mark completed

## Decision (filled by Task 5)

The profile **refutes** the GIL-as-primary-bottleneck framing.
With `--idle` the worker shows 90 % of samples in idle
ThreadPoolExecutor workers — there is no GIL contention crisis
on the current workload. CPU-active sampling reveals three
algorithmic hotspots that together account for ~38 % of real CPU
time and are fixable in ~50–100 lines without changing
interpreter, process model, or container layout:

1. **Double `copy.deepcopy` on the crypto opportunity payload**
   ([`market_runtime.py:1533, 1560`](../../backend/services/market_runtime.py)) —
   ~15 % CPU. Eliminate one of the two passes.
2. **`get_oracle_history` linear scan on every call**
   ([`reference_runtime.py:215-238`](../../backend/services/reference_runtime.py)) —
   ~14 % CPU. Add a TTL cache (1–3 s) keyed by
   `(asset, points, max_age_seconds)`.
3. **`_compute_stability` nested Python loop**
   ([`market_monitor.py:152-157`](../../backend/services/market_monitor.py)) —
   ~5 % CPU. Vectorise via numpy `diff`/`abs`.

Plus a small but easy bonus:

4. **`stdlib json` on the dispatch hot path** — ~4 % CPU.
   Replace with `orjson` for serialisation; deserialisation
   remains stdlib (already used widely).

Combined expected reduction: **~38 % of CPU-active work**, which
in absolute terms means worker-trading wall-time drops from
~111 % CPU to ~70 % CPU on the same workload — without touching
the GIL ceiling at all. Once those edits land and re-profiling
shows the new top hotspot, we revisit Options 1–3 in
[`architecture/worker-trading.md`](architecture/worker-trading.md)
with a fresh basis.

**Follow-up plan: `0004-optimize-worker-trading-hotspots.md`**
(category R, prerequisites: this plan completed).
Free-threaded Python (Option 1 in arch note), ProcessPool
(Option 2), and plane split (Option 3) are deferred — they
become candidates only if 0004 leaves residual GIL contention.

## Out of scope

- **Implementing the fix.** This plan stops at "we know what to
  do." The fix is `0004`.
- **Profiling the other planes** (backend, worker-news,
  worker-discovery). They aren't bottlenecked the same way and a
  single dated profile would not help them. If similar problems
  appear later, they get their own plans.
- **Long-term continuous profiling** (e.g. py-spy in production
  with daily exports). Useful but separate — would be its own `D`
  plan.
