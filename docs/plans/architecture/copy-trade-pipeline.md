# Architecture: Copy-Trade Pipeline (`source='traders'`)

> **Status (post Plan 0009).** The deferred-state gate that this
> note was originally written to document is **fixed**. As of
> plan 0009 (`completed/0009-fix-traders-source-on-normal.md`),
> `_strategy_runtime_metadata` (`backend/services/signal_bus.py`)
> uses an explicit allow-list — `crypto → immediate`, `scanner →
> ws_current`, `traders → immediate` — and unknown source keys
> fall back to `immediate` with a logged warning. The
> `_ea == "ws_post_arm_tick"` deferred branch in
> `intent_runtime.publish_opportunities` still exists as
> defensive code but is **not reachable from any current source
> key**; it would only fire if a future strategy explicitly
> opted into `ws_post_arm_tick` activation. Read the **The gate
> (historical)** section below for the original diagnosis and
> the **Operational guidance** section for the post-fix flow.

This note covers the end-to-end path of a `source='traders'`
signal — specifically `strategy_type='traders_copy_trade'` —
from "leader wallet trades on Polymarket" through to "consumer
trader produces an order." It exists because plan
[0008](../completed/0008-investigate-traders-source-routing-on-normal.md)
identified (and plan
[0009](../completed/0009-fix-traders-source-on-normal.md) fixed)
a routing asymmetry that was not captured in the more general
[trader-pipeline.md](trader-pipeline.md) note: pre-Plan-0009,
`traders_copy_trade` signals were silently dropped on
normal-tier traders and only sporadically reached fast-tier.

See also: [trader-pipeline.md](trader-pipeline.md) for the
generic signal-to-order flow, and [worker-trading.md](worker-trading.md)
for the trading-plane process model and fast vs normal latency
classes.

## Purpose

This file documents:

1. The **publish surface** for `source='traders'` signals —
   every component from the wallet WS feed through to a row in
   `trade_signals`.
2. The **consumer surfaces** — what fast-tier and normal-tier
   each do to discover and act on a `traders` signal.
3. **The gate (historical)** — the specific code path
   (`_strategy_runtime_metadata` →
   `execution_activation = "ws_post_arm_tick"` →
   `runtime_sequence = NULL` deferred state) that, on
   pre-Plan-0009 builds, made `traders_copy_trade` signals
   invisible to consumers until a fresh CLOB market-data quote
   arrived. Plan 0009 retired the `else` branch; the section
   is preserved as the canonical post-mortem for the bug.
4. **Post-fix flow** — what the current code does, and why
   `traders_copy_trade` now behaves like `crypto` at publish
   time (immediate visibility, no WS-quote precondition).

It is **not** a duplicate of `trader-pipeline.md`. That file
covers the "given a signal exists and a trader picks it up,
what happens" path. This file covers "why a `traders` signal
might never be picked up in the first place."

## Pipeline diagram (ASCII)

