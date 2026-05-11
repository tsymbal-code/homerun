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
| 0004 | [Optimize worker-trading CPU hotspots](backlog/0004-optimize-worker-trading-cpu-hotspots.md) | R        | 0003, 0005, 0006 |
| 0005 | [Tag-based market filter at ingest](completed/0005-tag-based-market-filter-at-ingest.md) | B        | —             |
| 0006 | [Crypto fast-binary lane toggle](completed/0006-crypto-fast-binary-lane-toggle.md) | B        | —             |
| 0008 | [Investigate `source='traders'` routing on normal-tier](completed/0008-investigate-traders-source-routing-on-normal.md) | D        | —             |
| 0009 | [Fix `source='traders'` deferred-state gate so normal-tier traders consume copy-trade signals](completed/0009-fix-traders-source-on-normal.md) | B        | 0008          |
| 0010 | [Fix `trader_decisions` FK race for in-process `source='traders'` publishes](completed/0010-fix-traders-publish-fk-race.md) | R        | 0009          |
| 0011 | [Defensive `expires_at` on skeleton `trade_signals` + retention sweep](completed/0011-skeleton-trade-signal-ttl-and-retention.md) | R        | 0010          |
| 0012 | [Agent onboarding baseline — Cursor rules + Claude Code skills](completed/0012-agent-onboarding-baseline.md) | D        | —             |
| 0013 | [Architecture documentation gap audit](completed/0013-architecture-doc-gap-audit.md) | D        | 0012          |
| 0014 | [Documentation hygiene and quality gates for AI agents](completed/0014-doc-sync-discipline-and-quality-gates.md) | D        | 0012, 0013    |
| 0015 | [Close remaining architecture-doc gaps](completed/0015-close-remaining-doc-gaps.md) | D        | 0013, 0014    |
| 0016 | [Documentation hygiene for the shadow-execution commit fix](completed/0016-document-shadow-commit-fix.md) | R        | 0014, 0015    |
| 0017 | [Real-diff audit of `<unverified>` architecture notes](completed/0017-audit-unverified-arch-notes.md) | D        | 0013, 0014    |
| 0018 | [Fix stuck shadow positions on `traders_copy_trade`](completed/0018-fix-stuck-shadow-positions-traders-copy-trade.md) | B        | —             |
| 0019 | [Test suite hardening — coverage, markers, smoke tests, remote runner](completed/0019-test-suite-hardening.md) | D        | —             |
| 0020 | [Make Alembic migrations replayable from base on a fresh DB](completed/0020-make-alembic-migrations-replayable.md) | R        | 0019          |
| 0021 | [Auto-resume orchestrator in shadow mode on application startup](completed/0021-orchestrator-auto-resume-shadow-on-startup.md) | B        | —             |
| 0022 | [Quiet `missing_polymarket_credentials` reseeder warn spam](completed/0022-quiet-missing-polymarket-credentials-warn-spam.md) | R        | 0018          |
| 0023 | [Broaden binary-market outcome normalisation beyond literal Yes/No](completed/0023-broaden-binary-outcome-normalisation-beyond-yes-no.md) | B        | 0018          |
| 0024 | [Upsert in `sync_trader_position_inventory` to eliminate `uq_trader_position_identity` IntegrityError](completed/0024-upsert-trader-position-inventory.md) | B        | —             |
| 0025 | [Knob interaction matrix — CRITICAL tier](completed/0025-knob-interaction-matrix-critical-tier.md) | D        | —             |
| 0026 | [Walkthrough template for CRITICAL knob changes](completed/0026-runtime-tweaks-walkthrough-template-critical.md) | D        | 0025          |
| 0027 | [Agent memory rule — CRITICAL knob walkthrough enforcement](completed/0027-agent-memory-rule-critical-knob-walkthrough.md) | D        | 0025, 0026    |
| 0028 | [Default Ralphex plan rule — CRITICAL knob touch policy](completed/0028-ralphex-plan-rule-critical-knob-touch.md) | D        | 0025, 0026, 0027 |
| 0029 | [Knob interaction matrix — HIGH tier expansion + 4 dead-code findings](completed/0029-knob-matrix-high-tier-expansion.md) | D        | 0025          |
| 0031 | [Flag dead-code `risk_limits` knobs in UI with red background](completed/0031-flag-dead-code-knobs-in-ui.md) | U        | 0029          |
| 0032 | [Eliminate fast-trader dedup-spam (signal_cache deep fix)](completed/0032-eliminate-fast-trader-dedup-spam.md) | D        | —             |
| 0033 | [Verify Cox-PH shadow-fill pessimism before tuning](completed/0033-verify-cox-ph-shadow-fill-pessimism.md) | D        | —             |
| 0034 | [Per-entry audit of the CRITICAL-tier knob matrix](completed/0034-critical-knob-matrix-per-entry-audit.md) | D        | 0025, 0029    |
| 0035 | [Split entry-band cap from execution-price cap in shadow chase-up](completed/0035-split-entry-band-from-execution-price-cap.md) | R        | 0033          |
| 0036 | [Per-entry audit of the HIGH-tier knob matrix](completed/0036-high-knob-matrix-per-entry-audit.md) | D        | 0029          |
| 0037 | [Verify Plan 0035 chase-cap drop on 2026-05-11](0037-verify-plan-0035-chase-cap-drop-2026-05-11.md) | D        | 0035          |
| 0038 | [Flag three additional `TRADER_RISK_DEFAULTS` knobs as dead-code in the UI](completed/0038-flag-three-trader-risk-knobs-as-dead.md) | U        | 0031, 0036    |
| 0039 | [Migrate Polymarket integration to CLOB V2](completed/0039-migrate-to-polymarket-clob-v2.md) | B        | —             |
| 0040 | [Extract Polymarket HTTP client into a separate process](backlog/0040-extract-polymarket-http-client-into-separate-process.md) | B        | —             |

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
- **Plan 0004 — Optimize worker-trading CPU hotspots.** Local
  opportunistic optimisations targeting the three hotspots from
  plan 0003: half the deepcopy in
  `market_runtime._queue_opportunity_dispatch`, TTL-cache
  `reference_runtime.get_oracle_history`, vectorise
  `market_monitor._compute_stability`, plus replace stdlib
  `json` with `orjson` on the dispatch path. **Re-archived to
  backlog on 2026-05-07** after plan 0006's re-profile showed
  all three hotspots collapse below 1 % of CPU when the crypto
  fast-binary lane is off (the actual operating mode for the
  current operator workload). Resurrect this plan if the
  operator turns the crypto lane back on and a fresh `py-spy`
  profile shows the post-filter distribution returning.
