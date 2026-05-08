# Plan: Close remaining architecture-doc gaps

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0015` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Plans 0012–0014 installed the agent-onboarding scaffolding,
filled five new architecture notes, and wired the documentation
hygiene discipline. A coverage audit during the closing of plan
0014 surfaced **roughly sixteen significant code modules** that
have **no architecture-note coverage at all** — together they
represent ~9 000 lines of code with no canonical reference for
agents or operators. This plan closes the remaining gaps in one
coordinated pass.

The gaps cluster into four natural groups; each becomes one new
architecture note. Plus one focused extension to
`execution-and-fills.md` for observability, plus the cross-link
debt the audit surfaced.

The four groups:

1. **Wallet intelligence and anomaly detection** —
   `insider_detector.py` (699 lines), `anomaly_detector.py`
   (687), `wallet_intelligence.py` (2 509). Together these are
   the wallet-scoring and risk-detection stack that drives the
   Discovery and Wallets UIs.
2. **Execution defence layer** — `execution_safety.py` (308),
   `execution_tiers.py` (370), `price_chaser.py` (440),
   `token_circuit_breaker.py` (385), `live_pressure.py` (179),
   `position_monitor.py` (295), `stuck_position_monitor.py`
   (638), `market_tradability.py` (172),
   `live_market_detector.py` (145). The protection layer that
   sits between strategy decisions and CLOB submission.
3. **Crypto fast-binary lane** — `crypto_service.py` (1 176).
   The dedicated high-speed path for BTC/ETH binary markets;
   currently mentioned only in SVG worker-trading profiles and
   passing references in `market-filter.md`.
4. **Market quality and prioritisation** — `market_regime.py`
   (91), `quality_filter.py` (418), `market_monitor.py` (960),
   `market_prioritizer.py` (537), `sport_classifier.py` (149),
   `category_buffers.py` (427), `depth_analyzer.py` (381). The
   pre-scanner gates and tier-assignment engine that decide
   which markets even reach strategy detection.

"Done" looks like: four new arch-notes exist, every module above
appears in a `Last verified: 2026-05-08` note, the existing five
notes from plan 0013 cross-link to the new ones, and `/sync-docs`
reports zero uncovered modules in the next run.

No runtime code changes.

## Context / References

- Coverage audit summary (this plan's evidence base): see
  per-task notes below; raw module sizes verified by
  `wc -l backend/services/<file>.py`
- [`docs/plans/README.md`](README.md) — Ralphex format
- [`docs/plans/architecture/system-overview.md`](architecture/system-overview.md) — "Where to look next" table to extend
- [`CLAUDE.md`](../../CLAUDE.md) — "Where to find more" table to extend
- Plans 0012–0014 — agent scaffolding, five new arch-notes,
  documentation hygiene. This plan continues their pattern.

## Validation Commands

- `for f in wallet-intelligence execution-defense crypto-fast-binary-lane market-quality-and-prioritization; do test -f docs/plans/architecture/$f.md || echo "MISSING $f"; done | grep -c MISSING; test $? -eq 1`
- `for f in docs/plans/architecture/*.md; do grep -q 'Last verified:' "$f" || echo "MISSING: $f"; done | grep -c MISSING; test $? -eq 1`
- `grep -q 'wallet-intelligence.md' docs/plans/architecture/system-overview.md`
- `grep -q 'execution-defense.md' docs/plans/architecture/system-overview.md`
- `grep -q 'crypto-fast-binary-lane.md' docs/plans/architecture/system-overview.md`
- `grep -q 'market-quality-and-prioritization.md' docs/plans/architecture/system-overview.md`
- `grep -q 'wallet-intelligence' CLAUDE.md`
- `grep -q 'crypto_service' docs/plans/architecture/crypto-fast-binary-lane.md`
- `grep -q 'execution_safety\|execution_tiers' docs/plans/architecture/execution-defense.md`

### Task 1: `wallet-intelligence.md` (new)

The largest single hole: 3 800 lines of wallet-scoring and
risk-detection code with no canonical reference. Operators
currently read the Wallets UI without knowing what `insider_score`
means, where it comes from, or how to tune it.

- [x] Author `docs/plans/architecture/wallet-intelligence.md`
      covering: Purpose / Key files (insider_detector.py,
      anomaly_detector.py, wallet_intelligence.py, plus the
      `WalletActivityRollup` and `DetectedAnomaly` tables) /
      Contracts (the metric formulas with their weights, the
      `AnomalyType` and `Severity` enums, the `insider_score`
      composition) / Dependencies / Extension points /
      Footguns. Match the depth of `ai-and-llm.md`.
- [x] Add `Last verified: 2026-05-08` line.
- [x] Cross-link from
      [`worker-discovery.md`](architecture/worker-discovery.md)
      § "Wallet discovery" — replace any inline metric summary
      with a pointer.
- [x] Mark completed

### Task 2: `execution-defense.md` (new)

Nine modules form the layered defence between strategy decision
and venue submission. They are scattered across `services/` with
no map; an operator debugging "why did this trade get blocked?"
has nine candidate gates to check.

- [x] Author `docs/plans/architecture/execution-defense.md`
      covering the layers in submission order: market-tradability
      semaphore (`market_tradability.py`,
      `live_market_detector.py`), execution-safety floors
      (`execution_safety.py`), tier routing
      (`execution_tiers.py`), price-chaser retry
      (`price_chaser.py`), token-level circuit breaker
      (`token_circuit_breaker.py`), live-pressure feedback
      (`live_pressure.py`), and the position-monitoring layer
      (`position_monitor.py`, `stuck_position_monitor.py`).
      Document each gate's contract: input, decision,
      `SafetyAssessment`-style return, persistence, manual-clear
      semantics.
- [x] Add `Last verified: 2026-05-08` line.
- [x] Cross-link from
      [`execution-and-fills.md`](architecture/execution-and-fills.md)
      § "Live path" with a one-line pointer; do not duplicate
      content.
- [x] Cross-link from
      [`trader-pipeline.md`](architecture/trader-pipeline.md)
      § Stage 6 ("Pre-flight gates").
- [x] Mark completed

### Task 3: `crypto-fast-binary-lane.md` (new)

Plan 0006 introduced an operator-managed toggle for this lane
but the lane itself has no architecture documentation. The lane
runs a parallel discovery pipeline, has its own market-fetch
logic, its own risk profile, and was the only thing keeping
plan 0004's CPU hotspots alive. Operators turning it on/off
need to know what they're switching.

- [x] Author `docs/plans/architecture/crypto-fast-binary-lane.md`
      covering: Purpose (why crypto markets get a separate path)
      / Key files (`crypto_service.py`,
      `market_runtime._refresh_crypto_markets`,
      `worker_control(name='crypto')` row, `routes_crypto.py`) /
      Contracts (`CryptoMarket` shape, fetch endpoint, on/off
      semantics — `is_enabled and not is_paused`) /
      Dependencies (Polymarket Gamma directly, no
      market_catalog) / Extension points / Footguns (`tags`
      field absent ⇒ tag whitelist cannot apply, hence the
      separate toggle).
- [x] Add `Last verified: 2026-05-08` line.
- [x] Cross-link from
      [`market-filter.md`](architecture/market-filter.md) — the
      market-filter note mentions the lane but cannot reach it;
      add a "see also" pointer.
- [x] Cross-link from
      [`worker-trading.md`](architecture/worker-trading.md) —
      lane is owned by the trading plane.
- [x] Mark completed

### Task 4: `market-quality-and-prioritization.md` (new)

Seven modules upstream of the scanner decide which markets are
worth scanning at all, how they are tiered, and what their
category-aware risk envelope is. They are pre-scanner gates,
invisible from `trader-pipeline.md`.

- [x] Author
      `docs/plans/architecture/market-quality-and-prioritization.md`
      covering: market regime classification
      (`market_regime.py`), quality filter
      (`quality_filter.py`), market monitor
      (`market_monitor.py`), market prioritizer
      (`market_prioritizer.py`, tier A/B/C assignment), sport
      classifier (`sport_classifier.py`), category buffers
      (`category_buffers.py`, per-category risk envelope), depth
      analyzer (`depth_analyzer.py`, book-depth metrics fed to
      Cox-PH).
- [x] Add `Last verified: 2026-05-08` line.
- [x] Cross-link from
      [`market-filter.md`](architecture/market-filter.md) —
      market-filter handles tag-based intake; this new note
      handles everything that happens to a market **after** it
      passes the tag filter.
- [x] Cross-link from
      [`trader-pipeline.md`](architecture/trader-pipeline.md)
      § Stage 1 ("Market universe") — the scanner consumes the
      tier assignment from `market_prioritizer`.
- [x] Cross-link from
      [`execution-and-fills.md`](architecture/execution-and-fills.md)
      § Cox-PH covariates — `depth_analyzer` is the source.
- [x] Mark completed

### Task 5: Extend `execution-and-fills.md` with observability

`execution_latency_metrics.py` (193) and `latency_tracker.py`
(243) instrument the entire submit path with microsecond-level
timing and persist to `latency_tracker`. The note mentions the
"Trader cycle slow" log but does not describe the metrics layer
itself. Small extension; not enough material for its own note.

- [x] Append a `## Observability` section to
      `execution-and-fills.md` covering the two files, the
      `PipelineLatencyLog` model, and the
      `latency_tracker` table. Document which stages are
      instrumented (signal-to-decision, decision-to-submit,
      submit-to-fill) and the typical thresholds the operator
      tunes.
- [x] Bump the file's `Last verified: YYYY-MM-DD` to today
      (2026-05-08) since this is a real diff against code.
- [x] Mark completed

### Task 6: Cross-link debt sweep (audit findings E.1–E.5)

The coverage audit surfaced four cross-link items in existing
notes that don't yet point at neighbours they reference. Fold
them all in here so every reader path is consistent.

- [x] `trader-pipeline.md` § Stage 1 — explicit pointers to
      `quality_filter.py`, `market_monitor.py`,
      `market_prioritizer.py` (these all live in the new
      `market-quality-and-prioritization.md`).
- [x] `execution-and-fills.md` — pointers to
      `price_chaser.py`, `execution_safety.py`,
      `execution_tiers.py`, `live_pressure.py` (all in the new
      `execution-defense.md`).
- [x] `worker-discovery.md` — pointers to `insider_detector.py`,
      `wallet_intelligence.py`, `anomaly_detector.py` (all in
      the new `wallet-intelligence.md`).
- [x] `ai-and-llm.md` "Where to look next" — pointers to
      `news/workflow_orchestrator.py` (already linked via
      worker-news.md), `services/autoresearch_service.py`,
      `services/ai/skills/`, `services/ai/tools/`. Verify each
      target exists; do not link to things that aren't real
      arch-notes.
- [x] `market-filter.md` — pointer to
      `crypto-fast-binary-lane.md` for the parallel pipeline
      that the tag filter cannot reach.
- [x] Mark completed

### Task 7: Index sync

After Tasks 1–6 land, the global index files need rows for the
four new notes.

- [x] `docs/plans/architecture/system-overview.md` "Where to
      look next" — four new rows.
- [x] `CLAUDE.md` "Where to find more" — four new rows.
- [x] `docs/plans/plan-control-index.md` Cross-references —
      mention the four new notes as recent additions.
- [x] `agents.md` § "Documentation hygiene" code↔doc table —
      add rows for the new code areas:
      `services/insider_detector.py + anomaly_detector.py + wallet_intelligence.py → wallet-intelligence.md`,
      `services/execution_safety.py + execution_tiers.py + price_chaser.py + token_circuit_breaker.py + live_pressure.py + position_monitor.py + stuck_position_monitor.py + market_tradability.py + live_market_detector.py → execution-defense.md`,
      `services/crypto_service.py → crypto-fast-binary-lane.md`,
      `services/market_regime.py + quality_filter.py + market_monitor.py + market_prioritizer.py + sport_classifier.py + category_buffers.py + depth_analyzer.py → market-quality-and-prioritization.md`.
- [x] `.claude/commands/sync-docs.md` Phase 1 mapping — same
      four new code-area rows added to the table so future
      `/sync-docs` runs cover them.
- [x] `.claude/commands/pre-commit-check.md` paired-doc table —
      same four rows.
- [x] Mark completed

### Task 8: Close-out

- [x] Run all Validation Commands locally; all pass.
- [x] `git log --grep='Plan: 0015'` shows the full commit set.
- [x] `git mv docs/plans/0015-close-remaining-doc-gaps.md
      docs/plans/completed/`.
- [x] Update the row in `plan-control-index.md` to point at the
      `completed/` path.
- [x] Push to `origin/main`.
- [x] Mark completed