```
+--------------------------+
| Polymarket leader wallet |   (39 wallets currently tracked,
|     trades on-chain      |    set by tracked_wallets +
+------------+-------------+    discovered_wallets pool)
             |
             | Polygon RPC + user-channel WS
             v
+--------------------------+    services/wallet_ws_monitor.py
| wallet_ws_monitor:       |     - persists every wallet trade
|   - WS subscriptions     |       to wallet_monitor_events
|   - _persist_event       |     - fans out via add_callback
|   - add_callback         |       (in-process, NOT event_bus)
+------------+-------------+
             |
             | _on_wallet_trade callback
             v
+----------------------------------+   services/traders_copy_trade_signal_service.py
| traders_copy_trade_signal_       |     - 8x _processor_loop tasks
|   service:                       |     - 1x _replay_loop task
|   - _on_wallet_trade :260        |     - DB-backed replay from
|   - _processor_loop  :158        |       wallet_monitor_events
|   - _replay_loop     :176        |
|   - _process_wallet_trade_event  |
|     :486                         |
+--------------+-------------------+
               |
               | runtime_strategy.detect_async(strategy_input, [], {})
               | (returns list[Opportunity])
               v
+----------------------------------+   services/strategies/traders_copy_trade.py
| TradersCopyTradeStrategy         |     - strategy_type = "traders_copy_trade"
|   .detect_async                  |     - source_key = "traders"
+--------------+-------------------+     - emits Opportunity per wallet trade
               |
               | bridge_opportunities_to_signals(
               |   opportunities,
               |   source="traders",
               |   signal_type_override="copy_trade",
               |   default_ttl_minutes=15)
               v
+----------------------------------+   services/strategy_signal_bridge.py
| bridge_opportunities_to_signals  |     - Thin wrapper.  Calls
|   (line 18)                      |       intent_runtime.publish_opportunities.
+--------------+-------------------+
               |
               v
+======================================================+
|  intent_runtime.publish_opportunities                |
|  (services/intent_runtime.py:1987)                   |
|                                                      |
|   For each opportunity:                              |
|     1. Build incoming_snapshot dict (line 2044+)     |
|     2. Compute _ea = payload.strategy_runtime        |
|        .execution_activation                         |
|        Allow-list in signal_bus._strategy_runtime_   |
|        metadata (post-Plan-0009):                    |
|          crypto  → "immediate"                       |
|          scanner → "ws_current"                      |
|          traders → "immediate"  ← traders bypasses   |
|                                    the deferred gate |
|          unknown → "immediate" + WARNING (logged     |
|                                  once per source)    |
|                                                      |
|     3. New-signal path (line 2173+)                  |
|        OR existing-upsert path (line 2095+):         |
|        if status=="filtered":         seq = None     |
|        elif _ea=="ws_post_arm_tick" and              |
|             required_token_ids:                      |
|             # Defensive only.  No current source     |
|             # key produces ws_post_arm_tick (Plan    |
|             # 0009).  Branch retained for any        |
|             # future strategy that opts in.          |
|             snapshot["deferred_until_ws"] = True     |
|             snapshot["runtime_sequence"] = None      |
|        elif source in {"scanner"}                    |
|              and required_token_ids                  |
|              and not fresh_ws_quotes:  seq = None    |
|        else:                                         |
|             snapshot["runtime_sequence"] =           |
|                 self._allocate_runtime_sequence()    |
|                                                      |
|     4. Write to _signals_by_id[signal_id]            |
|     5. publish_signal_batch(                         |
|         event_type, source, signal_ids, ...)         |
|        → runtime_signal_queue._queues[lane]          |
|                          (asyncio.Queue, in-process) |
|     6. _enqueue_projection({...}) →                  |
|        DB write of trade_signals via                 |
|        signal_bus.upsert_trade_signal                |
|        (services/signal_bus.py:1234)                 |
|                                                      |
|     7. event_bus.publish("signals_update", ...)      |
+======================================================+
               |
               +----------------------+----------------------+
               |                      |                      |
               v                      v                      v
+--------------------+   +----------------------+   +-------------------+
| trade_signals row  |   | runtime_signal_queue |   | event_bus topics  |
|  (DB projection)   |   |  ._queues["general"] |   |  trade_signal_*   |
|                    |   |                      |   |  signals_update   |
+--------------------+   +----------+-----------+   +---------+---------+
                                    |                         |
                                    v                         v
                  +----------------------+    +-------------------------+
                  | trader_orchestrator_ |    | fast_trader_runtime     |
                  |   worker             |    |   (workers/fast_trader_ |
                  |   (workers/trader_   |    |    runtime.py)          |
                  |    orchestrator_     |    |                         |
                  |    worker.py)        |    |   Per-trader event-     |
                  |                      |    |   driven loops with     |
                  |   wait_for_signal_   |    |   _wake events fed by   |
                  |   batch(lane=        |    |   event_bus subscribes  |
                  |   "general")         |    |   on _WAKE_EVENTS       |
                  +----------+-----------+    +-----------+-------------+
                             |                            |
                             | List unconsumed signals    |
                             | from intent_runtime cache  |
                             | filtered by source +       |
                             | strategy_type +            |
                             | runtime_sequence           |
                             v                            v
                  +-------------------------+
                  | intent_runtime.list_    |
                  |   unconsumed_signals    |
                  | (intent_runtime.py:2395)|
                  |                         |
                  |   if deferred_until_ws: |
                  |       continue          |
                  |   if row_sequence is    |
                  |       None: continue    |
                  |                         |
                  |   Post-Plan-0009: no    |
                  |   traders signal hits   |
                  |   either filter at      |
                  |   publish time.         |
                  +-------------------------+
                             |
                             v
                  trader_decisions, trader_orders
```

The publish path is now symmetric between `crypto` and
`traders` (both `immediate`), and between `scanner` (the
strict-WS source) and the rest (no required quote at publish).
The deferred-state filter inside `list_unconsumed_signals`
remains in place to handle the `scanner` prewarm case and any
future strategy that opts into `ws_post_arm_tick`; it just
does not match any current `traders` signal.

## Code-reference table

