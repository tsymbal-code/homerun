# Architecture: Copy-Trade Pipeline (`source='traders'`)

This note covers the end-to-end path of a `source='traders'`
signal — specifically `strategy_type='traders_copy_trade'` —
from "leader wallet trades on Polymarket" through to "consumer
trader produces an order or doesn't." It exists because plan
[0008](../0008-investigate-traders-source-routing-on-normal.md)
identified that `traders_copy_trade` signals are silently
dropped on normal-tier traders and only sporadically clear
through on fast-tier — a routing asymmetry that was not
captured in the more general
[trader-pipeline.md](trader-pipeline.md) note.

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
3. **The gate** — the specific code path
   (`_strategy_runtime_metadata` →
   `execution_activation = "ws_post_arm_tick"` →
   `runtime_sequence = NULL` deferred state) that makes
   `traders_copy_trade` signals invisible to consumers until a
   fresh CLOB market-data quote arrives, which for most
   leader-wallet markets never happens before the 15-min TTL.
4. The **operational implication** and recommended fix
   direction (left for a separate plan).

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
|        ┌─ For source="traders":  _ea =               |
|        │  "ws_post_arm_tick"  (set by                |
|        │  signal_bus._strategy_runtime_metadata      |
|        │  :493-524, else branch line 518)            |
|        └─ For source="scanner": _ea = "ws_current"   |
|                                                      |
|     3. New-signal path (line 2173+)                  |
|        OR existing-upsert path (line 2095+):         |
|        if status=="filtered":         seq = None     |
|        elif _ea=="ws_post_arm_tick" and              |
|             required_token_ids:        ←  GATE       |
|             snapshot["deferred_until_ws"] = True     |
|             snapshot["deferred_reason"] =            |
|                 "awaiting_post_arm_ws_tick"          |
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
                  |       continue   ←───── filters ALL traders signals
                  |   if row_sequence is    |
                  |       None: continue ←─ filters again
                  +-------------------------+
                             |
                             v
                  trader_decisions, trader_orders
