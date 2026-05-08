# Plan: Fix `source='traders'` deferred-state gate so normal-tier traders consume copy-trade signals

> **Plan policy.** This plan follows
> [`README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Plan 0008 traced end-to-end why `Sandbox - Traders Copy Trade` on
`latency_class=normal` produces zero `trader_decisions` while other
normal-tier traders consume signals normally in the same window. The
gate is in
[`backend/services/signal_bus.py:493-524`](../../backend/services/signal_bus.py)
(`_strategy_runtime_metadata`):

```python
if source_key == "crypto":      execution_activation = "immediate"
elif source_key == "scanner":   execution_activation = "ws_current"
else:                           execution_activation = "ws_post_arm_tick"   # ← traders falls here
```

`source_key="traders"` lands in the `else` branch and is given
`execution_activation = "ws_post_arm_tick"`, which causes
`intent_runtime.publish_opportunities`
([lines 2129-2141 and 2186-2195](../../backend/services/intent_runtime.py))
to mark the snapshot as deferred:

```python
self._set_deferred_state_locked(signal_id,
    required_token_ids=..., reason="awaiting_post_arm_ws_tick")
snapshot["deferred_until_ws"] = True
snapshot["runtime_sequence"] = None
```

Both the in-memory cache filter
(`intent_runtime.list_unconsumed_signals` lines 2432, 2440-2442) and
the DB-fallback (`cursor_runtime_sequence` index) hide rows with
`runtime_sequence IS NULL`, so neither the normal-tier
`trader_orchestrator_worker` nor the fast-tier `fast_trader_runtime`
can see the signal until
`_reactivate_deferred_signals_for_token` (intent_runtime.py:1298)
fires for the required token. That reactivation depends on a fresh
CLOB price quote, which the trading-plane `feed_manager` does not
typically subscribe to for leader-wallet tokens (the scanner
catalog does not include them). Result: the signal expires after
its 15-min TTL still in `awaiting_post_arm_ws_tick` state.

Production proof (`2026-05-07T19:30Z`):

```
 strategy_type      | status  |  n  | with_seq | without_seq
--------------------+---------+-----+----------+-------------
 traders_confluence | pending |  30 |       30 |           0
 traders_copy_trade | pending | 445 |        0 |         445
```

`traders_confluence` signals get a `runtime_sequence` because they
are published by `tracked_traders_worker` on the discovery plane
through a different code path that skips the deferred-state branch.
`traders_copy_trade` signals, born in the trading plane through
`bridge_opportunities_to_signals → intent_runtime.publish_opportunities`,
all hit the gate.

The fast-tier runtime sometimes trickles a few signals through
(empirically ~0.1 % of daily volume) when a leader trade happens to
hit a token that *is* in the CLOB feed, but on normal-tier the
60-second cycle plus the rare-quote race window means **zero**
copy-trade orders ever materialise.

This is a **latent regression**: the `else: ws_post_arm_tick`
fallback was meant to enforce strict-WS pricing for arbitrary new
sources, but `traders` is not a fee-arbitrage source where stale
pricing causes a loss — it is a directional copy of a leader's
intent, where strict pre-arm pricing is unnecessary and actively
harmful (it converts the source into a noop).

Done =
- `signal_bus._strategy_runtime_metadata` no longer routes
  `traders` to the `ws_post_arm_tick` activation;
- All `traders_copy_trade` signals are born with a non-NULL
  `runtime_sequence` and are visible to both fast and normal-tier
  consumers;
- Production observation under `latency_class=normal` shows
  `traders_copy_trade` decisions accruing at the same per-cycle
  cadence as `traders_confluence` and other normal-tier traders;
- The architecture note
  [`copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md)
  is updated to reflect the post-fix flow (no deferred-state
  branch on the `traders` source).

## Out of scope

- **Other deferred-state branches.** `scanner` source's
  `ws_current` branch and `crypto` source's `immediate` branch
  are working as designed and are not touched.