| # | Stage | File:line | Function / class |
|---|---|---|---|
| 1 | Wallet WS feed | `backend/services/wallet_ws_monitor.py` | `WalletWsMonitor`, `add_callback`, `_persist_event` |
| 2 | Service startup (trading plane) | `backend/workers/host.py:1095` | `traders_copy_trade_signal_service.start()` |
| 3 | Wallet-trade callback registration | `backend/services/traders_copy_trade_signal_service.py:103` | `wallet_ws_monitor.add_callback(self._on_wallet_trade)` |
| 4 | 8x async processor loops | `backend/services/traders_copy_trade_signal_service.py:113-119` | `_processor_loop` task fanout |
| 5 | DB-backed replay loop | `backend/services/traders_copy_trade_signal_service.py:122` | `_replay_loop` task |
| 6 | Wallet event → opportunity | `backend/services/traders_copy_trade_signal_service.py:486` | `_process_wallet_trade_event` |
| 7 | Strategy detect | `backend/services/strategies/traders_copy_trade.py:261, :440` | `TradersCopyTradeStrategy.detect_async` |
| 8 | Bridge to runtime | `backend/services/strategy_signal_bridge.py:18` | `bridge_opportunities_to_signals` |
| 9 | Strategy-runtime metadata | `backend/services/signal_bus.py:493-548` | `_strategy_runtime_metadata` (allow-list `_EXECUTION_ACTIVATION_BY_SOURCE_KEY`; `traders → immediate` post-Plan-0009) |
| 10 | Publish to runtime | `backend/services/intent_runtime.py:1987` | `IntentRuntime.publish_opportunities` |
| 11 | Deferred branch (now unreachable for `traders`) | `backend/services/intent_runtime.py:2129-2141, :2186-2195` | `if _ea=="ws_post_arm_tick" and required_token_ids:` — defensive code only, no current source key reaches it |
| 12 | Reactivation by fresh WS quote | `backend/services/intent_runtime.py:1206, :1298` | `_on_ws_price_update` → `_reactivate_deferred_signals_for_token` |
| 13 | Runtime queue publish | `backend/services/runtime_signal_queue.py:109` | `publish_signal_batch` |
| 14 | Lane routing | `backend/services/runtime_signal_queue.py:61` | `_default_lane_for_source` (`crypto` → `crypto`, else → `general`) |
| 15 | DB projection of signal | `backend/services/signal_bus.py:1234` | `upsert_trade_signal` |
| 16 | Cross-plane wake (event_bus) | `backend/services/signal_bus.py:1177, :1222` | `_publish_trade_signal_emission`, `_publish_trade_signal_batch` |
| 17 | Cross-plane fanout (Redis) | `backend/services/signal_bus.py:1181, :1188` | `SIGNAL_EMISSION_CHANNEL`, `SIGNAL_PAYLOADS_CHANNEL` |
| 18 | Orchestrator runtime-trigger consume | `backend/workers/trader_orchestrator_worker.py:1521` | `wait_for_signal_batch(lane=group, ...)` |
| 19 | Orchestrator periodic + runtime hybrid | `backend/workers/trader_orchestrator_worker.py:8467` | `run_worker_loop` |
| 20 | Orchestrator runtime-only loop | `backend/workers/trader_orchestrator_worker.py:8409` | `run_runtime_trigger_loop` |
| 21 | Orchestrator filter (lane + fast skip) | `backend/workers/trader_orchestrator_worker.py:8366, 8621, 8730` | `_trader_matches_lane and not _is_fast_tier_trader` |
| 22 | Orchestrator per-trader read | `backend/workers/trader_orchestrator_worker.py:671` | `list_unconsumed_trade_signals` (intent_runtime first, DB fallback) |
| 23 | In-memory list (the second filter) | `backend/services/intent_runtime.py:2432, :2440-2442` | `if deferred_until_ws: continue; if row_sequence is None: continue` |
| 24 | Fast-tier per-trader runtime | `backend/workers/fast_trader_runtime.py:752` | `_FastTraderTask._run_once` calls `intent_runtime.list_unconsumed_signals` |
| 25 | Fast-tier event_bus wake topics | `backend/workers/fast_trader_runtime.py:98, :1707` | `_WAKE_EVENTS = ("trade_signal_emission", "trade_signal_batch", "signals_update")` |
| 26 | Trader source filter (per-trader) | `backend/workers/trader_orchestrator_worker.py:2047, :2065` | `_runtime_trigger_matches_trader`, `_trigger_signal_ids_for_trader` |
| 27 | Strategy-types accepted per source | `backend/workers/trader_orchestrator_worker.py:1897` | `_accepted_signal_strategy_types` |
| 28 | Source registry (signal types) | `backend/services/trader_orchestrator/sources/registry.py:52-58` | `"traders"` adapter |
| 29 | Session policy for traders source | `backend/services/trader_orchestrator/session_engine.py:195-214` | `is_traders` execution profile (REPRICE_LOOP, taker_limit, IOC) |
| 30 | trade_signals table | (DB) | columns: `runtime_sequence`, `status`, `expires_at`, `payload_json` |