```

The gate is in the middle box. Every other path is symmetric
between fast-tier and normal-tier (and even between `scanner`
and `traders` source).

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
| 9 | Strategy-runtime metadata | `backend/services/signal_bus.py:493-524` | `_strategy_runtime_metadata` ← **gate origin** |
| 10 | Publish to runtime | `backend/services/intent_runtime.py:1987` | `IntentRuntime.publish_opportunities` |
| 11 | Deferred branch (gate) | `backend/services/intent_runtime.py:2129-2141, :2186-2195` | `if _ea=="ws_post_arm_tick" and required_token_ids:` |
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

## The asymmetry — fast-tier vs normal-tier

| Aspect | Normal-tier orchestrator | Fast-tier runtime |
|---|---|---|
| Process | Same (trading plane, `worker-trading` container) | Same (trading plane) |
| Reads from | `intent_runtime._signals_by_id` (in-process cache) → DB fallback | `intent_runtime._signals_by_id` → `signal_cache` (Redis) → DB fallback |
| Wake mechanism | `runtime_signal_queue._queues["general"]` (asyncio.Queue) | `event_bus` subscribers on `trade_signal_emission`, `trade_signal_batch`, `signals_update` |
| Cycle cadence | 60 s periodic + runtime-trigger wakeup | 250 ms polling fallback + event-driven wakeup |
| Filter on consume | `if deferred_until_ws: continue; if runtime_sequence is None: continue` | Same — they share `list_unconsumed_signals` |
| Roster | All `latency_class != 'fast'` traders | All `latency_class = 'fast'` traders only |

The asymmetry is NOT in either consumer path. Both consumers
share the same `intent_runtime.list_unconsumed_signals` filter.
The gate fires upstream of either consumer, in the publish
path.

The reason fast-tier is observed to "consume some
`traders_copy_trade` signals" while normal-tier observes zero
is **purely statistical**:

- Fast-tier wakes within 250ms of any of its three
  subscribed event_bus topics. When
  `_reactivate_deferred_signals_for_token` flips a signal out
  of deferred state (via a fresh CLOB WS quote), a
  `signals_update` event fires (`intent_runtime.py:2484`),
  and fast-tier picks the signal up within milliseconds — well
  inside the 15-min TTL.
- Normal-tier orchestrator polls every 60s plus
  runtime-trigger wakeup. The runtime-trigger fires when
  `publish_signal_batch` is called (which it IS for traders,
  on every publish), but the trigger's `signal_ids` list
  contains the signal that's flagged as deferred — and
  `_runtime_trigger_matches_trader` walks the per-snapshot
  `runtime_sequence`/strategy_type filter, where the deferred
  signal still has `runtime_sequence=None`. Even on the
  reactivation path, the orchestrator's 60s cycle window
  often closes before the next reactivation publish, leaving
  the signal to expire silently.

In production data captured 2026-05-07, the fast-tier window
(10:00-18:18 UTC) produced **14 executed `traders_copy_trade`
signals out of 13 941 published** — i.e. ~0.1%. The other
99.9% expired without any consumer ever seeing them. In the
normal-tier window (18:18-18:58 UTC), 0 of 174 ever-5-minute
batch of signals reached `executed`. The difference is one
signal-execution every ~30 minutes for fast vs. zero for
normal — both numbers are dominated by the same upstream
deferred-state filter.

## The gate

**File:** `backend/services/intent_runtime.py`
**Lines:** 2129-2141 (existing-signal upsert path) and
2186-2195 (new-signal path), both gated by `_ea ==
"ws_post_arm_tick"`.

**Provenance of `_ea`:** Set in
`backend/services/signal_bus.py:493-524`,
`_strategy_runtime_metadata`:

```python
def _strategy_runtime_metadata(opportunity: Opportunity) -> dict[str, Any]:
    ...
    if source_key == "crypto":
        execution_activation = "immediate"
    elif source_key == "scanner":
        execution_activation = "ws_current"
    else:
        execution_activation = "ws_post_arm_tick"   # ← traders falls here
    return {
        "strategy_slug": ...,
        "source_key": source_key,
        "subscriptions": ...,
        "execution_activation": execution_activation,
    }
```

The `else` branch was written to enforce strict-WS pricing on
arbitrary new sources. For `crypto` and `scanner` the
trade-off is correct: `crypto` markets get an `immediate`
flag (no WS-quote dependency), `scanner` markets are
already in the scanner's hot-subscription set so a fresh
WS quote is reliably available.

**For `traders`, the policy is wrong.** Copy Trade follows
leader wallets to whatever market the leader trades — which
is NOT in the scanner's hot-subscription set most of the
time. The signal is born deferred, requires a CLOB WS quote
to clear, but the CLOB WS isn't subscribed to that token, so
the deferred state never clears. The signal expires after
15 minutes (default TTL) without any consumer ever seeing it.

**Effect chain (lines refer to `intent_runtime.py`):**

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
7. `traders_copy_trade_signal_service` does NOT subscribe
   the leader-wallet token to the CLOB feed at publish time
   — it only ensures the user-channel WS is subscribed
   (`signal_bus.py:1278` calls
   `feed_manager.ensure_user_subscribed`, but that's the
   wallet user-channel, not the market-data CLOB channel).
   So unless another component (scanner discovery) has
   already pushed the token into the CLOB hot-subscription
   set, no `_on_ws_price_update` will ever fire for it.

**Production proof** (DB query 2026-05-07T19:30Z):

```
 strategy_type      | status  |  n  | with_seq | without_seq
--------------------+---------+-----+----------+-------------
 traders_confluence | pending |  30 |       30 |           0
 traders_copy_trade | pending | 445 |        0 |         445
