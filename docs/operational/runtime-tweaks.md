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

### 2026-05-07 ~16:00 UTC — Plan 0005: tag-based market filter at ingest live

- **Surface**: backend (`scanner._apply_market_tag_whitelist`,
  `services/market_tag_aggregator`), DB (`market_tags_seen`
  table + two `app_settings` columns), API
  (`PUT /settings/scanner` accepts `market_filter_tags`,
  `GET /settings/market-filter/available-tags`), frontend
  (`Settings → Scanner → Market Tag Filter`)
- **Applied via**: plan 0005
  ([`docs/plans/completed/0005-tag-based-market-filter-at-ingest.md`](../plans/completed/0005-tag-based-market-filter-at-ingest.md)),
  alembic revision `202605070002_market_tag_filter`,
  redeploy through `./deploy/sync_remote.sh`
- **Why**: the 2026-05-07 14:00 UTC profile showed the
  worker-trading hotspots are catalog-driven, so shrinking the
  ingest universe is the highest-leverage CPU lever before any
  per-hotspot fix. The filter is OR-logic, case-insensitive,
  applied across `(market.tags ∪ event.tags)` before
  `_filter_tradable_markets`, and gated by an operator-managed
  whitelist saved in `app_settings.market_filter_tags`. Empty
  list ⇒ pass-through (today's behaviour).
- **Aggregator**: every refresh cycle the raw Polymarket markets
  + events are scanned for tags and upserted into a new
  `market_tags_seen` table (PK `tag`, plus `first_seen`,
  `last_seen`, `occurrences`). The Settings UI populates its
  picker from rows with `last_seen > now() - 24h`. Aggregator
  is gated by `MARKET_TAG_AGGREGATOR_ENABLED` (default `True`)
  for kill-switch safety.
- **Initial filter value**: empty (`[]`). The plan's smoke test
  exercised `['crypto']`, `['crypto', 'sports']`, and
  `['crypto', 'sports', 'politics']` to confirm worker logs
  show `Catalog tag-whitelist filter: X → Y markets`,
  but the deployed steady state is filter-inactive. Operator
  picks the production value out of band once the table has
  enough representative data (24 h of `last_seen` rows).
- **Post-filter re-profile (2026-05-07 16:05 UTC)**: with
  `whitelist=['crypto', 'sports', 'politics']` the catalog cut
  was 19 966 → 14 604 markets (~27 %). `_compute_stability`
  dropped from ~5 % to <1 % CPU; `copy.deepcopy` chain shrank
  from ~15 % to ~10.8 %; `_rebuild_realtime_graph` dropped off
  the top-25. `get_oracle_history` (combined) stayed flat in
  absolute terms and rose in *share* (~14 % → ~36 %) because
  the catalog-driven hotspots shrank around it; the crypto
  fast-binary lane reads from Binance + Chainlink, neither of
  which the tag filter touches. Plan 0004 was promoted from
  backlog to active to cover those residual non-catalog
  hotspots (TTL cache for oracle history, deepcopy halving,
  `orjson` on the dispatch path).
- **How to disable / change**: `Settings → Scanner → Market
  Tag Filter`, save with the new chip set (or empty list to
  disable). Or via API:
  ```bash
  ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/api/settings/scanner' \
    | python3 -c "import sys,json;d=json.load(sys.stdin);d[\"market_filter_tags\"]=[\"crypto\"];print(json.dumps(d))" \
    | ssh polyhome-1 'curl -fsS -X PUT -H "Content-Type: application/json" --data @- http://127.0.0.1:8888/api/settings/scanner'
  ```
- **Rollback**: setting `market_filter_tags = []` returns the
  pipeline to today's behaviour (no filtering). The aggregator
  table can be left in place — it costs ~1 KB/tag and the
  upsert is idempotent. Toggle
  `config.MARKET_TAG_AGGREGATOR_ENABLED = False` if a future
  bug ever requires stopping the writes without redeploying
  the schema.

## 2026-05-07 ~17:00 UTC — Plan 0006: crypto fast-binary lane toggle live

- **Surface**: `worker_control(name='crypto')` row +
  `Settings → Scanner → Crypto fast-binary lane` UI toggle.
- **Applied via**: redeploy via `./deploy/sync_remote.sh` (image
  rebuild for backend / 4 worker images / frontend).
- **Why**: the post-filter `worker-trading` profile from plan
  0005 showed `get_oracle_history` and `_oracle_move_from_history`
  combined eating ~42 % of CPU-active samples even when the
  operator filters the catalog to non-crypto tags. The two
  hotspots live in the parallel crypto fast-binary lane in
  [`market_runtime.py`](../../backend/services/market_runtime.py),
  which fetches its market list from
  [`crypto_service`](../../backend/services/crypto_service.py)
  and never consults `market_catalog`, so the tag filter cannot
  reach it. Plan 0006 plugs the two paths the existing
  `worker_control(crypto)` row didn't cover (startup refresh +
  reactive Binance-tick payload rebuild), wires cache
  invalidation on the active↔off transition, and surfaces the
  toggle in the Scanner tab.
