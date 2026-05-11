# Architecture: Trader Pipeline & Diagnostics

This note covers the path **from a signal to an executed order** —
the trader-orchestrator pipeline that turns scanner / news / weather /
traders / crypto signals into shadow or live trades. It also doubles
as a top-down diagnostic playbook: when "the bot isn't opening
positions," this is the file to open first.

The pipeline is the most-asked-about subsystem in operations because
many things have to align (signals, bots, scope, gates, execution
simulator) before a position appears. Each stage has its own table,
its own log line, and its own failure mode. Running blind through
random log greps wastes hours; running the playbook below pinpoints
which stage is dark in under five minutes.

## Purpose

This layer is responsible for:

1. Routing **signals** (rows in `trade_signals`) to the **bots
   (traders)** whose source / strategy combinations match.
2. Asking each strategy `detect_async` / `evaluate` whether the
   signal yields an opportunity for *this* bot, given its
   `risk_limits` and `traders_scope`.
3. Persisting the verdict as a **decision** (`trader_decisions`),
   then materialising selected decisions into **execution sessions**
   that produce **orders** (`trader_orders`) and, in shadow mode,
   **simulation trades / positions** (`simulation_trades`,
   `simulation_positions`).
4. Bookkeeping every step so post-hoc diagnosis works (see the
   "Diagnostic playbook" section below).

It does **not** own:

- Signal *production* — that is the scanner / news / weather /
  traders / crypto worker responsibility (see the corresponding
  files under `services/strategies/` and the per-source workers
  under `backend/workers/`).
- Live-venue execution mechanics (HTTP signing, py-clob-client,
  Kalshi REST) — that is `live_execution_service` /
  `live_execution_adapter`.
- Shadow fill probability — that is the Cox-PH model in
  `services/fill_simulator/` and `services/simulation/execution_simulator.py`.

## Key files (code + tables side by side)

### Code

| Path | What it holds |
|---|---|
| [backend/services/trader_orchestrator/session_engine.py](../../../backend/services/trader_orchestrator/session_engine.py) | Per-cycle main loop. For each enabled trader: pull signals, route to strategy, build decisions, launch execution sessions. |
| [backend/services/trader_orchestrator/fast_submit.py](../../../backend/services/trader_orchestrator/fast_submit.py) | Hot-path submit pipeline used by both shadow and live. |
| [backend/services/trader_orchestrator/risk_manager.py](../../../backend/services/trader_orchestrator/risk_manager.py) | Pre-flight gates, per-trader risk envelope, kill-switch checks. |
| [backend/services/trader_orchestrator/position_lifecycle.py](../../../backend/services/trader_orchestrator/position_lifecycle.py) | Drives `should_exit`, scale-outs, resolution-hold, near-resolution windows. |
| [backend/services/traders_firehose_pipeline.py](../../../backend/services/traders_firehose_pipeline.py) | The `traders/*` source family — wallet-event firehose, qualified-source filter, deduplication. |
| [backend/services/traders_copy_trade_signal_service.py](../../../backend/services/traders_copy_trade_signal_service.py) | Live wallet-WS event consumer for copy-trade. Bridges every accepted leader trade into a `trade_signals` row via `bridge_opportunities_to_signals` (`source='traders'`, `signal_type='copy_trade'`). See the [copy-trade pipeline note](copy-trade-pipeline.md) for the full publish/consume path. |
| [backend/services/wallet_ws_monitor.py](../../../backend/services/wallet_ws_monitor.py) | Polymarket user-channel WS subscription per source-scope. |
| [backend/services/simulation/execution_simulator.py](../../../backend/services/simulation/execution_simulator.py) + [services/fill_simulator/](../../../backend/services/fill_simulator/) | Cox-PH fill model. The "did this limit price actually execute" oracle in shadow mode. |
| [backend/services/live_execution_service.py](../../../backend/services/live_execution_service.py) | Live submit path (Polymarket CLOB / Kalshi). |
| [backend/api/routes_trader_orchestrator.py](../../../backend/api/routes_trader_orchestrator.py) | `/api/trader-orchestrator/{overview, status, start, stop, settings, kill-switch}`. |
| [backend/api/routes_traders.py](../../../backend/api/routes_traders.py) | `/api/traders` — bot CRUD. |

### Tables (the canonical data model)

| Table | What lives here | Key columns |
|---|---|---|
| `traders` | One row per bot. | `id`, `name`, `mode` (shadow/live), `is_enabled`, `is_paused`, `block_new_orders`, `source_configs` (JSON: list of `{source_key, strategy_key, strategy_params}`), `risk_limits`, `traders_scope`, `selected_account_id` (sandbox account UUID for shadow) |
| `trade_signals` | Every signal a strategy or worker has emitted. | `id`, `source` (scanner/news/weather/traders/crypto), `signal_type`, `strategy_type`, `market_id`, `direction`, `entry_price`, `edge_percent`, `confidence`, `status` (pending/expired/consumed), `quality_passed` (true/false/null), `dedupe_key`, `created_at`, `expires_at` |
| `trader_signal_cursor` | Per-trader pointer into the signal stream. | `trader_id`, `last_signal_created_at`, `last_signal_id`, `last_runtime_sequence` |
| `trader_signal_consumption` | Audit log: which trader saw which signal and what happened. | `trader_id`, `signal_id`, `decision_id` (nullable), `outcome` (selected / skipped / blocked / failed), `reason`, `consumed_at` |
| `trader_decisions` | The verdict from `strategy.detect_async` / risk gates. | `id`, `trader_id`, `signal_id`, `source`, `strategy_key`, `decision` (selected / skipped / blocked), `reason`, `score`, `payload_json`, `risk_snapshot_json` |
| `trader_decision_checks` | Per-gate verdict for each decision. | `decision_id`, `check_name`, `status` (pass/fail), `detail` |
| `execution_sessions` | One per `decision='selected'`. The state machine that actually places orders. | `id`, `decision_id`, `trader_id`, `mode`, `status`, `attempt_count` |
| `trader_orders` | Per-leg order rows (live + shadow). | `id`, `decision_id`, `trader_id`, `mode`, `status` (submitted / executed / cancelled / failed), `price`, `size`, `created_at` |
| `trader_positions` | Open / closed positions (live mode). | `id`, `trader_id`, `market_id`, `side`, `quantity`, `entry_price`, `unrealized_pnl`, `status` |
| `simulation_trades` / `simulation_positions` | Shadow-mode equivalent of `trader_orders` / `trader_positions`. Capital is taken from `simulation_accounts.current_capital`. | See [Settings & Secrets](settings-and-secrets.md) for the sandbox-account model. |
| `tracked_wallets` / `discovered_wallets` | Wallet pool for the `traders/*` source family. The `wallet_trades` table is the local cache of wallet trade tape — empty unless `tracked_traders_worker` is actively crawling. | — |