```

Every pending `traders_copy_trade` signal has
`runtime_sequence = NULL`. None are visible to consumers.

`traders_confluence` does NOT exhibit the same shape — its
publishing entry is on the `discovery` plane via
`tracked_traders_worker`, which (a) goes through a different
publish path that does pass `runtime_sequence` to
`upsert_trade_signal`, and (b) typically targets markets
already in scanner subscription. So the gate is
source-key+`execution_activation`-driven, not source-key
alone.

## Why the gate isn't an "intentional fast-only" design

If the gate were intentional, we'd expect:

- A code comment near the `else: execution_activation =
  "ws_post_arm_tick"` line saying "fast-tier only by design"
  or similar. There isn't one.
- A symmetric handling on the consumer side: e.g.
  fast-tier explicitly looking for `traders` signals
  outside the deferred filter. There isn't.
- A configuration knob to toggle the behaviour. There
  isn't.
- Fast-tier execution that bypasses the deferred state.
  It doesn't — fast-tier hits the SAME
  `intent_runtime.list_unconsumed_signals` filter and the
  SAME `runtime_sequence is None` rejection.

The 0.1% fast-tier success rate is consistent with
incidental reactivation by tokens that happen to be in the
scanner's subscription set when the leader trades. That's
a fragile accident, not a design.

The gate is therefore classified as a **latent design
oversight**: the `else` clause in
`_strategy_runtime_metadata` was added to enforce strict-WS
pricing for arbitrary new sources, but it was never
re-evaluated when `traders` source went into production.

## Operational guidance (today)

Until a fix lands:

1. **Use `latency_class = fast` for all `traders`-source
   bots.** This won't fix the 99.9% drop rate, but it gets
   the few signals that DO reactivate to execute promptly.
   On `latency_class = normal`, even reactivated signals
   often expire before the next 60s cycle.
2. **Do not expect Copy Trade to follow more than ~0.1% of
   leader trades** until a fix lands. Operationally treat
   the source as "best-effort, sample only."
3. **Monitor `trade_signals` deferred backlog**:
   ```sql
   select count(*)
   from trade_signals
   where source = 'traders'
     and status = 'pending'
     and runtime_sequence is null
     and created_at > now() - interval '15 minutes';
   ```
   If this number is consistently > 50, the gate is hot
   and the operator should consider switching the bot off
   to avoid filling the projection queue with dead-letter
   signals.

## Conclusion

The `traders_copy_trade` signal pipeline publishes signals
into a runtime cache that flags them as
`deferred_until_ws=True, runtime_sequence=NULL`. This makes
them invisible to BOTH normal-tier orchestrator AND fast-tier
runtime until a fresh CLOB market-data quote arrives on the
signal's required token — which, for most leader-wallet
markets, never happens within the 15-min TTL because the
CLOB feed isn't subscribed to those tokens.

The asymmetry observed in production (zero normal-tier
decisions, ~14 fast-tier executed signals per day) is a
statistical artifact of fast-tier's tighter wake cadence
catching the rare reactivations within their narrow window.
It is NOT an intentional latency-class affinity.

The gate is in
`backend/services/signal_bus.py:_strategy_runtime_metadata`
(`else: execution_activation = "ws_post_arm_tick"`) which
forces every non-crypto, non-scanner source — `traders`
included — through the deferred-by-default branch in
`intent_runtime.publish_opportunities`. Removing or
narrowing that fallback (e.g. `traders` should get
`"immediate"`, like `crypto`, since it doesn't depend on a
strict-WS quote for the leader's already-executed trade) is
the right fix direction. Implementation belongs to a
separate plan.

## Open questions

None. The gate is identified, behavior is reproduced from
production data, fix direction is clear.

## See also

- [trader-pipeline.md](trader-pipeline.md) — generic signal-to-order pipeline, diagnostic playbook.
- [worker-trading.md](worker-trading.md) — process model for the trading plane, fast vs normal tiers.
- [system-overview.md](system-overview.md) — runtime topology.
- [`docs/plans/0008-investigate-traders-source-routing-on-normal.md`](../0008-investigate-traders-source-routing-on-normal.md) — the investigation plan that produced this note (now in `completed/`).
- [`docs/plans/architecture/_appendix/0008-baseline-2026-05-07.txt`](_appendix/0008-baseline-2026-05-07.txt) — baseline data captured during investigation.