- **Plan 0005 — Tag-based market filter at ingest.** Adds an
  OR-logic whitelist filter applied inside
  `scanner._apply_market_tag_whitelist` (called from every
  `_filter_tradable_markets` site), configurable from
  `Settings → Scanner` UI. Markets whose `(market.tags ∪
  event.tags)` doesn't intersect the whitelist are dropped
  before they reach `market_catalog`, the scanner cache, or the
  opportunity dispatch loop. A separate aggregator hook (runs
  **before** the filter) extracts tags from the raw Polymarket
  stream and upserts them into a new `market_tags_seen` table;
  the UI populates its multi-select from the rows
  `last_seen > now() - 24h`. Empty filter ⇒ no filtering
  (backward-compatible). New table + two nullable
  `app_settings` columns; no other contract changes. Secondary
  category: U (Settings tab section). Re-profile after this
  plan's Task 8 confirmed plan 0004's hotspots are still
  dominant, so plan 0004 was promoted back into the active
  queue on 2026-05-07. Also revealed a parallel ingest lane
  (crypto fast-binary) that the tag filter cannot reach,
  spawning plan 0006.
- **Plan 0006 — Crypto fast-binary lane toggle.** Plan 0005's
  tag filter only gates the Polymarket general scanner;
  `market_runtime._refresh_crypto_markets` runs a parallel
  pipeline via `crypto_service.get_live_markets()` that fetches
  crypto-binary markets directly from Gamma and never consults
  `market_catalog`. `CryptoMarket` has no `tags` field, so
  applying the whitelist there is semantically wrong; this plan
  added an operator-managed on/off toggle for the lane in
  `Settings → Scanner`. Most plumbing already existed
  (`worker_control(name="crypto")` row + generic
  `POST /api/workers/crypto/{pause|start}` API); the plan
  plugged the two existing gaps — startup refresh and
  `_drain_reactive_updates` ignored the control — plus surfaced
  the toggle in the Scanner tab. The toggle reflects the
  collapsed semantics of "lane active" =
  `is_enabled and not is_paused`, matching the existing
  `pause/start` API which writes `is_paused`. Default: lane on
  (backward-compatible). Re-profile on close confirmed the
  expected drop: `get_oracle_history` + `_oracle_move_from_history`
  collapsed from ~42 % to < 2 % CPU with the lane off, and
  `copy.deepcopy` collapsed alongside, so plan 0004 was
  re-archived. Secondary category: U.
