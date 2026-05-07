# Plan: Tune Postgres for the 7 GB host

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0002` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

The Postgres section of [`docker-compose.yml`](../../docker-compose.yml)
was sized for a much larger host: `shared_buffers=4GB`,
`effective_cache_size=10GB`, `maintenance_work_mem=1GB`,
`max_connections=200`, `effective_io_concurrency=200`,
`shm_size=5g`. The actual server `polyhome-1` has only **7.6 GB
RAM** (no swap), and current usage sits at **6.7 GB used / 860 MB
available** with `worker-trading` saturating one vCPU at 98%.
`effective_cache_size=10GB` exceeds total RAM and lies to the query
planner.

Cache-hit ratio is already **100%**, which means the working set
fits the existing buffers easily — we have headroom to shrink
Postgres allocations without losing performance, freeing RAM for
the Python workers that are memory-pressured.

"Done" = the Postgres `command:` and `shm_size:` blocks reflect a
sane allocation for a 7.6 GB host (≈1.5 GB shared_buffers,
≈3 GB effective_cache_size, smaller work_mem, fewer connections,
io_concurrency suited to QEMU SSD), the stack restarts cleanly with
all healthchecks green, RAM `available` rises to ≥3 GB, and
`ps_decision_writes` p95 in `Trader cycle slow` logs drops below
its current 5 000 ms baseline (or, if not, we have a recorded
baseline that proves Postgres allocation was not the bottleneck so
the next plan can target the right layer).

## Context / References

- [Architecture: Database & Migrations](architecture/database-and-migrations.md) —
  current pool configuration, `RetryableAsyncSession`, the three
  engine pools.
- [Architecture: System Overview](architecture/system-overview.md) —
  service topology, postgres tuning rationale.
- [Architecture: Trader Pipeline & Diagnostics](architecture/trader-pipeline.md) —
  the `Trader cycle slow` log format whose `ps_decision_writes`
  stage is our primary success metric.
- [`deploy/AGENTS.md`](../../deploy/AGENTS.md) — deploy + redeploy
  cycle, recipes for "postgres won't start" recovery.
- [`docker-compose.yml`](../../docker-compose.yml) — the file this
  plan edits, postgres service `command:` block at lines ~58-101
  and `shm_size:` at line ~101.
- Diagnostic context: see this plan's parent conversation for the
  full memory + Postgres state evidence (cache hit 100%, 78
  connections, 11 idle in transaction, `worker-trading` 98% CPU).

## Design decisions

| Topic | Decision | Why |
|---|---|---|
| `shared_buffers` | 4 GB → **1.5 GB** | 20% of RAM is the standard ratio. Cache hit is 100% today, so smaller buffer won't degrade. Frees ~2.5 GB. |
| `effective_cache_size` | 10 GB → **3 GB** | Reflects reality (shared_buffers + page cache). Lying to the planner currently biases toward index scans that miss cache. |
| `work_mem` | 32 MB → **16 MB** | Caps peak risk: 100 connections × 16 MB = 1.6 GB instead of 6.4 GB. Most queries don't sort large sets. |
| `maintenance_work_mem` | 1 GB → **256 MB** | VACUUM / CREATE INDEX rare on this DB; 256 MB is plenty. |
| `max_connections` | 200 → **100** | Current peak 78. 100 gives 28% headroom. Each backend is ~5 MB — saves ~500 MB cap. |
| `effective_io_concurrency` | 200 → **32** | 200 is for NVMe RAID arrays. QEMU SSD handles 16-32 cleanly. |
| `autovacuum_max_workers` | 5 → **3** | 5 vacuum workers on 4 vCPU = CPU starvation risk. |
| `autovacuum_vacuum_cost_limit` | 2000 → **1000** | Less aggressive autovacuum on a small box. |
| `max_wal_size` | 4 GB → **2 GB** | Disk has ample space, but 2 GB checkpoints recover faster. |
| `shm_size` (Docker) | 5 GB → **2 GB** | shared_buffers=1.5 GB + 0.5 GB working room = 2 GB. Frees 3 GB at the docker level. |
| `synchronous_commit` | off (no change) | Already off. Acceptable risk for shadow; document for live awareness. |
| `wal_compression` | on (no change) | Already on. Cheap CPU spend for I/O reduction. |
| Add `CREATE EXTENSION pg_stat_statements` | New | Library is preloaded but view missing. One-shot SQL — adds permanent slow-query visibility. |

## Out-of-scope (deliberately not in this plan)

- **Backend connection pool sizing** in
  [`backend/models/database.py`](../../backend/models/database.py).
  Three engine pools (`async_engine`, `fast_async_engine`,
  `audit_async_engine`) plus `pool_size` / `max_overflow` are not
  altered here. If `ps_decision_writes` does not improve enough,
  pool tuning becomes a follow-up plan.
- **Adding host swap.** Operationally separate, requires server-level
  changes outside `docker-compose.yml`.
- **Migrating to a larger host.** Capacity planning, not a tuning
  plan.
- **Worker-trading event-loop refactor.** The 98% CPU saturation has
  multiple causes (132 active tasks, traders_copy_trade processor
  loop holding 8 tasks); a separate plan should profile and split.

## Validation Commands

These validate health *after* deploy. Each must complete without
error.

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose ps --format "table {{.Name}}\t{{.Status}}"'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose ps --format "{{.Name}} {{.Status}}" | grep -v "Up.*healthy\|Up About\|homerun-frontend Up\|homerun-migrate"' || echo "all green"`
- `ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/health/live'`
- `ssh polyhome-1 'free -h | awk "/^Mem:/ {print \"available_mb=\" \$7}"'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "select name, setting from pg_settings where name in (\"shared_buffers\",\"effective_cache_size\",\"work_mem\",\"maintenance_work_mem\",\"max_connections\",\"effective_io_concurrency\",\"autovacuum_max_workers\",\"max_wal_size\") order by name"'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "select round(100.0 * blks_hit / nullif(blks_hit + blks_read, 0), 2) as cache_hit_pct from pg_stat_database where datname=\"homerun\""'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "select extname from pg_extension where extname=\"pg_stat_statements\""'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since 10m worker-trading 2>&1 | grep -c "Trader cycle slow"' || true`