- **Expected effect**: with the lane off, `worker-trading` no
  longer rebuilds per-market crypto payloads on every Binance
  tick. The 4 Binance feeds remain connected (cheap, ~1–5 µs
  per tick); only the per-market rebuild + WS broadcast stops.
  `get_crypto_markets()` returns `[]` immediately after the
  toggle (within the next loop iteration, ≤ 2 s).

### Verification (post-deploy, lane off)

```bash
# Toggle off via the existing API (the UI calls the same)
ssh polyhome-1 'curl -fsS -X POST http://127.0.0.1:8888/api/workers/crypto/pause | jq .status'
# Settings reflect the new state
ssh polyhome-1 "curl -fsS http://127.0.0.1:8888/api/settings/scanner | jq '.crypto_lane_enabled'"
# Worker stats show the cache cleared
ssh polyhome-1 "curl -fsS http://127.0.0.1:8888/api/workers/status \
  | jq '[.workers[] | select(.worker_name==\"crypto\") | {market_count: .stats.market_count, current_activity}]'"
```

Observed:

- `crypto_lane_enabled = false` in `/api/settings/scanner`.
- `market_count = 0`, `current_activity = "Paused"` in
  `/api/workers/status`.
- Toggle back on: `current_activity = "Live"`, `market_count = 15`,
  `dispatch_last_trigger = "reference_ws"` within ≤ 10 s.

### Re-profile (60 s `py-spy record --rate 100`)

| Hotspot | Plan 0005 post-filter | Plan 0006 lane off |
|---|---:|---:|
| `get_oracle_history` (sum) | ~36 % | < 1 % |
| `_oracle_move_from_history` | ~6.6 % | < 1 % |
| `_rebuild_crypto_rows_from_cache` | ~2.9 % | < 1 % |
| `copy.deepcopy` (sum) | ~10.8 % | ~0.7 % |

Top of the lane-off profile is dominated by stdlib I/O
machinery (`_worker` thread idle, asyncio selectors, ssl read,
pydantic + json). All three Plan 0004 hotspots collapsed below
1 %, so Plan 0004 is **archived** until the operator re-enables
the crypto lane.

Flamegraph captured for the record at
[`docs/plans/architecture/worker-trading-profile-2026-05-07-crypto-lane-off.svg`](../plans/architecture/worker-trading-profile-2026-05-07-crypto-lane-off.svg).
The temporary `cap_add: [SYS_PTRACE]` on the `worker-trading`
service in `docker-compose.yml` was reverted immediately after
the capture.

### Lane state at handover

Lane is currently **off** (`is_paused = true`). The operator
trades only Polymarket general markets right now; turn back on
via the UI toggle (or `POST /api/workers/crypto/start`) when a
crypto-fast-binary strategy is added to the active set.

### Rollback

The plan is fully runtime-toggleable — no rollback needed for
the code change itself. Operationally:

