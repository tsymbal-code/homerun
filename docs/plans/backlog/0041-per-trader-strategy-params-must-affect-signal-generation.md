# Plan: Per-trader strategy params must actually affect signal generation

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](../README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](../completed/) on close. Every commit produced by
> this plan carries a `Plan: 0041` git trailer (see
> [Commits and traceability](../README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](../plan-control-index.md).
>
> **Status: BACKLOG.** Activate when one of:
> (a) an operator opens a ticket reporting that values changed
>     under Trading Panel → Tune → Parameters → Save Parameters
>     have no observable effect on which markets the strategy fires
>     on, OR
> (b) we need to run two traders on the same strategy with
>     different `min_distance_bps` / `bet_size_usd` /
>     `min_seconds_to_resolution` / etc., OR
> (c) the LIVE-trader operator notices that lowering a per-trader
>     threshold doesn't produce more signals while raising the
>     global one (in `strategies.config`) does.

## Overview

Today, strategy instances are **process-wide singletons**: one
``BaseStrategy`` subclass instance per slug, loaded once by
[`strategy_loader._load`](../../../backend/services/strategy_loader.py)
and configured **once** at load time with the row's
``strategies.config`` (the global JSON in the ``strategies`` table).
The strategy's ``on_event`` reads ``self.config`` for every gate
(``min_distance_bps``, ``bet_size_usd``, ``max_entry_price``, …).

But the UI **Trading Panel → Tune → Parameters → Save Parameters**
persists those same fields into
``traders.source_configs_json[].strategy_params`` (per-trader, per
``(source_key, strategy_key)`` row), via
[`TradingPanel.tsx:7068-7096`](../../../frontend/src/components/TradingPanel.tsx).
The strategy never reads from there. The values land in DB,
re-render in the UI on next fetch (so the operator thinks they
applied), and are silently ignored at signal generation time.

The only place per-trader ``strategy_params`` is read is in the
order-submit layer
([`order_manager.py:277-303`](../../../backend/services/trader_orchestrator/order_manager.py))
for execution-side toggles such as
``allow_taker_limit_buy_above_signal``. Signal-side gates are
**not** consulted.

**Done means:** changing a per-trader signal-generation parameter
in the UI (e.g. dropping ``min_distance_bps`` from 15 → 3 on a
single trader) immediately changes which opportunities reach that
trader's signal queue, without restarting the worker and without
affecting any other trader bound to the same strategy. A
regression test exercises the per-trader path end-to-end: two
traders on the same strategy with different
``min_distance_bps``, a single ``crypto_update`` event, only the
trader with the looser threshold gets a ``trade_signals`` row.

## Context / References

- [`backend/services/strategy_loader.py:770-795`](../../../backend/services/strategy_loader.py)
  — singleton load + configure cascade.
- [`backend/services/strategies/crypto_5m_midcycle.py:300-340`](../../../backend/services/strategies/crypto_5m_midcycle.py)
  — example strategy: ``self.config`` reads inside ``on_event``.
- [`backend/services/strategies/base.py:986`](../../../backend/services/strategies/base.py)
  — ``BaseStrategy.configure`` (the contract every strategy
  inherits).
- [`backend/services/market_runtime.py:1622-1654`](../../../backend/services/market_runtime.py)
  — ``_run_opportunity_dispatch_loop`` — the single dispatch site.
  This is where per-trader fan-out would happen.
- [`backend/services/trader_orchestrator/order_manager.py:277-303`](../../../backend/services/trader_orchestrator/order_manager.py)
  — the only place per-trader ``strategy_params`` is currently
  consulted (execution-side toggles).
- [`frontend/src/components/TradingPanel.tsx:7068-7096`](../../../frontend/src/components/TradingPanel.tsx)
  — UI mutation that persists per-trader Tune values.

## Validation Commands

- `docker compose exec backend pytest -q backend/tests/test_strategy_loader_per_trader_params.py`
  (new file, written under Task 3)
- `docker compose exec backend pytest -q backend/tests/test_crypto_5m_midcycle_strategy.py`
  (existing — regression cover)
- `docker compose exec backend pytest -q backend/tests/test_routes_traders.py`
  (per-trader config persistence — should keep passing)
- `docker compose exec backend ruff check backend/services/strategy_loader.py backend/services/strategies/`

## Design tradeoffs to resolve before Task 1

Three architectures are viable; **pick one before writing code**.
Each has consequences for the
``BaseStrategy`` contract (currently F-category: every loaded
strategy implements it), so the choice ripples through every
strategy file under ``backend/services/strategies/``.

**Option A — Per-trader strategy instance.** Loader produces one
instance per ``(slug, trader_id)``, each ``.configure``d with a
merged ``global ∪ per_trader`` config. Pros: zero changes to
strategy code; cleanest mental model. Cons: 1 strategy × N
traders = N instances in memory; instance state (cycle trackers,
warm caches) duplicates per trader. Strategies that built
internal state assuming "I see every market exactly once"
(e.g. ``_cycle_trackers`` in
[`crypto_5m_midcycle.py:294`](../../../backend/services/strategies/crypto_5m_midcycle.py))
need an audit.

**Option B — Pass per-trader config into `on_event`.** Strategies
become stateless w.r.t. config: ``on_event(event,
trader_config: dict)``. Loader stays singleton; dispatcher calls
``on_event`` once per bound trader with the merged config. Pros:
no instance proliferation; opt-in per strategy. Cons: breaks the
public ``BaseStrategy`` contract — every strategy signature
changes; legacy strategies have to be migrated; cycle-tracker
state has to be re-keyed by ``(market_id, trader_id)``.

**Option C — Post-filter at intent_runtime.** Strategy keeps
emitting opportunities using the strictest (global) gates; per-
trader ``intent_runtime.publish_opportunities`` filters them
against the trader's looser overrides. Pros: smallest blast
radius; reuses the existing post-filter site. Cons: only works
when the per-trader value is **stricter** than global. The
operator's current real use case (drop ``min_distance_bps`` from
15 → 3 to see emissions) goes the **looser** direction — which
post-filter can't fabricate (the strategy already culled those
markets upstream). Option C is therefore **insufficient on its
own**; it can only complement A or B.