## Fast-tier vs normal-tier (post Plan 0009)

| Aspect | Normal-tier orchestrator | Fast-tier runtime |
|---|---|---|
| Process | Same (trading plane, `worker-trading` container) | Same (trading plane) |
| Reads from | `intent_runtime._signals_by_id` (in-process cache) → DB fallback | `intent_runtime._signals_by_id` → `signal_cache` (Redis) → DB fallback |
| Wake mechanism | `runtime_signal_queue._queues["general"]` (asyncio.Queue) | `event_bus` subscribers on `trade_signal_emission`, `trade_signal_batch`, `signals_update` |
| Cycle cadence | 60 s periodic + runtime-trigger wakeup | 250 ms polling fallback + event-driven wakeup |
| Filter on consume | `if deferred_until_ws: continue; if runtime_sequence is None: continue` (no longer matches traders signals at publish) | Same — they share `list_unconsumed_signals` |
| Roster | All `latency_class != 'fast'` traders | All `latency_class = 'fast'` traders only |

Both tiers share `intent_runtime.list_unconsumed_signals`. The
filters there still exist (deferred-by-WS for the `scanner`
prewarm case, `runtime_sequence is None` for the strictly
filtered-out cases), but **no `traders` signal is born in
either filtered state any more** because
`_strategy_runtime_metadata` returns `"immediate"` for
`source_key='traders'`. So both tiers see every published
`traders_copy_trade` signal as soon as the publish completes,
modulo their normal cycle latency (sub-second on fast, up to
60 s on normal).

### Pre-Plan-0009 production observation (preserved as
post-mortem)

In production data captured 2026-05-07T19:30Z, the fast-tier
window (10:00-18:18 UTC) produced **14 executed
`traders_copy_trade` signals out of 13 941 published** —
i.e. ~0.1%. The other 99.9% expired without any consumer ever
seeing them. In the normal-tier window (18:18-18:58 UTC),
**0 of 174** signals reached `executed`. The difference was
one execution every ~30 minutes for fast vs. zero for normal,
both numbers dominated by the same upstream deferred-state
filter. Plan 0009's "after" baseline (Task 6) demonstrates
the post-fix per-cycle cadence on normal-tier.

## The gate (historical, retired by Plan 0009)

> Pre-Plan-0009. Read this section as a post-mortem of the
> bug that Plan 0008 investigated and Plan 0009 fixed. The
> live publish path is described in **Post-fix flow** below.

**File:** `backend/services/intent_runtime.py`
**Lines:** 2129-2141 (existing-signal upsert path) and
2186-2195 (new-signal path), both gated by `_ea ==
"ws_post_arm_tick"`. The branches still exist but are no
longer reached for any source key in the system.

**Pre-Plan-0009 provenance of `_ea`** in
`backend/services/signal_bus.py`, `_strategy_runtime_metadata`:

```python
# pre-Plan-0009 — bug
if source_key == "crypto":
    execution_activation = "immediate"
elif source_key == "scanner":
    execution_activation = "ws_current"
else:
    execution_activation = "ws_post_arm_tick"   # ← traders fell here
```

The `else` branch was written to enforce strict-WS pricing on
arbitrary new sources. For `crypto` and `scanner` the
trade-off was correct: `crypto` markets get an `immediate`
flag (no WS-quote dependency), `scanner` markets are
already in the scanner's hot-subscription set so a fresh
WS quote is reliably available.

For `traders`, the policy was wrong. Copy Trade follows
leader wallets to whatever market the leader trades — which
is NOT in the scanner's hot-subscription set most of the
time. The signal was born deferred, required a CLOB WS quote
to clear, but the CLOB WS wasn't subscribed to that token, so
the deferred state never cleared. The signal expired after
15 minutes (default TTL) without any consumer ever seeing it.

**Pre-fix effect chain (lines refer to `intent_runtime.py`):**

1. Line 2186: `_ea == "ws_post_arm_tick"` AND
   `required_token_ids` is non-empty (extracted from
   opportunity.positions_to_take by
   `_extract_required_token_ids` line 395).
2. Line 2188-2192: snapshot's `deferred_until_ws=True`,
   `deferred_reason="awaiting_post_arm_ws_tick"`,
   `runtime_sequence=None`.
3. Line 2484: `event_bus.publish("signals_update", ...)`
   fires regardless. Fast-tier wakes; normal-tier already
   running. Both call `list_unconsumed_signals`.
