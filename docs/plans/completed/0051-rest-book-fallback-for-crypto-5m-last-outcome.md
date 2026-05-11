# Plan: REST book-fallback for crypto_5m_last_outcome

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0051` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

`crypto_5m_last_outcome` currently emits roughly every other 5 m
cycle. Diagnostics from `trader_events.firehose_evaluation` show the
remaining cycles fail at the `book_depth` gate: for the entire
~4.5 min retry window the WS-fed `PriceCache` returns `None` for the
side the strategy wants to buy (NO or YES, both observed). All three
configured assets (BTC / SOL / XRP) fail-and-pass in lockstep, which
points at a systemic Polymarket WS publication delay rather than
per-asset liquidity asymmetry.

`FeedManager.get_order_book(token_id)` already implements REST fallback
on top of the WS cache: when `cache.is_fresh(token_id)` is `False` it
calls `polymarket_client.get_order_book(token_id)` via
`_build_polymarket_http_fallback_order_book`, then writes the result
back into `PriceCache` ([ws_feeds.py:2161-2170](../../backend/services/ws_feeds.py),
[ws_feeds.py:2247-2264](../../backend/services/ws_feeds.py)). The
problem is that `StrategySDK.get_order_book_depth` reads
`cache.get_order_book(token_id)` directly
([strategy_sdk.py:2861](../../backend/services/strategy_sdk.py)) and
never goes through the async fallback path, so the WS-empty cycles
never get a chance to populate.

This plan wires `crypto_5m_last_outcome` to fire a fire-and-forget
`feed_mgr.get_order_book(token_id)` whenever its own `book_depth` gate
rejects with cache-miss. That call runs the existing REST fallback,
populates the cache, and the next retry tick (≈ 150 ms later, since
on_event fires ~6 Hz) finds a fresh book via the normal sync path.

Scope is **deliberately limited to this one strategy**. Other crypto
strategies (`midcycle`, `convergence`, `directional_edge`,
`maker_quote`, `entropy_maker`, `spike_reversion`) keep the current
sync-only path so their behaviour does not change as a side effect.
Once we have live evidence the fallback is safe and emit rate is
healthy, the same pattern can be lifted into `StrategySDK` itself in
a separate plan.

"Done" = bot emits on **≥ 90% of 5 m cycles per asset** over a
30-minute live window after deploy, vs the ~50% baseline observed
2026-05-11 between 15:30 and 16:00 UTC.

## Context / References

- [Strategy: Crypto 5m Last-Outcome Follow](../strategies/crypto-5m-last-outcome.md) —
  user-facing description, includes the retry-within-cycle behaviour
  introduced in Plan 0047.
- [Plan 0047: Crypto 5m last-outcome-follow strategy](completed/0047-crypto-5m-last-outcome-follow-strategy.md) —
  shipped the base strategy and the retry-within-cycle fix.
- [Plan 0045: WS cache empty for subscribed crypto tokens](completed/0045-diagnose-ws-cache-empty-for-subscribed-crypto-tokens.md) —
  prior investigation of the same WS publication delay.
- [backend/services/strategies/crypto_5m_last_outcome.py](../../backend/services/strategies/crypto_5m_last_outcome.py)
- [backend/services/strategy_sdk.py:2822](../../backend/services/strategy_sdk.py) —
  `get_order_book_depth` is the synchronous cache read we're bypassing
  for the prime-and-retry path.
- [backend/services/ws_feeds.py:2161](../../backend/services/ws_feeds.py) —
  `FeedManager.get_order_book` already has REST fallback.
- [backend/services/ws_feeds.py:1562](../../backend/services/ws_feeds.py) —
  `_build_polymarket_http_fallback_order_book` (already registered
  on the default FeedManager via `set_http_fallback`).
- [backend/services/polymarket.py:1959](../../backend/services/polymarket.py) —
  `polymarket_client.get_order_book(token_id)` — the actual HTTP
  fetch the fallback wraps.

## Validation Commands

- `docker compose exec backend pytest -q backend/tests/test_crypto_5m_last_outcome_strategy.py`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose run --rm --no-deps -v /home/polyhome/homerun/backend:/app/backend:ro backend python -m pytest -q tests/test_crypto_5m_last_outcome_strategy.py'`
- `ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c \"SELECT (payload_json->'market'->>'asset') AS asset, COUNT(*) FROM trader_events WHERE event_type='firehose_emit' AND payload_json::text LIKE '%crypto_5m_last_outcome%' AND created_at > NOW() - INTERVAL '30 minutes' GROUP BY asset ORDER BY asset;\""`

## Out of scope

- No change to `StrategySDK.get_order_book_depth` itself. We only
  call `feed_mgr.get_order_book` from inside the strategy as a
  cache-priming side effect. A general SDK-level fallback method
  would benefit every strategy but requires API-shape and migration
  thought beyond this plan.
- No change to the other crypto strategies. They keep failing
  silently on cache-miss exactly as today; their emit rate is not
  in scope.
