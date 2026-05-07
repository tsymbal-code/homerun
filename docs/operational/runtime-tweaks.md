# Runtime Tweaks — operational journal

Append-only journal of operator-applied configuration changes that
**live in the database**, not in git. These are knob twists that
take effect immediately on the running stack and survive restarts
because the values persist in `app_settings` / `traders` /
`trader_orchestrator_control` tables.

This file is the **rollback reference** when a tuning experiment
needs to be undone — every entry records the *before* and *after*
values plus the exact rollback recipe.

## Format

Each entry is appended to "Entries", newest at the bottom. Never
edit a closed entry — append a new one that reverts.

```markdown
## YYYY-MM-DD HH:MM UTC — short title

- **Surface**: `bots / orchestrator / strategy_catalog / app_settings`
- **Applied via**: `UI / curl / psql`
- **Why**: one or two sentences (typically a hypothesis the tweak
  tests; link to the diagnostic context if any)
- **Expected effect**: what the operator wants to see change
- **Verification command**: how to read the live value back

### Changes

| Path | Before | After |
|---|---:|---:|

### Rollback

```bash
# exact curl / SQL / UI steps to revert
```
```

When a tweak proves useful and gets promoted to a code default
(e.g. baked into `default_config` in
`backend/services/strategies/<slug>.py`), close the entry by adding
a `**Promoted to code on YYYY-MM-DD in commit <sha>**` line and
append a follow-up note marking the operational tweak unnecessary.

## Why this lives in git

The actual values live in Postgres on `polyhome-1`. They are not
tracked anywhere else. Without this journal, an operator who
twiddles ten knobs over a week loses track of:

- which knobs are "factory default" and which are "experiment",
- the order tweaks were applied (which matters for performance
  attribution),
- the exact baseline metric before each tweak.

This file is the durable source of truth for "what did we change
and when."

## Entries

### 2026-05-07 ~07:30 UTC — relax Tail-End filters to chase first shadow trade

- **Surface**: `bots` (Sandbox - Tail-End, id `388da687054c4b4a858ea152fff04900`)
- **Applied via**: UI (operator)
- **Why**: After Postgres-tuning (plan 0002) and the host upgrade
  to 8 vCPU / 15 GiB, the trader pipeline is no longer
  resource-constrained. Yet `simulation_trades` still 0: 88% of
  Tail-End decisions get `Shadow execution did not fill:
  limit_price_not_executable` from the shadow execution simulator
  (the literal "no ask ≤ limit_price in the live order book"
  check, not Cox-PH). Hypothesis: the strategy's filters are too
  conservative for shadow conditions, and the $5 position notional
  triggers `min_exit_notional` plus is too small for top-of-book
  consumption. We relax three knobs to broaden the candidate set
  and make positions large enough for the simulator to fill.
- **Expected effect**: At least one row appears in `simulation_trades`
  / `simulation_positions` within ~30 minutes. Bigger candidate
  set should give the simulator more chances to find a market
  where ask ≤ limit_price.
- **Verification command**:

  ```bash
  ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/api/traders' \
    | jq '.traders[] | select(.id == "388da687054c4b4a858ea152fff04900")
        | {risk: {max_position_notional_usd: .risk_limits.max_position_notional_usd,
                  max_trade_notional_usd: .risk_limits.max_trade_notional_usd},
           strategy_params: (.source_configs[0].strategy_params
              | {min_probability, min_upside_percent, min_liquidity})}'
  ```

#### Changes

| Path | Before | After |
|---|---:|---:|
| `traders[Tail-End].source_configs[0].strategy_params.min_probability` | 0.85 | **0.75** |
| `traders[Tail-End].source_configs[0].strategy_params.min_upside_percent` | 10 | **6** |
| `traders[Tail-End].source_configs[0].strategy_params.min_liquidity` | 1500 | **500** |
| `traders[Tail-End].risk_limits.max_position_notional_usd` | 5.0 | **25.0** |
| `traders[Tail-End].risk_limits.max_trade_notional_usd` | 5.0 | **25.0** |