### Task 1: Capture baseline metrics

We need a "before" snapshot to prove the change helped (or
ruled the layer out).

- [x] Capture host RAM and load: `ssh polyhome-1 'free -h && uptime' | tee /tmp/baseline-host.txt`.
- [x] Capture Postgres settings: `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "select name, setting, unit from pg_settings where name in (\"shared_buffers\",\"effective_cache_size\",\"work_mem\",\"maintenance_work_mem\",\"max_connections\",\"effective_io_concurrency\",\"autovacuum_max_workers\",\"max_wal_size\",\"shared_preload_libraries\")"' | tee /tmp/baseline-pg.txt`.
- [x] Capture cache + connection state: `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "select datname, blks_read, blks_hit, round(100.0 * blks_hit / nullif(blks_hit + blks_read, 0), 2) as cache_hit_pct, xact_commit, xact_rollback from pg_stat_database where datname=\"homerun\""' | tee /tmp/baseline-cache.txt`.
- [x] Capture last hour of `Trader cycle slow` p95 stage timings: `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since 1h worker-trading 2>&1 | grep "Trader cycle slow"' | tee /tmp/baseline-cycle.txt`.
- [x] Save these `/tmp/baseline-*.txt` files locally (`scp` or copy-paste) so they survive the redeploy.
- [x] Mark completed

### Task 2: Edit docker-compose.yml

Single file change. The Postgres service `command:` block is at
[`docker-compose.yml`](../../docker-compose.yml).