4. Line 2432: `if bool(snapshot.get("deferred_until_ws")):
   continue` — signal is filtered out.
5. Line 2440-2442: `if row_sequence is None: continue` —
   second filter, same outcome.
6. Reactivation (line 1298, `_reactivate_deferred_signals_for_token`)
   fires only when `_on_ws_price_update` (line 1206) is
   called for one of the signal's `required_token_ids`.
   `_on_ws_price_update` is registered as a callback on
   `feed_manager.cache.add_on_update_callback` (line 650).
   The CLOB feed only pushes for tokens in the
   `feed_manager` subscription set.
7. `traders_copy_trade_signal_service` does not subscribe
   the leader-wallet token to the CLOB feed at publish time
   — it only ensures the user-channel WS is subscribed
   (`signal_bus.py:1278` calls
   `feed_manager.ensure_user_subscribed`, but that's the
   wallet user-channel, not the market-data CLOB channel).
   So unless another component (scanner discovery) had
   already pushed the token into the CLOB hot-subscription
   set, no `_on_ws_price_update` would ever fire for it.

**Pre-fix production proof** (DB query 2026-05-07T19:30Z):

```
 strategy_type      | status  |  n  | with_seq | without_seq
--------------------+---------+-----+----------+-------------
 traders_confluence | pending |  30 |       30 |           0
 traders_copy_trade | pending | 445 |        0 |         445
```

Every pending `traders_copy_trade` signal had
`runtime_sequence = NULL`. None were visible to consumers.

`traders_confluence` did not exhibit the same shape — its
publishing entry is on the `discovery` plane via
`tracked_traders_worker`, which (a) goes through a different
publish path that does pass `runtime_sequence` to
`upsert_trade_signal`, and (b) typically targets markets
already in scanner subscription. So the gate was
source-key+`execution_activation`-driven, not source-key
alone.

## Post-fix flow (Plans 0009 + 0010)

**File:** `backend/services/signal_bus.py:493-548`,
`_strategy_runtime_metadata`:

```python
# post-Plan-0009 — bug fix
_EXECUTION_ACTIVATION_BY_SOURCE_KEY: dict[str, str] = {
    "crypto": "immediate",
    "scanner": "ws_current",
    "traders": "immediate",
}
_DEFAULT_EXECUTION_ACTIVATION = "immediate"
_UNKNOWN_SOURCE_KEY_WARNED: set[str] = set()


def _strategy_runtime_metadata(opportunity: Opportunity) -> dict[str, Any]:
    ...
    execution_activation = _EXECUTION_ACTIVATION_BY_SOURCE_KEY.get(source_key)
    if execution_activation is None:
        if source_key and source_key not in _UNKNOWN_SOURCE_KEY_WARNED:
            _UNKNOWN_SOURCE_KEY_WARNED.add(source_key)
            logger.warning(
                "Unknown strategy source_key %r (strategy=%r); defaulting "
                "execution_activation to %r. ...",
                source_key, strategy_slug, _DEFAULT_EXECUTION_ACTIVATION,
                source_key=source_key, strategy_slug=strategy_slug,
            )
        execution_activation = _DEFAULT_EXECUTION_ACTIVATION
    ...
```

**Effect chain post-fix** (intent_runtime.py line numbers
unchanged, behaviour different):

1. `traders` opportunities arrive with
   `payload.strategy_runtime.execution_activation = "immediate"`.
2. Lines 2186-2195: the `_ea == "ws_post_arm_tick"` branch
   does NOT match. Lines 2196-2210: the prewarm branch only
   matches `source in _PREWARM_SOURCES = {"scanner"}`, so it
   does NOT match either. The `else` clause at line 2212
   allocates a real `runtime_sequence` and stamps
   `execution_armed_at`. Same flow on the existing-row
   reactivate branch (lines 2161-2163).
3. `runtime_signal_queue.publish_signal_batch(source="traders",
   event_type="upsert_insert", ...)` is called → routes to
   the `general` lane. Both fast-tier and normal-tier wake.
4. `list_unconsumed_signals` returns the signal: it is not
   `deferred_until_ws`, and `runtime_sequence` is a positive
   integer.
5. The orchestrator picks the signal up on its next cycle
   (≤ 60 s on `latency_class=normal`, sub-second on `fast`).

**Tightening:** the silent `else: ws_post_arm_tick` fallback
is gone. Any future `source_key` that is not in
`_EXECUTION_ACTIVATION_BY_SOURCE_KEY` will (a) get the safe
`immediate` default, and (b) emit a one-time `signal_bus`
WARNING with the unknown source key. This prevents the next
silent-regression failure mode.

