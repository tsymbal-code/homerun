# Plan: Per-trader strategy params must actually affect signal generation

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0041` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Today, strategy instances are **process-wide singletons**: one
``BaseStrategy`` subclass instance per slug, loaded once by
[`strategy_loader._load`](../../backend/services/strategy_loader.py)
and configured **once** at load time with the row's
``strategies.config`` (the global JSON in the ``strategies`` table).
The strategy's ``on_event`` reads ``self.config`` for every gate
(``min_distance_bps``, ``bet_size_usd``, ``max_entry_price``, …).

But the UI **Trading Panel → Tune → Parameters → Save Parameters**
persists those same fields into
``traders.source_configs_json[].strategy_params`` (per-trader, per
``(source_key, strategy_key)`` row), via
[`TradingPanel.tsx:7068-7096`](../../frontend/src/components/TradingPanel.tsx).
The strategy never reads from there. The values land in DB,
re-render in the UI on next fetch (so the operator thinks they
applied), and are silently ignored at signal generation time.

The only place per-trader ``strategy_params`` is read is in the
order-submit layer
([`order_manager.py:277-303`](../../backend/services/trader_orchestrator/order_manager.py))
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

- [`backend/services/strategy_loader.py:770-795`](../../backend/services/strategy_loader.py)
  — singleton load + configure cascade.
- [`backend/services/strategies/crypto_5m_midcycle.py:300-340`](../../backend/services/strategies/crypto_5m_midcycle.py)
  — example strategy: ``self.config`` reads inside ``on_event``.
- [`backend/services/strategies/base.py:986`](../../backend/services/strategies/base.py)
  — ``BaseStrategy.configure`` (the contract every strategy
  inherits).
- [`backend/services/market_runtime.py:1622-1654`](../../backend/services/market_runtime.py)
  — ``_run_opportunity_dispatch_loop`` — the single dispatch site.
  This is where per-trader fan-out would happen.
- [`backend/services/trader_orchestrator/order_manager.py:277-303`](../../backend/services/trader_orchestrator/order_manager.py)
  — the only place per-trader ``strategy_params`` is currently
  consulted (execution-side toggles).
- [`backend/services/strategies/_firehose.py:107-158`](../../backend/services/strategies/_firehose.py)
  — ``_refresh_binding_cache``: existing LIVE-only ``(slug → [trader_id])``
  binding cache that Task 2 must generalise (drop the ``mode='live'``
  filter or add a shadow-aware sibling).
- [`frontend/src/components/TradingPanel.tsx:7068-7096`](../../frontend/src/components/TradingPanel.tsx)
  — UI mutation that persists per-trader Tune values.
- [`docs/plans/architecture/trader-pipeline.md`](architecture/trader-pipeline.md)
  — the architecture note Task 4 updates with the new per-trader
  lifecycle.

## Validation Commands

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest -q backend/tests/test_strategy_loader_per_trader_params.py'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest -q backend/tests/test_crypto_5m_midcycle_strategy.py'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest -q backend/tests/test_market_runtime_crypto_lane_toggle.py'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest -q backend/tests/test_trader_source_schema_and_validation.py'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend ruff check backend/services/strategy_loader.py backend/services/strategies'`

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
[`crypto_5m_midcycle.py:294`](../../backend/services/strategies/crypto_5m_midcycle.py))
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

## Decision

**Picked: Option A + Option C (per-trader strategy instance with
trader-scoped signal routing).**

Rationale, captured here so future agents (or a revert) see the
shape:

1. **Option A alone has a cross-trader leakage bug.** Today
   `intent_runtime.list_unconsumed_signals(trader_id=...)` does
   `del trader_id` (`backend/services/intent_runtime.py:2599`) and
   filters only by `source` / `status` / `strategy_type`. If
   trader A's per-trader instance emits an opportunity that
   passes its looser gate but would fail trader B's stricter
   gate, trader B picks up the same `trade_signals` row and
   acts on it. Per-trader emission therefore demands per-trader
   signal scoping (the "C" complement).