- No CRITICAL-tier knob touch. The new `rest_book_fallback_enabled`
  flag is MEDIUM-tier per-strategy and ships default `true`; rollback
  is a single UI toggle or one-line SQL `UPDATE`.
- No `polymarket_client.get_order_book` rate-limit tuning. The
  client already has its own limiter; our per-token cooldown is a
  belt-and-suspenders layer on top.

### Task 1: Add per-token REST-prime helper to the strategy

- [x] In `backend/services/strategies/crypto_5m_last_outcome.py`,
  introduce `self._rest_prime_last_ts: dict[str, float]` in
  `__init__` — maps `token_id → unix-seconds of last REST prime
  call`. Stored on the global strategy instance, never expired
  (entries fall out of relevance naturally as old token_ids stop
  appearing).
- [x] Add a `_REST_PRIME_COOLDOWN_S = 3.0` module constant. With
  on_event firing ~6 Hz that caps REST calls per token at ~20 per
  minute even in the worst case — well under the
  `polymarket_client` per-IP limit and under the natural ~30 calls
  in a 90 s book-empty window.
- [x] Add a static `_prime_book_via_rest(token_id: str)` helper:
  - Look up the global `FeedManager` via `get_feed_manager()`.
  - Fire-and-forget: schedule via `loop.create_task(feed_mgr.get_order_book(token_id))`
    after probing for a running event loop with `asyncio.get_running_loop()`.
    (`ensure_future` was the original sketch; switched to
    `get_running_loop` + `create_task` to avoid the
    Python 3.12+ deprecation warning when the helper is called
    from sync test code with no loop bound — the helper becomes a
    silent no-op in that case.)
  - Wrap in `try/except` and `logger.debug` on failure — never
    raise from this helper, since on_event must always complete.
  - The cooldown bookkeeping is done by the **caller** (Task 2)
    to keep this helper testable in isolation.
- [x] Mark completed

### Task 2: Wire the prime call into the `book_depth` reject path

- [x] In `_evaluate_market`, after the existing `book_depth` gate
  rejection branch (`if not depth_present: _emit_reject(MURMUR); return None`),
  prepend a single cooldown-gated call to `_prime_book_via_rest`:
  ```python
  if not depth_present:
      if self.config.get("rest_book_fallback_enabled", True):
          now_s = now_ms / 1000.0
          last = self._rest_prime_last_ts.get(token_id, 0.0)
          if now_s - last >= _REST_PRIME_COOLDOWN_S:
              self._rest_prime_last_ts[token_id] = now_s
              self._prime_book_via_rest(token_id)
      _emit_reject(MURMUR)
      return None
  ```
- [x] `token_id` here is the side-specific CLOB token id resolved
  earlier in the gate chain (`typed_market.clob_token_ids[0 if side
  == "YES" else 1]`). Lift it out of the existing
  `_last_outcome_context` payload-building section so it's
  available at the book_depth gate, not just at the create_opportunity
  step.
- [x] Mark completed

### Task 3: Surface the toggle in config + DB seed

- [x] In `DEFAULT_CONFIG`, add `"rest_book_fallback_enabled": True`.
- [x] In `crypto_5m_last_outcome_config_schema()`, add a corresponding
  `param_fields` entry (boolean, phase `"execution"`, default
  `True`) so the operator can flip it in the strategy-manager UI.
- [x] In `backend/services/opportunity_strategy_catalog.py`, add
  the same field to the inline `config_schema` for the
  `crypto_5m_last_outcome` seed entry (sort_order 195).
- [x] **Operator runbook**: deploying this plan via
  `./deploy/sync_remote.sh` is **not enough** by itself — the live
  `strategies.source_code` and `strategies.config` rows are seeded
  create-only ([Plan 0047 lesson](completed/0047-crypto-5m-last-outcome-follow-strategy.md)).
  Plan 0050 (auto-resync on boot) makes the source-code refresh
  automatic when the disk md5 differs, **and that auto-resync
  resets `config` back to the seed default** because
  `reset_strategy_to_factory` is the underlying primitive — so
  the operator's per-trader asset list (e.g. `["BTC","SOL","XRP"]`)
  is also wiped at boot. The operator must therefore re-apply the
  asset list via SQL `UPDATE` on `strategies.config` after every
  deploy that ships a strategy code change. Recipe lives in
  Task 5.
- [x] Mark completed

### Task 4: Tests