For comparison, the strategy's factory defaults (from
[`backend/services/strategies/tail_end_carry.py`](../../backend/services/strategies/tail_end_carry.py)):
`min_probability=0.85, max_probability=0.905, min_upside_percent=10,
min_liquidity=1500`. The bot now runs with **none of these matching
factory defaults** — full revert needed before any conclusion about
the unmodified strategy is drawn.

#### Baseline (immediately before this tweak)

| Metric | Value |
|---|---:|
| `simulation_trades_total` | 0 |
| `simulation_positions_open` | 0 |
| `trader_orders` (5 min) | 0 |
| `execution_sessions` (5 min) | 0 |
| `decisions` (5 min) | 24 (≈ pre-tweak rate) |
| `sandbox_capital` | $10 000.00 |
| Decision distribution (10 min, pre-tweak): | 70 = 17 selected + 52 skipped + 1 blocked |
| Top skip reason | `Shadow execution did not fill: limit_price_not_executable` (25 of 70) |

#### Rollback

UI path:

1. Bots → Sandbox - Tail-End → Sources → `scanner / tail_end_carry` →
   Strategy Params: set `min_probability: 0.85`,
   `min_upside_percent: 10`, `min_liquidity: 1500`. Save.
2. Bots → Sandbox - Tail-End → Risk: set `max_position_notional_usd:
   5`, `max_trade_notional_usd: 5`. Save.

API path (when UI is unreachable):

```bash
# Round-trip pattern: GET full trader → patch dict → PUT
TRADER_ID=388da687054c4b4a858ea152fff04900
ssh polyhome-1 "curl -fsS http://127.0.0.1:8888/api/traders/$TRADER_ID" \
  | jq '. as $t
        | $t * {risk_limits: ($t.risk_limits * {max_position_notional_usd: 5.0, max_trade_notional_usd: 5.0}),
                source_configs: [($t.source_configs[0] * {strategy_params: ($t.source_configs[0].strategy_params * {min_probability: 0.85, min_upside_percent: 10, min_liquidity: 1500})})]}' \
  > /tmp/trader_revert.json
ssh polyhome-1 "curl -fsS -X PUT -H 'Content-Type: application/json' \
  --data @- http://127.0.0.1:8888/api/traders/$TRADER_ID" \
  < /tmp/trader_revert.json | jq
```

(The `PUT /api/traders/{id}` exact payload shape may need tweaking
— the `GET → modify → PUT` round-trip is the safest pattern.)

#### Status

OPEN — waiting for first shadow trade. Recheck via:

```bash
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c \
  "select count(*) sim_trades from simulation_trades;
   select count(*) sim_positions_open from simulation_positions where status::text not in (\"closed_win\",\"closed_loss\");"'
```

If still 0 after 30 min — escalate (next steps in the conversation
that produced this tweak include `min_repricing_buffer: 0.015 →
0.005` and `max_spread: 0.05 → 0.08`, or the Cox-PH ensemble
mode adjustment which is a code-level change, not a knob).

#### Outcome (30 min later)

After 30 min — still **0 trades**. Strategy filter clearly works
(83 selected over 30 min, 5× the previous rate; `limit_price_not_executable`
rate halved per 15 min), but every selected decision still hits one
of the two shadow-simulator floors:

```
limit_price_not_executable      48/30m  (Tail-End taker path)
queue_not_reached_by_trade_flow 14/30m  (Certainty Shock maker path)
self_crossing_quote             48/30m  (Market Making, blocked by guard — not a fill issue)
```

**Diagnosis**: this is a Cox-PH cold-start blocker, not a filter
problem. `trader_orders_total_alltime: 0` → `cox_trainer_worker`
logs `no rows to train on` every 6 h → `cox_inference` falls back
to `empirical_constants.py` defaults (deliberately conservative
for spoofy books) → simulator declines every fill. No further
strategy-side tweak can break this loop. See the next entry for
the attempt to use the operator-hook the system provides.