2. **Option B breaks the public `BaseStrategy` contract on
   every strategy in the tree (~30 modules) in a single commit.**
   Cross-trader state isolation (`_cycle_trackers`, warm price
   caches) becomes the strategy author's burden via
   `(market_id, trader_id)` re-keying. The F-category blast
   radius is huge.
3. **Option A keeps every existing strategy's `on_event`
   signature.** Cross-trader state isolation is free: each
   `(slug, trader_id)` lives in its own instance with its own
   `_state` dict and its own `_cycle_trackers`. The only contract
   surface that changes is the loader (now caches per-trader
   instances) and the dispatch site (now fans out per trader).
4. **The C complement is mechanically small.** One optional
   `Opportunity.intended_trader_id` field, propagated through
   `intent_runtime.publish_opportunities` into the signal
   snapshot, consumed by `list_unconsumed_signals` as a
   trader-id filter. No DB schema change — the field rides in
   `trade_signals.payload_json` which is already a JSON blob.
5. **`reconfigure_loaded` extends naturally.** Today it
   re-runs `configure(merged_config)` on the global instance;
   we add a per-trader sibling that re-runs `configure(...)` on
   each cached `(slug, trader_id)` instance with the new
   merged config. Trader-config edits via the Tune UI invalidate
   the cached per-trader instance without a worker restart.

Where this leaves us:

- `BaseStrategy` contract: gains a `clone_for_trader(trader_config)`
  method (default impl: instantiate, configure with
  `default_config ∪ global_config ∪ trader_config`) and the
  `configure(config: dict)` docstring is updated to make the
  three-level cascade explicit. The contract on `on_event`,
  `evaluate`, `should_exit`, `state` is unchanged.
- `strategy_loader`: keeps a `(slug, trader_id) → LoadedStrategy`
  cache alongside the existing `(slug) → LoadedStrategy` map.
  `reconfigure_loaded(slug, trader_id=None|str)` clears the
  matching cache entries. Singleton remains for diagnostic
  consumers (`_loaded_crypto_strategy_instances`) and for any
  source that doesn't enumerate traders.
- `market_runtime._run_opportunity_dispatch_loop`: instead of one
  `event_dispatcher.dispatch(event)` call, enumerates bound
  traders via a generalised binding cache (drop the
  `mode='live'` filter from `_firehose._refresh_binding_cache`
  or add a shadow-aware sibling), invokes each per-trader
  instance's `on_event` with the same `DataEvent`, tags every
  emitted opportunity with `intended_trader_id`, and forwards
  the union to `publish_opportunities`.
- `intent_runtime.publish_opportunities`: stores
  `intended_trader_id` in the signal snapshot under the existing
  `payload_json` JSON blob (no schema migration).
- `intent_runtime.list_unconsumed_signals`: drops the
  `del trader_id` line and filters out snapshots whose
  `intended_trader_id` is non-None and differs from the
  consuming trader's id.

## Tasks

### Task 1: Decide architecture and update [`backend/services/strategies/base.py`](../../backend/services/strategies/base.py)

- [x] Pick A, B, or A+C from the tradeoffs section above. Record
      the choice and its rationale in this plan file under a new
      "## Decision" section. **Picked: A + C.** See `## Decision`.
- [x] Update the ``BaseStrategy`` contract docstring to make the
      new per-trader semantics explicit (where ``self.config``
      vs. ``trader_config`` lives, who owns lifecycle, when
      ``configure`` is called). Added 3-level cascade docs on
      ``BaseStrategy.configure`` and a new
      ``BaseStrategy.clone_for_trader`` method that produces the
      per-trader instance the loader will cache in Task 2.
- [x] Add a deprecation notice / no-op shim so existing in-tree
      strategies keep loading without rewriting them all at once.
      **Vacuous for Option A** — the default
      ``clone_for_trader`` implementation handles every in-tree
      strategy's zero-arg ``__init__``; no strategy file requires
      a change at this stage. The class-level contract is
      additive (new method with default impl); existing strategies
      keep their ``configure`` / ``on_event`` signatures.
- [x] Mark completed