- **Plan 0008 — Investigate `source='traders'` routing on
  normal-tier.** Investigation-only (D — Documentation/
  diagnostic). Symptom observed 2026-05-07: with the Polygon
  RPC fixed and `traders_copy_trade` signal stream restored,
  `Sandbox - Traders Copy Trade` on `latency_class=normal`
  receives **zero** decisions, while other normal-tier
  traders consume signals normally in the same window. The
  architectural pieces (`session_engine.py:195-214` traders
  policy, `_is_fast_tier_trader` filter, source-config
  routing) all *appear* to support normal-tier consumption,
  but in practice the orchestrator never starts a cycle for
  the trader. Plan 0008 traces the publish + consume paths
  end-to-end with file:line precision, identifies the gate
  by elimination across six hypotheses, and writes
  [`architecture/copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md)
  so future agents do not have to repeat the investigation.
  **Conclusion: bug — latent regression in
  `signal_bus._strategy_runtime_metadata`** (the `else` branch
  forces `traders` source into `execution_activation =
  "ws_post_arm_tick"`, which deletes the signal's
  `runtime_sequence` and hides it from both fast and normal-tier
  consumers). **No code changes in this plan**; the fix lands
  in plan 0009. Numbering note: ID 0007 was reserved earlier in
  the session for a potential `simulation_accounts` accounting
  bridge plan and not yet written; gaps in IDs are normal per the
  README rules.
- **Plan 0009 — Fix `source='traders'` deferred-state gate.**
  Backend feature (B — minimal but production-affecting). Direct
  follow-up to plan 0008. Replaced the if/elif/else chain in
  [`backend/services/signal_bus.py`](../../backend/services/signal_bus.py)
  `_strategy_runtime_metadata` with an explicit allow-list
  (`crypto/scanner/traders` → activation values; unknown source
  keys default to `"immediate"` plus a warn-once log). New unit
  tests pin both the metadata function and the publish-side
  invariant (snapshot has non-NULL `runtime_sequence`); the
  existing `test_intent_runtime_ws_freshness.py` was updated to
  reflect the new `traders → immediate` policy (was the
  strongest second-line regression check). Closed
  2026-05-08 ~05:00 UTC. **Verified.** Post-deploy
  `without_seq = 0` for both `traders_copy_trade` and
  `traders_confluence`; orchestrator now picks up Copy Trade
  signals (151 consumption attempts in the first 15-minute
  window vs. 0 before the fix). Architecture notes
  ([`copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md)
  and [`trader-pipeline.md`](architecture/trader-pipeline.md))
  rewritten for the post-fix state; the `latency_class=fast`
  workaround is retired in
  [`runtime-tweaks.md`](../operational/runtime-tweaks.md).
  **Note:** the gate fix exposed a pre-existing
  publish/projection FK race that was previously masked
  (`trader_decisions.signal_id → trade_signals.id` violations
  for in-process traders publishes); that is filed as plan 0010.
