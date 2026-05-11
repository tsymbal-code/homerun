# Plan: Firehose binding cache must include shadow traders

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0044` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

The firehose telemetry layer
([`backend/services/strategies/_firehose.py`](../../backend/services/strategies/_firehose.py))
buffers per-gate `firehose_evaluation` / `firehose_emit` events for
the strategy-decision UI. It is the **only** instrumentation that
records, per market, which gates a strategy passed or rejected on. A
plan-0041 live verification on the `BTC - 5min` shadow trader showed
zero `firehose_evaluation` rows in `trader_events` despite the
strategy being actively invoked every dispatch.

Root cause: `_refresh_binding_cache` at
[`_firehose.py:134-136`](../../backend/services/strategies/_firehose.py)
hard-filters the trader set to ``mode == "live"``:

```python
mode_lower = str(getattr(trader, "mode", "") or "").strip().lower()
if mode_lower != "live":
    continue
```

`_strategy_to_trader_ids` therefore never lists shadow-mode bindings.
Downstream, `_emit_should_fire(strategy_slug)` returns
``(False, [])`` for every shadow-bound strategy, and the
`emit_evaluation_nowait` / `emit_emit_nowait` calls scattered through
every crypto strategy drop their payloads silently
([`_firehose.py:210-224`](../../backend/services/strategies/_firehose.py)).

Result: a shadow operator iterating on a new strategy has **zero
visibility** into why opportunities aren't emitting. Every Plan 0041
audit step on the BTC - 5min trader hit this wall.

The fix is to drop the mode filter. The trader-binding cache
introduced for Plan 0041
([`trader_binding_cache.py`](../../backend/services/trader_binding_cache.py))
already does exactly this — it ingests both live and shadow traders
so the dispatcher can fan out per-trader signals to either tier. The
firehose cache should mirror that semantic. Keeping the two caches in
sync (one cross-mode for dispatch, one live-only for telemetry) is the
state matrix the binding-cache module docstring warned against in its
prologue.

**Done means:**
- A shadow trader configured with a strategy binding shows up in
  `firehose._strategy_to_trader_ids` and its rejections produce
  `firehose_evaluation` rows in `trader_events`.
- A live trader's existing telemetry behavior is unchanged.
- Regression test in
  [`backend/tests/test_firehose_binding_cache.py`](../../backend/tests/test_firehose_binding_cache.py)
  (new file) seeds two traders — one live, one shadow — both bound to
  the same strategy, refreshes the cache, asserts both are in the
  binding map.

## Context / References

- [`backend/services/strategies/_firehose.py:107-158`](../../backend/services/strategies/_firehose.py)
  — `_refresh_binding_cache` — site of the filter.
- [`backend/services/strategies/_firehose.py:210-224`](../../backend/services/strategies/_firehose.py)
  — `_emit_should_fire` — downstream consumer that drops events when
  the cache excludes a trader.
- [`backend/services/trader_binding_cache.py:55-110`](../../backend/services/trader_binding_cache.py)
  — Plan 0041 binding cache for dispatch. Already cross-mode. The
  module prologue explicitly warns about state matrix divergence.
- [`backend/services/strategies/crypto_5m_midcycle.py:364-371`](../../backend/services/strategies/crypto_5m_midcycle.py)
  — example `_emit_reject` site that is silenced for shadow traders.
- [Plan 0041](completed/0041-per-trader-strategy-params-must-affect-signal-generation.md)
  — introduced cross-mode binding for dispatch but did not touch
  the firehose cache.

## Validation Commands

- `docker compose exec backend pytest -q tests/test_firehose_binding_cache.py`
- `docker compose exec backend pytest -q tests/test_strategy_loader_per_trader_params.py`
  (regression — must keep passing)
- `docker compose exec backend ruff check backend/services/strategies/_firehose.py backend/tests/test_firehose_binding_cache.py`

### Task 1: Drop the mode filter

- [ ] In [`backend/services/strategies/_firehose.py`](../../backend/services/strategies/_firehose.py),
      remove the `if mode_lower != "live": continue` block at lines
      134-136. Add a one-line comment pointing at this plan and
      explaining the cross-mode contract (mirror the comment style
      used in [`trader_binding_cache.py`](../../backend/services/trader_binding_cache.py)).
- [ ] Update the module docstring at the top of `_firehose.py` to
      replace any "only live traders" language with the cross-mode
      contract.

### Task 2: Regression test

- [ ] Add `backend/tests/test_firehose_binding_cache.py` with at
      least:
      - `test_refresh_includes_shadow_traders` — seeds one live +
        one shadow trader bound to the same strategy, refreshes the
        cache, asserts both trader_ids are in
        `_strategy_to_trader_ids[slug]`.
      - `test_refresh_skips_disabled_traders` — a trader with
        `is_enabled=False` is excluded regardless of mode (this
        guards against accidentally widening the filter too much).
      - `test_refresh_skips_disabled_strategy_in_source_configs` —
        an enabled trader with `strategy_params.enabled=False` for
        a slug is excluded for that slug only.
      - `test_emit_should_fire_returns_true_for_shadow_trader` —
        end-to-end via `_emit_should_fire(slug)` returns
        `(True, [shadow_trader_id])` after refresh.

### Task 3: Temporary diagnostic in `crypto_5m_midcycle._emit_reject`

- [ ] Add a `logger.info` call inside `_emit_reject` in
      [`backend/services/strategies/crypto_5m_midcycle.py`](../../backend/services/strategies/crypto_5m_midcycle.py)
      that fires ONLY at `MURMUR` / `VOICE` verbosity (the post-
      milestone gates — bounded volume, ~1-2 lines per market per
      cycle). Format: include `slug`, `asset`, and the
      `(name, passed, score, detail)` tuples for every gate
      collected so far. Mark the block with a `# Plan 0044 follow-up`
      comment and a note to remove it after one week of live
      `firehose_evaluation` traffic confirms the same data is
      visible via the persistent path.
- [ ] Confirm no `WHISPER`-tier rejections (timeframe / asset /
      milestone gates) trigger the log — those fire on every market
      every tick and would drown the log stream.

### Task 4: Deploy and verify

- [ ] Run validation commands locally on the worker-trading
      container (via `docker cp tests/` workaround per repo norm).
- [ ] `./deploy/sync_remote.sh` to ship.
- [ ] On `polyhome-1`: keep the `BTC - 5min` trader in shadow.
      Observe over the next 5-min cycle:
      - `SELECT count(*) FROM trader_events WHERE event_type='firehose_evaluation' AND created_at > NOW() - INTERVAL '10 minutes';` → > 0.
      - `docker compose logs --since=5m worker-trading | grep 'crypto_5m_midcycle gate reject'` → entries showing
        which gate failed for SOL and XRP 5m markets.
- [ ] Record the verdict and the **identified blocker gate** in a
      "## Live verification" section in this plan file. Move to
      `docs/plans/completed/`.

### Task 5: Remove the temporary log (after verification)

- [ ] Once Task 4 confirms `firehose_evaluation` events flow for
      shadow traders, file a follow-up commit removing the
      Task 3 `logger.info` (with `Plan: 0044` trailer noting the
      cleanup). Target window: within 7 days of Task 4 completion.
