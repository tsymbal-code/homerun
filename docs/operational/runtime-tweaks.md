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
