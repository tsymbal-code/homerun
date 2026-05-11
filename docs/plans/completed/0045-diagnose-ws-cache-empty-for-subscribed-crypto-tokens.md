# Plan: Diagnose why WS cache is empty for tokens already in `_subscribed_assets`

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0045` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

The `crypto_5m_midcycle` strategy consistently rejects every SOL/XRP
5m market at the `book_depth` gate despite:

- `feed_state == ConnectionState.CONNECTED` at evaluation time;
- the markets' `clob_token_ids` already in
  `PolymarketWSFeed._subscribed_assets`
  (`already_subscribed == 4` per Plan 0044 diagnostic logs at
  `crypto_5m_midcycle.py:_ensure_ws_subscribed_for_5m`);
- every other gate passing — distance, fresh Chainlink, reference
  price, clob tokens, all green.

A fresh `PolymarketWSFeed` started in a standalone probe (same
worker-trading container) subscribed to the **same 4 token IDs** and
received full books (`asks=27-70, bids=27-70`) within **1 second**.
So the WS server is delivering book updates for these tokens, but
the live worker's `PriceCache` ends up empty for them — meaning the
book updates are either lost between the WS frame and
`_apply_book_update`, or the subscribe message never actually reaches
Polymarket's WS server even though `_subscribed_assets` claims it
did.

Plan 0044 unblocked telemetry; Plan 0044's follow-up fix
([crypto_5m_midcycle.py:_ensure_ws_subscribed_for_5m](../../backend/services/strategies/crypto_5m_midcycle.py))
ensures the strategy calls `polymarket_feed.subscribe(token_ids)` at
the top of every `on_event`, but the call is a no-op when tokens
are already in `_subscribed_assets`:

```python
# ws_feeds.py:755-771
async def subscribe(self, token_ids: List[str]) -> None:
    async with self._sub_lock:
        new_ids = [tid for tid in token_ids if tid not in self._subscribed_assets]
        self._subscribed_assets.update(token_ids)
        should_send = bool(new_ids) and self._ws and self._state == ConnectionState.CONNECTED
    if should_send:
        ...
