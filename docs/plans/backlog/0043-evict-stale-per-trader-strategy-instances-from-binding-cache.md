# Plan: Evict stale per-trader strategy instances on binding-cache refresh

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](../README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](../completed/) on close. Every commit produced by
> this plan carries a `Plan: 0043` git trailer (see
> [Commits and traceability](../README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](../plan-control-index.md).
>
> **Status: BACKLOG.** Activate when one of:
> (a) the active trader count on `polyhome-1` stabilizes above 50,
>     OR
> (b) `worker-trading` RSS grows monotonically (≥ 50 MB/h) for 24 h
>     with no explicit roster expansion (new traders / new strategies
>     loaded), OR
> (c) an operational incident shows that a deleted-trader row left
>     a stale ``(slug, trader_id)`` entry in ``_per_trader`` that
>     held a strategy instance + its caches alive past its useful
>     lifetime.

## Overview

[Plan 0041](../completed/0041-per-trader-strategy-params-must-affect-signal-generation.md)
introduced ``strategy_loader._per_trader``, a
``dict[tuple[str, str], LoadedStrategy]`` keyed by
``(slug, trader_id)`` that caches one strategy clone per
(strategy, trader) pair. Entries are populated lazily by
``get_or_clone_for_trader`` and dropped only by explicit
``invalidate_per_trader(slug=, trader_id=)`` calls wired into
``routes_traders`` mutation endpoints (create / update / delete /
start / stop / activate / deactivate / block_new_orders /
from-template).

If a trader row is removed via any path that **bypasses** those
endpoints — raw SQL, a race with a missed endpoint, a future
background mutation, an alembic migration that wipes a column —
the corresponding entry in ``_per_trader`` stays alive forever,
holding a ``LoadedStrategy`` instance and whatever in-process
state the strategy accumulated (e.g.
``crypto_5m_midcycle._cycle_trackers``,
``btc_eth_convergence._filter_diagnostics``).

The
[`backend/services/trader_binding_cache.py`](../../../backend/services/trader_binding_cache.py)
module already knows the authoritative live set of
``(slug, trader_id)`` pairs — it queries the ``traders`` table on
every refresh (3 s soft-TTL, 30 s hard-stale). Piggybacking the
eviction on that refresh keeps the leak surface bounded without
adding a separate sweeper task to the already-CPU-bound
``worker-trading`` process.

**Done means:** after a trader row disappears from the active set
(by any means — API mutation, raw SQL, or restart-time roster
diff), the next successful binding-cache refresh removes its
``(slug, trader_id)`` entries from ``_per_trader``. A regression
test exercises the eviction directly: seed the cache with two
trader keys, call ``evict_unbound_per_trader`` with a live set
containing only one of them, assert the other is dropped and the
strategy's clone is no longer reachable through the cache.

## Context / References

- [`backend/services/strategy_loader.py:680-689`](../../../backend/services/strategy_loader.py)
  — ``_per_trader`` declaration + the inline KNOWN-LEAK comment
  that pinpoints this plan.
- [`backend/services/strategy_loader.py:926-955`](../../../backend/services/strategy_loader.py)
  — existing ``invalidate_per_trader`` signature; the new
  ``evict_unbound_per_trader`` should follow the same return-int
  observability convention.
- [`backend/services/trader_binding_cache.py`](../../../backend/services/trader_binding_cache.py)
  — refresh path that will trigger the eviction.
- [`backend/api/routes_traders.py:_invalidate_trader_strategy_caches`](../../../backend/api/routes_traders.py)
  — the existing per-mutation invalidation hooks (these stay; the
  binding-cache eviction is a belt-and-suspenders second layer).
- [Plan 0041 audit (in conversation)](0041-per-trader-strategy-params-must-affect-signal-generation.md)
  — the audit flagged this as risk #1 (memory leak); the plan was
  not promoted because at single-digit trader counts the leak is
  negligible.

## Validation Commands

- `docker compose exec backend pytest -q tests/test_strategy_loader_per_trader_params.py::test_evict_unbound_per_trader_drops_stale_keys`
- `docker compose exec backend pytest -q tests/test_strategy_loader_per_trader_params.py`
  (regression — existing 14 tests must keep passing)
- `docker compose exec backend pytest -q tests/test_trader_binding_cache.py`
  (if a separate test file exists for the binding cache; otherwise
  inline the eviction-triggered-by-refresh assertion in the loader
  test)
- `docker compose exec backend ruff check backend/services/strategy_loader.py backend/services/trader_binding_cache.py`

### Task 1: Add `evict_unbound_per_trader` to strategy_loader

- [ ] In
      [`backend/services/strategy_loader.py`](../../../backend/services/strategy_loader.py),
      add a new public method:
      ```python
      def evict_unbound_per_trader(
          self,
          live_keys: set[tuple[str, str]],
      ) -> int:
          """Drop _per_trader entries whose (slug, trader_id) is
          not in the caller-supplied live set. Returns the eviction
          count for observability. Caller is expected to be the
          trader_binding_cache refresh path."""
      ```
- [ ] Take the loader-level lock around the dict mutation so
      concurrent ``get_or_clone_for_trader`` calls don't observe a
      half-evicted state.
- [ ] Update the inline KNOWN-LEAK comment above
      ``_per_trader`` to point at this method as the resolution.

### Task 2: Wire eviction into binding-cache refresh

- [ ] In
      [`backend/services/trader_binding_cache.py`](../../../backend/services/trader_binding_cache.py),
      at the tail of the successful-refresh path (after the new
      bindings map is committed to the module-level globals), build
      ``live_keys = {(slug, trader_id) for slug, traders in
      new_bindings.items() for trader_id, _params in traders}`` and
      call ``strategy_loader.evict_unbound_per_trader(live_keys)``.
- [ ] **Skip eviction on hard-stale / failed refresh.** If the DB
      query raised, the bindings are unchanged — evicting against a
      pre-existing (possibly empty) live set would wipe everything
      and force a flood of re-clones on the next dispatch. Guard
      with a "did this refresh succeed?" check.
- [ ] Log the eviction count at INFO when it's > 0 so operators
      can see in real-time when a trader removal propagates.

### Task 3: Regression tests

- [ ] Add ``test_evict_unbound_per_trader_drops_stale_keys`` to
      [`backend/tests/test_strategy_loader_per_trader_params.py`](../../../backend/tests/test_strategy_loader_per_trader_params.py):
      seed two ``(slug, trader_id)`` entries, call
      ``evict_unbound_per_trader({first_key_only})``, assert the
      other is gone and ``get_or_clone_for_trader`` for it returns
      a fresh clone rather than the evicted one.
- [ ] Add ``test_evict_unbound_per_trader_returns_count`` —
      eviction count matches the number of dropped entries.
- [ ] Add ``test_evict_unbound_per_trader_empty_live_set_drops_all``
      — guard against the operator passing ``set()`` by accident
      AND verify the binding-cache callsite never passes an empty
      set on a failed-refresh path.
- [ ] Add a binding-cache integration test (or a unit test against
      a stubbed loader): a refresh that finds a trader removed
      from the DB results in a single
      ``evict_unbound_per_trader`` call with the surviving traders'
      keys. A refresh that **fails** (DB exception) results in
      **zero** ``evict_unbound_per_trader`` calls.

### Task 4: Deploy and verify

- [ ] Run validation commands locally (in worker-trading container
      via ``docker cp tests/`` workaround if needed).
- [ ] ``./deploy/sync_remote.sh`` to ship.
- [ ] On ``polyhome-1``: with the current shadow ``BTC - 5min``
      trader, capture ``_per_trader`` size via a one-shot
      ``docker compose exec backend python -c "from
      services.strategy_loader import strategy_loader; print(len
      (strategy_loader._per_trader))"`` baseline. Then create a
      throwaway second shadow trader, observe size grows, delete
      it via API, observe size drops within one binding-cache
      refresh cycle (≤ 3 s).
- [ ] Record the before / after counts and the eviction-log line
      in this plan file under "## Live verification". Move to
      ``docs/plans/completed/``.

## Architectural tradeoff (why not LRU)

A classic LRU with a fixed cap (e.g. 1000 entries) was considered
and rejected: it bounds memory regardless of trader churn, but
it forces a costly re-clone on the next dispatch every time the
cap evicts a still-active trader. The binding-cache approach
evicts **only** entries that no longer correspond to a live
trader, so the steady-state re-clone count stays zero. If trader
counts climb into the thousands, layering an LRU on top of the
binding-cache eviction becomes worth re-evaluating — but that's
a separate follow-up plan, not this one.