### Publish/projection durability (post Plan 0010)

Plan 0009's gate fix unmasked a pre-existing FK race in
`intent_runtime.publish_opportunities`. With the gate gone,
every wallet-trade copy publish landed in the runtime cache
end-to-end and the orchestrator immediately tried to write a
`trader_decisions` row. In production this fired
`trader_decisions_signal_id_fkey` violations on virtually
every decision — 151 failed `trader_signal_consumption` rows
in the first 15 min and zero persisted `trader_decisions`.

**Root cause.** `publish_opportunities` minted a fresh
`uuid.uuid4().hex` for any dedupe_key not already in the
in-memory cache (intent_runtime.py:2174). After every
worker-trading restart the cache is empty, but
`trade_signals` still carries rows from the previous process
(traders TTL is 15 min and the wallet WS replays the same
trades on reconnect). The fresh in-memory id then collided
with the existing `(source='traders', dedupe_key=K)` row
under `uq_trade_signals_source_dedupe`:

- `signal_bus.upsert_trade_signal` (signal_bus.py:1334-1342)
  found the existing row by `(source, dedupe_key)` and
  updated it in place — **keeping the OLD id**. The fresh id
  was never written to `trade_signals`.
- The orchestrator's
  `_ensure_runtime_signal_persisted`
  (`trader_orchestrator_worker.py:744`) ran
  `INSERT ... ON CONFLICT DO NOTHING` for the fresh id;
  Postgres saw the same `(source, dedupe_key)` already
  occupied by the OLD id and silently no-op'd. The fresh id
  remained absent from `trade_signals`.
- `create_trader_decision_checks(session, ...)` flushed the
  pending `INSERT INTO trader_decisions (signal_id=fresh_id, ...)`
  → `trader_decisions_signal_id_fkey` violation; the whole
  per-signal transaction rolled back; the recovery path's
  `record_signal_consumption` then fired its own FK on
  `trader_signal_consumption.decision_id_fkey`.

**Fix.** `publish_opportunities` now prefetches the canonical
`(source, dedupe_key) → id` mapping from `trade_signals`
**before** acquiring `self._lock`, and the new-id branch
adopts the canonical id when one exists:

```python
# pre-lock prefetch
prefetch_dedupe_keys = [
    dedupe_key
    for opp in opportunities
    for (mid, *_unused) in [build_signal_contract_from_opportunity(opp)]
    if mid
    for dedupe_key in [make_dedupe_key(opp.stable_id, opp.strategy, mid)]
    if dedupe_key not in self._signal_ids_by_dedupe_key
]
prefetched_ids: dict[str, str] = {}
if prefetch_dedupe_keys:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(TradeSignal.id, TradeSignal.dedupe_key).where(
                TradeSignal.source == source,
                TradeSignal.dedupe_key.in_(prefetch_dedupe_keys),
            )
        )
        prefetched_ids = {dk: sid for sid, dk in rows.all()}

# Skeleton-INSERT pass for dedupe_keys with no existing DB row,
# committed BEFORE the lock so the row is visible to every consumer
# the moment publish_opportunities returns.  ON CONFLICT (source,
# dedupe_key) DO NOTHING is idempotent under concurrent publish; the
# re-query covers conflict-loser dedupe_keys (peer publisher won the
# race between prefetch and INSERT).
committed_ids: dict[str, str] = {}
skeleton_dedupe_keys = [dk for dk in prefetch_dedupe_keys if dk not in prefetched_ids]
if skeleton_dedupe_keys:
    async with AsyncSessionLocal() as session:
        await session.execute(
            pg_insert(TradeSignal)
              .values(skeleton_rows)  # id, source, signal_type, market_id, dedupe_key, status='pending'
              .on_conflict_do_nothing(index_elements=["source", "dedupe_key"])
        )
        await session.commit()
        # Re-query for the canonical id of every skeleton dedupe_key.
        ...
        committed_ids = {dk: sid for sid, dk in after_rows.all()}

# inside the lock, replacing `signal_id = uuid.uuid4().hex` — three-way
# fallback so the in-memory cache always matches the row that exists
# (or is about to be enriched) in trade_signals:
signal_id = (
    prefetched_ids.get(dedupe_key)        # existing row (post-restart staleness mode)
    or committed_ids.get(dedupe_key)      # row we just skeleton-inserted (in-process race mode)
    or uuid.uuid4().hex                    # fallback if both DB hops failed (logged at debug)
)
```