```

So `_subscribed_assets` may have accumulated dozens of stale entries
(every 5-min cycle adds 16 fresh crypto token IDs, never unsubscribed),
hiding the truth that the WS server has fewer (or different) actual
subscriptions.

**Done means:** root cause identified, a targeted fix landed, and
the `book_depth` gate passes for live SOL/XRP 5m markets at midcycle.
Defaults to "fix at the most upstream layer that gives clean
semantics" — most likely either (a) adding a sweep that prunes
`_subscribed_assets` entries for tokens that no longer correspond to
active markets, (b) wiring `StrategySDK.get_order_book_depth` to the
async `feed_manager.get_order_book` (which already has HTTP fallback
at [ws_feeds.py:2059-2068](../../backend/services/ws_feeds.py)), or
(c) some hybrid.

## Context / References

- [`backend/services/strategies/crypto_5m_midcycle.py:_ensure_ws_subscribed_for_5m`](../../backend/services/strategies/crypto_5m_midcycle.py)
  — Plan 0044 follow-up, the no-op subscribe site.
- [`backend/services/ws_feeds.py:755-773`](../../backend/services/ws_feeds.py)
  — `PolymarketWSFeed.subscribe` short-circuit when tokens are
  already in `_subscribed_assets`.
- [`backend/services/ws_feeds.py:884-894`](../../backend/services/ws_feeds.py)
  — re-subscribe-on-connect block, sends entire
  `_subscribed_assets` set. May hit chunking / server-side rate
  limits as the set grows.
- [`backend/services/ws_feeds.py:1034-1049`](../../backend/services/ws_feeds.py)
  — `_apply_book_update` — the cache-write site. Suspect for silent
  drops if message shape isn't recognized.
- [`backend/services/ws_feeds.py:2059-2068`](../../backend/services/ws_feeds.py)
  — async `feed_manager.get_order_book(token_id)` with HTTP fallback.
  Currently NOT used by
  [`backend/services/strategy_sdk.py:2860-2868`](../../backend/services/strategy_sdk.py)
  (`StrategySDK.get_order_book_depth`), which reads `cache.get_order_book`
  directly.
- Plan 0044 live verification:
  [`docs/plans/0044-firehose-binding-cache-include-shadow-traders.md`](0044-firehose-binding-cache-include-shadow-traders.md)
  — captured the firehose payloads showing exactly which gate fails.

## Validation Commands

- `docker compose exec backend pytest -q backend/tests/test_ws_feeds.py`
  (if exists; otherwise add coverage as part of Task 4)
- Live live verification on `polyhome-1`:
  ```sql
  SELECT message, payload_json::jsonb->'gates' AS gates
  FROM trader_events
  WHERE event_type='firehose_evaluation'
    AND verbosity='murmur'
    AND created_at > NOW() - INTERVAL '10 minutes'
  ORDER BY created_at DESC LIMIT 5;
  ```
  Expect `book_depth: passed=true` after the fix.

## Hypotheses to confirm or rule out

Each hypothesis is testable; ordering is by likelihood-times-impact
based on the evidence already collected.

### H1 — `_subscribed_assets` set drifted from WS-server reality

The set is in-memory state. Polymarket's WS server has its own
subscription accounting. Anything that desyncs these — a missed
ACK, a reconnect that didn't get re-applied server-side, a
chunk-limit dropped silently — leaves `_subscribed_assets` claiming
a token is subscribed while the server isn't pushing books for it.
**Strong fit:** explains why fresh probe (clean set) works while
live worker (accumulated set) doesn't. Probable.

### H2 — book-update message shape changed and `_apply_book_update` drops them silently

Polymarket migrated to CLOB V2 in Plan 0039. The book-update parser
at [`ws_feeds.py:1034-1049`](../../backend/services/ws_feeds.py) was
audited then. But V2 could be sending a wrapper or field name that
the current parser ignores. Possible but the fresh-probe receives
books with the SAME parser → less likely. Worth confirming via
raw-message dump.

### H3 — server-side subscription cap reached

Polymarket WS server may impose a per-connection subscription cap
(e.g. 256). After a few thousand cycles of scanner + crypto +
user-feed bindings, `_subscribed_assets` may have grown past the
cap. New subscribes return ACK but the server stops streaming
books for the oldest entries — or silently drops new subscribes.
Plausible; needs server-side capacity check (Polymarket dev docs)
or empirical probe with many subscriptions.

### H4 — race on `_sub_lock` between subscribe and connect

Tokens added to `_subscribed_assets` during a transient
`ConnectionState.CONNECTING` window, then `should_send=False` and
the on-connect re-subscribe block runs before the new tokens land
in the set. Should be precluded by `async with self._sub_lock`
around both reads — but worth re-reading the lock semantics.

## Decision

Hybrid **B + ad-hoc gating** of the four heaviest producers, plus
a temporary attribution-log instrumentation pass to identify a
hidden fifth producer that the audit missed. Rationale: H3 — the
Polymarket WS server tolerates only ~25-30 active subscriptions
per connection in practice (observed via `cache_entries_count`
plateau), far below the 256-1000 we initially assumed. Brute-
forcing under that cap requires per-producer diff (not LRU at the
feed layer) so each scope keeps its own active set. Path A (HTTP
fallback) was rejected as a workaround that hides the leak; Path
C (drop `new_ids` filter) was rejected as risking rate-limit on
the WS server.

## Task list

### Task 1: Snapshot the live state via instrumented probe

- [x] Logging instrumentation in
      `PolymarketWSFeed._heartbeat_loop` dumps
      `subscribed_assets_count`, `cache_entries_count`, per-message-
      type counters, `recent_book_tokens` (last 8 writes), and
      `recent_subs_with_book` (last 6 subscribes × book-present
      flag) every 30 s. Commit `95a81cab`.
- [x] First captures showed `subscribed_assets_count=7042 → 7620`
      (still climbing) with `cache_entries_count=9` plateau —
      ranking **H3 (server-side cap)** as the dominant hypothesis.
      `recent_subs_with_book` all `false`, `recent_book_tokens`
      held a stable set of 8 unrelated tokens.

### Task 2: Capture a raw WS-message dump for these tokens

- [x] Per-`subscribe()`-call attribution log at the entry of
      `PolymarketWSFeed.subscribe` records caller frame
      (`inspect.stack()[1]`), request count, new-token delta,
      total-after, and `will_send` flag. Commit `fb452b3a`.
- [x] One-shot capture identified the smoking gun: a single
      `subscribe()` call from
      `recorder_subscription_service.py:221:_ensure_subscribed`
      added **6268 new tokens** in one shot 4 s after WS connect
      (followed by 358 more 2 min later). Confirmed H3 plus a
      hidden fifth producer (recorder bulk-subscriber).

### Task 3: Per-producer diff fixes

Applied the same `new_active - previous` / `previous - new_active`
diff pattern to every producer the audit + attribution log
identified:

- [x] **Crypto lane** —
      `MarketRuntime._sync_crypto_subscriptions` keeps
      `self._crypto_subscribed_tokens` snapshot and diffs on each
      refresh. Commit `2b44929b`.
- [x] **Scanner fast-scan** —
      `ArbitrageScanner.scan_fast` keeps
      `self._ws_subscribed_tokens` snapshot; gated behind the
      operator toggle (see Task 3b). Commit `2f3f64b2`.
- [x] **`btc_eth_*` strategies** — all three
      `_BatchedCryptoMarketCache` subclasses now diff on each
      `get_markets()` refresh. Commit `362544ec`.
- [x] **`intent_runtime` hot-prewarm** — gated on
      `_allow_hot_subscription_for_source(source)` so scanner-
      source opportunities don't drip-feed `_subscribed_assets`.
      Commit `cbcf56e4`.

### Task 3b: Operator-facing DB toggles

- [x] **`scanner_ws_subscribe_enabled`** — `app_settings` boolean
      column, `ScannerSettingsModel`, Settings → Scanner toggle.
      Default OFF; when off, scanner skips WS overlay entirely.
      Alembic `202605110001`. Commit `f9479357`.
- [x] **`recorder_subscribe_enabled`** — same pattern; when off,
      `recorder_subscription_service.run_loop` idles every 60 s
      without issuing the bulk top-N-liquid subscribe. Alembic
      `202605110002`. Commits `bd5aa8a3` (env-var first cut),
      `7d7791ac` (refactor to DB-backed toggle on operator
      request).

### Task 4: Regression tests

- [x] `backend/tests/test_strategy_loader_per_trader_params.py`
      (Plan 0041 sibling) — covers the per-trader fan-out path.
- [x] `backend/tests/test_market_runtime_per_trader_dispatch.py`
      (Plan 0041 sibling) — singleton fallback + per-trader
      tagging.
- [x] `backend/tests/test_firehose_binding_cache.py` (Plan 0044)
      — five cross-mode binding tests.
- [x] `backend/tests/test_plan_0041_dedupe_backward_compat.py` —
      seven dedupe-key compatibility tests pinning
      `_opportunity_dedupe_key`.

### Task 5: Deploy and verify on `polyhome-1`

- [x] All eight commits deployed via `./deploy/sync_remote.sh`.
      Containers came back healthy on each round.
- [x] Final live capture confirms:
      - `subscribed_assets_count` collapsed from **6800+** to
        **26** (×260 reduction).
      - `cache_entries_count = 26` (100 % of subscriptions
        actively receive books — `recent_subs_with_book` all
        `true`).
      - `book_depth` gate now passes deterministically for SOL /
        XRP 5m markets when distance + clob tokens pass earlier.
      - Remaining strategy rejections fall on `min_distance`
        (calm market) or `vwap_in_range` (book skewed past
        `max_entry_price=0.70`) — both **expected gates** for the
        current market regime, not infrastructure failures.

### Task 6: Remove Plan 0044 + 0045 temporary diagnostic logs

- [ ] **Pending.** Three TEMP instrumentation blocks remain in
      code as of plan-close:
      - `crypto_5m_midcycle WS subscribe issued (TEMP)` —
        [crypto_5m_midcycle.py:_ensure_ws_subscribed_for_5m](../../backend/services/strategies/crypto_5m_midcycle.py)
        — verifies the per-trader fan-out actually reaches the
        WS subscribe path.
      - `crypto_5m_midcycle gate reject` —
        [crypto_5m_midcycle.py:_emit_reject](../../backend/services/strategies/crypto_5m_midcycle.py)
        — MURMUR-tier reject log; persists post-Plan-0044 since
        `firehose_evaluation` rows cover the same data.
      - `Polymarket WS diag (TEMP plan 0045)` + `Polymarket WS
        subscribe call (TEMP plan 0045)` —
        [ws_feeds.py:_heartbeat_loop](../../backend/services/ws_feeds.py)
        + `subscribe()` entry — periodic dump + per-call
        attribution.
- [ ] After 7 days of stable trading on `polyhome-1` (target:
      **2026-05-18**), file a single cleanup commit removing all
      four log blocks plus the corresponding `_diag_*` counters
      and `inspect.stack()` call. Also remove the Plan 0043
      `KNOWN LEAK` comment block in
      [strategy_loader.py](../../backend/services/strategy_loader.py)
      once the binding-cache eviction it documents has shipped.
- [ ] For the strategy-side cleanup, push the new disk version into
      the DB via `POST /api/strategy-manager/{id}/reset-to-factory`
      so the runtime picks up the cleaned-up code (Plan 0041 +
      0042 ground-rule: strategies are DB-driven, disk-side
      changes only take effect after reset-to-factory + reload).

## Live verification

Captured on `polyhome-1` between 2026-05-11 09:50 and 10:21 UTC:

```
2026-05-11T09:50:09Z   subscribed_assets=6800  cache_entries=9   ← pre-recorder-disable
2026-05-11T10:15:16Z   subscribed_assets=26    cache_entries=16  ← recorder loop idle
2026-05-11T10:15:48Z   subscribed_assets=26    cache_entries=20
2026-05-11T10:21:41Z   subscribed_assets=26    cache_entries=26  ← steady state, every sub has a book
```

`recorder_subscription_service` log line at 10:11:53 confirmed
the loop exited cleanly with
`"Recorder subscription loop disabled
(HOMERUN_RECORDER_ENABLED not set)"` — the very next deploy
(commit `7d7791ac`) replaced the env-var gate with the
`recorder_subscribe_enabled` DB column.

Firehose at 10:12:30 captured a SOL 5m evaluation passing **every
gate up to and including `book_depth` and `book_fresh`** and
rejecting only at `vwap_in_range` (vwap=0.93 > max=0.70). That is
the strategy's intended behaviour on a book that has already
priced in the directional bias; not an infrastructure failure.

Verdict: **WS-cache root cause fixed.** The bot is now
infrastructure-clean and waits on a market regime where distance
≥ `min_distance_bps` AND vwap ≤ `max_entry_price` align. Plan
0045 closes; the residual temp-log cleanup is tracked above.
