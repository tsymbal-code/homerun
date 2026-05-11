# Plan: Crypto 5m last-outcome-follow strategy

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](../README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](../completed/) on close. Every commit produced by
> this plan carries a `Plan: 0047` git trailer (see
> [Commits and traceability](../README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](../plan-control-index.md).

## Overview

Add the simplest possible directional crypto strategy: **on each new
5-minute Polymarket up-or-down cycle for an enabled asset, open a
position on the same side that won the immediately preceding cycle**.
No oracle-distance gate, no microstructure gate, no edge-percent gate —
the only filters are the ones execution cannot skip (5m timeframe,
asset in enable list, VWAP within the configurable entry band, fresh
order-book depth for the chosen side).

The previous cycle's outcome is reconstructed without depending on
catching the closing Chainlink read: Polymarket sets every new
cycle's `price_to_beat` to the Chainlink price at cycle start, which
equals the Chainlink price at the previous cycle's end. Therefore
`outcome_prev = "YES" if price_to_beat_new > price_to_beat_old else
"NO"`, evaluated per asset as soon as we observe a market with a new
`condition_id`. This is exact (modulo `price_to_beat` rounding by
Polymarket) and requires only the data already present on every
`crypto_update` event.

Defaults ship the strategy enabled for **BTC only** — that is the
asset the operator picked as the starting point — but the asset list
is editable in the strategy-manager UI so the same code path can be
exercised on ETH/SOL/XRP without code changes. The first cycle after
worker-trading boot (or after any prolonged gap) silently skips
because the previous outcome is unknown — this is intentional and
matches the spec "direct repetition of the last result without
additional filters."

## Context / References

- [Architecture: Crypto fast-binary lane](../architecture/crypto-fast-binary-lane.md)
- [Architecture: Trader pipeline](../architecture/trader-pipeline.md)
- [Strategy: Crypto 5m Midcycle](../../strategies/crypto-5m-midcycle.md) —
  template for the file layout, CycleTracker usage, and WS
  prewarm pattern.
- [backend/services/strategies/crypto_5m_midcycle.py](../../../backend/services/strategies/crypto_5m_midcycle.py)
- [backend/services/opportunity_strategy_catalog.py:891](../../../backend/services/opportunity_strategy_catalog.py)
- [backend/tests/test_crypto_5m_midcycle_strategy.py](../../../backend/tests/test_crypto_5m_midcycle_strategy.py)

## Validation Commands

- `docker compose exec backend pytest -q backend/tests/test_crypto_5m_last_outcome_strategy.py`
- `docker compose exec backend pytest -q backend/tests/test_strategy_catalog_seed_create_only.py`
- `docker compose exec backend python -c "from services.strategies.crypto_5m_last_outcome import Crypto5mLastOutcomeStrategy; s=Crypto5mLastOutcomeStrategy(); s.configure({}); print(s.config)"`

## Out of scope

- No DB migration is required — the strategy seed lands via
  `SYSTEM_OPPORTUNITY_STRATEGY_SEEDS` and is upserted at backend
  startup like every other system strategy.
- No CRITICAL-tier knob changes are touched. The new strategy only
  introduces MEDIUM-tier per-strategy params (asset list, VWAP
  entry band, bet size, entry-milestone seconds) — documented in the
  per-strategy doc and not requiring the
  `runtime-tweaks.md` walkthrough.
- The strategy emits but does not place positions on its own — a
  trader binding (created via the strategy-manager UI, like every
  other crypto strategy) is needed before any shadow / live trades
  flow. Creating that binding is an operator step, not part of this
  plan.

### Task 1: Implement the strategy class

- [x] Create `backend/services/strategies/crypto_5m_last_outcome.py`
  modeled on `crypto_5m_midcycle.py`. Class
  `Crypto5mLastOutcomeStrategy(BaseStrategy)`, slug
  `crypto_5m_last_outcome`, `source_key="crypto"`,
  `subscriptions=["crypto_update"]`.
- [x] Per-asset state dict
  `_last_seen[asset] = (market_id, price_to_beat)` plus
  `_last_outcome[asset] = "YES" | "NO" | None`. On every
  `_evaluate_market` call, if the incoming `(asset, market_id)`
  differs from the stored one, compute the previous outcome from
  the two `price_to_beat` values, update both dicts, and reset the
  CycleTracker for that asset.
- [x] Use the same `CycleTracker`-driven entry-milestone pattern as
  midcycle, with `entry_seconds_after_start` default 30 s (so the
  WS book has time to populate at cycle start). One emit per cycle.
- [x] Gates in order: timeframe (5 m), market_id present, asset in
  configured list, cycle end timestamp parseable, entry milestone
  crossed, `previous_outcome` known, CLOB tokens present, book
  depth available + fresh, VWAP within configurable
  `[min_entry_price, max_entry_price]`. No distance gate, no
  oracle-freshness gate, no min-seconds-to-resolution gate beyond
  what the milestone provides.
- [x] Add the same `_ensure_ws_subscribed_for_5m` prewarm helper as
  midcycle so the book-depth gate doesn't reject every first cycle.
- [x] Expose `crypto_5m_last_outcome_config_schema()` returning the
  `param_fields` for the strategy-manager UI.
- [x] Mark completed

### Task 2: Register in the system seed catalog

- [x] Append a `SystemOpportunityStrategySeed` entry to
  `SYSTEM_OPPORTUNITY_STRATEGY_SEEDS` in
  `backend/services/opportunity_strategy_catalog.py` with
  `slug="crypto_5m_last_outcome"`, `source_key="crypto"`,
  `import_module="services.strategies.crypto_5m_last_outcome"`,
  `sort_order=195` (between midcycle 193 and convergence 194 / 196).
  Inline a `config_schema` mirroring the one returned by
  `crypto_5m_last_outcome_config_schema()`.
- [x] Mark completed

### Task 3: Pytest coverage

- [x] Create `backend/tests/test_crypto_5m_last_outcome_strategy.py`
  using the same fixture pattern as
  `test_crypto_5m_midcycle_strategy.py` (synthetic market dict, WS
  cache seeded via `FeedManager.reset_instance` + `_seed_book`).
- [x] Cases (each test one gate or behaviour):
  - First-cycle observation does NOT emit (previous outcome unknown).
  - After rollover with `price_to_beat_new > price_to_beat_old`,
    the next cycle emits side `YES`.
  - After rollover with `price_to_beat_new < price_to_beat_old`,
    the next cycle emits side `NO`.
  - Equal `price_to_beat` across rollover → no emit (outcome
    ambiguous, treated as unknown).
  - Skipped when timeframe is not 5 m.
  - Skipped when asset is not in the configured list (default
    enables BTC only, so SOL is rejected).
  - User-extended asset list (`assets=["BTC","SOL"]`) accepts SOL.
  - Skipped before the entry milestone; fires at / after it.
  - Idempotent within the same cycle (one emit per cycle).
  - Skipped when VWAP > `max_entry_price` and when VWAP <
    `min_entry_price`.
  - Skipped when the order book is unavailable.
  - Disabled master switch produces zero opportunities through
    `on_event`.
  - Per-asset isolation: BTC rollover does not pollute SOL state.
- [x] Mark completed

### Task 4: Operator-facing strategy doc

- [x] Create `docs/strategies/crypto-5m-last-outcome.md` matching
  the structure of `crypto-5m-midcycle.md` — Сутність, Контракт,
  Логіка детекції, Логіка виходу, Налаштування за замовчуванням,
  Коли НЕ працює, Посилання. The strategy doc is in Ukrainian per
  `docs/strategies/` convention; the code, plan, and architecture
  notes stay English.
- [x] Add a row for the new strategy to
  `docs/strategies/README.md`.
- [x] Mark completed

### Task 5: Close out

- [x] All Validation Commands pass on the developer machine
  (pytest, syntax check). Live verification on `polyhome-1` is an
  operator step (sync-and-redeploy + create a trader binding via
  UI), tracked in the operator handoff, not in this plan.
- [x] Move this plan to `docs/plans/completed/` and update the
  link in `plan-control-index.md`.
- [x] Mark completed