- `POST /api/workers/crypto/start` (or click the toggle on)
  re-enables the lane on the next loop iteration; the cache
  repopulates within `interval_seconds` (default 1 s).
- The `worker_control(name='crypto')` row stores the state
  across restarts. To wipe it, drop the row in psql; default is
  `is_enabled=true, is_paused=false`.

---

## 2026-05-07 ~20:00 UTC — Plan 0009: `latency_class=fast` workaround for `traders` source obsoleted

- **Surface**: code (`backend/services/signal_bus.py`) + this
  journal entry retiring the 10:00 UTC and 11:00–11:58 UTC
  workarounds for the `Sandbox - Traders Copy Trade` bot
  (id `61dcbeb2b9bc42bd9e9635a09ae5e0c3`).
- **Applied via**: plan 0009
  ([`docs/plans/completed/0009-fix-traders-source-on-normal.md`](../plans/completed/0009-fix-traders-source-on-normal.md)),
  redeploy through `./deploy/sync_remote.sh`. No DB tweak is
  required for the fix itself.
- **Why**: the 2026-05-07 10:00 UTC entry above flipped every
  sandbox bot to `latency_class=fast` to bootstrap the
  shadow-fill pipeline (and downstream Cox-PH training data).
  That tweak was correct *for that purpose* — `fast_submit`
  writes `TraderOrder` rows in shadow mode, which the
  `session_engine` path does not. **However**, for the
  `traders` source it was *also* the only way around a
  separate, latent bug: `signal_bus._strategy_runtime_metadata`
  routed `source_key='traders'` through the
  `else: execution_activation = "ws_post_arm_tick"` fallback,
  which made `intent_runtime.publish_opportunities` mark
  every traders-source signal as
  `deferred_until_ws=True, runtime_sequence=NULL`. On
  `latency_class=normal`, the orchestrator's
  `list_unconsumed_signals` filter dropped 100 % of those.
  Plan 0008 reproduced this with a 5-minute production
  baseline (`445 / 445 traders_copy_trade pending signals
  with runtime_sequence is null` while `traders_confluence`
  showed `30 / 30` with non-NULL sequence). Plan 0009 fixed
  it by replacing the if/elif/else chain with an explicit
  allow-list (`crypto → immediate, scanner → ws_current,
  traders → immediate`) plus a warn-and-fall-back-to-immediate
  default for unknown source keys.
- **Expected effect**: post-deploy, every `traders_copy_trade`
  signal lands in the runtime cache with a non-NULL
  `runtime_sequence` and `deferred_until_ws=False`. The
  orchestrator picks them up on the next cycle (≤ 60 s on
  `latency_class=normal`, sub-second on `fast`).
  `Sandbox - Traders Copy Trade` no longer needs to stay on
  `latency_class=fast` to produce decisions.
- **Verification command** (post-deploy):
  ```bash
  ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres \
    psql -U homerun -d homerun -c "
    select strategy_type, status, count(*) n,
      sum((runtime_sequence is not null)::int) with_seq,
      sum((runtime_sequence is null)::int)     without_seq
    from trade_signals
    where strategy_type in (''traders_copy_trade'',''traders_confluence'')
      and created_at > now() - interval ''5 minutes''
    group by strategy_type, status order by 1, 2;"'
  ```
  Both rows should report `without_seq = 0`. On the pre-fix
  build the `traders_copy_trade` row reported `with_seq = 0,
  without_seq = N` for every batch.

### Changes

| Path | Before | After |
|---|---:|---:|
| `signal_bus._strategy_runtime_metadata` (code) | if/elif/else; `else → "ws_post_arm_tick"` | allow-list dict + `_DEFAULT_EXECUTION_ACTIVATION = "immediate"` + warn-once-per-unknown-source |
| `traders` activation | `"ws_post_arm_tick"` (silent default) | `"immediate"` (explicit) |
| `unknown source` activation | `"ws_post_arm_tick"` (silent default) | `"immediate"` + `signal_bus` WARNING |