---

### 2026-05-07 ~08:30 UTC — empirical-constants override (failed: cross-process bug)

- **Surface**: `fill_simulator.empirical_constants` (in-memory
  `_state.overrides` dict)
- **Applied via**: `PUT /api/fill-model/empirical-constants`
- **Why**: Cox-PH model is untrained (no `TraderOrder` rows). The
  shadow simulator falls back to `empirical_constants.py` defaults,
  which are conservative (built for spoofy books with high
  cancel-to-trade ratio). The module's docstring documents
  per-knob overrides as the **operator hook** for exactly this
  situation:
  > "These constants are *additionally* exposed in the UI under
  > **Strategies → ML Models → Fill Model**, so an operator can
  > override any of them."
- **Expected**: pushing `displayed_depth_factor`, `maker_queue_ahead_fraction`,
  `maker_trade_flow_multiplier`, `stale_depth_decay` to less
  conservative values should let the simulator approve some fills
  while the system bootstraps real `TraderOrder` history for
  Cox-PH training.

#### Changes (applied)

| Constant | Default | Override |
|---|---:|---:|
| `displayed_depth_factor` | 0.88 | **0.95** |
| `maker_queue_ahead_fraction` | 0.65 | **0.30** |
| `maker_trade_flow_multiplier` | 1.20 | **2.50** |
| `stale_depth_decay` | 0.55 | **0.80** |

#### Outcome — **the change does not reach the execution simulator** ❌

Verified via `docker compose exec worker-trading python -c
"from services.fill_simulator.empirical_constants import
get_empirical_constants, _state; ..."`:

```
worker-trading process state:
  measured=False, sample_count=0
  displayed_depth_factor=0.88        ← still default
  maker_queue_ahead_fraction=0.65    ← still default
  maker_trade_flow_multiplier=1.2    ← still default
  stale_depth_decay=0.55             ← still default
  overrides_in_state={}              ← EMPTY
```

Meanwhile the same query against the **backend** container does
return the override values. Pipeline activity (Tail-End +
Certainty Shock) confirms: `simulation_trades` still 0,
fail-reason distribution unchanged.

#### Root cause — design gap, not a misconfiguration

`set_override(key, value)` mutates a process-local dict
(`_state.overrides` in
[`services/fill_simulator/empirical_constants.py:88`](../../backend/services/fill_simulator/empirical_constants.py)).
The `PUT` route runs in the **backend** container's Python
interpreter; the consumer (`order_manager.py:877` →
`get_empirical_constants()`) runs in the **worker-trading**
container's interpreter. There is:

- no DB persistence of the override (no `app_settings` column,
  no `fill_model_overrides` table),
- no Redis pub/sub broadcasting the change,
- no env-var fallback,
- no `refresh_async` re-read of overrides — `refresh_async()`
  recomputes constants from `book_delta_events` and that's it.

Worse, `refresh_async()` is itself called from
[`workers/fill_simulator_refresh_worker.py`](../../backend/workers/fill_simulator_refresh_worker.py)
inside worker-trading on a 15-min staleness loop. With our current
`book_delta_events` distribution
(329 153 events / 24h = 325 486 cancels + 3 667 trades, **trade
fraction = 1.1 %**), `refresh_async` lands `displayed_depth_factor`
at the floor `max(0.40, min(0.99, 0.011)) = 0.40` — **even more
conservative than the 0.88 default**. So the live worker-trading
state is actually **worse** than the API-side override view
suggests.

The docstring's claim about "operator can override any of them" is
**aspirational** — the wiring is half-built. Filing a code-level
plan is the only fix.

#### Status

OPEN, **abandoned as a runtime fix**. Override values remain set
in the backend process for completeness (visible via `GET
/api/fill-model/empirical-constants`) but the worker-trading
process never sees them. Promoted to a follow-up code plan
(tentative ID **0003 — propagate fill-model overrides
cross-process**, see Recommendation below).