## Pipeline stages

```
┌─ producers ────────────────────────────────────────────────────────┐
│ scanner_worker  news_worker  weather_worker  crypto_worker         │
│ traders_firehose_pipeline  (wallet_ws_monitor → traders_copy_*)    │
└────────────────────────────┬───────────────────────────────────────┘
                             │ insert
                             ▼
                    trade_signals (source=…)              ← STAGE 1
                             │
                             │ async (worker-news)
                             ▼
                  quality_filter sets quality_passed      ← STAGE 2
                             │
                             │ orchestrator cycle (every run_interval_seconds)
                             ▼
       trader_orchestrator/session_engine._run_trader_once
                             │ for each enabled trader:
                             │   1. fetch new signals via cursor
                             │   2. firehose_* / quality / scope filters
                             ▼
        trade_signals routed to matching trader              ← STAGE 3
                             │
                             ▼
                strategy.detect_async (per-strategy module)  ← STAGE 4
                             │
                             ▼
                   trader_decision (selected | skipped | blocked)  ← STAGE 5
                             │ + trader_decision_checks per gate
                             │ + trader_signal_consumption row
                             ▼
                if decision='selected': execution_sessions row     ← STAGE 6
                             │
                             │ mode='shadow' → execution_simulator (Cox-PH)
                             │ mode='live'   → live_execution_service
                             ▼
                  trader_orders + trader_positions OR             ← STAGE 7
                  simulation_trades + simulation_positions
```

A row at every stage is the contract. If the stage you're inspecting
has zero rows in the relevant time window, the issue is upstream.

### Stage interpretations (essential vocabulary)

- **`trade_signals.status`**: `pending` (active), `expired` (TTL
  passed), `consumed` (used by all subscribed traders).
- **`trade_signals.quality_passed`**: `true` (filter pass), `false`
  (filter reject), **`null`** (filter has not yet processed it —
  signals start `null` and get a verdict asynchronously). A bot with
  `firehose_require_qualified_source=true` will skip `null`-state
  signals.
- **`trader_decisions.decision`**:
  - `selected` — strategy approved this signal for this bot, will
    feed into execution.
  - `skipped` — strategy itself returned no opportunity (edge gate,
    confidence floor, market filter, or **shadow execution did not
    fill**).
  - `blocked` — risk-manager / orchestrator gate killed it before
    strategy ran (max-orders-per-cycle, kill-switch, identity guard,
    self-crossing-quote, etc.).
- **`trader_signal_consumption.outcome`**: mirrors `decision` but
  one row per (trader × signal). Useful when you want to see what
  *each* trader decided about a *specific* signal.

## Dependencies (both directions)

**This layer depends on:**

- All signal producers (scanner, news, weather, crypto, traders
  firehose, wallet WS).
- `simulation_service` for shadow fills; `live_execution_service`
  for live fills.
- `pause_state.global_pause_state` (the global kill-switch shared
  with all workers).
- `wallet_state_cache` for live-position freshness gates.
- The `LLMManager` if `llm_verify_trades=true` is set in
  orchestrator settings (off by default).

**Depended on by:**

- The frontend Bots panel (`/api/traders`,
  `/api/trader-orchestrator/overview`).
- The Positions panel (reads `trader_positions` and
  `simulation_positions`).
- The Performance panel (reads PnL from `simulation_accounts` and
  live wallet state).
- Telegram notifier (when configured) for trade events and
  autotrader-summary digests.

## Diagnostic playbook (top-down)

Use this in order. Each step either confirms the layer is healthy or
isolates the failure to one stage. Stop when you find a layer with
zero activity that should have activity.

All commands assume `polyhome-1` SSH alias from
[`deploy/AGENTS.md`](../../../deploy/AGENTS.md). Each section has a
copy-pasteable one-liner.

### Step 0 — Verify the stack is up

```bash
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose ps'
```