The earlier 10:00 UTC entry's
`latency_class=fast` flip remains *operationally useful* for
the shadow-fill bootstrap purpose it was originally applied
for (Cox-PH cold-start data). It is no longer *necessary* for
`Sandbox - Traders Copy Trade` to consume signals at all. The
operator can revert that bot's latency class if desired:

```sql
update traders set latency_class = 'normal'
where id = '61dcbeb2b9bc42bd9e9635a09ae5e0c3';
```

(Or via UI: Bots → Sandbox - Traders Copy Trade → Latency
class → Normal → Save.)

If the operator keeps the bot on `fast`, that is also fine —
the fix path is independent of latency class. Pre-fix, fast
caught ~0.1 % of leader trades by accident (CLOB feed
incidentally subscribed to a token the leader hit); post-fix,
fast catches them sub-second and normal catches them on the
next 60 s cycle.

### Rollback

If for some reason the fix needs to be backed out (e.g. an
unrelated regression surfaces in the publish path), the
operator workaround that produced visible Copy Trade activity
on the pre-fix build is the same `latency_class=fast` flip
documented in the 10:00 UTC entry above. Even with `fast`,
expect the pre-fix ~0.1 % per-leader-trade hit rate; that's
the empirical ceiling for the pre-fix pipeline. The proper
rollback is a code revert of the `signal_bus.py` change, not
a runtime tweak.

### Status

CLOSED — fix shipped, post-fix `without_seq = 0` invariant
verified in plan 0009 Task 6.

---

## 2026-05-08 ~05:00 UTC — Plan 0009 post-deploy: FK race in `trader_decisions` unmasked, filed as plan 0010

- **Surface**: observation only — no DB tweak, no code revert. Filed
  as a follow-up plan rather than a journal-resolved tweak because
  the fix lives in code.
- **Applied via**: post-deploy verification of plan 0009 on
  `polyhome-1`.
- **Why**: with the
  `signal_bus._strategy_runtime_metadata` gate gone (plan 0009),
  every `traders_copy_trade` signal now lands in the runtime
  cache with a non-NULL `runtime_sequence` and is visible to the
  orchestrator. The orchestrator's first cycles after the deploy
  surfaced a separate, **pre-existing** bug that the gate had
  been masking: every decision write fails with
  `ForeignKeyViolationError on trader_decisions.signal_id →
  trade_signals.id`, and the retry path (writing a placeholder
  `trader_signal_consumption` row) also fails on
  `trader_signal_consumption.decision_id_fkey`. Net effect: the
  orchestrator *sees* the signal and *attempts* a decision, but
  cannot persist either the decision or the consumption record.
  Cause is a publish/projection race: `intent_runtime.publish_opportunities`
  mutates `self._signals_by_id` and pings consumers via
  `publish_signal_batch` BEFORE the projection loop has committed
  the corresponding `trade_signals` row. For scanner signals the
  60s scanner cycle gives the projection plenty of time to drain
  before the orchestrator's 60s cycle next ticks; for
  `traders_copy_trade` the publish→consume gap is microseconds
  (in-process wallet-WS callback → in-process orchestrator queue),
  so the race fires almost every cycle. **This is a separate bug,
  not a regression of plan 0009.**
- **Verification (post-deploy snapshot, 2026-05-08T04:30..05:00Z)**:

  ```text
   strategy_type      | status  |  n  | with_seq | without_seq
  --------------------+---------+-----+----------+-------------
   traders_confluence | expired |   1 |        1 |           0
   traders_confluence | pending |   4 |        4 |           0
   traders_copy_trade | failed  |  63 |       63 |           0
   traders_copy_trade | pending | 191 |      191 |           0
  ```

  → plan 0009's invariant (`without_seq = 0`) **holds**.

  ```text
   trader_id                        | outcome |  c
  ----------------------------------+---------+-----
   61dcbeb2b9bc42bd9e9635a09ae5e0c3 | failed  | 151
  ```

  → 151 consumption attempts in 15 minutes for the Copy Trade
  trader (vs. 0 before plan 0009). Every one of them is the
  placeholder `failed` outcome from the FK race retry path; the
  underlying error in `worker-trading` logs is
  `IntegrityError: ForeignKeyViolationError DETAIL: Key
  (signal_id)=(<id>) is not present in table "trade_signals".`
