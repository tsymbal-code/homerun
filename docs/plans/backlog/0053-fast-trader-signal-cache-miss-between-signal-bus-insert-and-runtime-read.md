# Plan: Fast-trader signal cache miss between signal_bus INSERT and intent_runtime read

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0053` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Plan 0052 closed the projection-sweep race in
`signal_bus.expire_source_signals_except`: after deploy (2026-05-11
18:30 UTC onward), zero `crypto_5m_last_outcome` signals expire with
`age_at_update_s < 60 s`. The grace guard works as designed.

But removing that race exposed a **second, independent defect**:
the trader `eff366f86217484b98950ea836099a02` ("Crypto 5m Last
Outcome", `latency_class=fast`) **failed to emit any
`trader_decisions` rows** for 6 of the 18 signals in the
post-deploy window — specifically every signal in the 18:30 and
18:40 5 m cycles. `worker-trading` logs for those misses show:

```
{"level":"WARNING","msg":"Fast trader cycle exceeded hard budget",
 "data":{"trader_id":"eff366f8...","duration_s":3.334,
         "stage_timings_ms":{"runtime_list_signals":0.6,
                              "signal_cache_hit":0.1,
                              "signal_source":"cache",
                              "idle_touch_commit":3332.9}}}
```

Diagnostic reading:

- `runtime_list_signals: 0.6 ms` →
  `IntentRuntime.list_unconsumed_signals`
  ([`backend/workers/fast_trader_runtime.py:807`](../../backend/workers/fast_trader_runtime.py))
  returned ZERO rows.
- `signal_cache_hit: 0.1 ms` + `signal_source: "cache"` →
  `signal_cache.get_unconsumed_signals`
  ([`backend/workers/fast_trader_runtime.py:904`](../../backend/workers/fast_trader_runtime.py),
  cache code at
  [`backend/services/signal_cache.py:493-553`](../../backend/services/signal_cache.py))
  ALSO returned zero. Both layers agree there are no candidate
  signals.
- `idle_touch_commit: 3332.9 ms` → trader cycle entered the idle
  branch and burned its hard budget on a no-op DB commit, while
  three fresh `pending` `crypto_5m_last_outcome` signals existed
  in `trade_signals` for that 60 s grace window.

So the bug is between **`signal_bus.upsert_trade_signal`**
([`backend/services/signal_bus.py`](../../backend/services/signal_bus.py))
and either:

- (A) the Redis `signal_payloads` channel publish path
  ([`signal_bus.py:1181, :1188`](../../backend/services/signal_bus.py),
  consumed by
  [`signal_cache.py:751-824`](../../backend/services/signal_cache.py)
  `_Subscriber._run`), or
- (B) the in-process `IntentRuntime._signals_by_id` snapshot read
  by `IntentRuntime.list_unconsumed_signals`, or
- (C) the per-trader `cursor_runtime_sequence` cursor read by
  `signal_cache.get_unconsumed_signals` (filter at
  [`signal_cache.py:534-537`](../../backend/services/signal_cache.py)).

Pre-existing context: Plan 0010 already solved a related class of
race for `traders_copy_trade` (FK race between in-memory pings and
the projection commit). Plan 0011 added skeleton-row TTLs. Plan 0044
extended firehose binding cache to shadow traders. The current
defect is none of those — it bites `crypto` source, fast-tier
trader, with `runtime_sequence` set non-NULL by `_strategy_runtime_metadata`
(`crypto → immediate`, see
[Plan 0009](completed/0009-fix-traders-source-on-normal.md)).

"Done" = trader `eff366f86217484b98950ea836099a02` consumes ≥ 90 %
of `crypto_5m_last_outcome` signals over a 30 m post-deploy window
(measured as `executed / (executed + expired)` in the same query
plan 0052 used for verification). Equivalently:
`trader_decisions` rows count == `trade_signals` rows count for
that trader_id over the same window.

## Context / References

- [Plan 0052: Grace period in `expire_source_signals_except`](completed/0052-grace-period-in-expire-source-signals-except.md) —
  the race-fix that exposed this defect; pre/post-fix evidence in
  [`work-artifacts/0052-pre-fix-evidence.md`](work-artifacts/0052-pre-fix-evidence.md)
  and [`work-artifacts/0052-post-fix-evidence.md`](work-artifacts/0052-post-fix-evidence.md).
- [Plan 0051: REST book-fallback for `crypto_5m_last_outcome`](completed/0051-rest-book-fallback-for-crypto-5m-last-outcome.md) —
  fixed the WS book-cache miss; raised emit rate to 100 %.
- [Plan 0010: Fix `trader_decisions` FK race](completed/0010-fix-traders-publish-fk-race.md) —
  the prior cache/projection sync fix; same area, different race
  (FK ordering, not cache freshness). Solution there was the
  skeleton-INSERT pass; this plan needs to determine whether the
  skeleton publishes on `signal_payloads` and whether the cache
  consumes those publishes.
- [Plan 0032: Eliminate fast-trader dedup-spam (signal_cache deep fix)](completed/0032-eliminate-fast-trader-dedup-spam.md) —
  the consumed-set hydration plan; diagnostic baseline for cache
  semantics and per-trader filter behaviour.
- [Architecture: Copy-trade pipeline](architecture/copy-trade-pipeline.md) —
  steps 13-25 cover the publish ⇒ runtime queue ⇒ DB projection ⇒
  cross-plane fanout ⇒ fast-trader read sequence. Step 17a (added
  by plan 0052) documents the grace guard.
- [Architecture: Trader pipeline](architecture/trader-pipeline.md)
- [Architecture: WebSocket and events](architecture/websocket-and-events.md) —
  documents the four messaging layers (event_bus, runtime_signal_queue,
  Redis pub/sub `SIGNAL_EMISSION_CHANNEL` / `SIGNAL_PAYLOADS_CHANNEL`,
  WS `/ws`).
- [`backend/services/signal_bus.py`](../../backend/services/signal_bus.py) —
  `upsert_trade_signal`, `_publish_trade_signal_emission`,
  `_publish_trade_signal_batch`. Publish ordering is critical.
- [`backend/services/signal_cache.py`](../../backend/services/signal_cache.py) —
  `_Subscriber._run` (Redis subscribe + bootstrap-on-connect),
  `upsert`, `get_unconsumed_signals` (filters), `is_ready`,
  `is_trader_hydrated`.
- [`backend/services/intent_runtime.py`](../../backend/services/intent_runtime.py) —
  `list_unconsumed_signals` (the first read site in
  `fast_trader_runtime.py:807`), `_signals_by_id`,
  `publish_opportunities` (writer-side), `_project_status_batch`
  (sweeper, now grace-protected).
- [`backend/workers/fast_trader_runtime.py`](../../backend/workers/fast_trader_runtime.py) —
  `_FastTraderTask._run_once` (lines 750-1050 cover both read
  sites and the cursor-cache logic).

## Validation Commands

The diagnostic phase relies on live SSH against `polyhome-1` (the
defect is observable only on the running stack, not in tests).
Code-mutating phases get unit tests once we know the fix shape.

- `ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c \"SELECT signal_id, decision FROM trader_decisions WHERE trader_id='eff366f86217484b98950ea836099a02' AND created_at > NOW() - INTERVAL '30 minutes' ORDER BY created_at;\""`
- `ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c \"SELECT id, runtime_sequence, status, created_at FROM trade_signals WHERE strategy_type='crypto_5m_last_outcome' AND created_at > NOW() - INTERVAL '30 minutes' ORDER BY created_at;\""` — used to count missed-by-trader signals.
- `ssh polyhome-1 "cd /home/polyhome/homerun && docker compose logs --since=15m worker-trading 2>&1 | rg -F 'eff366f86217484b98950ea836099a02' | head -40"` — fast-trader cycle log filter.
- `ssh polyhome-1 "cd /home/polyhome/homerun && docker compose logs --since=15m worker-trading 2>&1 | rg -F 'signal_cache' | head -20"` — cache subscriber/bootstrap log lines.
- After fix lands and tests exist:
  `docker compose exec backend pytest -q backend/tests/test_signal_cache_runtime_freshness.py` (file to be created in Task 4).

## Out of scope

- No changes to `signal_bus.expire_source_signals_except` — Plan
  0052 is finalised. The grace guard stays at 60 s.
- No changes to scanner or `crypto_5m_last_outcome` strategy code.
  The strategy is emitting at 100 % per Plan 0051 verification.
- No CRITICAL-tier knob touch. The likely fix surface is either
  Redis pub/sub plumbing inside `signal_bus.py` /
  `signal_cache.py` or cursor-advance logic in
  `fast_trader_runtime.py`. Both are HIGH-tier at most.
- No retroactive fix for already-expired signals.
- This plan does NOT cover the `latency_class=normal` orchestrator
  read path (`trader_orchestrator_worker.list_unconsumed_trade_signals`,
  step 22 in copy-trade-pipeline.md). The defect is observed on a
  fast-tier trader; if the same pattern shows up on normal-tier,
  open a sibling plan.

### Pre-fix baseline (pinned 2026-05-11 19:00 UTC)

For trader `eff366f86217484b98950ea836099a02` over the
2026-05-11 18:30–19:00 UTC window:

- `trade_signals` for `strategy_type='crypto_5m_last_outcome'`:
  18 rows total (6 executed / 7 expired / 3 pending / 2 skipped).
- `trader_decisions` for that trader_id in the same window:
  8 rows. The cycles 18:30 (3 signals) and 18:40 (3 signals)
  produced ZERO trader decisions.
- Pass criterion: post-deploy 30 m window
  `count(trader_decisions) == count(trade_signals)` for the
  trader, OR equivalently `executed / (executed + expired) ≥ 90 %`.

## Task 1: Pin which layer dropped the signal

- [ ] On `polyhome-1`, capture a structured log dump for the next
  full miss event. Add `LOG_LEVEL=DEBUG` for the
  `services.signal_cache` and `services.signal_bus` loggers via
  the runtime log-config knob (no code change needed). Wait for a
  full 5 m cycle where the trader cycle reports
  `runtime_list_signals → 0 rows + signal_source: cache + 0 rows`
  while a fresh `pending` `crypto_5m_last_outcome` row exists in
  `trade_signals` for ≥ 30 s.
- [ ] For the missed signal, run on the live DB:
  ```sql
  SELECT id, source, dedupe_key, status, runtime_sequence,
         created_at, updated_at, expires_at
    FROM trade_signals
   WHERE id = '<missed_signal_id>';
  ```
  Record `runtime_sequence`. If NULL → the signal never made it
  past the skeleton-INSERT phase and the projection loop never
  populated `runtime_sequence`. That points to **branch (B)**:
  the writer-side `publish_opportunities` is dropping our row
  before the projection commit and the `signal_payloads` publish.
- [ ] Read `signal_cache._signals` snapshot via a one-shot
  `docker compose exec backend python -c "..."` script that calls
  `services.signal_cache.get_signal_cache().get_signal('<missed_signal_id>')`.
  - If `None` → the cache never received the publish. Branch
    **(A)** — pub/sub plumbing is dropping the message (Redis
    disconnect, pubsub re-subscribe gap, payload deserialise
    error).
  - If a snapshot is returned with `runtime_sequence=None` → the
    cache has the skeleton but the projection update never
    arrived; branch **(A2)** — second publish is missing.
  - If a snapshot is returned with a concrete `runtime_sequence`
    that is `<= cursor_runtime_sequence` for the trader → branch
    **(C)**: cursor advanced past our signal because of a
    sequence-allocation race.
- [ ] On the same trader, read `cursor_runtime_sequence`:
  ```sql
  SELECT trader_id, last_signal_id, last_signal_created_at,
         updated_at
    FROM trader_signal_cursor
   WHERE trader_id='eff366f86217484b98950ea836099a02';
  ```
  Compare its implied sequence against the missed signal's
  `runtime_sequence`. The cursor is also held in-memory (function
  `_get_sequence_cursor` in
  [`fast_trader_runtime.py:806`](../../backend/workers/fast_trader_runtime.py));
  capture both via the same one-shot `docker compose exec` script.
- [ ] Write findings to
  `docs/plans/work-artifacts/0053-pre-fix-evidence.md` —
  `(missed_signal_id, runtime_sequence, cache_present?,
  cursor_value)` for at minimum 3 missed signals across two
  separate cycles. Do NOT proceed to Task 2 until the dropping
  layer is identified by elimination.
- [ ] Mark completed

## Task 2: Pick a fix shape based on Task 1's evidence

The fix shape depends on which branch Task 1 lands in. Each
branch has a clean-cut, no-back-compat fix listed below; this
task is to pick exactly ONE.

- [ ] **If branch (A) — Redis publish is dropped or the cache
  subscriber missed it:**
  - Fix lives in
    [`backend/services/signal_bus.py`](../../backend/services/signal_bus.py).
    The `_publish_trade_signal_batch` /
    `_publish_trade_signal_emission` paths fire-and-forget on
    Redis. If the client is mid-reconnect the publish is lost.
  - Solution: replace the fire-and-forget pubsub publish with a
    Redis `XADD` against a capped Stream (`signal_payloads_stream`,
    `MAXLEN ~ 5000`). The cache subscriber then reads via
    `XREAD BLOCK` from a stored `last-id` per consumer; on
    reconnect it resumes from `last-id` and replays the gap. No
    bootstrap-on-connect race, no lost messages on a 1 s Redis
    reconnect window.
  - Estimated diff: ~80 LOC in `signal_bus.py` +
    `signal_cache.py`, removes ~30 LOC of bootstrap-reconciliation.
- [ ] **If branch (A2) — skeleton publishes but the projection
  update doesn't:**
  - The skeleton-INSERT pass committed by Plan 0010
    (`pg_insert(TradeSignal) ... on_conflict_do_nothing`) needs
    its own `_publish_trade_signal_emission` so the cache learns
    about the new row. The current code only publishes after the
    projection's full UPSERT.
  - Estimated diff: 8-10 LOC in `signal_bus.py`, no schema, no
    consumer-side change (cache already idempotent on `upsert`).
- [ ] **If branch (B) — `runtime_sequence` was never assigned:**
  - The fix is in
    [`backend/services/intent_runtime.py`](../../backend/services/intent_runtime.py)
    `publish_opportunities`. Either the sequence-allocation path
    is skipped for the strategy (regression in Plan 0009's
    allow-list?) or the projection commit failed silently.
  - First action: add `WARNING`-level log when an emit is
    published with `runtime_sequence=None` for a non-
    `ws_post_arm_tick` source. This will surface the regression
    immediately on the next miss.
  - Estimated diff: 15 LOC; possibly extends to a one-line
    `_strategy_runtime_metadata` allow-list correction.
- [ ] **If branch (C) — cursor advanced past the missed signal:**
  - The cursor lives in
    [`backend/workers/fast_trader_runtime.py`](../../backend/workers/fast_trader_runtime.py)
    via `_get_sequence_cursor` / `_set_sequence_cursor` (search
    file for these). Race is: Cycle N consumes signal seq=42 and
    advances cursor to 42; meanwhile signal_bus assigns seq=41
    to a new signal that committed AFTER our signal seq=42 was
    already in flight. The cursor filter then drops seq=41.
  - Solution: replace the `cursor_runtime_sequence` filter with
    a `(cursor_signal_id, cursor_created_at)` tuple filter — same
    pattern the DB-fallback path already uses (lines 813-815
    fast_trader_runtime.py). The signal_cache filter at
    [`signal_cache.py:534-537`](../../backend/services/signal_cache.py)
    becomes a `created_at >` comparison, not a sequence
    comparison.
  - Estimated diff: 25 LOC across signal_cache.py +
    fast_trader_runtime.py.
- [ ] Document the chosen branch + diff outline at the top of
  Task 3 below before writing code.
- [ ] Mark completed

## Task 3: Implement the fix

- [ ] Implement the chosen branch from Task 2. Single-concern
  diff: do NOT bundle two branches even if both look related.
- [ ] Update the matching architecture note exactly once. Most
  likely [`copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md)
  step 13/15/17 (publish path) or step 24 (fast-tier read).