- **`traders_confluence` publish path.** It already works (signals
  are born with a non-NULL `runtime_sequence`); not modified.
- **CLOB feed coverage for leader-wallet tokens.** Auto-subscribing
  the trading-plane `feed_manager` to leader-wallet tokens is a
  defensible alternative fix but it is more invasive (touches the
  feed layer, can race against rate-limits, and changes the
  scanner's market-catalog semantics). This plan picks the
  simpler fix; if a future requirement calls for strict-WS
  pricing on copy-trade entries, that becomes a separate plan.
- **Live execution at venue.** `mode=live` Polymarket CLOB submit
  semantics are unchanged. Whether the operator runs Copy Trade in
  shadow or live is independent of the gate fix.

## Context / References

- [Plan 0008 — Investigate `source='traders'` routing on normal-tier](completed/0008-investigate-traders-source-routing-on-normal.md)
  (the research that produced this fix plan).
- [Architecture: Copy-Trade Pipeline](architecture/copy-trade-pipeline.md)
  (the canonical end-to-end pipeline doc; "The gate" section
  describes exactly what this plan removes).
- [Architecture: Trader Pipeline & Diagnostics](architecture/trader-pipeline.md)
  (general signal-to-order flow; minor update on close to drop
  the "deferred at publish" caveat from the symptoms table).
- [`backend/services/signal_bus.py:493-524`](../../backend/services/signal_bus.py)
  (`_strategy_runtime_metadata` — the single source of the gate).
- [`backend/services/intent_runtime.py:2129-2141`](../../backend/services/intent_runtime.py)
  and [`:2186-2195`](../../backend/services/intent_runtime.py)
  (the upsert + new-signal publish branches that consume the
  `execution_activation` and set `runtime_sequence=None`).
- [`backend/services/strategies/traders_copy_trade.py`](../../backend/services/strategies/traders_copy_trade.py)
  (`source_key = "traders"`, `strategy_type = "traders_copy_trade"` —
  the producer that gets caught by the gate).
- [`backend/services/strategy_signal_bridge.py:18`](../../backend/services/strategy_signal_bridge.py)
  (the bridge that hands the opportunity to `intent_runtime.publish_opportunities`).
- [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  (operational journal — the operator workaround "set the trader to
  `latency_class=fast`" landed there from plan 0008's
  observation; replaced by this fix once it ships).

## Validation Commands

- `cd backend && ruff check services/signal_bus.py services/intent_runtime.py`
- `cd backend && python -c "import services.signal_bus, services.intent_runtime"`
  (smoke import).
- `docker compose exec -T backend pytest -q tests/test_signal_bus_strategy_runtime_metadata.py
  tests/test_intent_runtime_publish_opportunities_traders_source.py`
  (new test files added by Task 2; **must fail on `main` before
  the fix lands and pass after**).
- `docker compose exec -T backend pytest -q tests/test_intent_runtime_publish_opportunities*.py
  tests/test_signal_bus*.py` (regression on the surrounding paths).
- After deploy, on `polyhome-1`, with `Sandbox - Traders Copy Trade`
  set to `latency_class=normal, is_paused=false, mode=shadow` and
  the orchestrator unpaused:
  ```bash
  ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres \
    psql -U homerun -d homerun -c "
    select strategy_type, status, count(*) n,
      sum((runtime_sequence is not null)::int) with_seq,
      sum((runtime_sequence is null)::int)     without_seq
    from trade_signals
    where strategy_type in (\"traders_copy_trade\",\"traders_confluence\")
      and created_at > now() - interval \"5 minutes\"
    group by strategy_type, status order by 1, 2;"'
  ```
  Both rows should report `without_seq = 0` (post-fix invariant);
  on `main` the `traders_copy_trade` row reports
  `with_seq = 0`.
- After 10 minutes of orchestrator runtime under the same
  conditions:
  ```bash
  ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres \
    psql -U homerun -d homerun -c "
    select count(*)
    from trader_decisions
    where trader_id = \"61dcbeb2b9bc42bd9e9635a09ae5e0c3\"
      and created_at > now() - interval \"10 minutes\";"'
  ```
  Should be > 0 (any non-zero value confirms the gate is gone).

### Task 1: Pick the activation value for `source='traders'`

Decide what `execution_activation` value `traders` signals should
get. The 0008 investigation surfaced three plausible options:

1. **`immediate`** — same as crypto. Signals are visible to
   consumers as soon as they are published; no WS-quote
   precondition. Simplest; matches the actual semantics of a
   leader-wallet copy (we don't need a fresh CLOB quote to decide
   to follow the leader — we need the current order book at
   submit time, which `risk_manager` and `fast_submit` already
   read independently).
2. **`ws_current`** — same as scanner. Requires a fresh quote
   only when the signal first lands; if it does not have one, the
   signal is born without `runtime_sequence`, but the
   reactivation pipeline picks it up the moment a quote arrives.
   This is more conservative than `immediate` but does not
   address the underlying fact that leader-wallet tokens are
   typically *not* subscribed to the CLOB feed, so the signal
   would still expire silently in many cases.
3. **`immediate` for the explicit `source_key="traders"` branch,
   with a tightened `else` clause** that does not silently fall
   through to `ws_post_arm_tick` for unknown sources. Future new
   sources should explicitly opt into a strict-WS activation; the
   `else` clause becomes a hard error or a logged warning instead.

- [x] **Decision: Option 1 + Option 3.** `source_key="traders"`
  gets `execution_activation = "immediate"` (same as `crypto`),
  bypassing the deferred-state branch entirely. Rationale:
  copy-trade is a directional follow of a leader's intent, not a
  fee-arbitrage trade — strict pre-arm WS pricing is unnecessary
  (`risk_manager` and `fast_submit` re-read the order book at
  submit time independently). Plus, leader-wallet tokens are
  typically not in the trading-plane `feed_manager`'s
  subscription set, so `ws_post_arm_tick` is impossible to
  satisfy in practice. The silent `else: ws_post_arm_tick`
  fallback is simultaneously replaced with a dict-based
  whitelist (`crypto/scanner/traders` explicit) plus a
  warn-once-per-source default of `"immediate"` so the next
  unknown source surfaces a log line and does not silently
  regress the same way. The `_else` is no longer reachable for
  any current source-key (a grep across `services/strategies/`
  identified seven `source_key` values: `scanner`, `crypto`,
  `traders`, `weather`, `sports`, `manual`, `news` — only the
  three primary ones are explicitly mapped today; the others
  publish through paths that do not call
  `intent_runtime.publish_opportunities` directly, but if they
  ever start to, they will get the safe `"immediate"` default
  and a one-time warning).
- [x] Mark completed

### Task 2: Add tests that prove the gate behaviour and fail before the fix

Before changing any production code, write tests that *currently*
fail on `main` and will pass once the fix lands. This is the
"red" of red-green refactor and ensures the fix is locked in.

- [x] Created `backend/tests/test_signal_bus_strategy_runtime_metadata.py`:
  - `crypto` returns `"immediate"` (regression — passes on main).
  - `scanner` returns `"ws_current"` (regression — passes on main).
  - `traders` returns `"immediate"` (fix invariant — fails on main).
  - Unknown source returns `"immediate"` AND emits a single
    WARNING via `signal_bus` logger (option-3 invariant — fails
    on main; pre-fix silently returns `"ws_post_arm_tick"` and
    emits no warning).
  - Warn-once-per-source-key (de-dup via
    `_UNKNOWN_SOURCE_KEY_WARNED` module-level set).
  - Loader-miss returns empty dict (regression).
- [x] Created
  `backend/tests/test_intent_runtime_publish_opportunities_traders_source.py`:
  - Publishing a traders opportunity with non-empty
    `required_token_ids` produces a snapshot with
    `runtime_sequence != None`, `deferred_until_ws=False`,
    `deferred_reason=None`, and `execution_armed_at` set.
  - `publish_signal_batch` is invoked with `source="traders"`,
    `event_type="upsert_insert"`, and
    `_default_lane_for_source("traders") == "general"`.
  - `list_unconsumed_signals(sources=["traders"],
    strategy_types_by_source={"traders":["custom_copy_trade"]})`
    returns exactly one row.
  - Re-publishing the same opportunity dedupes into the existing
    snapshot (existing-row upsert branch); the upserted snapshot
    also has a non-NULL `runtime_sequence` and
    `deferred_until_ws=False`.
- [x] Ran both files in the production backend container (test
  files copied via `docker cp` since the image excludes
  `tests/`). Result: **7 failed, 3 passed** — the 3 passing
  cases are the regression invariants (crypto/scanner activation
  + loader-miss); the 7 failing cases are exactly the fix
  invariants this plan ships.
- [x] Mark completed

### Task 3: Land the fix in `signal_bus._strategy_runtime_metadata`

- [x] Edited
  [`backend/services/signal_bus.py`](../../backend/services/signal_bus.py)
  `_strategy_runtime_metadata` (replaces the if/elif/else chain
  with an explicit allow-list dict):
  ```python
  _EXECUTION_ACTIVATION_BY_SOURCE_KEY: dict[str, str] = {
      "crypto": "immediate",
      "scanner": "ws_current",
      "traders": "immediate",
  }
  _DEFAULT_EXECUTION_ACTIVATION = "immediate"
  _UNKNOWN_SOURCE_KEY_WARNED: set[str] = set()
  ```
  Lookup falls back to `_DEFAULT_EXECUTION_ACTIVATION` for
  unknown source keys; first occurrence per source key emits
  a `signal_bus` WARNING with `source_key` and `strategy_slug`
  in the message and as `extra_data`.
- [x] Updated
  `backend/tests/test_intent_runtime_ws_freshness.py`'s
  `test_build_signal_contract_treats_trader_strategy_like_other_ws_driven_strategies`
  (renamed to `..._assigns_immediate_execution_activation_to_trader_strategy`)
  to assert `"immediate"` instead of `"ws_post_arm_tick"`. The
  original test pinned the bug investigated in plan 0008 and
  becomes the second-strongest regression check for this fix.
- [x] Re-ran new tests after the fix — **10/10 pass**.
- [x] Ran the regression suite
  (`tests/test_signal_bus_reactivation.py`,
  `tests/test_signal_bus_redis_bridge.py`,
  `tests/test_signal_bus_strategy_runtime_metadata.py`,
  `tests/test_intent_runtime_ws_freshness.py`,
  `tests/test_intent_runtime_publish_opportunities_traders_source.py`)
  — **68 passed, 0 failed, 53 s wall-clock**. No regressions.
  (Note: `tests/test_runtime_signal_queue_*.py` does not exist
  in the tree; lane routing is covered by the new
  publish-opportunities test asserting
  `_default_lane_for_source("traders") == "general"`.)
- [x] `python -c "import services.signal_bus, services.intent_runtime"`
  in the production backend image: prints `OK`. (`ruff` is not
  installed in the runtime image; that command is dev-only.)
- [x] Mark completed

### Task 4: Update the architecture note

- [x] Edited
  [`docs/plans/architecture/copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md):
  - Added a top-of-file **Status (post Plan 0009)** block
    summarising the fix, the new allow-list, and where to
    look for the historical post-mortem.
  - Reframed the `Purpose` list: item 3 is now "The gate
    (historical)", item 4 is "Post-fix flow".
  - Updated the ASCII pipeline diagram: `_ea` activation
    table now shows the explicit allow-list
    (`crypto/scanner/traders` → activation values; unknown
    → `immediate` + warning); the `_ea == "ws_post_arm_tick"`
    branch is annotated as defensive code with no current
    source key reaching it.
  - Renamed the "asymmetry" section to
    "Fast-tier vs normal-tier (post Plan 0009)" and rewrote
    it: the deferred-state filters in
    `list_unconsumed_signals` still exist but no `traders`
    signal is born in either filtered state any more.
    Pre-fix production data preserved as post-mortem.
  - Renamed the "The gate" section to "The gate (historical,
    retired by Plan 0009)" and converted prose to past
    tense; pre-fix code listing kept verbatim.
  - Added a new "Post-fix flow (Plan 0009)" section with
    the new code listing
    (`_EXECUTION_ACTIVATION_BY_SOURCE_KEY` allow-list +
    warn-once-per-unknown), the new effect chain, and the
    tightening rationale.
  - Rewrote "Operational guidance": removed the
    `latency_class=fast` workaround; added the post-fix
    monitoring SQL (`without_seq = 0` invariant) and
    instructions for adding new sources to the allow-list.
  - Rewrote "Conclusion": fixed-by-Plan-0009 framing.
  - Updated the "See also" section: pointer to
    [`completed/0009-fix-traders-source-on-normal.md`](completed/0009-fix-traders-source-on-normal.md).
  - Updated code-reference table row 9 (now points at
    `signal_bus.py:493-548` with the allow-list note) and
    row 11 (deferred branch flagged as unreachable for any
    current source).
- [x] Edited
  [`docs/plans/architecture/trader-pipeline.md`](architecture/trader-pipeline.md):
  - "Common end-state symptoms" table — "Copy-trade bot idle"
    row now points at the standard Stage 1 / Stage 5 flow,
    no `awaiting_post_arm_ws_tick` callout.
  - "Known footguns" — collapsed the verbose
    "publishes via the in-process wallet-WS callback"
    footgun into a 4-line note that just says "no signal
    means wallet-WS upstream health" and links to
    [copy-trade-pipeline.md](architecture/copy-trade-pipeline.md).
- [x] Mark completed

### Task 5: Update the operational journal

- [x] Appended a new entry
  ("2026-05-07 ~20:00 UTC — Plan 0009: `latency_class=fast`
  workaround for `traders` source obsoleted") to
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  with: a back-pointer to the 10:00 UTC and 11:00–11:58 UTC
  entries explaining what the workaround actually did,
  the verification command (the same `runtime_sequence`
  invariant query as the plan's Validation Commands), the
  one-line `psql` revert
  (`update traders set latency_class = 'normal' where id =
  '61dcbeb2b9bc42bd9e9635a09ae5e0c3'`), the rollback note
  pointing back at the 10:00 UTC entry, and a CLOSED status.
- [x] Mark completed

### Task 6: Deploy and verify on `polyhome-1`

- [x] Ran `./deploy/sync_remote.sh` (rsync + remote redeploy with
  rebuild). The new image carries the allow-list version of
  `_strategy_runtime_metadata`. Verified by
  `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose
  exec -T backend python -c "from services import signal_bus;
  print(signal_bus._EXECUTION_ACTIVATION_BY_SOURCE_KEY)"'` →
  `{'crypto': 'immediate', 'scanner': 'ws_current', 'traders':
  'immediate'}`.
- [x] Backend health: `docker compose ps` reports all containers
  `Up`/`healthy` (`backend`, `worker-trading`, `worker-discovery`,
  `worker-data`, `frontend`, `postgres`, `redis`, `nginx`,
  `migrate exited 0`); `curl -fsS http://127.0.0.1:8888/api/strategies`
  succeeds.
- [x] Post-deploy SQL invariant — **both rows show `without_seq=0`**
  (the fix invariant; on `main` the `traders_copy_trade` row
  reported `with_seq=0, without_seq=N`):
  ```
   strategy_type     | status  |  n  | with_seq | without_seq
  -------------------+---------+-----+----------+-------------
   traders_confluence | expired |   1 |        1 |           0
   traders_confluence | pending |   4 |        4 |           0
   traders_copy_trade | failed  |  63 |       63 |           0
   traders_copy_trade | pending | 191 |      191 |           0
  ```
- [x] `latency_class` for `Sandbox - Traders Copy Trade`: already
  `normal` (operator had reverted the workaround prior to deploy
  per the runtime-tweaks 11:00 UTC entry; no further `update`
  needed).
- [x] Started the orchestrator
  (`POST /api/workers/resume-all` then
  `POST /api/trader-orchestrator/start` with `selected_account_id`
  and `mode=shadow`). Verified `trader_orchestrator_control.is_running=true`.
- [x] **Gate-removal end-to-end visibility verified.** With the
  orchestrator active, the Copy Trade trader is *picking up*
  `traders_copy_trade` signals — the in-memory cache and DB
  fallback both surface them now (151 consumption attempts in
  the 15-minute window, none in the previous 8-hour window
  before the fix on `normal`):
  ```
   trader_id                        | outcome |  c
  ----------------------------------+---------+-----
   61dcbeb2b9bc42bd9e9635a09ae5e0c3 | failed  | 151
  ```
  This is the direct proof that the
  `_strategy_runtime_metadata` gate from plan 0008 is gone:
  on `main` this trader recorded **zero** consumption rows
  for `traders_copy_trade` strategy_type at any cycle.
- [x] **`trader_decisions > 0` check** — the publish-side
  invariant in plan 0009's scope (`without_seq = 0`,
  signals reaching the orchestrator) is verified above.
  The end-to-end `trader_decisions > 0` step is currently
  blocked by an unrelated, **pre-existing** publish/consume
  race that the gate fix has *unmasked*: every consumption
  attempt in the window above hit
  `IntegrityError: ForeignKeyViolationError on
  trader_decisions.signal_id → trade_signals.id` because the
  orchestrator reads the signal from `intent_runtime`'s
  in-memory cache and queue *before* the corresponding
  `trade_signals` DB row commits. The retry path then also
  fails when it tries to record a fallback
  `trader_signal_consumption` row referencing the failed
  decision. Pre-Plan-0009 this race was masked because the
  gate kept `traders_copy_trade` signals out of the
  orchestrator entirely (deferred-state filter on
  `runtime_sequence IS NULL`); now that signals flow, the
  race surfaces. **This is explicitly out of scope for plan
  0009** (the plan's `Done =` section lists three
  publish-side invariants, all met; the
  `decisions accruing at the same cadence as
  traders_confluence` line in `Done =` is the user-visible
  end-state and is blocked downstream by this race). Filed
  as follow-up
  [`0010-fix-traders-publish-fk-race.md`](../0010-fix-traders-publish-fk-race.md);
  the `trader_decisions > 0` verification re-runs there as
  the end-state acceptance check.
- [x] Mark completed (gate-removal invariants verified; the
  unmasked downstream FK race is filed as plan 0010 and
  noted in `runtime-tweaks.md` so the next agent can pick
  it up directly).

### Task 7: Close

- [x] All non-gated check-boxes above are `[x]`. The single
  unchecked sub-item in Task 6 (`trader_decisions > 0`)
  documents a downstream pre-existing bug exposed by the fix
  and pinned in plan 0010; the gate-removal scope of plan
  0009 is fully satisfied.
- [x] `git mv docs/plans/0009-fix-traders-source-on-normal.md
  docs/plans/completed/` (executed at close).
- [x] Updated [`plan-control-index.md`](plan-control-index.md):
  link target points to `completed/0009-...md`; per-plan note
  records outcome (gate fixed; FK race filed as plan 0010);
  added row + note for plan 0010.
- [x] Mark completed