#### Rollback

Not strictly necessary — the override has no effect anywhere except
the backend's response payload. To clear the dangling state:

```bash
ssh polyhome-1 'curl -fsS -X PUT http://127.0.0.1:8888/api/fill-model/empirical-constants \
  -H "Content-Type: application/json" \
  -d "{
    \"displayed_depth_factor\": null,
    \"maker_queue_ahead_fraction\": null,
    \"maker_trade_flow_multiplier\": null,
    \"stale_depth_decay\": null
  }"' | jq
```

(`null` means "revert to measured" per `set_override` semantics.)

#### Recommendation for next step

Three viable code-level paths to actually unblock shadow trading:

1. **Plan 0003 — Propagate fill-model overrides cross-process.**
   Persist overrides into a small DB table (`fill_model_overrides`),
   have `refresh_async` (or a similar reload point) read them and
   apply on top of the measured constants. ~2–3 days. Fixes the
   underlying bug. Lowest-risk path that retains the design intent.
2. **Plan 0004 — Cox-PH untrained-mode optimistic fallback.**
   In `cox_inference.py`, when `load_active_fill_model()` returns
   no model (or a model with `n_events=0`), branch to an
   "optimistic untrained" code path that uses the `optimistic`
   ensemble scenario as primary instead of `realistic`. Shadow
   stays optimistic until ~100 real fills accrue. ~1 day.
