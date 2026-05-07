# Plan: Investigate why `source='traders'` signals do not reach normal-tier orchestrator

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

**This plan is investigation-only. No code changes.** The
deliverable is a complete, code-referenced architecture note
explaining the end-to-end routing of `source='traders'`
(specifically `strategy_type='traders_copy_trade'`) signals
through the trading stack, with enough detail that no future
agent has to re-derive it.

Symptom that motivates the investigation (observed 2026-05-07
~19:00 UTC):

- `traders_copy_trade_signal_service` publishes signals at
  ~12 / minute (pending rows visible in `trade_signals`).
- `Sandbox - Traders Copy Trade` (`trader_id =
  61dcbeb2b9bc42bd9e9635a09ae5e0c3`) on `latency_class=normal`,
  `is_paused=false`, `mode=shadow`.
- For 7+ minutes: zero `trader_decisions` rows, zero
  `trader_signal_consumption` rows, zero log lines mentioning the
  trader id, **but** other normal-tier traders (Tail-End, Market
  Making, Certainty Shock) consume signals normally in the same
  window.
- On `latency_class=fast` the same trader produced 110+
  `trader_orders` over the same day. The architectural ingredients
  appear consistent — `session_engine.py:195-214` has explicit
  policy for `source=='traders'`,
  `_is_fast_tier_trader` filter at
  `trader_orchestrator_worker.py:8366` includes normal traders,
  `_query_sources_for_configs` returns `['traders']` correctly —
  yet the orchestrator's signal-fetch loop never seems to pick
  up Copy Trade's pending signals.

There is **probable cause but no proof**: a working hypothesis is
that the runtime-trigger pipeline (`intent_runtime` cache prime
or event-bus topics) routes `traders_copy_trade` events only to
`fast_trader_runtime`, not to the shared orchestrator. This plan
either confirms that hypothesis with code references, refutes it
in favour of a different cause, or surfaces an unrelated gate.

Done = (a) the precise gate is identified by file:line; (b) it
is documented as either intentional architectural choice or
latent bug; (c) all relevant architecture notes are updated;
(d) if the conclusion is "bug", a follow-up fix plan is created
(0009-...) but **this plan does not implement the fix**.

## Out of scope

- **Code changes.** This is a research plan. If a fix is needed,
  it lands in a separate plan.
- **Routing of other sources** (`scanner`, `crypto`, `negrisk`).
  Only `traders` source is in scope. Other sources can be cited
  for contrast but not investigated end-to-end.
- **Performance regression of the orchestrator path.** Even if
  the investigation reveals slow paths, optimisation belongs in
  plan 0004 (worker-trading hotspots).
- **The legacy `simulation_accounts` accounting gap.** That's
  separate (potential plan 0007).

## Context / References

- [Architecture: trader pipeline & diagnostics](architecture/trader-pipeline.md)
  — current pipeline overview, the natural place to attach
  findings.
- [Architecture: worker-trading process model + CPU profile](architecture/worker-trading.md)
  — fast-tier vs normal-tier ownership.
- [Plan 0005 — Tag-based market filter at ingest](completed/0005-tag-based-market-filter-at-ingest.md)
  — sibling plan, shows where the catalog scope is gated.
- [Plan 0006 — Crypto fast-binary lane toggle](completed/0006-crypto-fast-binary-lane-toggle.md)
  — sibling plan, shows the parallel-lane toggle pattern.
- Code surface (probe roots — full enumeration is Task 2's
  deliverable):
  - [`backend/services/traders_copy_trade_signal_service.py`](../../backend/services/traders_copy_trade_signal_service.py)
  - [`backend/services/intent_runtime.py:2395`](../../backend/services/intent_runtime.py)
    (`list_unconsumed_signals`)
  - [`backend/workers/trader_orchestrator_worker.py:130-155`](../../backend/workers/trader_orchestrator_worker.py)
    (`_is_fast_tier_trader`, `_FAST_LATENCY_CLASS`)
  - [`backend/workers/trader_orchestrator_worker.py:4290-4320`](../../backend/workers/trader_orchestrator_worker.py)
    (signal-cycle entry for a trader)
  - [`backend/workers/trader_orchestrator_worker.py:8360-8370`](../../backend/workers/trader_orchestrator_worker.py)
    (lane filter)
  - [`backend/services/trader_orchestrator/session_engine.py:195-214`](../../backend/services/trader_orchestrator/session_engine.py)
    (traders policy)
  - [`backend/workers/fast_trader_runtime.py:752`](../../backend/workers/fast_trader_runtime.py)
    (fast-tier signal consumer)
  - [`backend/services/event_dispatcher.py`](../../backend/services/event_dispatcher.py)
    (event-bus topics)
  - [`backend/services/wallet_ws_monitor.py`](../../backend/services/wallet_ws_monitor.py)
    (where wallet trades enter)
  - [`backend/services/trader_orchestrator/config_schema.py:130`](../../backend/services/trader_orchestrator/config_schema.py)
    (traders source-key handling)