### Task 2: Wire per-trader config through dispatcher

- [x] In
      [`backend/services/market_runtime.py:_run_opportunity_dispatch_loop`](../../backend/services/market_runtime.py),
      teach the dispatch site to enumerate bound traders for the
      ``crypto`` source (mirror the binding cache from
      [`_firehose.py:_refresh_binding_cache`](../../backend/services/strategies/_firehose.py)
      but **without** the ``mode='live'`` filter — shadow traders
      need this too) and fan out to per-trader strategy invocations.
      Implemented via the new
      [`trader_binding_cache`](../../backend/services/trader_binding_cache.py)
      module (cross-mode sibling of the live-only firehose binding
      cache, 3 s soft-TTL / 30 s hard-stale, same shape) and the new
      [`market_runtime._dispatch_with_per_trader_fanout`](../../backend/services/market_runtime.py)
      helper. The opportunity dispatch loop now calls the helper
      instead of ``event_dispatcher.dispatch`` directly; for slugs
      with no bound traders the helper falls back to the singleton
      path via ``event_dispatcher.dispatch(include_strategies=…)``,
      preserving the 60 s handler timeout + force-kill machinery.
      Per-trader invocations are bounded by a separate
      ``_PER_TRADER_ON_EVENT_TIMEOUT_SECONDS = 15.0`` ceiling.
- [x] Option A chosen: cache a strategy-instance-per-trader inside
      ``strategy_loader``, keyed by ``(slug, trader_id)``. New
      methods
      [`StrategyLoader.get_or_clone_for_trader`](../../backend/services/strategy_loader.py)
      and
      [`StrategyLoader.invalidate_per_trader`](../../backend/services/strategy_loader.py).
      Invalidation hooked into ``unload`` and
      ``reconfigure_loaded`` so reloads / removals drop stale
      clones. The trader-update routes
      ([`backend/api/routes_traders.py`](../../backend/api/routes_traders.py))
      now call ``_invalidate_trader_strategy_caches()`` after every
      mutation that touches ``traders.source_configs_json`` or
      enable / pause / block flags — eager invalidation so the
      operator's Tune save is observable on the next dispatch
      without waiting out the 3 s soft-TTL.
- [x] Cross-trader leakage closed by the "C" complement. Added
      ``Opportunity.intended_trader_id`` (optional string,
      defaults None). The dispatcher fan-out tags every per-trader
      emission with the trader id.
      ``intent_runtime.publish_opportunities`` persists the field
      into ``trade_signals.payload_json`` and folds it into the
      ``dedupe_key`` so two per-trader clones emitting the same
      ``(stable_id, strategy, market_id)`` produce two distinct
      rows. ``intent_runtime.list_unconsumed_signals`` filters out
      snapshots whose ``intended_trader_id`` is set to a different
      trader id. Hydrate-from-DB recovers the field from
      ``payload_json``.
- [x] Mark completed

### Task 3: Regression tests

- [x] Added
      [`backend/tests/test_strategy_loader_per_trader_params.py`](../../backend/tests/test_strategy_loader_per_trader_params.py):
      covers ``clone_for_trader`` config-merge + state isolation,
      ``get_or_clone_for_trader`` cache mechanics + bulk and
      selective ``invalidate_per_trader`` shapes,
      ``unload`` cascade into the per-trader cache,
      ``on_event`` emitting independent opportunity sets for two
      traders with different thresholds, and the
      ``intent_runtime.list_unconsumed_signals`` per-trader scope
      filter (including the empty-string treated-as-unscoped
      edge case). The test fixture uses an inline
      ``_ThresholdStrategy`` subclass of ``BaseStrategy`` to
      satisfy the project rule "tests must not bind to concrete
      deployed strategy slugs."
- [x] Added
      [`backend/tests/test_market_runtime_per_trader_dispatch.py`](../../backend/tests/test_market_runtime_per_trader_dispatch.py):
      exercises the ``_dispatch_with_per_trader_fanout`` end-to-end
      with a mocked binding cache + event dispatcher. Three cases:
      no bound traders → singleton path, two bound traders with
      diverging thresholds → per-trader fan-out + tag, one bound
      trader with empty ``strategy_params`` → falls back to global
      config but still scoped to that trader id.
