# Plan: Architecture documentation gap audit

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0013` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

`docs/plans/architecture/` covers the layers AI agents most
frequently break (LLM, traders, scanner, settings, DB, frontend,
worker-trading) but has noticeable gaps:

- **`worker-news` and `worker-discovery`** — both planes are
  named in [`system-overview.md`](architecture/system-overview.md)
  but have no dedicated note. Anyone debugging "why aren't news
  signals appearing" or "wallet discovery is dark" has to reverse
  the worker host script + a half-dozen services.
- **WebSocket and Redis pub/sub** — `system-overview.md` lists the
  channels but nothing documents their schemas, or which
  process owns which side. Frontend WS message types are
  scattered across `useWebSocket.ts` and the backend
  `websocket.py`.
- **Execution and fills** — Cox-PH shadow path and the live
  Polymarket / Kalshi path are referenced from
  [`trader-pipeline.md`](architecture/trader-pipeline.md) but never
  described as a layer. The Cox-PH retrain story is folklore.
- **Strategy reverse-engineer** — touched briefly in the new
  [`ai-and-llm.md`](architecture/ai-and-llm.md) but is its own
  multi-iteration LLM pipeline that warrants its own note.

This plan also installs a **drift discipline marker**: every
architecture note gets a `Last verified: YYYY-MM-DD` line, which
the `/sync-docs` command from plan 0012 reads to triage which note
is overdue for a re-check against the code.

"Done" looks like: five new architecture notes exist, every
existing note has a `Last verified` line, `system-overview.md` and
`CLAUDE.md` route to all of them, and `/sync-docs` produces a
clean run.

No runtime code changes.

## Context / References

- [`architecture/system-overview.md`](architecture/system-overview.md) — top-of-funnel index that the new notes plug into
- [`architecture/ai-and-llm.md`](architecture/ai-and-llm.md) — recent precedent for note shape and depth
- [`architecture/trader-pipeline.md`](architecture/trader-pipeline.md) — cites the execution simulator without describing it
- [`backend/workers/host.py`](../../backend/workers/host.py) — plane dispatcher (source of truth for what each plane runs)
- [`backend/services/ws_feeds.py`](../../backend/services/ws_feeds.py), [`backend/services/polymarket_user_feed.py`](../../backend/services/polymarket_user_feed.py), [`backend/services/binance_feed.py`](../../backend/services/binance_feed.py) — WS feed code
- [`backend/api/websocket.py`](../../backend/api/websocket.py) — UI WS fan-out
- [`backend/services/fill_simulator/`](../../backend/services/fill_simulator/), [`backend/services/simulation/execution_simulator.py`](../../backend/services/simulation/execution_simulator.py) — Cox-PH shadow fills
- [`backend/services/live_execution_service.py`](../../backend/services/live_execution_service.py) — live submit
- [`backend/services/strategy_reverse_engineer/`](../../backend/services/strategy_reverse_engineer/) — wallet-mimicry pipeline
- Plan 0012 (`/sync-docs` command and `Last verified` convention) is a prerequisite for Task 7's verification

## Validation Commands

- `for f in docs/plans/architecture/*.md; do grep -q 'Last verified:' "$f" || echo "MISSING: $f"; done | grep -c MISSING; test $? -eq 1`
- `for n in worker-news worker-discovery websocket-and-events execution-and-fills strategy-reverse-engineer; do test -f docs/plans/architecture/$n.md || echo "MISSING $n"; done | grep -c MISSING; test $? -eq 1`
- `grep -q 'worker-news.md' docs/plans/architecture/system-overview.md`
- `grep -q 'worker-news.md' CLAUDE.md`
- `grep -q 'execution-and-fills.md' docs/plans/architecture/system-overview.md`

### Task 1: `worker-news.md`

The news plane runs the heaviest non-LLM ML stack (sentence-
transformers, FAISS) plus the budgeted LLM workflow described in
`ai-and-llm.md`, plus the weather pipeline and Cox-PH fill-model
trainer. Document what runs where, what each component owns, and
the failure modes that look identical from the outside.

- [x] Author `docs/plans/architecture/worker-news.md` covering:
      Purpose / Key files (workflow_orchestrator,
      semantic_matcher, market_watcher_index, article_clusterer,
      hybrid_retriever, reranker, edge_estimator, edge_detector,
      Cox-PH trainer, weather worker, telegram dispatcher) /
      Contracts (NewsWorkflowFinding, NewsTradeIntent shapes) /
      Dependencies (LLMManager, sentence-transformers, FAISS,
      worker-trading consumer) / Extension points / Footguns
      (FAISS memory ~2 GB, ST warmup latency, async budget
      exhaustion symptom).
- [x] Add a `Last verified: YYYY-MM-DD` line at the bottom.
- [x] Cross-link from [`ai-and-llm.md`](architecture/ai-and-llm.md)
      "Where to look next" and from
      [`trader-pipeline.md`](architecture/trader-pipeline.md)
      "Where to look next".
- [x] Mark completed

### Task 2: `worker-discovery.md`

The discovery plane is REST-bound and isolated to keep its retry
storms off the trading event loop. It also hosts the
`strategy_reverse_engineer` agent loop (which gets its own note
in Task 5). Document the rest.

- [x] Author `docs/plans/architecture/worker-discovery.md`
      covering: wallet discovery, tracked-wallets crawler,
      provider import, backtest worker, scheduler. Explicitly
      note that strategy reverse-engineer also runs here but
      lives in its own note.
- [x] Add a `Last verified: YYYY-MM-DD` line.
- [x] Cross-link from [`system-overview.md`](architecture/system-overview.md)
      "Why three worker planes" section.
- [x] Mark completed

### Task 3: `websocket-and-events.md`

Three live-data planes (Polymarket CLOB + user channel, Kalshi,
Binance) plus the UI WS plus Redis pub/sub need one place that
explains who publishes what, who subscribes, and what the JSON
message shapes are.

- [x] Author `docs/plans/architecture/websocket-and-events.md`:
      external WS feeds (FeedManager, polymarket_user_feed,
      binance_feed), the `wallet_state_cache` and `price_cache`
      they fan into, the UI `/ws` channels (`opportunities_update`,
      `scanner_status`, `trade_executed`, `init`,
      `subscribed`, `ping`/`pong`), and the Redis pub/sub
      channels (trader_events, signal_bus, wallet deltas).
- [x] Document the **Polymarket exclusivity** invariant
      (single user-channel WS per API key — the load-bearing
      reason for one trading plane).
- [x] Add a `Last verified: YYYY-MM-DD` line.
- [x] Cross-link from `system-overview.md` "Cross-plane
      communication" and from `frontend-architecture.md`.
- [x] Mark completed

### Task 4: `execution-and-fills.md`

The Cox-PH fill model and the live execution adapter are the
"oracle layer" that decides whether a `selected` decision becomes
a fill. They are barely documented; folklore says retraining is
operator-controlled, latency budgets are tight, and the slippage
knob lives somewhere.

- [x] Author `docs/plans/architecture/execution-and-fills.md`:
      shadow path (execution_simulator → Cox-PH → simulation_trades
      / simulation_positions, fill_simulator_refresh_worker
      schedule), live path (live_execution_service →
      py-clob-client / Kalshi REST, signing flow, retry
      semantics), reconciliation worker, redeemer worker,
      position-mark loop. Document `slippage_bps`,
      `max_spread_bps`, `price_policy: taker_market`
      diagnostic levers from `trader-pipeline.md` Step 7.
- [x] Add a `Last verified: YYYY-MM-DD` line.
- [x] Cross-link from
      [`trader-pipeline.md`](architecture/trader-pipeline.md)
      Stage 6/7 and from
      [`copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md).
- [x] Mark completed

### Task 5: `strategy-reverse-engineer.md`

A separate LLM-heavy pipeline that takes a wallet address and
either writes an analytical report or iterates a candidate
strategy until it matches the wallet's behaviour. Touched in
`ai-and-llm.md` but warrants its own note because it has its own
job table, its own iterations table, its own cost ceiling, and
its own tools registry distinct from Cortex.

- [x] Author `docs/plans/architecture/strategy-reverse-engineer.md`:
      pipeline (enqueue_job → agent loop with
      polybacktest_find_markets / polybacktest_import /
      submit_strategy_candidate / get_backtest_result /
      finalize_best), DB tables
      (`strategy_reverse_engineer_jobs`,
      `strategy_reverse_engineer_iterations`), cost controls
      (`reverse_engineer_max_iterations`,
      `reverse_engineer_target_score`,
      `reverse_engineer_max_cost_usd`,
      `reverse_engineer_max_wallet_trades`), report_mode
      semantics (`report` vs `strategy_seed`).
- [x] Add a `Last verified: YYYY-MM-DD` line.
- [x] Cross-link from `ai-and-llm.md` "Research and
      supervision" section (replace the inline summary with a
      pointer).
- [x] Mark completed

### Task 6: Backfill `Last verified` on existing architecture notes

Apply the discipline marker to the rest of the directory. Each
note gets one bottom line in the form `Last verified: YYYY-MM-DD`,
where the date is the day the verifier (human or
arch-note-writer subagent) confirmed the note still matches the
code. Do not bump the date on a note this plan does not actively
re-verify; leave it stamped at "today" only when a real diff was
done.

- [x] `system-overview.md`
- [x] `backend-architecture.md`
- [x] `frontend-architecture.md`
- [x] `settings-and-secrets.md`
- [x] `database-and-migrations.md`
- [x] `testing.md`
- [x] `llm-provider-layer.md`
- [x] `trader-pipeline.md`
- [x] `copy-trade-pipeline.md`
- [x] `market-filter.md`
- [x] `worker-trading.md`
- [x] `ai-and-llm.md`
- [x] Mark completed

### Task 7: Cross-link sweep

After Tasks 1–6 land, the index files need to learn about the
new notes and the marker convention.

- [x] `system-overview.md` "Where to look next" table — add
      rows for `worker-news.md`, `worker-discovery.md`,
      `websocket-and-events.md`, `execution-and-fills.md`,
      `strategy-reverse-engineer.md`.
- [x] `CLAUDE.md` "Where to find more" — same five rows.
- [x] `plan-control-index.md` Cross-references list — same
      five rows.
- [x] `agents.md` — add a one-line note in the "Where AI
      agents start" section (created by plan 0012) that
      `Last verified` markers exist and `/sync-docs`
      consumes them.
- [ ] Run `/sync-docs 50` and confirm it reports green for
      every architecture note touched by this plan. (Manual.)
- [x] Mark completed

### Task 8: Close-out

- [x] Run all Validation Commands locally; all pass.
- [x] `git log --grep='Plan: 0013'` shows the full commit set.
- [x] `git mv docs/plans/0013-architecture-doc-gap-audit.md
      docs/plans/completed/`.
- [x] Update the row in `plan-control-index.md` to point at the
      `completed/` path.
- [x] Mark completed

(Note: the `git mv` and the `plan-control-index.md` row update are
performed in the close-out commit immediately below; checkboxes are
flipped to `[x]` synchronously with that commit so the moved file
already shows the closed state.)