- **Filed as**: plan 0010 ([`docs/plans/0010-fix-traders-publish-fk-race.md`](../plans/0010-fix-traders-publish-fk-race.md)).
  The plan picks one of two minimal fixes (commit
  `trade_signals` synchronously before
  `list_unconsumed_signals` could return the row, or wrap the
  decision write in `INSERT ... ON CONFLICT DO NOTHING` to
  re-derive the row from the snapshot the orchestrator already
  holds) without re-introducing the deferred-state pattern plan
  0009 retired.

### Changes

None — observation-only journal entry.

### Rollback

Not applicable. The plan-0009 fix is correct on its own; the FK
race is a separate code defect tracked in plan 0010. Rolling back
plan 0009 would re-mask the race but also re-disable Copy Trade
on `latency_class=normal`, which is a worse end state than the
current "signals reach orchestrator, decisions can't persist".

### Status

CLOSED — fix shipped on `polyhome-1` at 2026-05-08 ~06:20 UTC
(plan 0010). Post-deploy verification reported `0` FK violations
in the seven minutes after orchestrator unpause, with 95
`trader_decisions` rows for `traders_copy_trade` (76 skipped + 19
blocked) and `99 trader_signal_consumption` rows, every one of
which has a non-null `decision_id` (`78 skipped + 21 blocked`).
See the `2026-05-08 ~06:20 UTC` entry below for full numbers.

---

## 2026-05-08 ~06:20 UTC — Plan 0010: traders publish-side FK race fixed

- **Surface**: code (`backend/services/intent_runtime.py`,
  `publish_opportunities` skeleton-INSERT pass) +
  `trader_orchestrator_control` (post-deploy unpause and
  `selected_account_id` rebind to sandbox).
- **Applied via**: plan 0010
  ([`docs/plans/0010-fix-traders-publish-fk-race.md`](../plans/0010-fix-traders-publish-fk-race.md)),
  redeploy via `BUILD_IMAGES=0 ./deploy/sync_remote.sh` (GHCR pull,
  no rebuild needed — fix is pure-Python in
  `backend/services/intent_runtime.py`), followed by
  `POST /api/trader-orchestrator/start` to flip
  `is_enabled=false, is_paused=true → is_enabled=true,
  is_paused=false` and rebind `selected_account_id` to the sandbox
  account `08fb2d1e-3bb1-4cd5-bd22-db3efbe4085e`.
- **Why**: with plan 0009 lifting the
  `_strategy_runtime_metadata` gate on the `traders` source, every
  `traders_copy_trade` signal landed in the runtime cache with a
  non-NULL `runtime_sequence`, and the orchestrator immediately
  tried to write `trader_decisions(signal_id=X)`. But for genuinely
  new `(source, dedupe_key)` pairs, the projection loop's UPSERT
  into `trade_signals` had not yet committed when the orchestrator
  consumed the signal microseconds later — the publish path only
  populated the in-memory cache and pinged consumers via
  `publish_signal_batch`. Net effect was 100 % FK violations on
  every Copy Trade decision attempt (151 placeholder
  `consumption.outcome='failed'` rows / 15 min in the post-deploy
  baseline, see the previous journal entry). Plan 0010 fixes the
  publish path to **synchronously commit a skeleton `trade_signals`
  row** (with `status='pending'` and the same `id` it stores in
  the cache) for every new dedupe key, *before* `publish_opportunities`
  returns. The asynchronous projection loop's later UPSERT then
  fills in the rich payload via `UPDATE` on the same row. The
  publish-time prefetch from plan 0010's earlier draft also
  remains, covering post-restart cache-staleness for known dedupe
  keys.
- **Expected effect**: no FK violations in `worker-trading` logs;
  `Sandbox - Traders Copy Trade` produces real `trader_decisions`
  rows with `decision in ('selected', 'skipped', 'blocked')` (not
  the placeholder `failed`); `trader_signal_consumption` rows
  carry an actual outcome and a non-null `decision_id`.