- **Plan 0010 — Fix `trader_decisions` FK race for in-process
  `source='traders'` publishes.** Refactor / hardening (R).
  Direct follow-up to plan 0009. The publish path
  ([`backend/services/intent_runtime.py`](../../backend/services/intent_runtime.py)
  `publish_opportunities`) mutated the in-memory
  `self._signals_by_id` and pinged consumers via
  `publish_signal_batch` BEFORE the projection loop committed
  the corresponding `trade_signals` row, and additionally
  minted fresh uuids for `(source, dedupe_key)` pairs the
  in-memory cache had forgotten across restarts (the projection
  then upserted the existing row by `(source, dedupe_key)`,
  silently demoting the new id). The orchestrator's
  `list_unconsumed_signals` read the in-memory map directly,
  so `trader_orchestrator_worker` picked up `signal_id`s whose
  `trade_signals` rows did not yet exist (or never would) and
  the `trader_decisions.signal_id_fkey` insert blew up — 100 %
  of `traders_copy_trade` decisions failed for both reasons.
  Closed 2026-05-08 ~06:20 UTC. **Verified.** Fix is two-part:
  publish-side prefetch of canonical `(source, dedupe_key) → id`
  for known dedupe keys, plus a synchronous skeleton-INSERT
  pass for genuinely new dedupe keys (`pg_insert(TradeSignal)
  ... on_conflict_do_nothing(['source','dedupe_key'])` in a
  separate committed session, before `publish_opportunities`
  returns). Post-deploy: 0 FK violations, 95 `trader_decisions`
  for `traders_copy_trade` in 7 minutes (76 skipped + 19
  blocked, the strategy's actual gate-filter output once the
  race no longer masks it as `failed`), 99 `trader_signal_consumption`
  rows all linked to a real `decision_id`. Architecture notes
  ([`copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md),
  [`trader-pipeline.md`](architecture/trader-pipeline.md)) and
  the operational journal
  ([`runtime-tweaks.md`](../operational/runtime-tweaks.md))
  updated to reflect the post-fix invariant.
- **Plan 0011 — Defensive `expires_at` on skeleton
  `trade_signals` + retention sweep.** Refactor / hardening
  (R). Direct follow-up to plan 0010, **completed**
  2026-05-08. Plan 0010's skeleton-INSERT pass committed
  `(source, dedupe_key)` rows with `expires_at = NULL`; if
  `publish_opportunities` died between the skeleton commit
  and the projection-loop UPSERT, the row sat in
  `trade_signals` forever (`payload_json IS NULL`,
  `runtime_sequence IS NULL`, invisible to the existing
  pruner that keys on `expires_at < now()`).
  Plan 0011 adds (a) a defensive
  `expires_at = now + INTENT_RUNTIME_SKELETON_TTL_SECONDS`
  (default 300 s) on every skeleton row, overwritten by the
  projection loop's later UPSERT, and (b) a
  worker-discovery sweep
  (`services.skeleton_signal_retention.prune_stuck_skeletons`)
  that DELETEs orphans older than 1 h every 15 min.
  Post-deploy on `polyhome-1` (20-min soak): 0 FK violations,
  331/331 new traders skeletons carry the defensive TTL,
  Tier 1 invariant `without_seq=0` holds for both
  `traders_copy_trade.expired` (125) and `.skipped` (139),
  Tier 2 invariant `stuck_skeletons=0` holds, and a manually
  injected orphan was reaped by the live loop within 14 min.
  Operational guidance ([`copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md))
  now documents a two-tier monitoring scheme; the journal
  ([`runtime-tweaks.md`](../operational/runtime-tweaks.md))
  carries the deploy snapshot and rollback recipe.

- **Plan 0012 — Agent onboarding baseline.** Documentation /
  tooling (D). Installs the missing scaffolding that makes a
  fresh AI session in either Cursor or Claude Code arrive
  pre-loaded with the right context. Adds per-area auto-attach
  rules under `.cursor/rules/` (backend, frontend, migrations,
  plans, AI/LLM, strategies); a `.claude/settings.json` with a
  curated permissions allowlist plus one reminder hook; a single
  `/sync-docs` slash command that audits recent commits against
  the architecture notes; three subagents (plan-validator,
  arch-note-writer, commit-trailer-checker). Closes with a short
  "Where AI agents start" section in the root `agents.md` that
  serves as the canonical index. No runtime code changes.
- **Plan 0013 — Architecture documentation gap audit.**
  Documentation / tooling (D). Direct follow-up to plan 0012:
  uses the `/sync-docs` command and the `Last verified` marker
  introduced there. Fills five missing architecture notes
  (`worker-news.md`, `worker-discovery.md`,
  `websocket-and-events.md`, `execution-and-fills.md`,
  `strategy-reverse-engineer.md`), backfills `Last verified`
  on every existing note, and rewires `system-overview.md` /
  `CLAUDE.md` to route to the new notes. No runtime code
  changes.

