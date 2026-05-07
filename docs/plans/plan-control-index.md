# Plan Control Index

Per-plan summary: purpose, prerequisites, category. Use this file to
avoid implementing plans out of order and to pick the right next
plan.

This index is for **ordering and prerequisites**, not status. Each
plan file under `docs/plans/` carries its own status via the
checkboxes inside its tasks; once every task is `- [x]`, the plan is
moved to [`completed/`](completed/) (see
[`README.md`](README.md) for the full lifecycle).

For tracing which commits belong to which plan, every commit
produced while executing a plan carries a `Plan: <NNNN>` git
trailer. Run `git log --grep='Plan: 0001'` to recover the full
commit history for a plan. The convention is described in
[`README.md`](README.md#commits-and-traceability).

## Categories

- **F** — Foundational. Changes a public contract (DB schema,
  Pydantic API surface, `BaseLLMProvider` / `BaseStrategy` /
  `BaseDataSource` interface, `LLMManager` signatures, settings
  envelope). Other plans likely block on these. Keep small and
  reviewed carefully.
- **I** — Integration. Adds a new connector that follows an existing
  contract — new LLM provider, new data source kind, new venue
  adapter. Mostly DB columns + thin glue + UI tile.
- **U** — UI feature. Frontend-only or frontend-led work that doesn't
  change backend contracts. Adds a panel, view, modal, keyboard
  shortcut, theme, etc.
- **B** — Backend feature. New worker loop, new background process,
  new strategy, new background analytic. Touches services / workers
  but not public contracts.
- **R** — Refactor / hardening. Restructures existing code without
  user-visible behaviour change. Should ship behind tests, not behind
  a flag.
- **D** — Documentation / tooling. Plans whose deliverable is a
  document, a CI check, a script, a Ralphex prompt template. No
  runtime behaviour change.

When a plan touches more than one category, pick the most blocking
one (F > I > B > U > R > D) and note the secondary in the per-plan
notes.

## Index

| ID   | Title                                                | Category | Prerequisites |
| ---- | ---------------------------------------------------- | -------- | ------------- |
| 0001 | [Add NVIDIA NIM as LLM provider](completed/0001-add-nvidia-nim-provider.md) | I        | —             |
| 0002 | [Tune Postgres for the 7 GB host](completed/0002-tune-postgres-for-7gb-host.md) | R        | —             |
| 0003 | [Profile worker-trading hotspots](completed/0003-profile-worker-trading-hotspots.md) | R        | —             |
| 0004 | [Optimize worker-trading CPU hotspots](backlog/0004-optimize-worker-trading-cpu-hotspots.md) **(BACKLOG)** | R        | 0003          |
| 0005 | [Tag-based market filter at ingest](0005-tag-based-market-filter-at-ingest.md) | B        | —             |

When adding a row: keep this table sorted by ID ascending. Don't
re-number plans — gaps in IDs are normal and expected (deleted or
abandoned plans leave their numbers reserved).

## Per-plan notes

Only notes that aren't obvious from the title. All plans must follow
[`README.md`](README.md) (file format, validation commands,
"Mark completed" final task, move to `completed/` on close).

- **Plan 0001 — Add NVIDIA NIM as LLM provider.** Pure integration
  on top of the existing OpenAI-compatible delegate pattern (same
  shape as xAI / DeepSeek / OpenRouter). Reads
  [`architecture/llm-provider-layer.md`](architecture/llm-provider-layer.md)
  and [`architecture/settings-and-secrets.md`](architecture/settings-and-secrets.md)
  as binding context. No public contracts change; the only schema
  delta is two nullable columns on `app_settings`. Safe to land in
  isolation.
- **Plan 0002 — Tune Postgres for the 7 GB host.** Right-sizes the
  Postgres `command:` block in `docker-compose.yml` for the actual
  `polyhome-1` capacity (7.6 GB RAM, 4 vCPU). Existing config was
  written for a much larger host (`shared_buffers=4GB`,
  `effective_cache_size=10GB` exceeds total RAM). Cache hit is
  already 100%, so the change is safe — its purpose is freeing
  ~3 GB RAM for the memory-pressured Python workers and giving the
  query planner a truthful `effective_cache_size`. Includes brief
  redeploy downtime and a rollback recipe. Touches infrastructure
  only — no schema, no business code. Diagnostic ground truth is
  the `Trader cycle slow` log's `ps_decision_writes` p95.
- **Plan 0003 — Profile worker-trading hotspots.** Diagnostic
  plan: captured a live py-spy flamegraph of the `worker-trading`
  process under steady-state load. Result: the four hypotheses
  in
  [`architecture/worker-trading.md`](architecture/worker-trading.md)
  (strategy eval, WS JSON, Cox-PH, copy-trade processor) were
  largely refuted; ~90 % of the apparent "100 % CPU" was idle
  ThreadPool workers, and real CPU-active work is dominated by
  three algorithmic hotspots (`copy.deepcopy` ×2, uncached
  `get_oracle_history`, nested-loop `_compute_stability`). Local
  fix written up in
  [`backlog/0004-...`](backlog/0004-optimize-worker-trading-cpu-hotspots.md);
  upstream-filter plan supersedes it as the higher-leverage
  next step. Temporarily added `CAP_SYS_PTRACE` to the
  worker-trading container; reverted on close. No schema, no
  business code.
- **Plan 0004 — Optimize worker-trading CPU hotspots
  (BACKLOG).** Local opportunistic optimisations targeting the
  three hotspots from plan 0003: half the deepcopy in
  `market_runtime._queue_opportunity_dispatch`, TTL-cache
  `reference_runtime.get_oracle_history`, vectorise
  `market_monitor._compute_stability`, plus replace stdlib
  `json` with `orjson` on the dispatch path. Parked because plan
  0005 (upstream tag filter) is expected to reduce the same
  hotspots by shrinking the input volume. Activate this plan
  only if the re-profile in plan 0005 Task 8 still shows ≥ 10 %
  self-time in any of these three frames.
- **Plan 0005 — Tag-based market filter at ingest.** Adds an
  OR-logic whitelist filter applied inside
  `scanner._is_market_tradable`/`_filter_tradable_markets`,
  configurable from `Settings → Scanner` UI. Markets without
  intersecting tags are dropped before they reach
  `market_catalog`, the scanner cache, and the opportunity
  dispatch loop. A separate aggregator hook (runs **before** the
  filter) extracts tags from the raw Polymarket stream and
  upserts them into a new `market_tags_seen` table; the UI
  populates its multi-select from the rows
  `last_seen > now() - 24h`. Empty filter ⇒ no filtering
  (backward-compatible). New table + two nullable
  `app_settings` columns; no other contract changes. Secondary
  category: U (Settings tab section). Closes the door on
  worker-trading CPU concerns once plan 0005 Task 8 confirms the
  hotspots dropped — otherwise pulls plan 0004 back into the
  active queue.

## Ordering decision tree (for agents picking the next plan)

0. Read [`README.md`](README.md) and the relevant
   [`architecture/`](architecture/) note(s) before doing anything
   else. The plan can't be evaluated without that context.
1. Filter the index to rows whose every Prerequisite is either `—`
   or already in [`completed/`](completed/). If no such row exists,
   stop — the queue is blocked and a new plan must be written first.
2. From the eligible set, prefer in this order:
   1. **F** plans (foundational — they unblock everything else).
   2. The lowest-numbered plan in any other category. Numbers
      reflect creation order, which is a rough proxy for the
      operator's intent.
3. Before starting, check that the plan's Validation Commands run
   green on `main`. If they don't, the failure is unrelated tech
   debt and the plan must record it (either fix as part of the plan
   or carve out a separate plan and link it as a prerequisite).
4. While executing: flip checkboxes as work lands; never batch
   completions. Every commit must carry a `Plan: <NNNN>` trailer
   (see
   [`README.md`](README.md#commits-and-traceability)). If new
   sub-tasks emerge, append them under the right `### Task N:`
   rather than starting a new plan unless they're genuinely out of
   scope (see the plan's own `## Out of scope` section).
5. On close: every `- [ ]` becomes `- [x]`, including the trailing
   `- [ ] Mark completed` of each task and the final
   architecture-update / `git mv ... completed/` task. Only then
   move the file.

## How to add a plan

1. Pick the next free ID by listing this directory:
   `ls docs/plans/[0-9]*.md docs/plans/completed/[0-9]*.md | tail`.
   Don't reuse IDs.
2. Create `<NNNN>-<verb>-<subject>.md` from the format in
   [`README.md`](README.md).
3. If the plan needs context that isn't yet in
   [`architecture/`](architecture/), write the architecture note
   first as a separate commit, then write the plan that links to
   it.
4. Append a row here with category and prerequisites.
5. Add a per-plan note **only** if there is something
   non-obvious — don't restate the title.

## Cross-references

- Plan format and lifecycle: [`README.md`](README.md)
- Architecture notes that plans cite:
  [`architecture/`](architecture/) (start with
  [`system-overview.md`](architecture/system-overview.md))
- Completed plans archive: [`completed/`](completed/)