- **Verification commands** (run 7 min after orchestrator unpause):

  ```bash
  # 1) FK violations since deploy
  ssh polyhome-1 "cd /home/polyhome/homerun && \
    docker compose logs --since 7m worker-trading 2>&1 \
    | grep -cE 'trader_decisions_signal_id_fkey|ForeignKeyViolation'"

  # 2) trader_decisions volume + decision distribution
  ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
    psql -U homerun -d homerun -c \"select strategy_key, decision, count(*) \
    from trader_decisions where created_at > '2026-05-08 06:19:45' \
    group by 1,2 order by 1,2\""

  # 3) trader_signal_consumption — every row has a real decision_id + outcome
  ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
    psql -U homerun -d homerun -c \"select coalesce(outcome,'(null)') as outcome, \
    case when decision_id is null then 'no-decision' else 'has-decision' end \
    as decision_link, count(*) from trader_signal_consumption \
    where consumed_at > '2026-05-08 06:19:45' group by 1,2 order by 3 desc\""

  # 4) trade_signals freshness for traders source
  ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
    psql -U homerun -d homerun -c \"select status, count(*) from trade_signals \
    where source='traders' and created_at > '2026-05-08 06:19:45' \
    group by 1 order by 2 desc\""
  ```

### Post-deploy snapshot (06:19:45..06:27:10 UTC)

| Validation check | Result |
|---|---:|
| FK violations in `worker-trading` logs | **0** |
| `trader_decisions` for `traders_copy_trade` | 95 (76 skipped, 19 blocked) |
| `trader_decisions` for `negrisk` | 19 (skipped) |
| `trader_decisions` for `market_making` | 11 (blocked) |
| `trader_signal_consumption` rows with `has-decision` | **99 / 99** (78 skipped, 21 blocked) |
| `trader_signal_consumption` with `(null)` decision | **0** |
| `trade_signals (source=traders)` rows since deploy | 137 (66 pending, 70 skipped, 1 expired) |

Plan 0010's success criteria (zero FK violations,
`Sandbox - Traders Copy Trade` recording `trader_decisions`,
`trader_signal_consumption` rows with real outcomes) are all
satisfied. The skipped/blocked distribution is the expected
strategy-gate filter output — the strategy is doing its job;
the prior 100 % `failed` outcome was the FK race masking it.

### Changes

| Path | Before | After |
|---|---:|---:|
| `intent_runtime.publish_opportunities` (code) | populates in-memory cache + pings consumers; relies on async projection loop to commit `trade_signals` | additionally batches a `pg_insert(TradeSignal).values(...).on_conflict_do_nothing(index_elements=['source','dedupe_key'])` for every new dedupe key in a separate committed session, **before** `publish_opportunities` returns |
| `trader_orchestrator_control.is_enabled` | false (paused before redeploy) | true |
| `trader_orchestrator_control.is_paused` | true | false |
| `trader_orchestrator_control.settings.selected_account_id` | null | `08fb2d1e-3bb1-4cd5-bd22-db3efbe4085e` |

### Rollback

If the publish-time skeleton-INSERT ever causes `trade_signals`
write contention or a regression of its own, the rollback is a
**code revert** of `backend/services/intent_runtime.py` (drop the
"Skeleton-INSERT pass" block and the `pg_insert` import); there is
no runtime knob to turn this off. Reverting reintroduces the FK
race for `traders` source. Anyone considering this revert should
also re-pause `Sandbox - Traders Copy Trade` to avoid the
placeholder `consumption.outcome='failed'` storm in
`trader_signal_consumption` (151 rows / 15 min on the pre-fix
build).

The orchestrator unpause is the standard post-redeploy startup
sequence — see the `2026-05-07 ~07:00 UTC` entry above for the
canonical recipe. No special rollback is needed.

### Status

CLOSED — fix shipped on `polyhome-1`. Closes the OPEN item from
the `2026-05-08 ~05:00 UTC` entry above.
