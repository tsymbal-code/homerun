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
| Copy-trade bot idle | Stage 1 (signals deferred at publish) | `traders_copy_trade` writes signals to `trade_signals` but they are born in `awaiting_post_arm_ws_tick` deferred state (`runtime_sequence=NULL`); on `latency_class=normal` they are invisible to the orchestrator. See [copy-trade-pipeline.md](copy-trade-pipeline.md) for the gate and the operator workaround. |
| Bot enabled, signals flowing, zero consumption | `source_configs` mismatch with signal `source` / `strategy_type` | Compare bot's `source_configs[*].strategy_key` to `trade_signals.strategy_type` |

## Known footguns

- **`quality_passed=null` is not a bug, it's an in-flight state.**
  The quality filter (`worker-news`) processes signals
  asynchronously after they land in `trade_signals`. Bots with
  `firehose_require_qualified_source=true` (the default for
  `traders_confluence`) will reject every signal until the filter
  completes. If `worker-news` is overloaded or the filter has a bug,
  signals stay `null` indefinitely — and the bot looks dead.
- **Copy-trade publishes via the in-process wallet-WS callback,
  not via the cross-plane Redis bus.** `wallet_ws_monitor.add_callback`
  fans every leader trade directly into
  `traders_copy_trade_signal_service` running in the same trading
  plane. That service then synthesises an opportunity, calls
  `bridge_opportunities_to_signals`, and the bridge writes a
  `trade_signals` row plus pushes a `runtime_signal_queue` batch
  for the orchestrator and an `event_bus` wake for the fast-tier
  runtime. So a "no signal in `trade_signals`" symptom for copy-trade
  means upstream wallet-WS health (no live wallet trade in the last
  `max_signal_age_seconds`, leader pool empty, scope filter excluded
  the wallet, etc.). A "signal written but bot idle" symptom for
  copy-trade is a different problem — see the deferred-state gate
  documented in [copy-trade-pipeline.md](copy-trade-pipeline.md).
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
- **`Trader cycle slow` log is the gold-standard latency view.**
  When `_run_trader_once_inner` exceeds half the timeout it emits a
  WARNING with a per-stage breakdown
  ([trader_orchestrator_worker.py:7944](../../../backend/services/trader_orchestrator/) — line varies by build):

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
| Operator-applied runtime knob-twists (rollback recipes) | [`../../operational/runtime-tweaks.md`](../../operational/runtime-tweaks.md) |