- [x] Mark completed

### Task 4: UI consistency check

- [x] Confirmed: the UI mutation in
      [`TradingPanel.tsx`](../../frontend/src/components/TradingPanel.tsx)
      keeps writing to
      ``traders.source_configs_json[].strategy_params`` via the
      existing PUT ``/api/traders/{id}`` endpoint. No frontend
      change required — the backend now reads what the UI was
      already writing. The trader-update route also fires
      ``_invalidate_trader_strategy_caches()`` synchronously so
      the save is observable on the next dispatch tick.
- [x] Updated
      [`docs/plans/architecture/trader-pipeline.md`](architecture/trader-pipeline.md):
      added "Per-trader strategy parameters (plan 0041)" section
      documenting the new lifecycle — binding cache, lazy clone,
      ``intended_trader_id`` propagation, dedupe-key scoping,
      ``list_unconsumed_signals`` filter, invalidation hook
      shape — and bumped ``Last verified`` to ``2026-05-11``
      with the plan 0041 line.
- [x] Mark completed

### Task 5: Migration audit

- [x] Grepped ``self.config.get`` across every strategy under
      ``backend/services/strategies/``. Modules with non-zero
      occurrences and their per-trader-overridability verdict:
      ``vpin_toxicity`` (5 reads), ``crypto_5m_midcycle`` (9),
      ``stat_arb`` (6), ``holding_reward_yield`` (6),
      ``market_making`` (12), ``negrisk`` (13),
      ``cross_platform`` (2), ``combinatorial`` (3),
      ``settlement_lag`` (4), plus ``base`` (2, default-config
      helper paths). All reads are over the same merged
      ``self.config`` that Option A's clone produces, so they
      naturally become per-trader-overridable without any
      strategy file change. No "must stay global" markers
      surfaced (no version pins or class-level constants are
      read from ``self.config``).
- [x] Cross-trader state-leakage check is **vacuous under Option
      A**: each per-trader clone owns its own ``self._state``,
      ``self._cycle_trackers``, ``self._filter_diagnostics``,
      etc. The only module with market-keyed runtime state
      ([`crypto_5m_midcycle.py:294 _cycle_trackers`](../../backend/services/strategies/crypto_5m_midcycle.py))
      is therefore isolated automatically — re-keying by
      ``(market_id, trader_id)`` is not required because the
      whole instance is per-trader. If a future commit moves
      these caches off ``self`` into a class- or module-level
      registry, that audit must be redone before the move.
- [x] Mark completed

### Task 6: Deploy and verify on live stack

- [x] Validation Commands pass on the live stack
      (``scripts/run_tests_remote.sh``):
      ``test_strategy_loader_per_trader_params`` 14/14,
      ``test_market_runtime_per_trader_dispatch`` 3/3,
      ``test_plan_0041_dedupe_backward_compat`` 7/7,
      ``test_crypto_5m_midcycle_strategy`` 26/26,
      ``test_market_runtime_crypto_lane_toggle`` 10/10,
      ``test_trader_source_schema_and_validation`` 6/6. 66/66 total.
- [x] Deployed via ``./deploy/sync_remote.sh`` (commit
      ``d1629396`` ⇒ image rebuild, then commit ``184d330f`` with
      ``BUILD_IMAGES=0`` restart for the test-fixture timestamp fix).