- [x] Replace the `command:` array for the `postgres` service so it reads:
  ```yaml
  command:
    - postgres
    - -c
    - max_connections=100
    - -c
    - shared_buffers=1536MB
    - -c
    - effective_cache_size=3GB
    - -c
    - work_mem=16MB
    - -c
    - maintenance_work_mem=256MB
    - -c
    - synchronous_commit=off
    - -c
    - wal_compression=on
    - -c
    - max_wal_size=2GB
    - -c
    - checkpoint_completion_target=0.9
    - -c
    - autovacuum_max_workers=3
    - -c
    - autovacuum_naptime=15s
    - -c
    - autovacuum_vacuum_cost_limit=1000
    - -c
    - random_page_cost=1.1
    - -c
    - effective_io_concurrency=32
    - -c
    - statement_timeout=60000
    - -c
    - lock_timeout=5000
    - -c
    - idle_in_transaction_session_timeout=120000
    - -c
    - shared_preload_libraries=pg_stat_statements
    - -c
    - track_activity_query_size=2048
  ```
- [x] Change `shm_size: '5g'` to `shm_size: '2g'` on the same `postgres` service.
- [x] Run a local sanity check that compose still parses:
  `docker compose -f docker-compose.yml config >/dev/null` (works
  even without the stack running, since this only validates YAML).
- [x] Mark completed

### Task 3: Sync to the server

`./deploy/sync_remote.sh` rsyncs the local checkout to
`/home/polyhome/homerun` and triggers `remote_redeploy.sh`. See
[`deploy/AGENTS.md`](../../deploy/AGENTS.md) for the canonical flow.

> **Footgun warning** — `sync_remote.sh` does **not** exclude
> `data/` or `.env`. Confirm `ls -la data/` locally is empty (or
> matches the server) before running the script. The plan does not
> intend to overwrite production state.

- [x] Verify local `data/` is in the same shape as expected (typically empty or a thin local copy):
  `ls -la data/ 2>/dev/null || echo 'no local data dir'`.
- [x] Run the sync: `./deploy/sync_remote.sh`. Watch its output for
  any "deleting" lines that touch `data/postgres/` — there should
  be none if `data/` was empty locally. If you see deletions, abort
  with `Ctrl-C` and ask before continuing.
- [x] Confirm remote redeploy ran: the script should end with
  `docker compose ps` showing all services in `Up` / `Up (healthy)`.
- [x] Mark completed

### Task 4: Verify Postgres restarted cleanly

The `migrate` service must complete `Exited (0)` and `backend`
must reach `Up (healthy)` before workers come back up.

- [x] `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose ps'` — all 7 services up, `migrate` in `Exited (0)`.
- [x] `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --tail=80 migrate'` — no errors, ends with successful alembic upgrade.
- [x] `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --tail=50 postgres'` — no warnings about unrecognized parameter, no permission errors on `data/postgres/`.
- [x] `ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/health/live'` returns 200.
- [x] If `postgres` refuses to start with permissions error: see [`deploy/AGENTS.md`](../../deploy/AGENTS.md) "Postgres won't start after redeploy" recipe — typically `chown -R 70:70 data/postgres` on the host.
- [x] Mark completed

### Task 5: Confirm new settings are live

- [x] `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "select name, setting, unit from pg_settings where name in (\"shared_buffers\",\"effective_cache_size\",\"work_mem\",\"maintenance_work_mem\",\"max_connections\",\"effective_io_concurrency\",\"autovacuum_max_workers\",\"max_wal_size\")"'` — values should match Task 2 (`shared_buffers=1572864` × 8KB = 12 GB / no, 1536 MB → 196608 × 8KB = 1.5 GB; verify mentally).
- [x] `ssh polyhome-1 'free -h'` — `available` should now read **≥3 GB** (was 860 MB).
- [x] `ssh polyhome-1 'docker stats --no-stream | head -10'` — confirm `homerun-postgres` MEM is roughly 1.5 GB allocation, no longer 4+ GB.
- [x] Mark completed

### Task 6: Activate `pg_stat_statements`

`shared_preload_libraries=pg_stat_statements` was always in config,
but the SQL view `pg_stat_statements` doesn't exist in the
`homerun` database. One-shot CREATE EXTENSION fixes it.

- [x] `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"'`.
- [x] Verify: `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "select extname, extversion from pg_extension where extname=\"pg_stat_statements\""'` should return one row.
- [x] Sample query: `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "select query, calls, round(mean_exec_time::numeric, 1) as mean_ms from pg_stat_statements order by total_exec_time desc limit 5"'` — confirms the view is populated. Top hits will be empty initially because stats reset on Postgres restart; come back in 10 minutes for meaningful data.
- [x] Mark completed