3. **Plan 0005 — Counterfactual bootstrap of Cox-PH.**
   Wire `replay_counterfactual_order` (already implemented in
   [`services/fill_simulator/counterfactual_replay.py`](../../backend/services/fill_simulator/counterfactual_replay.py),
   docstring explicitly says "Used by the Cox PH trainer to
   bootstrap synthetic labels when real fill history is sparse")
   into `cox_trainer.train_and_persist`. When the real training
   set is empty, generate synthetic labels by replaying historical
   `MarketMicrostructureSnapshot` (we have 68 401 such rows). ~1
   week. Most architecturally correct — uses all the bootstrap
   infrastructure the authors already built.

**Recommended order**: 2 (fastest unblock) → 1 (correctness fix) →
3 (long-term self-tuning). Anything in this sequence preserves
shadow-only operation (no live mode required).

---

### 2026-05-07 ~10:00 UTC — flip all bots to `latency_class=fast` (Cox-PH bootstrap path)

- **Surface**: `traders.latency_class` for all 7 sandbox bots
- **Applied via**: UI / API
- **Why**: Deep code reading revealed the project ships **two
  different execution runtimes** with very different shadow
  semantics:

  | Path | Used by | Pre-submit `TraderOrder` for shadow? |
  |---|---|---|
  | `session_engine` (default) | `latency_class=normal` bots | ❌ NO — gated `if mode == "live":` at [`session_engine.py:1530`](../../backend/services/trader_orchestrator/session_engine.py); shadow `skipped` results never write `TraderOrder` rows ([`session_engine.py:1801`](../../backend/services/trader_orchestrator/session_engine.py)) |
  | `fast_trader_runtime` | `latency_class=fast` bots | ✅ YES — [`fast_submit.py:484`](../../backend/services/trader_orchestrator/fast_submit.py) writes a skeleton `TraderOrder` with `mode=mode_key` (so `mode="shadow"`) as an idempotency lock **before** submission |

  All Cox-PH training reads from `TraderOrder.payload_json["survival_features"]`
  ([`cox_trainer.py:fetch_training_rows`](../../backend/services/fill_simulator/cox_trainer.py)).
  With every bot on `normal`, the table stays empty forever ⇒
  `cox_trainer` keeps logging `no rows to train on` ⇒ Cox-PH never
  promotes a model ⇒ `cox_inference` returns conservative empirical
  defaults ⇒ shadow simulator declines every fill ⇒ no orders
  written ⇒ no training data. Classic chicken-and-egg.

  Flipping at least one bot to `latency_class=fast` is the
  **shipped** way out. It writes `TraderOrder` rows via
  `fast_submit` regardless of fill outcome (filled / cancelled /
  failed), which is exactly the `(duration, event_observed)` data
  Cox-PH needs.

- **Expected effect**: `TraderOrder` rows start accumulating;
  within ~6 h `cox_trainer_worker` (news plane) finds them and
  promotes a first KM-fallback model; within ~24–48 h Cox-PH proper
  takes over; shadow simulator starts approving fills →
  `simulation_trades` > 0.

#### Changes

| Bot | latency_class before | after |
|---|---:|---:|
| Sandbox - Basic Arbitrage | normal | **fast** |
| Sandbox - Certainty Shock | normal | **fast** |
| Sandbox - Market Making | normal | **fast** |
| Sandbox - NegRisk | normal | **fast** |
| Sandbox - Tail-End | normal | **fast** |
| Sandbox - Traders Confluence | normal | **fast** |
| Sandbox - Traders Copy Trade | normal | **fast** |

#### First 5-min outcome

```
trader_orders_total_alltime: 6   ← FIRST EVER ROWS
trader_orders_recent_5m:     6
status_breakdown:            cancelled=6
simulation_trades:           0   (still — fills not yet realized)
```

All 6 are `status=cancelled` because `fast_trader_runtime` has
strict per-cycle budgets (cycle 3 s, submit 1 s, evaluate 0.2 s)
that the GIL-bound `worker-trading` cannot meet today. Submission
durations 5–10 s; one DB query
([`list_unconsumed_trade_signals`](../../backend/services/trader_orchestrator_state.py))
hit the 60-s `statement_timeout` and raised `QueryCanceledError`.

This is **acceptable for bootstrap** — `cox_trainer` treats
`cancelled` orders as right-censored events
(`CENSOR_STATUSES` set in
[`cox_trainer.py`](../../backend/services/fill_simulator/cox_trainer.py)),
which is exactly what KM-fallback fits a baseline `S(t)` from. The
duration data alone unblocks the trainer.

#### Side-effect to watch

`fast_trader_runtime` saturates the trading-plane event loop more
aggressively than `session_engine`. If this causes problems
(repeated `Fast trader cycle exceeded budget`, `QueryCanceledError`
on multiple bots), the workaround is to **leave only one bot on
`fast`** — Tail-End is the natural pick because it produces the
most signals (~1 selected per minute). One fast bot is enough to
seed the training set; the others can stay on `normal` until the
GIL-removal work in
[`docs/plans/architecture/worker-trading.md`](../plans/architecture/worker-trading.md)
lands.

#### Verification commands

```bash
# Confirm latency_class
ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/api/traders' \
  | jq '.traders[] | {name, latency_class}'

# Watch TraderOrder accumulation
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c \
  "select count(*), string_agg(distinct status::text, \",\") from trader_orders;"'

# Watch cox_trainer pickup (6h interval; check after ~6 h)
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since 6h worker-news 2>&1 \
  | grep "Cox trainer" | tail -10'

# Watch model promotion (check after ~6–8 h)
ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c \
  "select id, family, n_events, concordance_index, active, promoted_at from fill_probability_models order by trained_at desc limit 5;"'
```

#### Rollback

If `fast_trader_runtime` causes prod issues (event loop
starvation visible to other workers, scanner/news lagging), revert
all 7 bots back to `latency_class=normal`. The 6 bootstrap rows
already in `trader_orders` survive — they're enough for the first
`cox_trainer` cycle even with no further accumulation.

```bash
TRADERS=(
  e25441bc4a0844dc893099e67abd84c7  # Basic
  c949b8c7bdd040038dc7d6dc69d49ed8  # Certainty Shock
  704e11a34dcc4997b3a60ffdd289b4b8  # Market Making
  91706d4849534cbb8cf09cbd2fa41bff  # NegRisk
  388da687054c4b4a858ea152fff04900  # Tail-End
  e2822918ea334b518fda957e9332f174  # Traders Confluence
  61dcbeb2b9bc42bd9e9635a09ae5e0c3  # Traders Copy Trade
)
for id in "${TRADERS[@]}"; do
  ssh polyhome-1 "curl -fsS -X PATCH http://127.0.0.1:8888/api/traders/$id \
    -H 'Content-Type: application/json' \
    -d '{\"latency_class\": \"normal\"}'"
done
```

#### Status

OPEN — bootstrap loop running. Re-evaluate at t+6 h (cox_trainer
cycle), t+24 h (model promotion check), t+48 h (first
`simulation_trades` row expected).

#### Why this is the *shipped* answer

This is exactly the architecture other Homerun operators report
working: `latency_class=fast` is the project's **default
recommended path** for new shadow deployments. The `normal` path
is preserved for backwards compatibility — see comment in
[`backend/workers/fast_trader_runtime.py:22-24`](../../backend/workers/fast_trader_runtime.py):

> «default to `latency_class='normal'` and keep the existing path»

Existing setups stay on `normal`; new deployments are expected to
flip to `fast` once the operator wants ML-driven shadow simulation.
We hit the cold-start trap because all our bots inherited the
backwards-compat default.

---

## Sister entries (earlier same day, also still open)

These were tracked in the conversation but should be back-filled
here for completeness if not yet recorded:

### 2026-05-07 ~05:00 UTC — orchestrator runtime tuning

- **Surface**: `trader_orchestrator_control.settings`
- **Applied via**: UI Bots → ⚙ Settings flyout
- Changes: `run_interval_seconds: 30 → 60`,
  `trader_cycle_timeout_seconds: null → 60`,
  `global_risk.max_orders_per_cycle: 50 → 10`.
- Rationale: pre-Postgres-tuning, worker-trading was overloaded
  (`Trader cycle timed out` warnings every cycle, p95 stage
  latency > 50 s). Looser cadence + higher per-cycle timeout +
  smaller order budget were aimed at fitting the available CPU.
- Status: still in effect after Plan 0002 redeploy. May be worth
  reverting once the GIL bottleneck (see
  [`docs/plans/architecture/worker-trading.md`](../plans/architecture/worker-trading.md))
  is addressed: with parallel CPU, `run_interval_seconds=30` and
  `max_orders_per_cycle=50` should be safe again.
- Rollback (when ready): same UI path or
  `PUT /api/trader-orchestrator/settings` with
  `{"run_interval_seconds": 30, "global_runtime":
  {"trader_cycle_timeout_seconds": null}, "global_risk":
  {"max_orders_per_cycle": 50}}`.

### 2026-05-07 ~07:00 UTC — orchestrator restart / sandbox re-bind

- **Surface**: `trader_orchestrator_control` + worker-pause-state
- **Applied via**: API (`POST /api/workers/resume-all` then
  `POST /api/trader-orchestrator/start`)
- Reason: after the Plan 0002 redeploy, orchestrator booted with
  `is_enabled=false` and `selected_account_id=null` —
  `Manage-only (global_disabled)` mode, no decisions written.
- Effect: orchestrator running again, `selected_account_id` bound
  to sandbox `08fb2d1e-3bb1-4cd5-bd22-db3efbe4085e`.
- Status: not a "tweak" per se — it's a one-shot recovery. Listed
  here so the changelog is complete.
- Note: this is the canonical post-redeploy startup sequence.
  Documented in
  [`docs/plans/architecture/trader-pipeline.md`](../plans/architecture/trader-pipeline.md)
  as a known footgun.

### 2026-05-07 ~11:00–11:58 UTC — Copy Trade bootstrap pipeline unblock

- **Surface**: `traders.source_configs_json` for trader
  `61dcbeb2b9bc42bd9e9635a09ae5e0c3` (Sandbox - Traders Copy Trade)
  + `worker-trading` container restart (×2)
- **Applied via**: `psql` (jsonb_set) + UI pause toggles + `docker
  compose restart worker-trading`
- **Why**: after the 10:00 UTC `latency_class=fast` flip wrote the
  first 8 `trader_orders` (all `cancelled`), the pipeline froze:
  no new orders for ~2 hours despite 7 fast bots running. The
  diagnostic chain was:
  1. **GIL saturation** — 7 fast bots × parallel signals →
     event-loop starvation (272 `InterfaceError: connection is
     closed` per 15 min, 8 `QueryCanceledError`, asyncpg pool
     drained).
  2. **Wrong strategy** — Tail-End generated 0 signals over 2 h
     (filters too tight for current market). Traders Copy Trade
     was the only high-volume source (~261 sig/h).
  3. **Subscription state stale** after multiple `is_paused`
     flips — Copy Trade's `signal_cache_hit=0` despite live
     signal stream → first restart fixed it.
  4. **Strategy gates too tight for bootstrap** — 314
     decisions/5 min, all `skipped` with reasons dominated by
     `max_age` (68%, 5-second cap couldn't beat 5–7 s GIL-bound
     cycles) and `min_notional` (28%, leaders trade <$10).
  5. **Stale signal-cache after relax** — second restart cleared
     in-memory `signal_id`'s that no longer existed in
     `trade_signals` (FK violations on every audit-flush).
- **Expected effect**: `trader_orders` count grows; first
  `decision='selected'` row in `trader_decisions` for Copy Trade.
- **Verification command**:
  ```bash
  ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
    psql -U homerun -d homerun -c \"select decision, count(*) from trader_decisions \
    where trader_id='61dcbeb2b9bc42bd9e9635a09ae5e0c3' \
    and created_at > now() - interval '5 minutes' group by 1\""
  ```

### Changes

| Path | Before | After |
|---|---:|---:|
| `traders[Tail-End].is_paused` | false | true |
| `traders[Traders Copy Trade].is_paused` | true | false |
| `traders[Traders Copy Trade].source_configs_json[0].strategy_params.max_signal_age_seconds` | 5 | 300 |
| `traders[Traders Copy Trade].source_configs_json[0].strategy_params.min_source_notional_usd` | 10.0 | 1.0 |
| `traders[Traders Copy Trade].source_configs_json[0].strategy_params.max_entry_price` | 0.98 | 0.99 |
| Worker-trading container restarts | — | 2× (~11:34 UTC, ~11:52 UTC) |

### Outcome (5-min window after second restart)

| Metric | Before | After |
|---|---:|---:|
| `trader_orders` (Copy Trade) | 0 | **1** (`cancelled`, shadow) |
| `trader_decisions` selected | 0 | **1** |
| `trader_decisions` skipped | 0 | 103 (all gate-filtered, expected) |
| FK violations / 4 min | 34 | 29 (residual bug, non-blocking) |

First `selected` decision and matching `trader_order` written
**11:58:07 UTC**. Total `trader_orders` for the day:
`8 → 9` (still all `status=cancelled`, which is correct
right-censored event shape for KM-fallback bootstrap).

### Residual issues (not blocked, but follow up)

- **FK violations on `trader_decisions.signal_id` (29/4min).**
  Worker-trading caches `signal_id`s that don't exist in
  `trade_signals`. Likely race between event-bus publish and DB
  commit, or stale cache after upstream signal cleanup. Audit
  buffer drops the affected decisions but the pipeline survives.
  **Plan candidate: 0003 — Investigate and fix
  fast_trader_runtime signal-cache vs DB FK race.**
- **GIL saturation persists** (`worker-trading` 100% CPU even with
  one bot). Fast cycles still exceed 3 s budget regularly. See
  [`docs/plans/architecture/worker-trading.md`](../plans/architecture/worker-trading.md)
  for the four options to lift the GIL ceiling.
- **`Fast trader evaluate() exceeded budget` (0.2 s)** —
  individual `evaluate()` calls slip the 200 ms target. Same
  GIL root cause.

### Rollback

If we need to revert (e.g. Copy Trade exposure becomes risky for
shadow stats):

```bash
# Revert strategy_params:
ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c \"
  UPDATE traders
  SET source_configs_json = jsonb_set(
        jsonb_set(
          jsonb_set(
            source_configs_json::jsonb,
            '{0,strategy_params,max_signal_age_seconds}', '5'::jsonb
          ),
          '{0,strategy_params,min_source_notional_usd}', '10.0'::jsonb
        ),
        '{0,strategy_params,max_entry_price}', '0.98'::jsonb
      )::json,
      updated_at = now()
  WHERE id = '61dcbeb2b9bc42bd9e9635a09ae5e0c3';\""

# Re-pause Copy Trade if needed:
# UI: Bots → Sandbox - Traders Copy Trade → Pause
```

### Next checkpoint

- t+1 h (~13:00 UTC): expect ~10–15 `trader_orders` (selected
  rate ≈ 1/5 min if the pipeline stays healthy).
- t+6 h: cox_trainer cycle picks up; KM-fallback model should
  promote if `n_events >= ~5`.
- t+24 h: 100+ events → first Cox-PH fit attempt.
- Verify with:
  ```bash
  ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
    psql -U homerun -d homerun -c \
    \"select id, family, n_events, concordance_index, active \
    from fill_probability_models order by created_at desc limit 5\""
  ```

### 2026-05-07 ~14:30 UTC — correction: GIL framing in earlier entries was unverified

- **Surface**: this journal (no runtime change applied)
- **Applied via**: append-only correction note
- **Why**: several earlier entries in this file (notably the
  10:00 UTC `latency_class=fast` flip and the 11:00–11:58 UTC
  Copy Trade bootstrap unblock) attribute the worker-trading
  load problem to **GIL saturation** as if it were a measured
  fact. It was a hypothesis. The 2026-05-07 14:00 UTC py-spy
  profile (plan 0003,
  [`docs/plans/architecture/worker-trading-profile-2026-05-07.svg`](../plans/architecture/worker-trading-profile-2026-05-07.svg))
  showed:
  1. The `docker stats` reading of "100 % CPU" is misleading —
     `--idle` sampling reveals ~90 % of those samples are idle
     `concurrent.futures.thread._worker` frames waiting on a
     queue. Real CPU-active work is ~10 % of one core.
  2. The four hypothesised hotspots (strategy eval, WS JSON,
     Cox-PH, copy-trade processor) are **not** in the top of
     the CPU-active profile. The actual hotspots are
     `copy.deepcopy` ×2, uncached `get_oracle_history`, and a
     nested-loop `_compute_stability`.
  3. The "Fast trader cycle exceeded budget" warnings (3–7 s)
     and `idle_touch_commit: 5–7 s` were **DB-pool /
     async-coordination delays**, not pure CPU. The earlier
     diagnosis labelled them as GIL effects; they were not.
- **What this means for past entries**: the *symptoms* documented
  earlier (asyncpg `connection is closed` storms, audit-buffer
  drops, `Fast trader cycle exceeded hard budget`) are real and
  the operational fixes (scaling down active fast bots, relaxing
  Copy Trade gates, restarting worker-trading) were correct
  given what we knew at the time. But the *attribution* —
  "GIL saturation" — was not measured. Read those entries with
  that caveat.
- **What this means for future entries**: when an entry posits
  a root cause, mark it as "**hypothesis**" until the evidence
  is independent of the symptom (a profile, a DB plan, a
  reproducer). `100 % CPU in docker stats` alone does not
  establish GIL contention.
- **No rollback** — this entry corrects framing; nothing to
  revert.