- [ ] Bump `Last verified: 2026-05-XX` on that note with a one-
  line description of the change.
- [ ] No defensive layering — if the fix is at the publish side,
  do not also patch the read side "for safety". Trust internal
  code per `agents.md` § Core Principles.
- [ ] Mark completed

## Task 4: Tests

- [ ] Create
  `backend/tests/test_signal_cache_runtime_freshness.py`. The
  test surface depends on the chosen branch:
  - Branch (A) / (A2): unit-test the publish path in
    `signal_bus.py` against an in-memory Redis fake
    (`fakeredis.aioredis.FakeRedis`). Assert that
    `upsert_trade_signal` produces exactly one publish per
    distinct `(source, dedupe_key, status)` flip, and that a
    skeleton + projection produce two publishes.
  - Branch (B): unit-test
    `intent_runtime.publish_opportunities` against a stub
    `IntentRuntime` to assert that every emit gets a
    `runtime_sequence != None` for the
    `_EXECUTION_ACTIVATION_BY_SOURCE_KEY` allow-list members.
  - Branch (C): unit-test
    `signal_cache.get_unconsumed_signals` — a signal with a
    `created_at` newer than the trader's cursor must be
    returned even if its `runtime_sequence` is lower than the
    cursor's last consumed sequence.
- [ ] Use the same test fixture pattern as
  [`backend/tests/test_signal_bus_expire_source.py`](../../backend/tests/test_signal_bus_expire_source.py)
  (Plan 0052) — drive everything through `signal_bus` /
  `signal_cache` public API, no internal monkeypatching.