Decision criterion: pick the option that lets a LIVE-mode
operator validate the change with a single ``UPDATE traders``
and **no** redeploy / restart. Option A meets that bar most
directly because reconfiguring one trader's instance is a local
operation.

## Tasks

### Task 1: Decide architecture and update [`backend/services/strategies/base.py`](../../../backend/services/strategies/base.py)

- [ ] Pick A, B, or A+C from the tradeoffs section above. Record
      the choice and its rationale in this plan file under a new
      "## Decision" section.
- [ ] Update the ``BaseStrategy`` contract docstring to make the
      new per-trader semantics explicit (where ``self.config``
      vs. ``trader_config`` lives, who owns lifecycle, when
      ``configure`` is called).
- [ ] Add a deprecation notice / no-op shim so existing in-tree
      strategies keep loading without rewriting them all at once.

### Task 2: Wire per-trader config through dispatcher

- [ ] In
      [`backend/services/market_runtime.py:_run_opportunity_dispatch_loop:1622`](../../../backend/services/market_runtime.py),
      teach the dispatch site to enumerate bound traders for the
      ``crypto`` source (mirror the binding cache from
      [`_firehose.py:_refresh_binding_cache`](../../../backend/services/strategies/_firehose.py)
      but **without** the ``mode='live'`` filter — shadow traders
      need this too) and fan out to per-trader strategy invocations.
- [ ] If Option A: cache a strategy-instance-per-trader inside
      ``strategy_loader``, keyed by ``(slug, trader_id)``. Invalidate
      on trader config update via the existing
      [`reconfigure_loaded`](../../../backend/services/strategy_loader.py)
      hook (which today only handles the global instance).
- [ ] If Option B: extract ``trader_config`` from
      ``traders.source_configs_json[].strategy_params``, merge with
      ``strategies.config``, pass to ``on_event``. Migrate
      ``crypto_5m_midcycle`` and the other five crypto strategies
      to read from the passed dict instead of ``self.config``.

### Task 3: Regression tests

- [ ] Add ``backend/tests/test_strategy_loader_per_trader_params.py``
      covering: (a) two traders with different
      ``min_distance_bps`` on the same strategy produce
      independent opportunity sets for the same ``crypto_update``
      event, (b) trader config update without worker restart is
      reflected in the next dispatch (no stale-singleton bug),
      (c) a trader whose ``strategy_params`` is empty falls back
      to ``strategies.config``.
- [ ] Add an end-to-end test in
      ``backend/tests/test_market_runtime_crypto_lane_toggle.py``
      (or a sibling file): two configured traders, one
      ``crypto_update`` payload, assert ``trade_signals`` rows
      land only for the trader whose merged config passes its
      gates.

### Task 4: UI consistency check

- [ ] Confirm
      [`TradingPanel.tsx:7068-7096`](../../../frontend/src/components/TradingPanel.tsx)
      keeps writing to ``traders.source_configs_json[].strategy_params``
      (no UI change required; the backend now actually honors it).
- [ ] Add a short note to
      [`docs/plans/architecture/trader-pipeline.md`](../architecture/trader-pipeline.md)
      explaining the new lifecycle: per-trader override merged
      over ``strategies.config`` at dispatch, applied to either
      a per-trader instance (A) or the singleton via passed
      config (B).

### Task 5: Migration audit

- [ ] Grep every strategy under
      ``backend/services/strategies/`` for ``self.config.get``
      reads. For each: confirm the value should be per-trader-
      overridable (most are: thresholds, sizes, asset lists), or
      flag it as "must stay global" (e.g. version markers,
      class-level constants).
- [ ] For each strategy that has runtime state keyed by market
      (``_cycle_trackers``, etc.), re-key by ``(market_id,
      trader_id)`` if Option A was chosen — otherwise audit for
      cross-trader leakage if Option B was chosen.

### Task 6: Deploy and verify on live stack

- [ ] Run all validation commands locally / in CI.
- [ ] Deploy via ``./deploy/sync_remote.sh``.
- [ ] On ``polyhome-1``, take a shadow trader (e.g. the existing
      ``BTC - 5min`` trader, id ``5d07f744…3717f1``), drop its
      ``min_distance_bps`` to ``3`` via the Tune UI, save, observe
      ``trade_signals`` and ``trader_decisions`` start landing
      within two 5-minute cycles. Capture timestamp + counts in
      this plan file.
- [ ] Reset the trader's ``min_distance_bps`` back to 15. Close
      this plan: move to ``docs/plans/completed/``.