All seven containers should be `Up (healthy)` (frontend has no
healthcheck; that's normal). If `worker-trading` restarted within
the last few minutes, expect partial decision history.

### Step 1 — Orchestrator + control state

```bash
ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/api/trader-orchestrator/overview' \
  > /tmp/orch.json && jq '{
    control: {mode: .control.mode, paused: .control.is_paused, kill_switch: .control.kill_switch, selected_account: .control.settings.selected_account_id, enabled_strategies: .control.settings.enabled_strategies},
    worker: {running: .worker.running, traders_total: .worker.traders_total, traders_running: .worker.traders_running, decisions_count: .worker.decisions_count, orders_count: .worker.orders_count, last_error: .worker.last_error, current_activity: .worker.current_activity}
  }' /tmp/orch.json
```

Healthy state: `paused=false, kill_switch=false, running=true`,
`traders_total > 0`, `last_error=null`. Any of these wrong → fix
here before anything else.

`traders_total=0` → no bots created. Stop, create at least one.
This is the most common reason "the bot isn't trading" turns out to
mean "no bot exists."

### Step 2 — Bot configs

```bash
ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/api/traders' > /tmp/traders.json
jq '.traders[] | {name, mode, is_enabled, is_paused, block_new_orders,
  sources: [.source_configs[]? | "\(.source_key)/\(.strategy_key)"],
  position_size_usd: .risk_limits.max_position_notional_usd,
  max_orders_per_cycle: .risk_limits.max_orders_per_cycle,
  last_error, last_run_at}' /tmp/traders.json
```

Look for:

- `is_enabled=false` or `is_paused=true` → operator-disabled.
- `block_new_orders=true` → entries blocked by lifecycle rule.
- `last_run_at=null` → orchestrator never invoked this bot. Causes:
  no signal matched its `source_configs`, **or** firehose-pre-filter
  killed every candidate before strategy invocation (this is
  pernicious — the bot looks broken but the gate is upstream).
- `last_error != null` → strategy raised. Read it.

### Step 3 — Signal flow (are upstream signals arriving?)

```bash
ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c \"
  select source, count(*) total,
    sum((created_at > now() - interval '1 hour')::int) recent_1h,
    sum((status='pending')::int) pending,
    sum((quality_passed is null)::int) quality_unknown,
    sum((quality_passed)::int) quality_pass,
    sum((not quality_passed)::int) quality_fail
  from trade_signals
  group by source order by source\""
```

Healthy: each source you have bots for shows `recent_1h > 0`.

Footgun signature: `quality_unknown = total` for a source means the
quality filter (`worker-news`) hasn't processed any of them. Bots
with `firehose_require_qualified_source=true` will reject all of
them.

For the `traders/*` family, check the wallet pool too:

```bash
ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c \"
  select 'tracked: ' || count(*) from tracked_wallets;
  select 'discovered_pool: ' || count(*) from discovered_wallets;
  select 'discovered_with_trades: ' || count(*) from discovered_wallets where total_trades > 0\""
```

If `tracked + pool` is empty, `traders/*` signals will never appear.

### Step 4 — Decision activity (is the orchestrator routing?)

```bash
ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c \"
  select t.name, d.decision, count(*),
    substring(d.reason from 1 for 80) as reason_sample
  from trader_decisions d join traders t on t.id = d.trader_id
  where d.created_at > now() - interval '10 min'
  group by t.name, d.decision, substring(d.reason from 1 for 80)
  order by t.name, d.decision, count(*) desc\""
```

Three buckets matter:

- `selected` — went forward to execution. If this is non-zero but
  `trader_orders` is empty, the failure is at Stage 6 (next step).
- `skipped` — strategy decided no. Read the reason; the most common
  in shadow mode is `"Shadow execution did not fill: limit_price_not_executable"`
  which means the Cox-PH simulator decided the limit price you set
  was unreachable.
- `blocked` — risk gate or orchestrator pre-check killed it. Common
  reasons: `self_crossing_quote`, `Min-exit-notional guard`,
  `kill_switch_active`, `max_orders_per_cycle_reached`.

If a bot has neither row in this query at all, it's not consuming
signals. Cross-check with Step 5.

### Step 5 — Signal consumption per bot (deeper-dive routing)

```bash
ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c \"
  select t.name, count(*),
    sum((c.outcome='selected')::int) selected,
    sum((c.outcome='skipped')::int) skipped,
    sum((c.outcome='blocked')::int) blocked,
    sum((c.outcome='failed')::int) failed,
    max(c.consumed_at) latest
  from trader_signal_consumption c join traders t on t.id = c.trader_id
  where c.consumed_at > now() - interval '30 min'
  group by t.name order by count(*) desc\""
```

A bot in this list with zero rows means **no signal ever reached
its strategy** — same situation as `last_run_at=null` in Step 2.

If a bot is consuming signals but all `outcome=skipped` with the
same reason, that's strategy-level filtering. If most are
`outcome=blocked`, that's risk-gate filtering — escalate to Step 6.

### Step 6 — Decision checks (which specific gate?)

```bash
ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c \"
  select dc.check_name, dc.status, count(*),
    substring(dc.detail from 1 for 80) as detail_sample
  from trader_decision_checks dc
  join trader_decisions d on d.id = dc.decision_id
  where d.created_at > now() - interval '15 min' and dc.status='fail'
  group by dc.check_name, dc.status, substring(dc.detail from 1 for 80)
  order by count(*) desc limit 20\""
```

This is the most surgical view: **which specific gate fired, how
often, with what detail**. Common gates and their meaning:

| Gate (`check_name`) | Failure means |
|---|---|
| `min_edge_percent` | Strategy's edge floor; signal didn't clear it. |
| `min_confidence` | Strategy confidence floor. |
| `firehose_require_qualified_source` | Signal `quality_passed != true`. |
| `firehose_max_age_minutes` | Signal too old. |
| `max_orders_per_cycle` | Bot has hit its per-cycle order budget. |
| `max_gross_exposure_usd` | Bot already at exposure cap. |
| `kill_switch_active` | Global or per-bot kill switch on. |
| `self_crossing_quote` | Two-sided MM creating internal arb. |
| `Min-exit-notional guard` | Position size too small for clean exit. |
| `provider_health_block` | Live-mode CLOB unhealthy in last window. |

### Step 7 — Order materialisation (selected → submitted)

```bash
ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c \"
  select t.name,
    (select count(*) from trader_orders o where o.trader_id=t.id and o.created_at > now() - interval '15 min') trader_orders,
    (select count(*) from simulation_trades s where s.account_id = t.selected_account_id and s.executed_at > now() - interval '15 min') sim_trades,
    (select count(*) from execution_sessions es where es.trader_id=t.id and es.created_at > now() - interval '15 min') exec_sessions
  from traders t order by t.name\""
```

If `selected` decisions exist but `exec_sessions = 0`, something is
wrong between decision creation and session launch — read
worker-trading logs filtered for the trader id.

If `exec_sessions > 0` but `trader_orders = 0` (live) or
`sim_trades = 0` (shadow), the session is failing to submit / fill.
For shadow mode, this is almost always **Cox-PH `limit_price_not_executable`**
on the chosen entry price. Loosen `slippage_bps`, `max_spread_bps`,
or switch `price_policy: taker_market` for diagnosis.

If `exec_sessions = 0` despite `decisions.decision='selected'` rows
existing, and the orchestrator log shows a successful submission
without a matching `execution_session persisted` INFO line, the
canonical cause is the **commit-missing failure mode** in
`_persist_execution_projection` (commit `936f96a4`,
`runtime-tweaks.md` 2026-05-09 entry). Signature:

- `selected` decisions accumulate normally
- `trader_open_positions` blocker fires on > 80 % of cycles
- `execution_sessions`, `trader_orders`, `trader_positions` all empty
- no rollback log line, no Cox-PH reason
- INFO `execution_session persisted` is **absent** (the permanent
  fingerprint added by the fix); ERROR
  `execution_session persist commit failed` is also absent (no
  exception was raised — the rollback came from the async session
  close, not an error path)

The fix added `await self.db.commit()` at the end of
`_persist_execution_projection`. If the symptom returns after a
refactor of the persister, run the regression test
`test_execute_signal_shadow_persists_with_commit_so_async_session_close_does_not_rollback`
before any deeper investigation — it pins the invariant.

Note: shadow trades land in `trader_orders` (`mode='shadow'`) +
`trader_positions` + `execution_sessions`, **not** in
`simulation_trades` / `simulation_positions`. The latter are
owned by the legacy standalone simulator
([`services/simulation/execution_simulator.py`](../../../backend/services/simulation/execution_simulator.py))
and are empty in current production. The Step 7 diagnostic SQL
above predates this clarification — when `sim_trades = 0` for a
shadow bot, that is normal; check `trader_orders` instead. See
[`execution-and-fills.md`](execution-and-fills.md) § "Shadow path"
for the current ledger surface.

### Step 8 — Worker-trading log slice

When the SQL views above conflict (e.g. signals exist, decisions
exist, but bot stays cold), grep the worker log for the trader id
or strategy slug:

```bash
TRADER_NAME="Sandbox - Tail-End"
ssh polyhome-1 "cd /home/polyhome/homerun && docker compose logs --since 10m worker-trading 2>&1 \
  | grep -iE \"$TRADER_NAME|tail_end_carry|preflight|fast_submit|reject|skip|gate\" | tail -50"
```

Search hints:

- `event-loop stall` — heavy worker; not a bug per se but causes
  flakiness if persistent (>5 s). See "Footguns" below.
- `Cycle[scheduled:general] signals=N decisions=N orders=N` — every
  cycle's summary line. `signals=0` for a bot whose source has
  signals = scope mismatch.
- `traders_copy_trade_signal_service:_processor_loop` — the live
  copy-trade processor (8 concurrent asyncio tasks inside
  `worker-trading`). Active here means it's draining wallet-WS
  events into `bridge_opportunities_to_signals` (which writes the
  `trade_signals` row); see [copy-trade-pipeline.md](copy-trade-pipeline.md).

## Common end-state symptoms and their first-suspect

| Symptom | First-suspect stage | Recipe |
|---|---|---|
| `traders_total=0` | No bots configured | Step 1 → create bot |
| `decisions_count=0` for hours, signals exist | Step 5 → consumption | Check `trader_signal_consumption` for the bot |
| `selected > 0`, `orders=0`, mode=shadow | Step 7 → Cox-PH fill simulator | Loosen slippage / spread / `taker_market` |
| All `decision=blocked` for one bot | Step 6 → specific gate | `trader_decision_checks` filter |
| `last_run_at=null` for traders bot | Step 5 → firehose pre-filter | Inspect `firehose_*` params; check `quality_passed` distribution for the source |
| Copy-trade bot idle | Standard Stage 1 / Stage 5 flow | `traders_copy_trade` requires a recent leader trade in the wallet-WS feed (Stage 1, `max_signal_age_seconds`). Once a signal is written it follows the same pipeline as every other source — check the firehose pre-filter (Stage 5) and `trader_decision_checks` (Step 6) like any other bot. See [copy-trade-pipeline.md](copy-trade-pipeline.md) for the source-specific publish topology (post-Plan-0009 + 0010, including publish-time `(source, dedupe_key) → id` adoption from `trade_signals`). |
| Bot enabled, signals flowing, zero consumption | `source_configs` mismatch with signal `source` / `strategy_type` | Compare bot's `source_configs[*].strategy_key` to `trade_signals.strategy_type` |

## Known footguns

- **`quality_passed=null` is not a bug, it's an in-flight state.**
  The quality filter (`worker-news`) processes signals
  asynchronously after they land in `trade_signals`. Bots with
  `firehose_require_qualified_source=true` (the default for
  `traders_confluence`) will reject every signal until the filter
  completes. If `worker-news` is overloaded or the filter has a bug,
  signals stay `null` indefinitely — and the bot looks dead.
- **Copy-trade reads the wallet-WS feed in-process,
  not the cross-plane Redis bus.** A "no signal in
  `trade_signals` for copy-trade" symptom means upstream
  wallet-WS health (no live wallet trade in the last
  `max_signal_age_seconds`, leader pool empty, scope filter
  excluded the wallet, etc.) — not a signal-bus problem. The
  end-to-end publish/consume topology is in
  [copy-trade-pipeline.md](copy-trade-pipeline.md).
- **`tracked_wallets.total_trades` is an analytics aggregate**,
  populated by wallet-discovery analysis. It's *not* a count of
  rows in `wallet_trades`. The local `wallet_trades` tape can be
  empty even when `total_trades=500`. The aggregate signals "this
  wallet has historically traded N markets," not "we have N rows
  cached locally."
- **Worker-trading event-loop stalls** show up as
  `level=WARNING logger=event_loop_watchdog message="Event-loop
  stall detected"` with `stall_seconds` in the payload. Sustained
  stalls > 5 s indicate the trading plane is overloaded
  (typical cause: too many concurrent traders × signals × cycle
  intervals). The orchestrator timeout will eventually skip
  stragglers; you'll see this as missing `last_run_at` updates on
  some bots.
- **Decisions persist forever; signals expire.** A `selected`
  decision in `trader_decisions` from yesterday does NOT mean an
  open order — match by `created_at` window, and cross-reference
  with `trader_orders` / `simulation_trades` to see realised
  outcome. The decision is the *intent*, not the *execution*.
- **`selected_account_id` must be set on the bot for shadow mode.**
  Without it, shadow execution has no ledger to write to. The
  orchestrator's `selected_account_id` setting is a default, but
  the bot field overrides it. If both are empty, decisions stay
  `selected` but no `simulation_trades` rows appear — the symptom
  looks like Step 7 fill failure but is actually configuration.
- **`_persist_execution_projection` must commit.** A subtle race
  killed shadow trading on 2026-05-09: the persister flushed
  `trader_orders` / `execution_sessions` / `trader_positions`
  rows but did not commit, and async session close rolled them
  back. Symptom looked like Step 7 fill failure but
  `execution_sessions` was empty too.  Fix: commit
  `936f96a4` adds explicit `await self.db.commit()` at the end
  of the projection persister, with rollback + ERROR log on
  failure (`execution_session persist commit failed`) and INFO
  log on success (`execution_session persisted`). Regression
  test: `test_execute_signal_shadow_persists_with_commit_so_async_session_close_does_not_rollback`.
  Diagnosis details and rollback recipe in
  [`runtime-tweaks.md`](../../operational/runtime-tweaks.md)
  2026-05-09 entry. Do not refactor the persister without
  re-running the regression test.
- **Backend restart and orchestrator boot state.** The FastAPI
  lifespan calls `_reset_orchestrator_boot_state` in
  [`backend/main.py:190`](../../../backend/main.py) on every
  process startup. As of plan 0021 the reset is **conditional**:
  - **Auto-resume branch** — when the prior persisted state is
    `mode='shadow' AND is_enabled=true AND is_paused=false`, the
    orchestrator preserves it across restart. `selected_account_id`
    and `shadow_account_id` are kept; only `live_preflight` and
    `live_arm` are nulled (they must never survive a process
    restart). Snapshot reads `current_activity="Resumed in shadow
    on application startup"` until the first cycle overwrites it.
  - **Hard-reset branch** — for `mode='live'`, or any
    `is_enabled=false` / `is_paused=true` prior state, the legacy
    safety reset still fires: `is_enabled=false, is_paused=true,
    mode='shadow', requested_run_at=null,
    selected_account_id=null, shadow_account_id=null`. Operator
    must `POST /api/trader-orchestrator/start` with a fresh account
    selection. This applies to all post-crash live recovery paths.
  Regression tests pin both branches in
  [`test_main_lifespan_smoke.py`](../../../backend/tests/test_main_lifespan_smoke.py).
  To revert to unconditional hard-reset, `git revert` the
  Plan 0021 commit and redeploy.
- **Frontend / API validation mismatch on `trader_cycle_timeout_seconds`.**
  The `Trader Cycle Timeout` input in the Bots → ⚙ Settings flyout
  ([TradingPanel.tsx:12751](../../../frontend/src/components/TradingPanel.tsx))
  declares `min=3, max=120`, but the backend Pydantic model
  ([routes_trader_orchestrator.py:159](../../../backend/api/routes_trader_orchestrator.py))
  enforces `ge=30.0, le=180.0`. Values in `[3, 30)` pass the UI but
  return HTTP 422 from the API; values in `(120, 180]` get capped by
  the UI before they're sent. Safe range that satisfies both today:
  **`30..120`**. This is a known divergence — when fixing, raise
  the UI minimum to 30 and the maximum to 180 to match backend
  semantics. Direct API call works around the UI bound:
  `curl -X PUT /api/trader-orchestrator/settings -d
  '{"global_runtime":{"trader_cycle_timeout_seconds":150}}'`.

  **Sibling knob — `runtime_trigger_cycle_timeout_seconds`** (added
  2026-05-08, commit `c8b2c144`). Same flyout exposes a second
  timeout that governs the *lightweight* runtime-trigger cycles
  (no maintenance, no scanning). UI input
  ([TradingPanel.tsx:12780](../../../frontend/src/components/TradingPanel.tsx))
  and the backend normaliser
  ([trader_orchestrator_state.py:387-390](../../../backend/services/trader_orchestrator_state.py))
  agree on `[3, 60]` with default 10 s. No mismatch, no footgun —
  noted here so readers tracing the timeout flyout aren't surprised
  by the second field.
- **Three execution-policy fields on `risk_limits`** (added
  2026-05-08, commit `6ab5f3a6`). These live alongside the existing
  `max_position_notional_usd` / `max_orders_per_cycle` you saw in
  Step 2 and are read by the decision-and-execution path:

  | Field | Default | Read by | Effect |
  |---|---|---|---|
  | `max_entry_drift_pct` | 10.0 | `decision_gates.apply_platform_decision_gates` ([decision_gates.py](../../../backend/services/trader_orchestrator/decision_gates.py)) | Symmetric tolerance: `|live_price − signal_entry_price| / signal_entry_price · 100`. Lower → strategies skip when market moves; higher → more fills at worse prices. |
  | `max_market_data_age_ms` | `None` | `decision_gates._resolve_market_data_age_budget_ms` ([decision_gates.py:212-217](../../../backend/services/trader_orchestrator/decision_gates.py)) | Per-bot ceiling for live quote staleness at gate-time. Empty → fall back to `strategy_params.max_market_data_age_ms` then env `EXECUTION_MARKET_DATA_MAX_AGE_MS`. |
  | `allow_taker_limit_buy_above_signal` | `False` | `order_manager._allow_taker_limit_buy_above_signal` ([order_manager.py:267-274](../../../backend/services/trader_orchestrator/order_manager.py)) | When ON, **shadow** simulator may fill BUY legs at prices above signal `entry_price` (chase-up). Default OFF rejects whenever the book moved up since signal — the dominant cause of `Execution submission: limit_price_not_executable` rejections seen in Step 7. Live-mode price discipline is unaffected. The chase-up ceiling is reduced via [`_chase_up_execution_caps`](../../../backend/services/trader_orchestrator/order_manager.py) over **execution-price caps only** (`max_execution_price`, `max_entry_price`); entry-band guards (`max_probability`, `min_upside_percent`-derived) are excluded — see [Plan 0035](../0035-split-entry-band-from-execution-price-cap.md) and the "Chase-up cap reduction" subsection in [`execution-and-fills.md`](execution-and-fills.md). |

  These are passed end-to-end through the cascade
  `submit_execution_leg(strategy_params, risk_limits)` →
  `fast_submit.execute_fast_signal(risk_limits)` →
  `fast_trader_runtime._FastTraderTask` (every fast-tier cycle
  forwards `dict(self._trader.get("risk_limits") or {})`). The
  fallback precedence inside `_allow_taker_limit_buy_above_signal`
  is **strategy_params first** (with alias support for
  `allow_taker_limit_pay_up`,
  `allow_taker_limit_to_exceed_signal_price`,
  `allow_buy_above_signal_price`), then `risk_limits`, then
  `False`. SDK defaults and validation live in
  [`strategy_sdk.py:411-413, 453-481, 1937-1949`](../../../backend/services/strategy_sdk.py)
  (`TRADER_RISK_DEFAULTS`).
- **`Trader cycle slow` log is the gold-standard latency view.**
  When `_run_trader_once_inner` exceeds half the timeout it emits a
  WARNING with a per-stage breakdown
  ([backend/workers/trader_orchestrator_worker.py](../../../backend/workers/trader_orchestrator_worker.py) — exact emit-line varies by build):

  ```json
  {"duration_s": 10.2, "processed_signals": 1, "decisions_written": 1,
   "orders_written": 0,
   "stage_timings_ms": {"signal_loop": 9531, "ps_decision_writes": 5029,
     "ps_submit_order": 2143, "ps_risk_eval_setup": 1638,
     "per_signal_total": 7376, ...}}
  ```

  Search this log line first when latency is the suspect. Stages worth
  watching:
  `ps_decision_writes` (DB write latency on `trader_decisions` /
  `trader_signal_consumption`),
  `ps_submit_order` (shadow Cox-PH or live CLOB submit),
  `ps_risk_eval_setup` (risk-gate fixture loading),
  `per_signal_total` (whole signal-to-decision round-trip).
  A healthy signal round-trip is 50–200 ms; anything > 1000 ms is a
  bottleneck. `ps_decision_writes > 1000ms` typically means DB-pool
  exhaustion or lock contention on `trader_decisions`-related tables.

  Recorded baselines on the `polyhome-1` host:

  | When | `ps_decision_writes` samples | Notes |
  |---|---|---|
  | Pre Plan 0002 (May 7 2026, 1 h pre-redeploy window) | `1077.6 ms`, `5029.4 ms` (n=2) | Postgres oversized for the host: `shared_buffers=4GB` against 7.6 GiB RAM, `effective_cache_size=10GB` (lying to the planner). RAM `available` ≈ 363 MiB. |
  | Post Plan 0002 (15 min post-redeploy window) | n=0 — no `Trader cycle slow` events fired | Postgres re-sized to `shared_buffers=1.5GB`, `effective_cache_size=3GB` (truthful), `max_connections=100`. Cache hit 99.29%, RAM `available` 1.7 GiB. The trade-signal feed collapsed to ≈0.4/min after the restart due to **out-of-scope** `worker-trading` event-loop saturation, so no signals → no slow-cycle samples. The Postgres layer is no longer the bottleneck on this host. |

- **`uq_trader_position_identity` race on position re-open (FIXED
  by Plan 0024).** Before plan 0024,
  `sync_trader_position_inventory`
  ([`trader_orchestrator_state.py:8480-8534`](../../../backend/services/trader_orchestrator_state.py:8480))
  did `session.add(TraderPosition(...))` for any identity tuple
  not present in its `existing_by_identity` snapshot. The snapshot
  was a SELECT taken at the top of the function; between that SELECT
  and the eventual flush, another concurrent path (or a recently-
  closed row from `circuit_breaker_safe_exit`) could leave a row
  with the same `(trader_id, mode, market_id, direction)` that
  collided with `uq_trader_position_identity`
  ([`models/database.py:4373-4379`](../../../backend/models/database.py:4373)).
  The resulting `IntegrityError` was counted by the signal-processing
  layer as a "failed signal", which fed `halt_on_consecutive_losses`
  and tripped the circuit breaker — which then `circuit_breaker_safe_exit`-flattened
  more positions, seeding more collisions. Documented chain in
  [`runtime-tweaks.md`](../../operational/runtime-tweaks.md)
  2026-05-10 ~10:12-11:24 UTC entry. **Plan 0024 replaced the
  INSERT with `pg_insert(...).on_conflict_do_update(constraint="uq_trader_position_identity", set_={...})`**;
  the conflict branch overwrites the colliding row with the same
  fields the previous UPDATE branch would have written (re-opens
  closed positions, refreshes sizing/timing, merges payload_json
  in Python before the UPSERT). Two regression tests in
  [`test_trader_live_provider_reconciliation.py`](../../../backend/tests/test_trader_live_provider_reconciliation.py)
  pin the contract: re-open existing closed row + no
  IntegrityError when the snapshot misses a row that DOES exist.
  Post-deploy verification: 4 IntegrityError / 15 min → 0 / 30 min;
  `halt_on_consecutive_losses=true` restored, no
  `circuit_breaker_pause` events in 30 min observation window.

## Per-trader consumed-set (post-Plan 0032)

The fast trader runtime maintains an in-process per-trader consumed-set
inside [`backend/services/signal_cache.py`](../../../backend/services/signal_cache.py)
to skip signals this trader has already handled without paying for the
DB query each cycle. After Plan 0032 the bookkeeping has four rules:

1. **Pre-cycle hydration from the DB ledger (Task 7).** The first
   cycle of every `_FastTraderTask` opens a `FastAsyncSessionLocal()`
   session and calls
   `trader_orchestrator_state.fetch_recent_consumed_signal_ids` —
   the last 48 h of `trader_signal_consumption` rows for that
   `trader_id`, capped at 50 000 entries, newest-first. The hydrate
   runs **before** the first
   `intent_runtime.list_unconsumed_signals` call so the post-fetch
   filter (rule 4 below) has authoritative consumption history;
   without this, every worker-trading restart leaks the entire
   pending-signal backlog as "trader_order already exists" decisions
   for the duration of the
   `_UNCHANGED_SCANNER_TERMINAL_REACTIVATION_COOLDOWN_SECONDS = 180`
   cooldown window (200–400 / hour observed on `Sandbox - Tail-End`
   before the fix). The hydrate is gated by the per-task
   `_consumed_set_hydrated` flag and soft-fails on DB errors —
   `fast_submit`'s `(trader_id, signal_id)` idempotency-guard
   continues to absorb duplicates when hydrate is degraded.

2. **Unbounded set with lazy prune.** The pre-Plan 0032 implementation
   used a `deque(maxlen=1_000)` ring; on a busy trader the ring
   wrapped after ~1.4 h and old `signal_id`s rolled out, re-triggering
   duplicate-submit attempts when the scanner re-emitted the same
   signal. Plan 0032 retired the ring and made the set unbounded.
   Long-term memory is capped by a lazy prune triggered inside
   `mark_consumed` once the set crosses 50 000 entries: any
   `signal_id` no longer in the snapshot cache, OR whose snapshot is
   older than 24 h (terminal-state cutoff), is dropped. Prune is
   O(N) over the trader's set — sub-millisecond at the cap.

3. **`cache.upsert` skips when every known trader has consumed.**
   Before writing a fresh snapshot, `signal_cache.SignalCache.upsert`
   checks `_consumed_set` keys (process-wide, < 100 traders) and
   skips outright when every trader's consumed-set already contains
   the signal_id. Re-emitting would only bump `runtime_sequence` and
   pay for filter cycles every trader will short-circuit on the
   consumed-set lookup anyway. The skip is strict: a brand-new
   trader (no consumed-set yet) is treated as interested, so the
   snapshot is upserted and stays available for hydration.

4. **`intent_runtime` post-filter (Task 7).**
   `IntentRuntime.list_unconsumed_signals` is the **first** signal
   source the fast trader consults each cycle, but it explicitly
   does `del trader_id` and never filters by per-trader consumption
   history — without an extra gate the scanner's 180 s reactivation
   cooldown for unchanged terminal signals re-presents every signal
   whose `TraderOrder` already exists on every trader cycle. After
   `list_unconsumed_signals` returns, the fast trader looks up
   `signal_cache.get_signal_cache().consumed_ids_for(trader_id)`
   (frozen-set snapshot, sub-microsecond, no DB) and drops any
   signal whose id is in the consumed-set before forwarding to
   `_process_signals_parallel_by_market`. The dropped count is
   surfaced via `_last_stage_timings_ms["consumed_set_filtered"]`
   for operator visibility.

Operator-visible counters surfaced via `signal_cache.status_snapshot`
(folded into `/api/diagnostics`): `consumed_set_size_per_trader`,
`consumed_set_lazy_prunes_total`,
`upserts_skipped_consumed_overlap`. Pre-cycle hydrate cost is
captured per-trader at `_last_stage_timings_ms["precycle_consumed_hydrate"]`;
post-fetch drop count at `_last_stage_timings_ms["consumed_set_filtered"]`.

## Extension points

| When you want to… | Touch |
|---|---|
| Add a new gate (e.g. "block trades during press conferences") | `risk_manager.py` — add the check, write to `trader_decision_checks`, return blocking outcome from `should_block`. |
| Add a new signal source family | New worker emitting to `trade_signals` with a unique `source` value, plus a new strategy in `services/strategies/` with matching `source_key`. The orchestrator routes generically — no orchestrator code changes needed. |
| Change shadow fill probability | `services/fill_simulator/` (Cox-PH model) — retraining is operator-controlled via `services/fill_simulator_refresh_worker.py`. |
| Add per-trader notifications | Subscribe to `trader_events` Redis pub/sub channel (see `services/trader_events_bridge.py`). |
| Audit "why didn't bot X enter on signal Y" | Run Step 5 with `where c.signal_id = '<id>' and c.trader_id = '<id>'` for the exact pair. |

## Where to look next

| Topic | File |
|---|---|
| What goes in `trade_signals` and how the scanner produces it | [docs/strategies/](../../strategies/) (per-strategy notes) |
| Copy Trade end-to-end (`source='traders'`) and why normal-tier drops it | [copy-trade-pipeline.md](copy-trade-pipeline.md) |
| Sandbox account model that backs shadow execution | [settings-and-secrets.md](settings-and-secrets.md) |
| How to add a new strategy | [backend-architecture.md](backend-architecture.md) — Plug-in patterns section |
| Why the trading plane is its own container | [system-overview.md](system-overview.md) |
| LLM verification path (`llm_verify_trades=true`) | [llm-provider-layer.md](llm-provider-layer.md) |
| Pre-scanner gates (regime, quality, monitor, prioritiser, depth) | [market-quality-and-prioritization.md](market-quality-and-prioritization.md) |
| Submission-side defence layer (9 modules between decision and fill) | [execution-defense.md](execution-defense.md) |
| Operator-applied runtime knob-twists (rollback recipes) | [`../../operational/runtime-tweaks.md`](../../operational/runtime-tweaks.md) |

## Per-trader strategy parameters (plan 0041)

The UI **Trading Panel → Tune → Parameters → Save Parameters**
persists per-trader overrides into
``traders.source_configs_json[].strategy_params`` (per
``(trader_id, source_key, strategy_key)`` row). Plan 0041 made
these overrides honoured at **signal-generation** time (not only
at order submit). The mechanism:

1. The opportunity dispatcher in
   [`market_runtime._dispatch_with_per_trader_fanout`](../../../backend/services/market_runtime.py)
   reads the trader→strategy binding map from
   [`trader_binding_cache`](../../../backend/services/trader_binding_cache.py)
   (3 s soft-TTL, 30 s hard-stale ceiling — cross-mode: covers
   live AND shadow traders).
2. For every subscribed strategy slug that has at least one bound
   trader, the dispatcher invokes a per-trader strategy instance
   via
   [`StrategyLoader.get_or_clone_for_trader`](../../../backend/services/strategy_loader.py).
   The clone is lazily produced by
   [`BaseStrategy.clone_for_trader`](../../../backend/services/strategies/base.py)
   with its config merged as
   ``default_config ∪ strategies.config ∪ traders.source_configs_json[].strategy_params``.
   The clone owns its own ``_state``, ``_cycle_trackers``, and
   filter diagnostics — there is no cross-trader leakage by
   construction.
3. Each opportunity the per-trader clone emits is tagged with
   ``intended_trader_id`` (set by the dispatcher, never by the
   strategy itself).
4. ``intent_runtime.publish_opportunities`` persists
   ``intended_trader_id`` into ``trade_signals.payload_json`` so
   the scope survives worker restart. It also folds the trader id
   into the dedupe key so two per-trader clones emitting on the
   same market produce two distinct ``trade_signals`` rows.
5. ``intent_runtime.list_unconsumed_signals(trader_id=...)`` now
   filters out snapshots whose ``intended_trader_id`` is set to
   a different trader. ``intended_trader_id = None`` keeps the
   legacy multi-trader-visible routing (used by sources with no
   per-trader bindings at all).
6. Cache invalidation. ``StrategyLoader.invalidate_per_trader``
   fires from ``unload`` / ``reconfigure_loaded`` (global
   reload) and may be called by the trader-update route to take
   effect immediately. Without an explicit call the 3 s
   ``trader_binding_cache`` TTL still propagates the change.

A trader whose ``strategy_params`` for a slug is an empty dict
still goes through the per-trader path; its effective config
mirrors the global ``strategies.config``, and its opportunities
are scoped to the trader id (so adding a second trader with
non-empty params later does not retroactively cross-contaminate).

The fan-out also preserves the original safety machinery
(``event_dispatcher.dispatch`` with its 60 s handler timeout +
force-kill) on the singleton path. Per-trader clones run with a
narrower ``_PER_TRADER_ON_EVENT_TIMEOUT_SECONDS = 15.0`` ceiling.

## Offline backtest sources of truth (crypto `crypto_update` strategies)

Plan 0046 added an offline replay harness for crypto strategies fed
by `DataEvent(event_type="crypto_update")` (today: `crypto_5m_midcycle`,
extensible to any future `crypto_update`-fed binary strategy). It
lives in `services/strategy_backtester._run_crypto_replay_detection`
and is exposed at `POST /api/validation/code-backtest/optimize-strategy`.

Two sources of truth feed the replay:

- **`crypto_oracle_history`** (Postgres, populated by
  `services/chainlink_feed.ChainlinkFeed`). Throttled to 1 row per
  `(asset, source)` per 1000 ms; the latest sample within each 1-s
  slot wins. A housekeeper task in the same module drops rows older
  than 14 days every 6 hours. This is the oracle source-of-truth
  for resolution PnL — at cycle end the replay queries
  `order by timestamp_ms desc limit 1 where timestamp_ms <= end_ms`
  to determine whether YES or NO won.
- **`trader_events` rows with `event_type = "firehose_evaluation"`**
  (Postgres, populated by `services/strategies/_firehose.emit_evaluation`).
  Each row captures the full gate chain a live evaluation walked,
  including `vwap_price`, `staleness_ms`, oracle-age, and the
  `book_depth`/`book_fresh` scores. The replay prefers these
  persisted values over re-simulating depth — it can only fire on
  cycles that originally reached the book gates, but for those it
  reproduces the exact VWAP the live run saw. Sweeping `bet_size_usd`
  is therefore caveated: the persisted slippage is bound to the
  live bet size at the time the row was logged.

These two tables together let an operator answer "if I had run
`min_distance_bps=10` last week, how many positions would the bot
have opened and what PnL would they have closed at?" without spinning
up parallel shadow traders and waiting for the queue to fill. The
endpoint returns a leaderboard of `(params → emit_count,
total_pnl_usd, win_rate, samples, composite_score)` sorted by
`composite_score = total_pnl_usd * win_rate`.

Last verified: 2026-05-11 (Plan 0046: documented `crypto_oracle_history`
as the backtest oracle source-of-truth and `firehose_evaluation` rows
as the VWAP source-of-truth. Real-diff against
`backend/services/strategy_backtester.py:602-981`,
`backend/services/chainlink_feed.py` persist block, and
`backend/models/database.py` `CryptoOracleHistory` model confirms
the schema + lookup paths.); previously 2026-05-11 (Plan 0041: documented the per-trader
clone + binding cache + opportunity scope. Real-diff against
``strategy_loader.py``, ``market_runtime.py``,
``intent_runtime.py`` confirms the cache, the fan-out, the
dedupe-key prefix, and the ``list_unconsumed_signals`` filter
all in place.); previously 2026-05-10 (Plan 0035: amended the
`allow_taker_limit_buy_above_signal` row in the Step 5 risk-limit
table to point at `_chase_up_execution_caps` and the
entry-band-vs-execution-price split. Real-diff against
`order_manager.py` confirms the helper exists at line ~239 and
both reduction sites delegate to it.); previously 2026-05-10 (Plan 0032 Task 7: documented the
pre-cycle consumed-set hydrate before the first
`intent_runtime.list_unconsumed_signals` call and the post-fetch
filter that drops signals already in the trader's consumed-set
before forwarding to `_process_signals_parallel_by_market`;
diagnosis cited the scanner's 180 s reactivation cooldown for
unchanged terminal signals as the residual cause of the dedup
spam after Tasks 1-5 deployed. Earlier same-date verification:
"Per-trader consumed-set" section for Plan 0032 — cold-start
hydrate from `trader_signal_consumption`, unbounded
`_consumed_set` with lazy prune, `cache.upsert` skip when every
known trader already consumed; documents the new diagnostics
counters and the `coldstart_consumed_hydrate` per-stage timing.
Prior same-date verification: footgun added for plan 0024 —
`sync_trader_position_inventory` UPSERT eliminating the
`uq_trader_position_identity` race that fed false losses into
`halt_on_consecutive_losses`; footgun added for plan 0021's
conditional boot-state reset.
Earlier 2026-05-09: Step 7 + footguns updated for the
`_persist_execution_projection` commit-missing failure mode
introduced by commit `936f96a4`; corresponds to plan 0016.
/sync-docs N=5 audit on the same date added the
`runtime_trigger_cycle_timeout_seconds` sibling-knob note plus
the new `risk_limits` triple — `max_entry_drift_pct`,
`max_market_data_age_ms`, `allow_taker_limit_buy_above_signal` —
for commits `c8b2c144`/`6ab5f3a6`, and corrected the stale link
to `backend/workers/trader_orchestrator_worker.py`.)