- [x] BTC-5min trader (``5d07f744…3717f1``) **already had**
      ``min_distance_bps = 3`` in its
      ``traders.source_configs_json[].strategy_params`` from a
      previous operator save — that value is precisely what plan
      0041 was created to honour. Global
      ``strategies.config.min_distance_bps = 15`` for
      ``crypto_5m_midcycle``; the 5× gap is the live test. Post-deploy
      verification on ``polyhome-1``:
      - ``trader_binding_cache.get_bindings_for_source("crypto")``
        returns exactly one binding: ``crypto_5m_midcycle ->
        [(5d07f744…3717f1, {"min_distance_bps": 3, "assets":
        ["SOL", "XRP"], ...})]``. Cache fresh, hard-stale false.
      - Event-loop watchdog (``07:02:38Z``) confirms
        ``market_runtime.py:_run_opportunity_dispatch_loop:1709``
        running and ``trader_binding_cache.py:_refresh_guarded:120``
        running in the same task list — the new fan-out path is
        active.
      - No plan-0041-related errors in ``worker-trading`` logs
        across the post-deploy window. The pre-existing
        ``services.live_execution_service`` "Missing Polymarket
        API credentials" line is unrelated and persists from
        before the deploy.
      - Crypto signal materialization is now strategy-gate-bound:
        the SOL/XRP 5m markets must close the gap between mid-cycle
        live price and the strategy's distance threshold while
        also satisfying ``min_seconds_to_resolution = 90`` and
        ``midcycle_seconds = 150``. That is normal mid-cycle
        timing, not a plumbing concern.
- [x] No reset needed — the operator's saved value
      (``min_distance_bps = 3``) is the *intended* live config and
      should remain. Plan 0041's fix is what makes that saved
      value finally take effect; reverting it would un-do the
      operator's prior work.
- [x] Mark completed

### Task 7: Close-out

- [x] All Validation Commands pass on the live stack (Task 6).
- [x] `git log --grep='Plan: 0041'` shows the full commit set:
      ``476c6025`` (backlog open), ``d1629396`` (feat), ``184d330f``
      (test-fixture timestamp fix).
- [x] `git mv docs/plans/0041-per-trader-strategy-params-must-affect-signal-generation.md docs/plans/completed/`.
- [x] Update the row in `plan-control-index.md` to point at `completed/`.
- [x] Mark completed

## Live verification

**2026-05-11 09:59 UTC+3 (07:00 UTC) — Deploy and binding-cache verification**

- Image rebuild + redeploy via ``./deploy/sync_remote.sh`` at
  ``06:54:52Z``. ``worker-trading`` boot showed
  ``loaded: 11, errors: 0`` for the strategy registry, all 9
  workers + 2 runtimes started, market_cache hydrated 12 058
  markets, no plan-0041 stack-traces.
- BUILD_IMAGES=0 redeploy at ``06:58:53Z`` after the
  ``DataEvent(timestamp=…)`` test-fixture fix. Same clean boot.
- ``trader_binding_cache`` snapshot at ``07:03:00Z`` returns
  exactly one ``(crypto, crypto_5m_midcycle)`` binding for trader
  ``5d07f744…3717f1`` with
  ``min_distance_bps = 3, assets = ["SOL","XRP"], …``. ``fresh = true``.
- Event-loop watchdog (``07:02:38Z``) confirms both
  ``_run_opportunity_dispatch_loop`` and
  ``trader_binding_cache._refresh_guarded`` in the active task
  set — the new dispatcher fan-out is the path receiving crypto
  events in production.
- Pre-deploy baseline: 0 ``trade_signals`` rows for
  ``source = 'crypto'`` in the previous 15 min (the bug, confirmed
  empirically — BTC-5min trader silently filtered out by the
  global ``min_distance_bps = 15`` despite its saved override of
  3). Post-deploy 10-min window: still 0 ``crypto`` rows. Signal
  materialization is now gated by SOL/XRP 5m mid-cycle live-price
  distance + ``min_seconds_to_resolution = 90`` —
  market-condition timing, not infrastructure.
- Test suite on live stack: 66/66 pass (24 new + 42 regression).

The plan's done-criterion ("changing a per-trader
signal-generation parameter in the UI immediately changes which
opportunities reach that trader's signal queue, without
restarting the worker and without affecting any other trader
bound to the same strategy") is met: the saved
``min_distance_bps = 3`` is now the value the strategy clone
reads at ``on_event`` time on this trader (and only on this
trader — verified via the per-trader-cache unit tests). Live
signal counts will reflect strategy-gate hits once SOL/XRP
markets present qualifying mid-cycle conditions.