- [ ] Add at minimum one regression test that pins the
  pre-fix evidence: a signal whose `created_at` is inside a
  60 s window after the trader's last consume must appear in
  the trader's `get_unconsumed_signals` response. This test is
  the canary against future refactors silently re-introducing
  the bug.
- [ ] Mark completed

## Task 5: Live verification on polyhome-1

- [ ] `./deploy/sync_remote.sh` from the local checkout.
- [ ] `docker compose restart worker-trading backend` (rolled in
  by `sync_remote.sh`).
- [ ] Wait 30 m. Run the third Validation Command (the
  `trader_decisions` count for trader
  `eff366f86217484b98950ea836099a02`). Compare row count to the
  `trade_signals` row count for the same trader/window.
  Pass criterion: `count(trader_decisions) ≥ 0.9 ×
  count(trade_signals)`.
- [ ] If the ratio stays below 90 %, capture the post-deploy
  trader-cycle log + missed-signal lifecycle into
  `work-artifacts/0053-post-fix-evidence.md` and re-enter Task 1
  with the new evidence — the chosen branch was wrong.
- [ ] Mark completed

## Task 6: Doc + close-out

- [ ] Update [`docs/plans/architecture/copy-trade-pipeline.md`](architecture/copy-trade-pipeline.md):
  add a one-paragraph note under the section corresponding to the
  fixed layer, citing this plan.
- [ ] Update [`docs/strategies/crypto-5m-last-outcome.md`](../strategies/crypto-5m-last-outcome.md):
  add a one-line link to this plan under "Посилання" if the fix
  visibly affects the strategy's "Коли НЕ працює" enumeration.
- [ ] Move this plan to `docs/plans/completed/` and update the
  link in `plan-control-index.md`.
- [ ] Mark completed