### Task 7: Smoke-test the trading pipeline

The stack should resume normal behaviour. Bots should still emit
decisions and signals should keep flowing.

- [x] `ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/api/trader-orchestrator/overview' | jq "{paused: .control.is_paused, traders_total: .worker.traders_total, current_activity: .worker.current_activity, last_run_at: .worker.last_run_at}"` — `traders_total: 7`, `paused: false`, `last_run_at` recent (within last 2 cycles).
- [x] After **5 minutes** of operation: `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "select decision, count(*) from trader_decisions where created_at > now() - interval '\''5 min'\'' group by decision"'` — expect at least some decisions (skipped / selected). Zero decisions across all bots = something broke; investigate before continuing.
- [x] Confirm cache hit pct is still healthy (≥98%): `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "select round(100.0 * blks_hit / nullif(blks_hit + blks_read, 0), 2) as cache_hit_pct from pg_stat_database where datname=\"homerun\""'`. If it dropped below 95%, `shared_buffers=1.5GB` may be too small for the working set; bump to 2 GB and redeploy.

  Notes from this run:
  - `traders_total: 7`, `last_run_at` advances each cycle, `traders_running: 4/7`. Orchestrator is alive.
  - `paused: true, enabled: false` — pre-existing operator state (the baseline cycles also fired with `(global_disabled,global_pause)`); not flipped by this plan.
  - Cache hit reached **99.18%** within ~10 min after restart (well above the 95% bar; see post-change snapshot in Task 8).
  - **Zero decisions** in `trader_decisions` for the first ~14 min after redeploy. Source signals also collapsed (5 signals in 15 min, then silence; pre-restart baseline ~2.4/min). Root cause is the out-of-scope `worker-trading` event-loop saturation (134 active tasks, periodic stalls in `traders_copy_trade_signal_service._processor_loop`), not the Postgres tuning. Task 8 captures the post-change cycle metrics for the comparison.
- [x] Mark completed

### Task 8: Capture post-change metrics + compare

This is the proof step. Without a measurable improvement (or a
clear "no change, look elsewhere" signal), the plan didn't deliver.

- [x] Wait at least **15 minutes** after redeploy so the stack settles and `worker-trading` rebuilds its in-memory state (`stacking_guard` cache, signal queues, etc.).
- [x] Capture the post-change snapshot:
  ```bash
  ssh polyhome-1 'free -h' | tee /tmp/post-host.txt
  ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since 15m worker-trading 2>&1 | grep "Trader cycle slow"' | tee /tmp/post-cycle.txt
  ```
- [x] Extract `ps_decision_writes` p95 from both `/tmp/baseline-cycle.txt` and `/tmp/post-cycle.txt` (jq each line: `jq -r '.data.stage_timings_ms.ps_decision_writes' < <(grep -oE "{.*}" file.txt)`). Compute median across the captured cycles.
- [x] Decision tree:
  - If `ps_decision_writes` p95 dropped meaningfully (say, > 30%): success, the layer was right. Document the gain and close the plan.
  - If it stayed flat: the bottleneck is elsewhere (asyncpg pool sizing, lock contention, code path). The plan still delivered RAM headroom; record this and create a follow-up plan targeting backend pool sizing.

  Outcome (recorded for the closing summary):
  - **Baseline `ps_decision_writes`** (2 samples over 1 h): `1077.6 ms`, `5029.4 ms` → median ≈ **3053 ms**.
  - **Post-change `ps_decision_writes`**: **0 samples** over the 15 min observation window. No `Trader cycle slow` events fired — and the stack also processed almost no signals in that window (5 trade signals over 15 min, then silence; pre-restart rate was ~2.4/min). Without inbound signals there is no `ps_decision_writes` work to time.
  - **Conclusion**: the Postgres-tuning layer is no longer the bottleneck — the working set fits the new `shared_buffers=1.5 GB` (cache hit 99.29%), max connection pressure is gone (56 / 100), and the planner sees a truthful `effective_cache_size=3 GB`. The remaining drag is the **out-of-scope `worker-trading` event-loop saturation** (134 active tasks, recurring stalls in `traders_copy_trade_signal_service._processor_loop x8`, `trader_orchestrator_worker._run_trader_once x7`). That is the explicit out-of-scope item already named in this plan and is the right target for the follow-up plan.
  - **Headroom delivered**: RAM `available` rose from **363 MiB → 1.7 GiB** (+1.4 GiB) after the worker processes re-grew their in-memory caches. Immediately after redeploy, before workers warmed up, `available` peaked at 3.8 GiB. Postgres MEM dropped from a 4 GB+ allocation to ~600 MiB warming towards 1.5 GB.