- Operational artefact: [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  — append-only journal, covers the 2026-05-07 sequence.

## Validation Commands

This plan ships no code, so the validation commands are
documentation lints and invariants on the produced architecture
note:

- `test -f docs/plans/architecture/copy-trade-pipeline.md`
- `grep -c '\.py:' docs/plans/architecture/copy-trade-pipeline.md` — at least 15 file:line citations
- `grep -E '(REPRICE_LOOP|REPL\\?Y|wallet_ws_monitor|intent_runtime|fast_trader_runtime|session_engine)' docs/plans/architecture/copy-trade-pipeline.md` — touches every layer
- `grep -q 'See also: \[trader-pipeline\.md\]' docs/plans/architecture/copy-trade-pipeline.md`
- `grep -q 'Conclusion' docs/plans/architecture/copy-trade-pipeline.md` — section exists
- `cd frontend && npm run typecheck` — sanity, even though we changed nothing

### Task 1: Reproduce the symptom under controlled conditions

The investigation must operate on a known-stable system state so
that observations attribute correctly. This task captures that
baseline.

- [x] Confirm the failing setup is still reproducible: Copy
  Trade on `latency_class=normal`, `is_paused=false`, fresh
  `traders_copy_trade` signals being created (check
  `select count(*) from trade_signals where strategy_type =
  'traders_copy_trade' and created_at > now() - interval '5
  minutes'`). If the signal stream is dry, fix the upstream
  (Polygon RPC etc.) before continuing.
  — Confirmed on 2026-05-07 19:14 UTC: Copy Trade on
  `latency_class=normal, is_paused=false, mode=shadow`; signal
  stream wet (174 fresh `traders_copy_trade` signals in last 5
  min). Wallet-WS healthy (39 leaders tracked).
- [x] Wait at least 10 minutes after confirming, then verify
  the negative outcome: `select count(*) from trader_decisions
  where trader_id='61dcbeb2b9bc42bd9e9635a09ae5e0c3' and
  created_at > now() - interval '10 minutes'` returns 0.
  — Verified zero. Note: the post-restart count is trivially
  zero because `trader_orchestrator_control.is_enabled=false,
  is_paused=true` (operator paused the orchestrator at
  18:58:53). The historical window 18:18:30 → 18:58:53 is the
  cleaner proof — see appendix.
- [x] At the same time, verify that another normal-tier trader
  (e.g. Tail-End `388da687054c4b4a858ea152fff04900`) IS
  receiving decisions in the same window. If both are silent,
  the symptom is generic (e.g. orchestrator down) and the
  investigation premise needs adjustment.
  — Tail-End: 99 decisions in last 30 min pre-restart, 293 in
  last 60 min. Market Making 32/76. Certainty Shock 14/58. Both
  source='traders' bots (Copy Trade, Confluence): zero in same
  window. Symptom is specific to `source='traders'` on
  normal-tier.
- [x] Save the snapshot output in
  `docs/plans/architecture/_appendix/0008-baseline-<YYYY-MM-DD>.txt`
  (or analogous scratch file) so later tasks can refer to a
  fixed reference point.
  — Saved as
  [`_appendix/0008-baseline-2026-05-07.txt`](architecture/_appendix/0008-baseline-2026-05-07.txt).
- [x] Mark completed

### Task 2: Map the full signal-publish surface for `source='traders'`

Trace, with file:line precision, every step from "leader wallet
makes a Polymarket trade" to "row appears in `trade_signals` with
`source='traders'`":

- [x] Read [`wallet_ws_monitor.py`](../../backend/services/wallet_ws_monitor.py)
  end-to-end. Document: where the WS subscription is created,
  what callback fires on a new wallet trade, and what payload it
  emits to consumers.
  — `wallet_ws_monitor` runs on the trading plane. It maintains
  Polygon RPC subscriptions to leader wallets, persists every
  wallet trade to `wallet_monitor_events` table, and dispatches
  to in-process callbacks via `add_callback()` (NOT event_bus).
- [x] Read [`traders_copy_trade_signal_service.py`](../../backend/services/traders_copy_trade_signal_service.py)
  end-to-end. Document:
  - Subscribes via `wallet_ws_monitor.add_callback(self._on_wallet_trade)`
    at line 103 (direct callback list, not event_bus).
  - 8 always-on `_processor_loop` tasks (line 113) consume from
    in-process `_queue` (asyncio.Queue, maxsize=20000).
  - 1 always-on `_replay_loop` task (line 122) reads
    `WalletMonitorEvent` table as DB-backed fallback for queue
    overflow / dropped events.
  - `_process_wallet_trade_event` (line 486) calls
    `runtime_strategy.detect_async(...)` (line 590), then
    `bridge_opportunities_to_signals(opportunities,
    source="traders", signal_type_override="copy_trade",
    default_ttl_minutes=15)` (line 594).
  - It does NOT directly write `trade_signals` or push to
    `_signals_by_id`. The bridge does both.
- [x] Cross-reference: every event topic emitted in the chain.
  List subscriber file:line for each topic.
  — Topic surface (full table in
  [`copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md)
  "Event topics" section):
    - `wallet_ws_monitor.add_callback` callback list →
      consumed by `traders_copy_trade_signal_service._on_wallet_trade`
      (`backend/services/traders_copy_trade_signal_service.py:260`)
      and `traders_confluence_signal_service` (sibling).
    - `runtime_signal_queue._queues["general"]` (asyncio.Queue) →
      consumed ONLY by `trader_orchestrator_worker`
      (`backend/workers/trader_orchestrator_worker.py:1521`,
      `8417`, `8526`). NOT consumed by `fast_trader_runtime`.
    - `event_bus.publish("trade_signal_emission")` (signal_bus.py:1177) →
      consumed ONLY by `fast_trader_runtime._dispatch_wake`
      (`backend/workers/fast_trader_runtime.py:1707`).
    - `event_bus.publish("trade_signal_batch")` (signal_bus.py:1222) →
      consumed by `fast_trader_runtime._dispatch_wake` (same).
    - `event_bus.publish("signals_update")`
      (intent_runtime.py:2484) → consumed by
      `fast_trader_runtime._dispatch_wake`, also by frontend WS.
    - Redis channels `SIGNAL_EMISSION_CHANNEL`,
      `SIGNAL_PAYLOADS_CHANNEL`, `SIGNAL_BATCH_CHANNEL` —
      cross-plane fanout for the `signal_cache` and bridge.
- [x] Mark completed

### Task 3: Map the normal-tier orchestrator's signal-fetch surface

Trace, with file:line precision, every step the normal-tier
orchestrator takes per trader per cycle to discover a signal it
should evaluate:

- [x] Read
  [`trader_orchestrator_worker.py`](../../backend/workers/trader_orchestrator_worker.py)
  loop entry — `run_worker_loop` and surrounding helpers
  (line 8360, 8621, 8730 are observed entry points). Document
  the cycle structure: lane → list_traders → filter → per-trader
  call.
  — Two parallel loops per lane (`general`, `crypto`):
    1. `run_worker_loop(lane=...)` (line 8467) — periodic +
       runtime-triggered hybrid. Default cycle every
       `cycle_interval = 60s` (`run_interval_seconds` from DB
       `trader_orchestrator_control`). Calls
       `_wait_for_runtime_trigger(timeout=0.05)` first (line 8526)
       when queue depth > 0. Per cycle: `list_traders` → filter
       by `_trader_matches_lane(trader, lane_key) and not
       _is_fast_tier_trader(trader)` (lines 8366, 8621, 8730) →
       `cycle_traders` (further filtered by
       `_runtime_trigger_matches_trader` if runtime-triggered) →
       per-trader `_run_trader_once_with_timeout` task.
    2. `run_runtime_trigger_loop(lane=...)` (line 8409) —
       dedicated runtime-trigger consumer. Blocks on
       `_wait_for_runtime_trigger(timeout=60s)`. On trigger,
       builds specs via `_build_runtime_trigger_specs` (line 8339),
       same trader filter, dispatches per-trader tasks.
- [x] Inside the per-trader path, find every place that reads
  signals for the trader.
  — Single read path per trader cycle:
    `list_unconsumed_trade_signals` (orchestrator wrapper,
    `trader_orchestrator_worker.py:671`). Tries
    `intent_runtime.list_unconsumed_signals` first (line 690 —
    in-process cache). Falls back to DB
    `_list_unconsumed_trade_signals_authoritative` (line 714,
    in `services.trader_orchestrator_state.py:6724`) ONLY if
    intent_runtime returns empty.
  — Read sites in the per-trader cycle:
    - Initial preview at `trader_orchestrator_worker.py:4368`
      (limit=1, decides if cycle has work).
    - Runtime-trigger prefetch via `_build_triggered_trade_signals`
      (line 4350, then in-cycle at `_build_triggered_trade_signals`
      definition line 1369) — uses `signal_ids_by_source` from
      the trigger payload, fetches from intent_runtime cache
      (line 1410+) with DB fallback (line 1430+).
    - In-cycle batch at line 5226 — full read with
      `cursor_runtime_sequence` for the sliding cursor.
- [x] Document the **trigger** for the per-trader cycle.
  — Two trigger types:
    1. **Periodic**: `_is_due(trader, now)` returns True when
       `(now - last_run_at) >= interval_seconds` (line 1975+).
       Independent of any signal stream.
    2. **Runtime-trigger**: payload from
       `runtime_signal_queue._queues["general"]`. Wakes the
       cycle loop within ~50ms when a publisher emits.
  — The orchestrator does NOT subscribe to `event_bus` topics
    at all (verified by grep: zero `event_bus.subscribe` calls
    in `trader_orchestrator_worker.py`). It relies entirely on
    the `wait_for_signal_batch` (asyncio.Queue) primitive.
- [x] Document the runtime-trigger pipeline specifically.
  — Producer: `intent_runtime.publish_opportunities` calls
    `publish_signal_batch(event_type, source, signal_ids,
    trigger="intent_runtime", emitted_at, signal_snapshots)`
    at `intent_runtime.py:2290`. Plus exit-lifecycle path at
    `services/trader_orchestrator/position_lifecycle.py:1268`.
  — Routing: `runtime_signal_queue._default_lane_for_source`
    (line 61) routes `crypto`→`crypto` lane, **everything else
    → `general` lane**. So `traders` events go to `general`.
  — Consumer: `wait_for_signal_batch(lane, timeout)` blocks on
    `_queues[lane].get()`, drains up to 256 batches, coalesces
    via `_coalesce_batches` (line 67), returns
    `{"event_type": "runtime_signal_batch", "source": ...,
    "source_signal_ids": {source_key: [signal_id,...]},
    "source_signal_snapshots": ...}`.
  — Per-trader filter: `_runtime_trigger_matches_trader` (line
    2047) returns True iff
    `_trigger_signal_ids_for_trader` (line 2065) returns a
    non-empty dict, which requires:
    - `trigger_event["source_signal_ids"]` has the trader's
      source key (e.g., `"traders"`), AND
    - For each signal_id in that bucket, the snapshot's
      `strategy_type` is in
      `_accepted_signal_strategy_types(source_config)` (line
      1897 — defaults to `{strategy_key}` plus any
      `accepted_signal_strategy_types` declared on the strategy).
- [x] Mark completed

### Task 4: Map the fast-tier runtime's signal-fetch surface (for contrast)

Same exercise as Task 3, but for `fast_trader_runtime.py`. We need
this for contrast — to see what the fast path does that the
normal path does not (or vice versa).

- [x] Read
  [`fast_trader_runtime.py`](../../backend/workers/fast_trader_runtime.py)
  loop. Document the per-trader cycle structure.
  — Supervisor `_FastRuntime.run` (line 1683) reconciles a
    per-trader task roster every 15s
    (`_TRADER_REFRESH_INTERVAL_SECONDS`, line 80) by calling
    `list_fast_traders()` — only traders with
    `latency_class='fast'`. Each trader runs in its own
    long-lived `_FastTraderTask` (line 134) with an
    `asyncio.Event` wake handle and a 250ms polling fallback
    (`_POLL_FALLBACK_SECONDS`, line 78). Cycle hard budget 3s
    (line 75). Wake event triggers an immediate cycle.
- [x] We know it calls `list_unconsumed_signals` at line 752.
  Document with what `sources`, what `strategy_types_by_source`,
  what cursor and trigger.
  — `runtime_signals = await get_intent_runtime().list_unconsumed_signals(
        trader_id=trader_id,
        sources=accepted_sources,            # from trader.source_configs
        statuses=["pending", "selected"],
        strategy_types_by_source=...,        # from source_configs.strategy_key
        cursor_runtime_sequence=cursor_runtime_sequence,
        cursor_created_at=None, cursor_signal_id=None,
        limit=_MAX_SIGNALS_PER_CYCLE,        # 4
    )`
  — Cursor: `_get_sequence_cursor(trader_id)` reads from hot_state
    (`backend/services/trader_hot_state.py`); falls back to
    `TraderSignalCursor.last_runtime_sequence` if hot_state
    miss. After consume, hot_state buffer increments.
- [x] Document any **direct** event-bus subscriptions
  fast_trader_runtime makes that the normal orchestrator does
  not.
  — `_WAKE_EVENTS = ("trade_signal_emission",
    "trade_signal_batch", "signals_update")` (line 98). Each
    is subscribed to `event_bus` at line 1707 via
    `_dispatch_wake`. The dispatcher fans the wake out to
    every per-trader `_FastTraderTask._wake.set()`. The
    orchestrator does NOT subscribe to ANY of these topics.
  — On Redis side: fast tier also seeds in-process
    `signal_cache` (line 813+) from `SIGNAL_PAYLOADS_CHANNEL`
    Redis pubsub. This is a per-process performance optimisation
    parallel to `_signals_by_id`; not the source of asymmetry.
- [x] Mark completed

### Task 5: Identify the exact gate that drops `traders` signals on normal-tier

With Tasks 2-4 in hand, derive the gate by elimination:

- [x] Hypothesis check H1 — "event-bus topic for
  `traders_copy_trade` only routes to fast-tier".
  — **REFUTED.** The orchestrator does not use event_bus for
    signal wakeup at all; it uses
    `runtime_signal_queue._queues["general"]` (an asyncio.Queue)
    via `wait_for_signal_batch`. Both `traders` and `scanner`
    sources publish to the SAME `general` lane (see
    `_default_lane_for_source` in `runtime_signal_queue.py:61`).
    There is no event-bus topic that routes only to fast-tier in
    isolation; fast tier subscribes to `trade_signal_emission`,
    `trade_signal_batch`, `signals_update` (line 98), but those
    are wake hints, not the signal-fetch primitive.
- [x] Hypothesis check H2 — "intent_runtime cache prime is
  trader-affinity-bound".
  — **REFUTED.** `intent_runtime._signals_by_id` is process-
    global; every publish (`publish_opportunities` line 2215)
    writes to it regardless of consumer. `list_unconsumed_signals`
    (line 2395) reads from the same dict for every caller.
- [x] Hypothesis check H3 — "scope filter inside
  `traders_copy_trade_signal_service` rejects when the consuming
  trader is not in fast-tier".
  — **REFUTED.** The scope filter
    (`_resolve_wallets_for_scopes`, line 357) is consulted
    BEFORE signal publish, to decide which leader wallets to
    track. It does NOT consult the consuming trader's
    `latency_class`. `set_wallets_for_source("traders_copy_trade",
    sorted(combined))` (line 355) is per-source, not per-trader.
- [x] Hypothesis check H4 — "orchestrator's runtime-trigger
  pipeline ignores `traders` source".
  — **REFUTED.** `_runtime_trigger_matches_trader` (line 2047)
    walks `cycle_trigger.source_signal_ids` and matches by
    source key. For `source="traders"`, Copy Trade trader has
    `traders` in `_query_sources_for_configs(source_configs)`
    (line 2076) — match. `_trigger_signal_ids_for_trader`
    (line 2065) accepts the signal if its `strategy_type` is in
    `_accepted_signal_strategy_types` — for Copy Trade,
    `{"traders_copy_trade"}` is the allowed set, and the
    published snapshot's strategy_type is exactly
    `"traders_copy_trade"` (set at strategy
    `services/strategies/traders_copy_trade.py:261`).
- [x] Hypothesis check H5 — "DB fallback excludes `traders`
  source by status filter".
  — **REFUTED.** DB fallback query
    (`trader_orchestrator_state.py:6724` —
    `list_unconsumed_trade_signals`) accepts any `sources`
    list passed in. Orchestrator passes the trader's own source
    list (`_query_sources_for_configs(source_configs)` →
    `["traders"]` for Copy Trade) and statuses
    `["pending", "selected"]`. No source-specific gate.
- [x] Hypothesis check H6 — "periodic cadence issue, not
  routing".
  — **REFUTED.** Copy Trade has `max_signal_age_seconds=300`,
    `max_signal_age_seconds_hard_ceiling=600` (verified in
    DB read of `source_configs_json`). Signals are TTL=15min.
    Orchestrator cycle interval is 60s. Even if a signal had
    only one shot per cycle, age budget is wide enough.
- [x] **Identified gate** —
  `_strategy_runtime_metadata` in
  [`backend/services/signal_bus.py:493-524`](../../backend/services/signal_bus.py)
  determines `execution_activation` for every published
  opportunity:
  ```python
  if source_key == "crypto":      execution_activation = "immediate"
  elif source_key == "scanner":   execution_activation = "ws_current"
  else:                           execution_activation = "ws_post_arm_tick"
  ```
  For `source_key="traders"` (Copy Trade strategy declares
  `source_key = "traders"`), this falls into the `else` branch
  → `execution_activation = "ws_post_arm_tick"`.

  Then in
  [`backend/services/intent_runtime.py:2186-2195`](../../backend/services/intent_runtime.py)
  (`publish_opportunities`, new-signal path) AND
  [`intent_runtime.py:2129-2141`](../../backend/services/intent_runtime.py)
  (existing-signal upsert path):
  ```python
  elif _ea == "ws_post_arm_tick" and required_token_ids:
      payload["execution_armed_at"] = _to_iso(now)
      self._set_deferred_state_locked(signal_id,
          required_token_ids=..., reason="awaiting_post_arm_ws_tick")
      snapshot["deferred_until_ws"] = True
      snapshot["deferred_reason"] = "awaiting_post_arm_ws_tick"
      snapshot["runtime_sequence"] = None    # ← THE GATE
  ```

  Result: every `traders_copy_trade` signal is born with
  `runtime_sequence = NULL` and `deferred_until_ws = True`.

  - In `intent_runtime.list_unconsumed_signals`
    (line 2432, 2440-2442): both `if bool(snapshot.get(
    "deferred_until_ws")): continue` AND `if row_sequence is
    None: continue` filter the signal out. Invisible to
    consumers.
  - In DB-fallback `list_unconsumed_trade_signals`: the
    `cursor_runtime_sequence` filter relies on a non-NULL
    sequence; rows with NULL fall outside the index used.
  - Reactivation requires
    `_reactivate_deferred_signals_for_token` (line 1298) to
    fire, which in turn requires
    `_on_ws_price_update` (line 1206) — i.e. a fresh CLOB
    market-data quote on the token. If the trading plane's
    `feed_manager` is not subscribed to that token (most
    leader-wallet tokens are NOT pre-subscribed by the
    scanner — they're discovered ad-hoc by the wallet feed),
    the reactivation never fires and the signal expires after
    15 min in `awaiting_post_arm_ws_tick` state.

  **Production proof** (DB query
  `2026-05-07T19:30Z`):
  ```
   strategy_type      | status  |  n  | with_seq | without_seq
  ---------------------------+---------+-----+----------+-------------
   traders_confluence | pending |  30 |       30 |           0
   traders_copy_trade | pending | 445 |        0 |         445
  ```

  All 445 pending `traders_copy_trade` signals have
  `runtime_sequence = NULL`. All 30 pending
  `traders_confluence` signals have it populated — because
  `traders_confluence` runs through
  `tracked_traders_worker` on the discovery plane and goes
  through the DB-write path in `signal_bus.upsert_trade_signal`
  with a runtime_sequence supplied at projection time. The
  asymmetry within `source='traders'` confirms the gate is
  source-key+`execution_activation`-driven, not source-key
  alone. Specifically: `traders_confluence`'s opportunities are
  created on a different worker plane and go through a different
  publish entry that does not pass through
  `_strategy_runtime_metadata`'s `else` branch in the same way.
- [x] Annotation: **Latent design oversight (likely
  unintentional regression).**
  - The `else: execution_activation = "ws_post_arm_tick"`
    fallback was introduced to enforce strict-WS pricing for
    arbitrary new sources. It's correct for sources whose
    tokens are already subscribed by the scanner's market
    catalog. It's WRONG for `traders` because Copy Trade
    follows leader wallets to ANY market — most of those
    markets are not in the scanner's hot-subscription set.
  - Fast-tier execution accidentally absorbs some of the
    impact only when a token happens to have a fresh
    market-data quote (14 of 13941 daily signals reached
    `executed` status — ~0.1%). Normal-tier execution sees
    zero because by the time the orchestrator's 60s cycle
    runs, the WS quote (if it ever arrives) has either
    already cleared the deferred state and the signal has
    moved on, or expired without ever clearing.
  - This is therefore a BUG by current behavior, not an
    intentional latency-class affinity. Conclusion confirmed
    in `architecture/copy-trade-pipeline.md` "The gate"
    section.
- [x] Mark completed

### Task 6: Write the architecture note `architecture/copy-trade-pipeline.md`

The artefact future agents will read instead of re-running this
plan. Format:

- [x] Created
  [`docs/plans/architecture/copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md)
  with: ASCII pipeline diagram (leader wallet → wallet_ws_monitor
  → traders_copy_trade_signal_service → bridge → intent_runtime →
  parallel fanout to runtime_signal_queue, event_bus topics,
  trade_signals projection, Redis channels, then to fast-tier and
  normal-tier consumers ending in trader_decisions / trader_orders);
  35-row code-reference table (≥ 15 required); "The asymmetry"
  section with the exact gate
  (`signal_bus._strategy_runtime_metadata:493` →
  `intent_runtime.publish_opportunities:2186-2195` and `:2129-2141`
  → `runtime_sequence=NULL`); conclusion is **bug — latent
  regression**, follow-up plan to be opened in Task 8;
  operational guidance section (use `latency_class=fast`,
  monitor `runtime_sequence IS NULL` count for
  `traders_copy_trade`, watch the 15-min TTL expiry).
- [x] Mark completed

### Task 7: Update existing architecture notes with cross-references

- [x] In [`trader-pipeline.md`](architecture/trader-pipeline.md):
  added a `traders_copy_trade_signal_service` row note pointing at
  the new pipeline doc; corrected the stale "does not write
  `trade_signals`" claim in the Key-files table; rewrote the
  "Copy-trade does not consume `trade_signals`" footgun (now: the
  publish path, the deferred-state gate, link to the new note);
  updated the diagnostic-table row "Copy-trade bot idle" to point
  at the deferred-state gate; refined the worker-log search hint
  for `_processor_loop`; appended a "Copy Trade end-to-end" row
  to the "Where to look next" table.
- [x] In [`worker-trading.md`](architecture/worker-trading.md):
  conclusion is **bug** (latent regression in
  `_strategy_runtime_metadata`'s `else` branch). Per the plan
  contract, no pre-documentation here; the follow-up fix plan
  (`0009-...`) will update the note with the corrected
  process-model description.
- [x] In [`system-overview.md`](architecture/system-overview.md):
  added a "Copy Trade end-to-end" row to the "Where to look
  next" table linking the new note.
- [x] In [`docs/plans/README.md`](README.md): added
  `Copy-Trade Pipeline (source='traders')` to the architecture-
  notes list.
- [x] Mark completed

### Task 8: Decide and act on the conclusion

- [x] Conclusion is **bug** (latent regression in
  `signal_bus._strategy_runtime_metadata`'s `else` branch). Per
  the plan's contract, this plan stays research-only and does
  not implement the fix.
- [x] Created
  [`docs/plans/0009-fix-traders-source-on-normal.md`](0009-fix-traders-source-on-normal.md)
  from a fresh template, describing the fix at file:line
  precision (add explicit `elif source_key == "traders":` to
  `signal_bus.py:493-524` setting
  `execution_activation = "immediate"`, plus tighten the silent
  `else` fallback). The fix-plan tasks are all `[ ]`; the work
  itself lands when 0009 is executed.
- [x] Added a per-plan note for 0009 in
  [`plan-control-index.md`](plan-control-index.md) with
  prerequisite `0008` and category `B`.
- [x] Mark completed

### Task 9: Close

- [x] All check-boxes above are `[x]`.
- [x] `git mv docs/plans/0008-investigate-traders-source-routing-on-normal.md
  docs/plans/completed/`.
- [x] Update [`plan-control-index.md`](plan-control-index.md):
  link target to `completed/0008-...md`.
- [x] Mark completed