- **Plan 0014 — Documentation hygiene and quality gates for AI
  agents.** Documentation / tooling (D). Direct follow-up to
  plans 0012 and 0013. Closes the discipline gap that lets agents
  change a strategy or a documented layer without updating the
  paired doc. Adds: strategy-doc sync obligation in
  `.cursor/rules/strategies.mdc` and `agents.md`, arch-note sync
  reminder in every per-area Cursor rule, `/sync-docs` extension
  to cover `docs/strategies/`, a new `/pre-commit-check` slash
  command, a `PreToolUse` Bash hook that nudges (does not block)
  on `git commit` without a `Plan:` trailer, and a single
  "Documentation hygiene" section in `agents.md` that consolidates
  all obligations. No runtime code changes.

- **Plan 0016 — Documentation hygiene for the shadow-execution
  commit fix.** Refactor / hardening (R, secondary D). Post-hoc
  audit-trail for commit `936f96a4`
  (`fix(session_engine): ensure execution session persistence
  with explicit commit in shadow mode`) which landed on `main`
  as an emergency hotfix without a `Plan:` trailer. Records the
  cross-reference in `plan-control-index`, corrects two
  architecture notes that the fix made wrong (the shadow ledger
  is `trader_orders`/`trader_positions`/`execution_sessions`,
  not the legacy `simulation_*` tables; and a second canonical
  cause for "selected → 0 orders" exists alongside the Cox-PH
  one), and lists three out-of-scope drift items the
  `/sync-docs 5` audit surfaced for future plans. No runtime
  code changes — the patch is already shipped; this plan moves
  documentation only.
- **Plan 0020 — Make Alembic migrations replayable from base.**
  Refactor / hardening (R). Direct follow-up to Plan 0019, which
  surfaced that `alembic upgrade base→head` against a fresh DB
  fails with `DuplicateColumnError` because the baseline migration
  `Base.metadata.create_all`s every current ORM column at revision
  1 and ~13 later migrations then collide. Adds `safe_add_column`
  / `safe_create_table` / `safe_create_index` helpers to
  `alembic_helpers.py`, retrofits the unguarded migrations to use
  them, and extends the round-trip test with a full base→head
  replay case. Once green, removes the "Known footgun" entry from
  `architecture/testing.md`. Production is unaffected — it stays
  at head with the schema applied incrementally.
- **Plan 0019 — Test suite hardening.** Documentation / tooling
  (D, secondary R). Closes four structural gaps in the backend
  test suite that let real regressions through despite ~1 990
  passing tests: no coverage report, no `lifespan` startup smoke,
  no Alembic round-trip, no marker categorisation, and no
  operator-friendly recipe to run pytest on the remote stack
  (the runtime image excludes `tests/` via `.dockerignore`).
  Adds `pytest-cov`/`hypothesis`/`pytest-xdist`, registers
  `unit`/`db`/`slow` markers, ships two smoke tests
  (`test_main_lifespan_smoke.py`, `test_alembic_roundtrip.py`),
  and a `scripts/run_tests_remote.sh` helper that bind-mounts
  `backend/tests/` into the live backend image and runs pytest
  against the live Postgres (the `homerun` DB user is superuser
  + CREATEDB-able, so `build_postgres_session_factory` allocates
  throwaway databases without disturbing prod). Explicitly **not**
  in scope: mass marker sweep across the existing 195 files,
  CI job split (unit-fast / db-slow), Hypothesis property tests
  for FIFO/Kelly/Cox-PH, `respx` cassettes, mutation testing.
  Each is a follow-up.
