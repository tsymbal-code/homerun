# Plan: 2026-05-12 — first crypto_5m_midcycle param sweep on 24 h of collected data

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](../README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](../completed/) on close. Every commit produced by
> this plan carries a `Plan: 0048` git trailer (see
> [Commits and traceability](../README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](../plan-control-index.md).
>
> **Status: BACKLOG.** Activate on or after **2026-05-12 12:00 UTC**
> (≥ 21 h after the Plan 0046 deploy at 2026-05-11 13:38 UTC, so the
> 24 h replay window has continuous `crypto_oracle_history` coverage
> and ≥ 4–5 live emits per 5-bot A/B set to cross-check the
> leaderboard against).

## Overview

Plan 0046 shipped the offline backtest harness for crypto strategies
on 2026-05-11. By the time this plan activates we will have ≥ 21 h
of continuous `crypto_oracle_history` rows and ≥ 21 h of
`firehose_evaluation` rows for the 5-bot A/B grid created the same
day (`BTC - 5min` + `BTC5m-dist{05,07,10,15}`).

This plan is the **first real use** of `POST /api/validation/code-backtest/optimize-strategy`.
Done state is a decision table: which `min_distance_bps`
× `max_entry_price` cell maximises win-rate-weighted PnL across the
last 24 h, and whether the offline leaderboard agrees with the live
shadow PnL from the 5 A/B bots. If the two agree, we promote one
config; if they disagree, we treat the offline backtester as not yet
trustworthy and document the failure mode.

## Context / References

- [Plan 0046 — Offline backtest harness](../completed/0046-offline-backtest-for-crypto-strategies.md)
- [Architecture: execution-and-fills](../architecture/execution-and-fills.md) §
  "Crypto resolution truth source"
- [Strategy doc: crypto-5m-midcycle](../../strategies/crypto-5m-midcycle.md)
- [`backend/services/strategy_backtester.py`](../../../backend/services/strategy_backtester.py) —
  `_run_crypto_replay_detection`, `run_crypto_strategy_optimize`
- 5 live A/B bots in `traders` table:
  `BTC - 5min` (3 bps) / `BTC5m-dist05` / `dist07` / `dist10` / `dist15`

## Validation Commands

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "select min(timestamp_ms), max(timestamp_ms), count(*) from crypto_oracle_history"'` —
  confirm ≥ 21 h continuous coverage before kicking the sweep
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "select count(*) from trader_events where event_type='\''firehose_evaluation'\'' and payload_json->>'\''strategy_slug'\'' = '\''crypto_5m_midcycle'\'' and payload_json->>'\''outcome'\'' = '\''emitted'\''  and created_at > now() - interval '\''24 hours'\''"'` —
  this is the upper bound the sweep can attribute to any cell
- `ssh polyhome-1 'curl -fsS -X POST http://127.0.0.1:8888/api/validation/code-backtest/optimize-strategy -H "Content-Type: application/json" -d @/tmp/sweep-input.json' | tee /tmp/sweep-output.json | jq '.leaderboard | sort_by(-.composite_score) | .[0:5]'` —
  run + inspect top 5

### Task 1: kick the sweep

- [ ] Confirm `crypto_oracle_history` has ≥ 21 h of rows for every
      `(asset, source)` pair the strategy might read. Spot-check that
      Chainlink rows for BTC/SOL/XRP cover the entire window without
      gaps > 30 s. Document any gaps in the result write-up as caveats.
- [ ] Build the sweep request payload at `/tmp/sweep-input.json` on
      the operator's local machine:
      ```json
      {
        "strategy_slug": "crypto_5m_midcycle",
        "window_hours": 24,
        "grid": {
          "min_distance_bps":  [1, 2, 3, 5, 7, 10, 15, 20],
          "max_entry_price":   [0.50, 0.60, 0.65, 0.70, 0.80]
        },
        "top_k": 50
      }
      ```
      That is 8 × 5 = 40 configurations.
- [ ] Run via the curl command above. Expected runtime: ≤ 5 min at
      ~90 s per single-config replay × 40 configs (the implementation
      caches `firehose_rows` across configs in one outer load — verify
      from logs that we only see one `_load_firehose_rows` call).
- [ ] Persist the raw JSON output at
      `docs/data/plan-0048-sweep-2026-05-12.json` (operator commits it
      to the repo for posterity — large but compresses well).
- [ ] Mark completed

### Task 2: analyse the leaderboard

- [ ] Top 5 by `composite_score`. For each, write down: params,
      `emit_count`, `win_rate`, `total_pnl_usd`, `samples`. Look for
      cells that have ≥ 10 emits **and** positive `total_pnl_usd`
      **and** `win_rate` ≥ 0.65 — anything thinner than that is noise.
- [ ] Sanity check against `rows_without_book_snapshot` — if it
      dominates `cycles_evaluated`, the offline run is data-starved
      and the leaderboard is not actionable. Document the ratio.
- [ ] Build a 2D heatmap (or just a markdown table) of
      `total_pnl_usd` over the
      `min_distance_bps` × `max_entry_price` cells. Look for
      monotonicity — if PnL jumps around randomly, the strategy is
      not yet stable enough at these gate settings.
- [ ] Mark completed

### Task 3: cross-check against the 5 live A/B bots

- [ ] Pull the 24 h live shadow PnL per A/B bot from the orchestrator
      UI (or directly from `trader_positions` / `trader_decisions`
      with `mode='shadow'`). Record the 5 numbers for
      `min_distance_bps ∈ {3, 5, 7, 10, 15}` (their actual deployed
      configs).
- [ ] Compare with the offline leaderboard's rows for those same 5
      `min_distance_bps` values **at `max_entry_price=0.70`** (their
      shared config). The offline `total_pnl_usd` should be within
      ±20 % of live shadow PnL — if not, document the disagreement
      and treat the offline tool as not yet trustworthy.
- [ ] If they agree: pick the highest-scoring cell from the offline
      leaderboard and update the `BTC - 5min` trader's
      `source_configs_json.strategy_params` to those values via the
      Strategy Manager UI. **Do not auto-apply via SQL** — the UI
      path triggers the per-trader strategy clone + binding-cache
      eviction (Plan 0041). Keep the 4 dist* A/B bots running as
      controls.
- [ ] If they disagree: leave all 5 bots untouched. Document the gap
      with file:line refs to where the offline path drifts from live
      (likely `_run_crypto_replay_detection` reconstructing only
      cycles that originally reached the book gate — live evaluates
      the book gate every cycle).
- [ ] Mark completed

### Task 4: close

- [ ] Write up findings as a new "Findings" section at the bottom of
      [`docs/strategies/crypto-5m-midcycle.md`](../../strategies/crypto-5m-midcycle.md)
      including: chosen params, expected daily PnL (offline), live
      24 h baseline, and the offline-vs-live agreement %.
- [ ] If A/B bots have served their purpose, delete them:
      `DELETE FROM traders WHERE name LIKE 'BTC5m-dist%';`
      Keep `BTC - 5min` running with the new (or unchanged) params.
- [ ] Move this plan from `backlog/` to `completed/`, update
      `plan-control-index.md`, commit with `Plan: 0048` trailer.
- [ ] Mark completed