The in-memory cache is now authoritative-equal-to-DB by
construction: every consumer (orchestrator, fast_trader_runtime,
UI) gets the canonical id, `_ensure_runtime_signal_persisted`'s
ON CONFLICT DO NOTHING never has a `(source, dedupe_key)`
conflict to swallow, and the FK race is closed at its
publish-side root rather than per-consumer.

**Cost.** One `SELECT id, dedupe_key FROM trade_signals
WHERE source = $1 AND dedupe_key = ANY($2)` per
`publish_opportunities` call, only over the cache-missing
dedupe_keys. Scanner steady-state hits the cache for 99%+
of dedupe_keys (the prefetch is a no-op); traders publishes
a handful at a time. The projection loop is unchanged — it
remains the single owner of the actual `trade_signals` write.

## Operational guidance

1. **Run `traders` bots on `latency_class=normal` if the
   operator prefers** — there is no longer any reason to
   require `latency_class=fast` for copy-trade. (The
   pre-Plan-0009 workaround was to set the bot to `fast`;
   that workaround is now obsolete and should be reverted
   if it's still in place.)
2. **Monitor the post-fix invariants.** Plan 0009 + Plan 0010 +
   Plan 0011 produce a two-tier monitoring scheme: a steady-state
   alert that tolerates publish-time transients, and a stuck-
   skeleton alert that detects publish-side failures.

   **Tier 1 — steady-state (production alerting).** The DB query
   ```sql
   select strategy_type, status, count(*) n,
     sum((runtime_sequence is not null)::int) with_seq,
     sum((runtime_sequence is null)::int)     without_seq
   from trade_signals
   where strategy_type in ('traders_copy_trade','traders_confluence')
     and status != 'pending'
     and payload_json is not null
     and created_at > now() - interval '5 minutes'
   group by strategy_type, status order by 1, 2;
   ```
   should always show `without_seq = 0` for both rows.
   The `payload_json is not null` filter excludes the publish-time
   transient (Plan 0010 commits a skeleton ~10–500 ms before the
   projection loop enriches it; that's healthy in-flight state,
   not a regression). The `status != 'pending'` filter excludes
   skeletons too (they ship with `status='pending'`).

   **Tier 2 — stuck-skeleton (Plan 0011).** A skeleton older than
   the projection's drain budget is an orphan — `publish_opportunities`
   died between the skeleton commit and the projection's UPSERT.
   The retention sweep on the discovery plane DELETEs rows older
   than `INTENT_RUNTIME_SKELETON_RETENTION_MAX_AGE_SECONDS`
   (default 1 h), and Plan 0011 stamps a defensive
   `expires_at = now() + INTENT_RUNTIME_SKELETON_TTL_SECONDS`
   (default 5 min) so the existing terminal-row pruner can also
   reach orphans.  Operator monitoring query:
   ```sql
   select count(*) stuck_skeletons from trade_signals
   where payload_json is null
     and runtime_sequence is null
     and status = 'pending'
     and created_at < now() - interval '1 minute';
   ```
   Should always be 0 in steady state. A non-zero result means
   the publish path is dying mid-call (process kill, connection
   drop, unhandled exception). Correlate with `worker-trading`
   logs for `publish_opportunities` exceptions.
3. **`traders_copy_trade` does NOT belong in `_PREWARM_SOURCES`.**
   Plan 0009 fixed the explicit `ws_post_arm_tick` gate, but
   `intent_runtime.py` retains a second deferred-state path keyed
   by `_PREWARM_SOURCES = {"scanner"}` (see
   [`intent_runtime.py:76, 2308, 2373`](../../../backend/services/intent_runtime.py)).
   If a future change adds `"traders"` to that set, Copy Trade
   signals will once again be deferred until the leader-wallet
   token IDs receive a strict-WS quote — for tokens that aren't
   in the CLOB feed subscription, that quote may never arrive,
   and we'll be back to the pre-0009 symptom. Document any
   addition to `_PREWARM_SOURCES` and re-run Plan 0008's
   diagnostic queries before merging.
4. **Add new sources to the allow-list explicitly.** The
   warn-once log line surfaces the next missing source key,
   but it does not fail closed. If you see
   `WARNING signal_bus: Unknown strategy source_key '...'`
   in the backend logs, register the source in
   `_EXECUTION_ACTIVATION_BY_SOURCE_KEY` (and, if it needs
   strict-WS pricing, also in
   `intent_runtime._uses_runtime_price_revalidation` /
   `_PREWARM_SOURCES`). Note that **four production strategies
   currently have unregistered source keys** that fall through
   to the `_DEFAULT_EXECUTION_ACTIVATION = "immediate"` branch
   with a one-time WARN: `weather` (`weather_distribution.py`),
   `manual` (`manual_manage_hold.py`), `sports`
   (`sports_overreaction_fader.py`), `news` (`news_edge.py`).
   These are all empirically active in shadow today; the safe
   default is correct for them, but registering them
   explicitly silences the WARN and locks the contract.

## Conclusion

The `traders_copy_trade` signal pipeline now publishes
signals with `execution_activation='immediate'` and a
non-NULL `runtime_sequence`, making them visible to both
normal-tier orchestrator and fast-tier runtime as soon as
they land in `_signals_by_id`. The pre-Plan-0009
deferred-state branch (`else: execution_activation =
"ws_post_arm_tick"` in `signal_bus._strategy_runtime_metadata`)
was a latent regression: it dropped 99.9% of leader-wallet
copy trades on the floor because the CLOB feed wasn't
subscribed to leader-wallet tokens.

Plan 0009 retired the `else` clause and replaced it with an
explicit allow-list that maps every known source key to its
audited activation policy, with a warn-and-fall-through
default of `immediate` for any future source that ships
without a registered policy.

Plan 0010 then closed the publish-side FK race that the
runtime-sequence gate had masked: `publish_opportunities`
prefetches the canonical `(source, dedupe_key) → id` mapping
from `trade_signals` before allocating ids, AND synchronously
skeleton-INSERTs a `(source, dedupe_key)` placeholder row for
genuinely new dedupe keys before pinging consumers. The
in-memory cache is authoritative-equal-to-DB by construction
and the orchestrator's `_ensure_runtime_signal_persisted`
never has a unique-constraint conflict to silently swallow.

Plan 0011 hardened the orphan path of Plan 0010's skeleton
INSERT: the skeleton row now ships with a defensive
`expires_at = now + INTENT_RUNTIME_SKELETON_TTL_SECONDS`
(so the existing `_run_trade_signal_pruner_loop` can reach
orphans), and a new retention sweep on the discovery plane
(`services.skeleton_signal_retention.prune_stuck_skeletons`)
DELETEs orphaned skeletons (`payload_json IS NULL AND
runtime_sequence IS NULL AND status='pending' AND
created_at < now() - max_age`) outright every 15 min.
Operators monitor the orphan count via the Tier 2 query in
**Operational guidance**.

The deferred-state branches in `publish_opportunities`
(lines 2129-2141 and 2186-2195) remain as defensive code
but are not reachable from any current source key.

## Open questions

None. Plan 0009 closed the runtime-sequence gate; Plan 0010
closed the FK race that gate had been masking; Plan 0011
hardened the skeleton-INSERT orphan path.  Three post-fix
invariants apply:

1. `without_seq = 0` for `traders_*` rows in the Tier 1 DB
   query from **Operational guidance** (Plan 0009).
2. Zero `trader_decisions_signal_id_fkey` violations for
   `traders_copy_trade` over a 30-minute soak under
   steady-state load (Plan 0010, validated by the four
   post-deploy checks listed in plan 0010's
   `## Validation Commands`).
3. `stuck_skeletons = 0` in the Tier 2 DB query from
   **Operational guidance** — i.e. no skeleton row sits
   in `trade_signals` for more than 1 minute without the
   projection loop committing its UPSERT (Plan 0011,
   enforced both by the defensive `expires_at` TTL and by
   the discovery-plane retention sweep).

## See also

- [trader-pipeline.md](trader-pipeline.md) — generic signal-to-order pipeline, diagnostic playbook.
- [worker-trading.md](worker-trading.md) — process model for the trading plane, fast vs normal tiers.
- [system-overview.md](system-overview.md) — runtime topology.
- [`docs/plans/completed/0008-investigate-traders-source-routing-on-normal.md`](../completed/0008-investigate-traders-source-routing-on-normal.md) — the investigation plan that produced this note.
- [`docs/plans/completed/0009-fix-traders-source-on-normal.md`](../completed/0009-fix-traders-source-on-normal.md) — runtime-sequence gate fix.
- [`docs/plans/completed/0010-fix-traders-publish-fk-race.md`](../completed/0010-fix-traders-publish-fk-race.md) — publish-side FK race fix (publish-time id adoption from `trade_signals`).
- [`docs/plans/0011-skeleton-trade-signal-ttl-and-retention.md`](../0011-skeleton-trade-signal-ttl-and-retention.md) — defensive `expires_at` on skeleton-INSERTed rows + stuck-skeleton retention sweep on the discovery plane.
- [`docs/plans/architecture/_appendix/0008-baseline-2026-05-07.txt`](_appendix/0008-baseline-2026-05-07.txt) — baseline data captured during investigation.

Last verified: <unverified>
