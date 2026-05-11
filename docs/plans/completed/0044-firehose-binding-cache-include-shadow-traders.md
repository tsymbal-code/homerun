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

- [x] In [`backend/services/strategies/_firehose.py`](../../backend/services/strategies/_firehose.py),
      removed the `if mode_lower != "live": continue` block. Added a
      cross-mode contract comment pointing at this plan (commit
      `6b50ee83`).
- [x] Updated the module docstring with the cross-mode contract.

### Task 2: Regression test

- [x] Added `backend/tests/test_firehose_binding_cache.py` (5 tests,
      commit `6b50ee83` + fix `5a118908`):
      - `test_refresh_includes_shadow_traders`
      - `test_refresh_skips_disabled_traders`
      - `test_refresh_skips_per_strategy_disabled_in_source_configs`
      - `test_emit_should_fire_returns_true_for_shadow_only_binding`
      - `test_emit_should_fire_returns_false_when_orchestrator_disabled`
      5/5 passing on the worker-trading container.

### Task 3: Temporary diagnostic in `crypto_5m_midcycle._emit_reject`

- [x] Added MURMUR/VOICE-only `logger.info("crypto_5m_midcycle gate
      reject", …)` inside `_emit_reject` (commit `6b50ee83`).
- [x] WHISPER-tier rejections (timeframe / asset / milestone) do
      not trigger the log — verified in live logs.

### Task 4: Deploy and verify

- [x] Validation commands ran on `worker-trading` container post-
      deploy (5/5 firehose tests green).
- [x] Deployed via `./deploy/sync_remote.sh` (containers healthy).
- [x] Live verification on `polyhome-1`: `firehose_evaluation` rows
      now land for the BTC - 5min shadow trader. The diagnostic log
      identified ``book_depth`` as the blocker gate, escalated into
      [Plan 0045](0045-diagnose-ws-cache-empty-for-subscribed-crypto-tokens.md).

### Task 5: Remove the temporary log (after verification)

- [ ] **Pending.** The `crypto_5m_midcycle gate reject` info log is
      still in code as of plan-close; tracked together with the
      other Plan 0044 / 0045 diagnostic logs in Plan 0045 Task 6.
      Target removal: within 7 days of Plan 0045 close, once the
      persistent firehose path has been confirmed stable.

## Live verification

Observed at 2026-05-11 08:38–09:07 UTC on the `BTC - 5min` shadow
trader on `polyhome-1`:

- Pre-fix (same trader, same shadow mode): zero
  `firehose_evaluation` rows over the prior session.
- Post-fix: 7991 `firehose_evaluation` rows over 10 minutes
  immediately after deploy (whisper-tier ~10044 / 5 min, murmur-
  tier 2 / 5 min). The MURMUR firehose payloads carried the full
  gate-by-gate breakdown, immediately surfacing `book_depth:
  passed=false` as the blocker for the BTC - 5min trader. Without
  Plan 0044 this would have stayed silent.

Verdict: **firehose telemetry now flows for shadow traders.**
Plan 0044 is complete; the live observability it unlocked drove
the Plan 0045 root-cause diagnosis. Move to `completed/`.