- **Plan 0039 — Migrate Polymarket integration to CLOB V2.**
  Backend feature (B). **Completed 2026-05-10.** Polymarket cut
  over from CLOB V1 to V2 on 2026-04-28 — V1 exchange contracts
  and the V1 `OrderFilled` topic emit zero events on Polygon
  since then. Our hard-coded V1 constants in
  `backend/services/wallet_ws_monitor.py` silently broke
  wallet-trade detection, which in turn broke every
  `traders_copy_trade` consumer (including
  `Focused - 0x10c95474a8`). The plan replaced the V1 addresses
  + topic with V2 in the monitor, rewrote
  `_parse_order_filled_log` for the V2 ABI shape (4 topics,
  7 data words, with `side`/`tokenId`/`builder`/`metadata`),
  rewrote `_determine_trade_side_and_details` against the new
  `side` byte, added V2 operators to
  `ctf_execution.ensure_exchange_approval`, and verified that
  submit-side `ClobClient`/`OrderBuilder` already signs EIP-712
  with V2 `verifyingContract` (the SDK's `__resolve_version()`
  returns `2` against the live CLOB API since the cutover, so no
  submit-side change was required). Clean cut — V1 fallback
  branches deleted, not retained, per the project's
  no-back-compat rule. Live verification: 236 events / 10 min
  post-deploy vs 0 / 24 h pre-deploy. No risk-knob changes;
  CRITICAL knob walkthrough policy did not apply.

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
  [`system-overview.md`](architecture/system-overview.md)). Notable
  recent additions:
  [`ai-and-llm.md`](architecture/ai-and-llm.md) (LLM-vs-classical-ML
  map, news-edge "winning market" pipeline),
  [`copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md)
  (the `source='traders'` family),
  [`worker-news.md`](architecture/worker-news.md) /
  [`worker-discovery.md`](architecture/worker-discovery.md) (the
  two non-trading planes),
  [`websocket-and-events.md`](architecture/websocket-and-events.md)
  (four messaging layers in one map),
  [`execution-and-fills.md`](architecture/execution-and-fills.md)
  (Cox-PH shadow + live submit),
  [`strategy-reverse-engineer.md`](architecture/strategy-reverse-engineer.md)
  (wallet-mimicry pipeline), and the four notes added by plan
  0015:
  [`wallet-intelligence.md`](architecture/wallet-intelligence.md)
  (insider + anomaly + intelligence stack),
  [`execution-defense.md`](architecture/execution-defense.md)
  (9-module submission-side defence layer),
  [`crypto-fast-binary-lane.md`](architecture/crypto-fast-binary-lane.md)
  (parallel crypto pipeline + operator toggle),
  [`market-quality-and-prioritization.md`](architecture/market-quality-and-prioritization.md)
  (pre-scanner gates).
  Every note ends with `Last verified: YYYY-MM-DD` (introduced by
  plan 0013); `/sync-docs` consumes it.
- Agent scaffolding (Cursor rules, Claude Code commands, subagents):
  [`agents.md`](../../agents.md) § "Where AI agents start" — single
  index. The `/sync-docs` command in
  [`.claude/commands/sync-docs.md`](../../.claude/commands/sync-docs.md)
  audits this directory's architecture notes against the code.
- Completed plans archive: [`completed/`](completed/)

## Follow-up plan proposals (drafts; not yet IDed)

- **Promote 3-4 HIGH knobs to CRITICAL.** Plan 0036 (HIGH-tier
  audit, 2026-05-10) surfaced four state-flipping knobs that the
  current tier-walkthrough policy does not cover:
  `pending_live_exit_guard.*` (race-condition fix; dual gate),
  `live_provider_health.*` (halts ALL live entries on rolling-window
  errors), `TradingProxySettings.require_vpn` (binary VPN gate that
  blocks all live trades when VPN unreachable), and arguably
  `live_risk_clamps_explicit` (flips legacy-vs-explicit override
  semantics for the CRITICAL `live_risk_clamps.*` umbrella). Tier
  promotion changes the walkthrough policy footprint and was
  out-of-scope for plan 0036 — this is a candidate for a successor
  plan to evaluate the four candidates and either move them or
  document why HIGH stays correct.
- **Wire or move scanner_max_opportunities_*.** Plan 0036
  confirmed `scanner_max_opportunities_total` and
  `scanner_max_opportunities_per_strategy` have zero runtime
  consumers (currently in HIGH/Group D with `confirmed dead`
  marker). Either wire them into `scanner.py` top-N cut OR move
  to the `Dead code in config.py` subsection of the matrix —
  pick one. Operator-facing settings UI exposes both knobs today.
