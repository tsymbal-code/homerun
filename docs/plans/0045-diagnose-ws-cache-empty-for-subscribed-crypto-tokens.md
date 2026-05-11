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

## Task list

### Task 1: Snapshot the live state via instrumented probe

- [ ] Add a short-lived debug endpoint
      (`GET /api/_debug/ws-feed-snapshot`) to
      [`backend/api/`](../../backend/api/) that returns the live
      `feed_manager.polymarket_feed._subscribed_assets` count, the
      cache's `_entries` count, and per-asset `(subscribed, has_book,
      has_entry, staleness_s)` for the 4 active SOL/XRP 5m tokens.
- [ ] Hit the endpoint twice — once at cycle start (T+0s) and once
      at mid-cycle (T+150s) — and record the deltas in the plan
      file under a new "## Live snapshots" section. Specifically
      compare `subscribed_assets_count` over time vs `entries_count`.

### Task 2: Capture a raw WS-message dump for these tokens

- [ ] Subscribe to the same 4 active token IDs in a probe that
      logs **every raw message** for ~30 seconds — both the
      `topic`/`event_type` and the full payload. Reproduce in
      worker-trading container so the env is identical (creds, TLS,
      network namespace).
- [ ] Run the probe against the SAME tokens that
      `StrategySDK.get_order_book_depth` is failing on. Look for
      either: (a) no `bids`/`asks` messages → server isn't sending
      → H3; or (b) messages arriving but parser conditions don't
      match → H2.

### Task 3: Decide between three fix paths and write the implementation

Decision criterion: minimal blast radius for the immediate trade-
gating problem; defer broader WS architecture work to a follow-up.

**Path A — HTTP fallback in `StrategySDK.get_order_book_depth`.**
Replace the synchronous
`cache.get_order_book(token_id)` at
[`strategy_sdk.py:2861`](../../backend/services/strategy_sdk.py)
with the async `feed_manager.get_order_book(token_id)` (already has
HTTP fallback at `ws_feeds.py:2059-2068`). Requires the SDK method
to become `async`; audit downstream call sites in every crypto
strategy. Pros: works regardless of root cause; least guesswork.
Cons: API surface change; HTTP fallback adds 50-500ms per
evaluation under WS failure.

**Path B — Periodic `_subscribed_assets` sweep + re-subscribe.**
Add a background loop that every N seconds compares
`_subscribed_assets` to the current "needed" token set (from
`market_runtime._crypto_markets` + scanner state), unsubscribes
the diff, and **forcibly re-subscribes** the kept set even if
already present. Pros: keeps set bounded, heals desync. Cons: heavier
runtime change; need to identify what "needed" means for each
subscription source.

**Path C — Defensive `subscribe()` semantics change.**
Drop the `new_ids` filter inside `PolymarketWSFeed.subscribe` —
always send the WS subscribe message, even for tokens already in
the set. Pros: simplest one-line change. Cons: may overwhelm the
WS-server with redundant subscribes, especially on tick-driven call
sites; risk of rate-limit / ban.

- [ ] Pick one. Write the chosen patch as a separate plan child OR
      inline here if it's small (Path A or C). Record the chosen
      path in a "## Decision" subsection with one-line rationale.

### Task 4: Add regression test pinning the cache-write path

- [ ] Add a test under
      `backend/tests/test_ws_feeds_polymarket.py` (new file) or in
      the existing nearest neighbor. Construct a synthetic
      `PolymarketWSFeed` with a fake WS server; verify that:
      - Subscribe with new tokens issues exactly one WS message.
      - A book-update message for a subscribed token populates the
        cache.
      - A book-update for an UN-subscribed token does NOT crash but
        also does NOT populate the cache (this is the current
        behaviour we want to preserve).
- [ ] If Path A is chosen: add a test that
      `StrategySDK.get_order_book_depth` returns a non-None dict
      when the WS cache is empty but the HTTP fallback returns a
      valid book.

### Task 5: Deploy and verify on `polyhome-1`

- [ ] `./deploy/sync_remote.sh` to ship the chosen fix.
- [ ] After redeploy: wait for two consecutive 5-min cycles.
      Capture firehose `murmur` rejections and confirm
      `book_depth.passed == true` for at least one cycle on SOL
      and/or XRP whose distance gate passes.
- [ ] Confirm `trade_signals` count > 0 for
      `strategy_type='crypto_5m_midcycle'`, and the BTC - 5min
      shadow trader writes its first `trader_decision`.
- [ ] Record the verdict in a "## Live verification" section. Move
      to `docs/plans/completed/`.

### Task 6: Remove Plan 0044 temporary diagnostic logs

- [ ] Once Task 5 confirms the fix, remove the
      `crypto_5m_midcycle gate reject` info log and the
      `crypto_5m_midcycle WS subscribe issued (TEMP)` info log from
      [`crypto_5m_midcycle.py`](../../backend/services/strategies/crypto_5m_midcycle.py).
      These were marked TEMPORARY in Plan 0044 Task 5 (target 7 days
      post-deploy); now that Plan 0045 has root-caused the
      `book_depth` gate, the persistent firehose telemetry is the
      single source of truth.
- [ ] Apply the disk → DB sync via `POST
      /api/strategy-manager/{id}/reset-to-factory` so the cleanup
      lands in the runtime, not just the file.