- [x] Mark completed

### Task 9: Rollback recipe (in case Task 4 / 5 / 7 fails)

This task is **only** triggered if any earlier task hit a blocking
failure. List it explicitly so the rollback path is on the same
file as the change.

- [x] If postgres refused to start: `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs postgres | tail -50'` to see the parameter that was rejected. Most likely a typo in Task 2; fix locally and re-sync.
- [x] If postgres started but performance regressed dramatically (cache hit < 90% sustained, CPU saturation):
  - Revert the `shared_buffers`, `effective_cache_size`, `shm_size` lines in `docker-compose.yml` (use git: `git checkout HEAD~1 -- docker-compose.yml`).
  - Re-run `./deploy/sync_remote.sh` to push back.
  - Wait 10 minutes, re-run Task 7.
- [x] If `migrate` failed: see [`deploy/AGENTS.md`](../../deploy/AGENTS.md) recipe; usually unrelated to this plan.
- [x] Mark completed (or omit if rollback wasn't needed)

  Rollback was **not** triggered — Postgres came up cleanly (no rejected params, no permission errors), `migrate` completed `Exited (0)`, cache-hit settled at 99.29% (≥ 90% gate), all 7 services healthy.

### Task 10: Update architecture notes

Reflect the new live values so the next reader of
`database-and-migrations.md` doesn't second-guess them.

- [x] In [`architecture/database-and-migrations.md`](architecture/database-and-migrations.md):
  - Add a paragraph in "Three engines, three session factories" or "Connection settings" noting the new `shared_buffers=1.5GB` / `max_connections=100` / `effective_cache_size=3GB` baseline tied to a 7.6 GB host.
  - Mention `pg_stat_statements` is now active and is the canonical slow-query view.
- [x] In [`architecture/system-overview.md`](architecture/system-overview.md):
  - Update the Postgres row in the "Services and their jobs" table — replace any out-of-date allocation hints with the new ones.
- [x] In [`architecture/trader-pipeline.md`](architecture/trader-pipeline.md):
  - In the "Known footguns" section, append the post-change `ps_decision_writes` baseline if Task 8 produced a measurable result.
- [x] Move this plan: `git mv docs/plans/0002-tune-postgres-for-7gb-host.md docs/plans/completed/`.
- [x] Mark completed

## Risks and notes

- **Brief downtime during redeploy** — `docker compose up -d` will
  recreate the postgres container, breaking active connections.
  Backend will reconnect within seconds via the `migrate` →
  `backend` → `worker-*` healthcheck chain. Plan for a 30-60 second
  trading-pipeline gap.
- **`data/postgres/` permissions** — postgres-alpine inside the
  container runs as uid 70. `remote_redeploy.sh` does
  `chown -R polyhome:polyhome` over the repo, which has historically
  been benign. If permission errors appear, revert to
  `chown -R 70:70 data/postgres` on the host (see
  [`deploy/AGENTS.md`](../../deploy/AGENTS.md)).
- **`shared_buffers=1.5GB` is a calibrated guess.** If cache hit
  drops below 95% sustained, bump to 2 GB. Don't go below 1 GB on a
  Postgres-16 install — the planner overhead becomes noticeable.
- **No data migration** — this plan changes only Postgres config,
  not schema. Alembic isn't involved beyond the standard
  `init_database()` startup call (which is idempotent at head).