- [x] In `backend/tests/test_crypto_5m_last_outcome_strategy.py`,
  add the following cases (one test per behaviour):
  - `test_book_depth_miss_fires_rest_prime_within_cooldown` —
    monkeypatch `_prime_book_via_rest` to a counter; first tick
    with no book seeded fires the prime once; second tick within
    cooldown does **not** fire again; tick after cooldown fires
    again.
  - `test_rest_prime_disabled_when_flag_off` — set
    `rest_book_fallback_enabled=False`; book_depth fails as before
    but no prime is fired.
  - `test_rest_prime_uses_correct_side_token` — for
    `previous_outcome="NO"` the prime helper receives the NO token
    id, not the YES; symmetric assertion for `"YES"`.
  - `test_rest_prime_helper_swallows_exceptions` — direct call
    to the static helper with `services.ws_feeds.get_feed_manager`
    monkey-patched to raise; assert no exception escapes.
    **Plan deviation:** the original plan asked to assert
    `_evaluate_market` returns `None` after the patch, but
    `StrategySDK.get_order_book_depth` also calls
    `get_feed_manager()` (uncaught), so a module-wide patch
    explodes the gate before the helper is ever reached. Testing
    the helper in isolation is the testable form of "swallows
    exceptions" and matches Task 1's intent
    ("keep this helper testable in isolation").
- [x] The existing 20 tests must still pass without modification
  (the new flag defaults to `True` but the test fixtures don't
  rely on the REST-prime side effect because they seed the book
  directly). 23 pre-existing + 4 new + 1 unaffected pre-existing
  in this file = 24 green on
  `pytest -q tests/test_crypto_5m_last_outcome_strategy.py` against
  the live polyhome-1 backend image.
- [x] Mark completed

### Task 5: Live verification on polyhome-1

- [x] Run `./deploy/sync_remote.sh` from the local checkout to
  rsync the code and rebuild the worker image. Done 2026-05-11
  16:35 UTC; all containers healthy on the next compose status.
- [x] After deploy, fetch the strategy id and call
  `POST /api/strategy-manager/{strategy_id}/reset-to-factory` to
  refresh `strategies.source_code` and `strategies.config_schema`
  from the new on-disk file. **Not needed manually** — Plan 0050
  (auto-resync on boot) ran during the post-deploy startup of
  `worker-trading` and rewrote `source_code` automatically.
  Strategy version bumped 2 → 3, and the new
  `rest_book_fallback_enabled: true` default landed in `config`
  via the `reset_strategy_to_factory` primitive that 0050's
  resync calls under the hood.
- [x] Re-apply the operator's asset list via SQL:
  ```
  UPDATE strategies
  SET config = '{"assets":["BTC","SOL","XRP"],
                 "max_entry_price":0.95,
                 "min_entry_price":0.05,
                 "bet_size_usd":15.0,
                 "entry_seconds_after_start":30.0,
                 "rest_book_fallback_enabled":true,
                 "enabled":true}'::json,
      updated_at = NOW()
  WHERE slug='crypto_5m_last_outcome';
  ```
  Required because Plan 0050's auto-resync hard-resets `config`
  back to seed defaults (BTC only). Operator's
  `["BTC","SOL","XRP"]` was wiped on boot and had to be
  re-applied.
- [x] `docker compose restart worker-trading` to reload the
  strategy from the refreshed DB row.
- [x] Wait 30 minutes (6 full 5 m cycles per asset × 3 assets = 18
  potential emits) and run the post-deploy emit-rate query in
  `## Validation Commands`. Pass criterion: each asset's count is
  **≥ 5 / 6 cycles** (≥ 83%); ideally 6/6 once REST fallback
  stabilises. **Result 2026-05-11 17:08 UTC over the 30 min
  window after re-apply: BTC 6/6, SOL 6/6, XRP 6/6 — 100%
  emit rate, every asset.** Total 18 / 18 cycles emitted vs
  ~9 / 18 baseline. Pass criterion exceeded.
- [x] If emit rate fails to improve, capture
  `trader_events.firehose_evaluation` breakdown by last gate name
  for the 30 m window and attach to the plan close notes — that
  tells us whether the prime calls didn't fire, fired but REST
  also returned no book, or fired but the cooldown was too long.
  Not triggered (emit rate hit 100%); reference breakdown for the
  same 30 min window: `entry_milestone rejected = 21214`,
  `previous_outcome_known rejected = 574`,
  `book_depth rejected = 31`, `vwap_in_range emitted = 18`. The
  31 `book_depth` rejections are the cache-miss ticks that fired
  the REST prime; every cycle then passed within the same window
  — exactly the prime-and-retry pattern the plan designed for.
- [x] Mark completed

### Task 6: Doc + close-out

- [x] Update `docs/strategies/crypto-5m-last-outcome.md`:
  - Add the new `rest_book_fallback_enabled` knob to the defaults
    table.
  - Replace the "Stale book" bullet in the "Коли НЕ працює"
    section with a note that the strategy now self-heals
    cache-miss via REST prime, citing this plan.
  - Add a one-line cross-reference at the bottom under
    "Посилання".
- [x] Add a paragraph to
  `docs/plans/architecture/crypto-fast-binary-lane.md` describing
  the prime-on-miss pattern under a new sub-section
  "REST cache-prime hook (Plan 0051)" — short, since the
  authoritative fallback machinery already lives in
  `FeedManager._http_fallback`. `Last verified` bumped to
  2026-05-11.
- [x] Move this plan file to `docs/plans/completed/` and update
  the row in `plan-control-index.md`.
- [x] Mark completed
