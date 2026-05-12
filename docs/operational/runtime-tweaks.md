# Runtime Tweaks — operational journal

Append-only journal of operator-applied configuration changes that
**live in the database**, not in git. These are knob twists that
take effect immediately on the running stack and survive restarts
because the values persist in `app_settings` / `traders` /
`trader_orchestrator_control` tables.

This file is the **rollback reference** when a tuning experiment
needs to be undone — every entry records the *before* and *after*
values plus the exact rollback recipe.

> **Host references in this journal are historical.** Past entries
> name the literal host (`polyhome-1`, `polyhome-prod`) that was
> correct on the day the entry was written and must not be
> rewritten retroactively. Future entries should use the
> branch-derived `<HOMERUN_HOST>` placeholder, with the resolved
> alias spelled out in the entry's `Verification` block —
> consult [`docs/plans/architecture/deploy-targets.md`](../plans/architecture/deploy-targets.md)
> for the SSOT mapping.

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

### CRITICAL-tier knob changes — consult the interaction matrix first

If the change touches any field listed in the **Knob interaction
matrix — CRITICAL tier** section of
[`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md#knob-interaction-matrix--critical-tier),
the entry in this journal is **incomplete** without a 5-step
walkthrough — see the [Walkthrough template](#walkthrough-template-for-critical-knob-changes)
below. The list of 21 CRITICAL knobs covered today (per-entry
**audited per Plan 0034 on 2026-05-10** — read each entry's
`<!-- audited YYYY-MM-DD: <verdict> -->` marker before applying;
the sibling **HIGH-tier** matrix was audited per **Plan 0036 on
2026-05-10** with verdicts on each of the 46 entries — see the
matrix's **Knob interaction matrix — HIGH tier** section, where
4 entries were demoted to `confirmed dead`: `slippage_bps`,
`max_spread_bps`, `use_dynamic_sizing` (Group A) and
`scanner_max_opportunities_total/_per_strategy` (Group D)):

`max_position_notional_usd` · `max_trade_notional_usd` ·
`max_gross_exposure_usd` · `min_exit_notional` (gate, not knob;
see `enforce_min_exit_notional`) · `max_open_orders` ·
`max_open_positions` · `max_daily_loss_usd` ·
`circuit_breaker_drawdown_pct` (DEAD CODE — flagged in the
matrix) · `halt_on_consecutive_losses` ·
`max_consecutive_losses` · `circuit_breaker_safe_exit` (event,
not knob) · `block_new_orders` · `traders.is_paused/is_enabled`
· `worker_control.is_paused/is_enabled` ·
`allow_taker_limit_buy_above_signal` ·
`max_per_market_exposure_usd` · `live_risk_clamps.*` (umbrella,
9 fields) · `MAX_TRADE_SIZE_USD/MAX_DAILY_TRADE_VOLUME/MIN_ACCOUNT_BALANCE_USD`
· `MAX_PER_MARKET_USD` · `worker_control.kill_switch` ·
`runtime_metadata.resume_policy`.

Without the walkthrough, a "tighten the loss cap" tweak can
silently kill the copy-trade bot via the
`copy_drawdown` indirect consumer (see matrix entry on
`max_daily_loss_usd`). This journal saw that exact failure
mode three times in the 2026-05-08 / 2026-05-09 sessions —
hence the three-layer fix (Phase 1 = matrix, Phase 2 = this
template, Phase 3 = agent memory rule).

## Walkthrough template for CRITICAL knob changes

Every new entry under `## Entries` that touches a CRITICAL-tier
knob (per the list above) **must** include the five steps
below. Prose-only ("no impact expected") answers are rejected
at audit; numeric values or explicit `n/a — verified against
matrix on YYYY-MM-DD` are required. `n/a` is acceptable
**only when the matrix entry confirms zero impact**, not when
the writer "doesn't expect" anything.

If the change is wrapped in a Ralphex plan rather than a one-off
operator tweak, the plan must additionally satisfy the
[CRITICAL knob touch policy](../plans/README.md#critical-knob-touch-policy)
(plan 0028). The journal entry produced by the plan still
fills this template — that's the artefact the plan-design rule
binds against.

### Step 1 — Direct gate impact (numeric)

For each CRITICAL field changed:

| Field | Before | After | Direct gate(s) | Pre threshold | Post threshold |
|---|---:|---:|---|---:|---:|
| <field> | <num> | <num> | <gate from matrix> | <formula evaluated at "before"> | <formula evaluated at "after"> |

### Step 2 — Indirect-metric impact (numeric)

For each indirect consumer documented in the matrix entry
for the changed field:

| Field changed | Derived metric | Pre value (today's data) | Post value (today's data) | Sibling gate that reads it |
|---|---|---:|---:|---|

If the matrix entry has zero indirect consumers, write
`n/a — matrix confirms zero indirect consumers`.

### Step 3 — Live data simulation (SQL or curl, not prose)

```sql
-- "How many decisions in the last 24 h would have been blocked
--  under the new threshold?"  Or the gate-specific equivalent.
SELECT ... FROM trader_decisions WHERE ...
```

Paste the **actual query result** (1–2 numbers, ideally a
before/after pair). Prose-only responses fail the audit.

### Step 4 — Compound-effect checklist

Tick every sibling-knob row that interacts with this change
according to the matrix. At least one row must be ticked **or**
the value `none — verified against matrix on YYYY-MM-DD`
written explicitly. Compound effects beyond the matrix
(strategy-params, etc.) go in a free-form bullet below the
checklist.

- [ ] `max_position_notional_usd` — <effect or `n/a`>
- [ ] `max_trade_notional_usd` — <effect or `n/a`>
- [ ] `max_gross_exposure_usd` — <effect or `n/a`>
- [ ] `max_open_orders` / `max_open_positions` — <effect or `n/a`>
- [ ] `max_daily_loss_usd` — <effect or `n/a`>
- [ ] `circuit_breaker_drawdown_pct` — DEAD CODE per matrix; ignore unless matrix changes
- [ ] `halt_on_consecutive_losses` / `max_consecutive_losses` — <effect or `n/a`>
- [ ] `circuit_breaker_safe_exit` (force-flatten event) — <effect or `n/a`>
- [ ] `block_new_orders` — <effect or `n/a`>
- [ ] `traders.is_paused` / `traders.is_enabled` — <effect or `n/a`>
- [ ] `worker_control.is_paused` / `worker_control.is_enabled` — <effect or `n/a`>
- [ ] `allow_taker_limit_buy_above_signal` — <effect or `n/a`>

### Step 5 — Rollback recipe (must run in < 30 s)

```bash
# Exact SQL / curl / UI path that returns the system to the
# pre-tweak state in under 30 seconds.  Tested before applying.
```

### Worked example — hypothetical `max_daily_loss_usd: 300 → 100`

Illustrative. **Not an actual tweak applied to production.**
Shows the dimensional-bug class the matrix was written to
prevent.

#### Step 1 — Direct gate impact

| Field | Before | After | Direct gate(s) | Pre threshold | Post threshold |
|---|---:|---:|---|---:|---:|
| `max_daily_loss_usd` | 300 | 100 | `trader_daily_loss` ([`risk_manager.py:61-84`](../../backend/services/trader_orchestrator/risk_manager.py)) | `trader_daily_realized_pnl > -300 → pass` | `trader_daily_realized_pnl > -100 → pass` |
| `max_daily_loss_usd` | 300 | 100 | `trader_daily_total_loss` ([`risk_manager.py:104-117`](../../backend/services/trader_orchestrator/risk_manager.py)) | `realized + unrealized > -300 → pass` | `realized + unrealized > -100 → pass` |

#### Step 2 — Indirect-metric impact

| Field changed | Derived metric | Pre value (today's data) | Post value (today's data) | Sibling gate that reads it |
|---|---|---:|---:|---|
| `max_daily_loss_usd` | `trader_drawdown_pct` = `(-trader_total_daily_pnl / max_daily_loss_usd) × 100` | sample $30 loss → `30 / 300 × 100 = 10%` | sample $30 loss → `30 / 100 × 100 = 30%` | `copy_drawdown` ([`traders_copy_trade.py:599, 797`](../../backend/services/strategies/traders_copy_trade.py)) |

The same nominal $30 loss now reads as **3× the drawdown%**.
Any `max_copy_drawdown_pct < 30` (the copy-trade
strategy_param) trips `copy_drawdown` — silently disabling
the bot for the rest of the session even though the dollar
loss is unchanged.

#### Step 3 — Live data simulation

```sql
-- Block-rate counterfactual: how many "selected" decisions in
-- the last 24 h would have hit the trader_daily_loss gate at
-- the tighter $100 floor?
SELECT
  count(*) FILTER (WHERE pnl_at_decision <= -100) AS would_block_post,
  count(*) FILTER (WHERE pnl_at_decision <= -300) AS would_block_pre,
  count(*)                                          AS total_selected
FROM (
  SELECT
    d.id,
    (SELECT coalesce(sum(o.actual_profit), 0)
     FROM trader_orders o
     WHERE o.trader_id = d.trader_id
       AND o.created_at::date = d.created_at::date
       AND o.created_at <= d.created_at) AS pnl_at_decision
  FROM trader_decisions d
  WHERE d.created_at > now() - interval '24h'
    AND d.decision = 'selected'
) sub;
```

Expected output shape (illustrative):
`would_block_post=12 | would_block_pre=0 | total_selected=180`
→ tightening blocks **12 of 180 (~6.7 %)** decisions that
would have proceeded under the old threshold. Confirms
non-trivial impact even before considering the indirect
`copy_drawdown` chain.

#### Step 4 — Compound-effect checklist

- [ ] `max_position_notional_usd` — n/a (independent cap)
- [ ] `max_trade_notional_usd` — n/a (independent cap)
- [ ] `max_gross_exposure_usd` — n/a (independent cap)
- [ ] `max_open_orders` / `max_open_positions` — n/a (count-cap, not pnl)
- [ ] `max_daily_loss_usd` — **THIS IS THE CHANGE**
- [ ] `circuit_breaker_drawdown_pct` — DEAD CODE per matrix; no effect
- [x] `halt_on_consecutive_losses` / `max_consecutive_losses` — **compound**: if CB safe-exit fires after change, force-flatten N positions can realize > $100 instantly → re-block on `daily_loss` immediately, looks like permanent halt
- [x] `circuit_breaker_safe_exit` — see above
- [ ] `block_new_orders` — n/a
- [ ] `traders.is_paused` / `traders.is_enabled` — n/a
- [ ] `worker_control.is_paused` / `worker_control.is_enabled` — n/a
- [ ] `allow_taker_limit_buy_above_signal` — n/a

Strategy-param compound (outside matrix scope):
- **`max_copy_drawdown_pct`** (Copy Trade strategy_param,
  default 100.0): `trader_drawdown_pct` becomes 3× more
  sensitive. If operator ever tightens `max_copy_drawdown_pct`
  below 30, the bot silences immediately. Recommendation:
  audit `max_copy_drawdown_pct` for every active
  Copy-Trade-class bot **before** applying this change.

#### Step 5 — Rollback

```bash
ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c \
  \"UPDATE traders SET risk_limits = jsonb_set(risk_limits::jsonb, '{max_daily_loss_usd}', '300'::jsonb)::json
    WHERE name = 'Sandbox - Traders Copy Trade';\""
```

(In real entries, the rollback applies to **all** traders
the tweak touched, not just one.)

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

---

## 2026-05-08 ~10:30 UTC — Plan 0011: defensive `expires_at` on skeleton rows + stuck-skeleton retention sweep

- **Surface**: code (`backend/services/intent_runtime.py`,
  `backend/services/skeleton_signal_retention.py`,
  `backend/workers/host.py`, `backend/config.py`).  No DB tweak —
  three new config knobs default in code, no migration.
- **Applied via**: plan 0011
  ([`docs/plans/0011-skeleton-trade-signal-ttl-and-retention.md`](../plans/0011-skeleton-trade-signal-ttl-and-retention.md)),
  redeploy via `BUILD_IMAGES=0 ./deploy/sync_remote.sh`.
- **Why**: Plan 0010's publish-side skeleton-INSERT closed the
  in-process FK race for the `traders` source by committing a
  `(source, dedupe_key)` placeholder row in `trade_signals`
  BEFORE the projection loop's UPSERT.  The skeleton was
  committed with `expires_at = NULL`.  If `publish_opportunities`
  dies between the skeleton commit and the projection-loop UPSERT
  (process kill, connection drop, unhandled exception, mid-call
  `docker compose restart`), the skeleton row stays in
  `trade_signals` with `payload_json IS NULL`, `runtime_sequence
  IS NULL`, `status='pending'`, `expires_at IS NULL` forever.
  In the steady state the next genuine publish for the same
  dedupe_key adopts the stuck row's id (Plan 0010's ON CONFLICT
  DO NOTHING + re-SELECT path), so the system self-heals — but
  a dedupe_key that never republishes leaves its skeleton in the
  table forever.  The existing `_run_trade_signal_pruner_loop`
  keys on `expires_at < now()` so it cannot reach `expires_at IS
  NULL` orphans.  Plan 0011 adds two safeguards: (a) defensive
  `expires_at = now + INTENT_RUNTIME_SKELETON_TTL_SECONDS` on
  every skeleton-INSERTed row, and (b) a discovery-plane sweep
  (`services.skeleton_signal_retention.prune_stuck_skeletons`)
  that DELETEs orphaned skeletons matching `payload_json IS NULL
  AND runtime_sequence IS NULL AND status='pending' AND created_at
  < now() - max_age` outright (no status flip — they never
  carried any consumer-visible state).
- **Chosen defaults**:

  | Knob | Default | Rationale |
  |---|---:|---|
  | `INTENT_RUNTIME_SKELETON_TTL_SECONDS` | 300 (5 min) | Generous; the projection loop commits within ~500 ms in steady state.  Strategy expires_at always wins via the projection's own UPSERT. |
  | `INTENT_RUNTIME_SKELETON_RETENTION_INTERVAL_SECONDS` | 900 (15 min) | Cheap DELETE; orphans are rare so a frequent sweep is wasted cost. |
  | `INTENT_RUNTIME_SKELETON_RETENTION_MAX_AGE_SECONDS` | 3600 (1 h) | Service-level helper additionally clamps caller value to `>= 60` to avoid racing the projection loop in dev / under heavy load. |

- **Verification commands**:

  ```bash
  # Steady-state stuck-skeleton count (Tier 2 monitoring; should be 0).
  ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
    psql -U homerun -d homerun -c \"select count(*) stuck_skeletons \
    from trade_signals where payload_json is null \
      and runtime_sequence is null and status='pending' \
      and created_at < now() - interval '1 minute'\""

  # Tier 1 invariant (Plan 0009 + Plan 0010); should report without_seq=0.
  ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
    psql -U homerun -d homerun -c \"select strategy_type, status, count(*) n, \
    sum((runtime_sequence is not null)::int) with_seq, \
    sum((runtime_sequence is null)::int) without_seq \
    from trade_signals \
    where strategy_type in ('traders_copy_trade','traders_confluence') \
      and status != 'pending' and payload_json is not null \
      and created_at > now() - interval '5 minutes' \
    group by strategy_type, status order by 1, 2\""

  # Confirm the retention loop is alive on the discovery plane.
  ssh polyhome-1 "cd /home/polyhome/homerun && docker compose logs --since 16m worker-discovery 2>&1 | \
    grep -E 'skeleton-signal-retention|Pruned stuck trade_signals' | tail -10"
  ```

  Plus: inject a stuck skeleton manually via psql to exercise the
  sweep end-to-end (the 5-step reproducer is in plan 0011
  Task 6).

### Changes

| Path | Before | After |
|---|---:|---:|
| `intent_runtime.publish_opportunities` skeleton row | `expires_at` absent → SQL NULL → invisible to terminal-row pruner | `expires_at = now + INTENT_RUNTIME_SKELETON_TTL_SECONDS` (default 300 s) |
| `worker-discovery` background tasks | terminal-row pruner only on trading plane | adds `skeleton-signal-retention` loop (DELETE every 15 min for orphans older than 1 h) |
| `backend/config.py` | — | three new env-overridable knobs (TTL, retention interval, retention max age) |

### Rollback

The retention sweep is opt-in via the discovery plane's task list;
removing it from `host.py` (one `if self._plane_name == 'discovery'`
block) reverts to plan-0010-era behaviour where orphan skeletons
accumulate forever invisible to the terminal-row pruner.  Stale
skeletons that already accumulated under that mode can be cleaned
out manually:

```sql
DELETE FROM trade_signals
WHERE payload_json IS NULL
  AND runtime_sequence IS NULL
  AND status = 'pending'
  AND created_at < now() - interval '1 hour';
```

The defensive `expires_at` on skeleton rows is also reverted by
removing the `"expires_at": skeleton_expires_at` line from the
skeleton row dict; rolling back this single line returns to plan
0010 publish-time behaviour.  None of the three config knobs
require a migration.

### Status

CLOSED — fix shipped on `polyhome-1` via
`BUILD_IMAGES=1 ./deploy/sync_remote.sh` at 2026-05-08
~07:33 UTC.  Post-deploy verification (20-minute soak):

| Check | Result |
|---|---:|
| 7 containers Up healthy | OK |
| FK violations (`trader_decisions_signal_id_fkey`) since deploy | **0** |
| Tier 1 (`without_seq=0` for `traders_copy_trade`) | **0/264** |
| Tier 2 (stuck skeletons in steady state) | **0** |
| Defensive `expires_at` coverage on new `traders` skeletons | **331 / 331** |
| Retention loop alive on discovery plane | first iteration logged at 07:34:07 UTC, second at 07:49:07 UTC |
| Manual orphan injection → reap by live loop | **deleted=1 within 14 min** (within the ≤15 min budget) |
| `trader_decisions` outcomes (`traders_copy_trade`) | 13 selected / 158 skipped / 48 blocked |

The publish path's invariant chain is now: skeleton-INSERT
synchronously commits a `(source, dedupe_key)` row with a
defensive TTL → projection-loop UPSERT enriches it with
`payload_json`, `runtime_sequence`, and the strategy's intended
`expires_at` (overwriting the TTL) → if publish dies between
those two steps, the discovery-plane sweep DELETEs the orphan
within at most 1 hour.  Plans 0009 + 0010 + 0011 form the
complete fix; the FK race that surfaced after Plan 0009 unmasked
it (and that Plan 0010 closed for the in-process consume path)
is now defended in depth at the storage layer too.

---

## 2026-05-08 ~15:50 UTC — relax shadow gates so `selected → trader_orders` materializes (orchestrator + Copy Trade bot)

- **Surface**: orchestrator `global_runtime` (`app_settings`-backed)
  + bot `risk_limits_json` for `Sandbox - Traders Copy Trade` +
  new UI surface in `TradingPanel.tsx` for the previously
  API-only `runtime_trigger_cycle_timeout_seconds` knob.
- **Applied via**: UI (Trading Panel → Settings; per-bot Risk
  Limits) + one-shot `curl PUT /api/trader-orchestrator/settings`
  for the new knob (UI now also exposes it).
- **Why**: throughout 2026-05-07 / 2026-05-08 the shadow stack
  produced steady `selected` decisions (≥100/h on
  `traders_copy_trade`) but **0 `trader_orders`**.  Multiple
  causes intersected.  The two operator-tunable ones identified
  while reading `trader_decision_checks`:
  1. `Strict WS pricing source` rejected entries because the
     in-process WS ladder lagged the snapshot freshness budget;
  2. `Source notional floor` and shadow ordersize hit
     `max_trade_notional_usd=5` before the strategy could
     express any meaningful position.

  Concurrently, runtime-trigger cycles fired by `signals.publish`
  ran with the hard-coded fallback `_RUNTIME_TRIGGER_DEFAULT_CYCLE_TIMEOUT_SECONDS = 10.0`
  (`backend/workers/trader_orchestrator_worker.py:934` family).
  The 10 s budget is too tight for Cox-PH + microstructure +
  multi-gate evaluation under shadow load — heavy cycles
  reliably timed out before reaching the order-write path.
  Orchestrator settings already had a JSON-only knob
  `runtime_trigger_cycle_timeout_seconds`
  (`backend/api/routes_trader_orchestrator.py:160`, validator
  range `3.0 – 60.0`) but the Settings sheet in
  `TradingPanel.tsx` did not expose it.
- **Important correction to a prior assistant note**: an earlier
  draft of this journal entry claimed `session_engine` does not
  write `trader_orders` in shadow because of the
  `if mode == "live"` block at
  `backend/services/trader_orchestrator/session_engine.py:1530`.
  That is **wrong**.  Line 1530 is the *pre-submit* placeholder
  (idempotency keying for live submission only); the
  *post-execution* path at
  `backend/services/trader_orchestrator/session_engine.py:2604`
  (`build_trader_order_row(...)` + `trader_orders.append(...)`)
  runs unconditionally for both shadow and live legs.  The
  classic / `latency_class=normal` pipeline therefore IS the
  ML-feeding path in shadow — switching every bot to `fast` is
  not a prerequisite for `trader_orders` to appear.  All seven
  active bots stay on `latency_class=normal` (the single
  exception, `Sandbox - Tail-End`, is on `fast` because the
  strategy is single-leg by design and predates the ML
  refactor — leave it).  This is the operator's explicit policy:
  classic-only so ML training data is consistent and dry-run
  results are representative of the live path.

### Changes

| Path | Before | After |
|---|---:|---:|
| Orchestrator → Live Market Context → `Strict WS Pricing Only` | `true` | `false` |
| Orchestrator → Live Market Context → `Max Market Data Age (ms)` | `10000` (10 s) | `20000` (20 s) |
| Orchestrator → Loop → `Runtime-Trigger Cycle Timeout (seconds)` | unset → hard-coded 10 s default | `45` |
| `Sandbox - Traders Copy Trade` → Risk Limits → `max_trade_notional_usd` | `5.0` | `25.0` |
| `Sandbox - Traders Copy Trade` → Risk Limits → `max_position_notional_usd` | `5.0` | `5.0` (unchanged — operator only raised per-trade size) |
| `frontend/src/components/TradingPanel.tsx` Settings sheet | `runtime_trigger_cycle_timeout_seconds` not surfaced | new "Runtime-Trigger Cycle Timeout" input next to "Trader Cycle Timeout" |
| `frontend/src/services/apiTraders.ts` `TraderOrchestratorConfig.global_runtime` | missing field | adds `runtime_trigger_cycle_timeout_seconds: number \| null` |
| `backend/services/trader_orchestrator_state.py` `_normalize_global_runtime_settings` + `compose_trader_orchestrator_config` | did not read or echo `runtime_trigger_cycle_timeout_seconds` | normalizes it (clamp `3.0..60.0`) and echoes it on `GET /api/trader-orchestrator/status` |

### Verification commands

```bash
# Orchestrator runtime — confirm the three knobs landed
ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/api/trader-orchestrator/status' \
  | python3 -c "
import json, sys
gr = json.load(sys.stdin)['config']['global_runtime']
print('strict_ws_pricing_only:', gr['live_market_context']['strict_ws_pricing_only'])
print('max_market_data_age_ms:', gr['live_market_context']['max_market_data_age_ms'])
print('runtime_trigger_cycle_timeout_seconds:', gr.get('runtime_trigger_cycle_timeout_seconds'))
print('trader_cycle_timeout_seconds:', gr.get('trader_cycle_timeout_seconds'))
"
# Expected output:
#   strict_ws_pricing_only: False
#   max_market_data_age_ms: 20000
#   runtime_trigger_cycle_timeout_seconds: 45.0
#   trader_cycle_timeout_seconds: 60.0

# Per-bot risk limits — confirm Copy Trade got the bump
ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c \"select name, latency_class, \
    (risk_limits_json->>'max_trade_notional_usd')::float trade_cap, \
    (risk_limits_json->>'max_position_notional_usd')::float pos_cap \
    from traders where is_enabled and not is_paused order by name\""

# Shadow trader_orders flow — should grow if the gates were the actual blocker
ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c \"select \
    count(*) filter (where created_at > now() - interval '30 minutes') orders_30m, \
    count(*) filter (where created_at > now() - interval '10 minutes') orders_10m, \
    max(created_at) last_order_at \
    from trader_orders where mode='shadow'\""

# Decision distribution — sanity that bots are still firing
ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c \"select decision, count(*) n \
    from trader_decisions where created_at > now() - interval '60 minutes' \
    group by decision order by n desc\""

# Stage failure attribution — what's still gating selected → orders
ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres \
  psql -U homerun -d homerun -c \"select check_label, count(*) n \
    from trader_decision_checks \
    where created_at > now() - interval '60 minutes' \
      and not passed \
    group by check_label order by n desc limit 15\""
```

### Rollback

Each tweak is independently revertable through the same UI
flow that applied it (UI is the operator-facing surface; the
storage is an `app_settings` row + a `traders.risk_limits_json`
column, both transactional).  Direct SQL recipes if the UI is
unreachable:

```sql
-- Orchestrator runtime knobs (atomic JSONB update; preserves the rest of the doc)
UPDATE trader_orchestrator_control
SET settings_json = jsonb_set(
  jsonb_set(
    jsonb_set(
      settings_json,
      '{global_runtime,live_market_context,strict_ws_pricing_only}',
      'true'::jsonb,
      true
    ),
    '{global_runtime,live_market_context,max_market_data_age_ms}',
    '10000'::jsonb,
    true
  ),
  '{global_runtime,runtime_trigger_cycle_timeout_seconds}',
  'null'::jsonb,
  true
)
WHERE id = 1;

-- Copy Trade bot trade-cap revert
UPDATE traders
SET risk_limits_json = jsonb_set(
  risk_limits_json,
  '{max_trade_notional_usd}',
  '5.0'::jsonb,
  true
)
WHERE id = '61dcbeb2b9bc42bd9e9635a09ae5e0c3';
```

The `runtime_trigger_cycle_timeout_seconds` UI surface is a
code change (one input field + one TS interface field + one
backend normalizer entry) that lives in git; it does not have a
"DB rollback" — revert by removing the input from
`TradingPanel.tsx` and the field from
`apiTraders.ts` / `trader_orchestrator_state.py`.

### Status

OPEN — `0 trader_orders` in the 30-minute window after the
tweaks landed.  `trader_decisions` continues to produce
`selected` (~110/h on the 60-minute lookback) but the path
between `selected` and the `session_engine` post-execution
write is still gated.  Next step: query
`trader_decision_checks` after a fresh 30-minute soak to
identify the remaining blocking check (likely
`Source notional floor`, `Live liquidity floor`, or a
`Minimum exit notional feasibility` cousin) and either tune the
strategy parameter (vkladka **Tune** per bot) or relax the risk
limit further.

### Operator policy reminder (sticky)

All bots stay on `latency_class=normal` (classic) by default.
The fast-tier runtime is reserved for the single bot that
explicitly needs it (`Sandbox - Tail-End`).  ML training data
flows through the classic path — switching everything to fast
would split the dataset and break dry-run-to-live
representativeness.  Do not propose `latency_class=fast` as a
generic "make orders appear" remedy.

## 2026-05-08 ~19:00 UTC — three new Risk Limits exposed in UI: chase-up policy, market-data age, entry-drift

### Why this is needed

The 24-hour audit (after the previous-section tweaks) showed
that `trader_orders` remained at 0 for the entire day across
**all four active bots** despite ~990 `selected` decisions.
Cause: a single systemic blocker —
`Execution submission: limit_price_not_executable` — fired
4 242 times (Tail-End 96 % of decisions, NegRisk 86 %, Certainty
Shock 78 %, Copy Trade 16 %).  The shadow simulator
(`backend/services/optimization/execution_estimator.py`) refuses
to fill BUY legs whenever any book level is priced above the
strategy's `signal.entry_price`; for chase-driven strategies
(copy-trade, news-edge, tail-end) the live ask routinely
exceeds the leader's fill price by a few bps and the simulator
rejects every wave.

The toggle that disables this guard already lives in the code
(`StrategySDK.allow_taker_limit_buy_above_signal_price`,
default `false`), but it was reachable only through hand-edited
`strategy_params` JSON — not through any UI surface.  Two
sibling parameters had the same problem:

* `max_market_data_age_ms` — only readable from
  `strategy_params` or the env-default
  `EXECUTION_MARKET_DATA_MAX_AGE_MS=10000`.  Per-bot tuning
  required a strategy-schema patch.
* `max_entry_drift_pct` — orchestrator-level (read from
  `effective_risk_limits` in `trader_orchestrator_worker.py`),
  but the field was missing from
  `TRADER_RISK_FIELDS_SCHEMA`, so it never rendered in
  `RiskLimitsView`.

### What changed (in git, not DB)

Backend (rebuild required):

| File | Change |
|---|---|
| `backend/services/strategy_sdk.py` | Added `max_entry_drift_pct: 10.0`, `max_market_data_age_ms: None`, `allow_taker_limit_buy_above_signal: False` to `TRADER_RISK_DEFAULTS`; appended three field descriptors to `TRADER_RISK_FIELDS_SCHEMA`; extended `validate_trader_risk_config` with bounded coercion (drift `0–100 %`, age `50–300 000 ms`, chase-up bool). |
| `backend/services/trader_orchestrator/decision_gates.py` | `_resolve_market_data_age_budget_ms(strategy_params, timeframe, risk_limits=None)` now falls back to `risk_limits.max_market_data_age_ms` before the env-default; both call-sites in `apply_platform_decision_gates` pass `effective_risk_limits`. |
| `backend/services/trader_orchestrator/order_manager.py` | `_allow_taker_limit_buy_above_signal(...)` and `_aggressive_limit_buy_submit_as_gtc(...)` now accept a `risk_limits` arg and use it as the SDK-default when `strategy_params` is silent.  `submit_execution_leg(...)` and `submit_execution_wave(...)` accept and forward `risk_limits`. |
| `backend/services/trader_orchestrator/session_engine.py` | `_submit_execution_wave_with_cancellation_protection` closure now passes `risk_limits=risk_limits` (already in scope from `execute_signal`). |
| `backend/services/trader_orchestrator/fast_submit.py` | `execute_fast_signal(...)` accepts `risk_limits` and forwards to `submit_execution_leg`. |
| `backend/workers/fast_trader_runtime.py` | `_submit_and_persist` reads `self._trader.get("risk_limits")` and passes through. |

Tests added:

* `backend/tests/test_strategy_sdk_trader_risk.py` — four new
  cases covering normalization, clamping, empty→None
  semantics, and schema exposure.
* `backend/tests/test_trader_orchestrator_decision_gates.py` —
  three cases for the `_resolve_market_data_age_budget_ms`
  precedence chain (strategy → risk → env), plus an
  end-to-end `apply_platform_decision_gates` case where a
  15 000 ms-old quote passes only because
  `risk_limits.max_market_data_age_ms = 20 000`.
* `backend/tests/test_trader_order_manager_live.py` — four
  cases for the `_allow_taker_limit_buy_above_signal` /
  `_aggressive_limit_buy_submit_as_gtc` precedence chain.

Frontend: **no changes**.  `RiskLimitsView` renders directly
from the backend-published schema via `StrategyConfigForm`, so
adding fields to `TRADER_RISK_FIELDS_SCHEMA` is enough.

### Precedence (read this before tuning)

For each of the three new fields the resolution order is:

1. `strategy_params.<key>` (per-bot, set in **Tune** vkladka or
   via API) — highest priority.  Lets a strategy override
   risk-level defaults when its execution model needs it.
2. `risk_limits.<key>` (per-bot, set in **Risk Limits** vkladka
   — the new fields).  This is the layer the operator should
   reach for when adjusting whole-bot behaviour.
3. Env-default (only for `max_market_data_age_ms` →
   `EXECUTION_MARKET_DATA_MAX_AGE_MS`; the other two default to
   `False` / `10.0` respectively).

For `max_entry_drift_pct` the orchestrator reads
`effective_risk_limits.max_entry_drift_pct` directly (no
strategy_params layer) — the new SDK field simply makes that
existing read renderable in the UI.

### Recommended values for shadow / dry-run

These are the values that should reasonably let `selected`
flow through to `trader_orders` without breaking the
microstructure realism that ML training depends on:

| Bot | `allow_taker_limit_buy_above_signal` | `max_market_data_age_ms` | `max_entry_drift_pct` |
|---|---|---:|---:|
| Sandbox - Traders Copy Trade | `true` | `20 000` | `15 %` |
| Sandbox - Tail-End | `true` | `15 000` | `12 %` |
| Sandbox - NegRisk | `true` | `10 000` | `10 %` |
| Sandbox - Certainty Shock | `true` | `10 000` | `10 %` |
| Sandbox - Market Making | leave `false` | leave empty | leave `10 %` |
| Sandbox - Traders Confluence | leave `false` | leave empty | leave `10 %` |
| Sandbox - Basic Arbitrage | leave `false` | leave empty | leave `10 %` |

Rationale: the four chase/event-driven bots benefit from
chase-up because the 24-h pattern is "leader fills at $X,
market drifts to $X+ε within seconds, our limit at $X is
rejected".  Market-Making is a maker strategy where chasing up
defeats the spread-capture thesis.  Basic Arb / Confluence
have other blockers (no signals or token-conflict bug) — the
new fields don't help them.

### Live-mode caveat

`allow_taker_limit_buy_above_signal=true` materially relaxes
the simulator's price discipline: BUY legs may fill at prices
above the leader's entry by an unbounded amount (capped only
by `max_execution_price` if the strategy supplies one).  In
**shadow** this is the right tradeoff — every realistic
chase-fill should be captured for ML training.  Before
flipping a bot to **live** mode revisit each value: keep
`max_market_data_age_ms` tight (≤ 5 000 for normal-latency
copy-trade), reduce `max_entry_drift_pct` (≤ 5 %), and weigh
whether chase-up should remain on for that strategy or be
gated by an explicit `max_execution_price` per-leg cap.

### How to roll back

If a tweak hurts a bot, clear the field in **Risk Limits**:

* `allow_taker_limit_buy_above_signal` → uncheck (defaults
  back to `False`, which restores the original
  `limit_price_not_executable` behaviour)
* `max_market_data_age_ms` → leave empty (falls back to
  strategy default → env default)
* `max_entry_drift_pct` → leave at `10` (the historical
  default the orchestrator already used)

There is no DB-only rollback for the schema/code change —
revert is by removing the three field descriptors from
`TRADER_RISK_FIELDS_SCHEMA` and the matching coercion lines in
`validate_trader_risk_config`.

### Status

OPEN — schema is live on the server (verified via
`GET /api/trader-sources/schema → shared_risk_fields`), all
three keys round-trip through `PUT /api/traders/{id}`.  No bot
has the new fields populated yet — next step is for the
operator to set the recommended values for the four
chase/event bots in **Risk Limits** vkladka and observe
whether `Execution submission: limit_price_not_executable`
drops as the dominant blocker over a 30–60 minute soak.

## 2026-05-08 ~19:30 UTC — chase-up shadow simulator fix (code patch, not DB)

- **Surface**: `code (backend image)`
- **Applied via**: `code change → BUILD_IMAGES=1 ./deploy/sync_remote.sh`
- **Why**: The previous entry exposed
  `allow_taker_limit_buy_above_signal` in **Risk Limits**, but a
  6-minute soak on `Sandbox - Traders Copy Trade` with the flag
  set to `True` showed `Execution submission:
  limit_price_not_executable` was *still* the dominant blocker —
  82 instances vs 1 selected/cycle.  Investigation traced the
  flag to `_resolve_execution_price_bounds`, which only adjusts
  `max_execution_price` (a **live**-mode broker cap), while the
  shadow path in `submit_execution_leg` always passes
  `limit_price = price` (live mid / signal entry) to
  `ensemble_estimate(...)`.  In a venue with any spread
  (`ask = mid + ε`) the simulator's first iteration evaluates
  `level.price (= ask) > limit_price (= mid)` → `break` →
  `_empty_estimate(reason="limit_price_not_executable")`.  Net
  effect: the toggle was **a no-op for shadow** — exactly the
  mode where ML/dry-run lives.
- **Code change**: `submit_execution_leg` (shadow branch) now
  derives an **effective shadow ceiling**:
  - Default = live mid / signal entry (existing behaviour).
  - When `allow_taker_limit_buy_above_signal=True` and
    `order_side="BUY"`: lift the ceiling to the **strongest
    explicit cap** from `strategy_params` / `leg` /
    `leg.metadata` (`max_execution_price`, `max_entry_price`,
    `max_probability`, `_derive_min_upside_price_cap` of
    `min_upside_percent`).  If no explicit cap exists, ceiling
    = `1.0` (the natural binary-market boundary).
  - Note: `max_execution_price` from
    `_resolve_execution_price_bounds` cannot be used here
    because that function injects the signal-price as a
    *fallback* even when chase-up is on — the whole point of
    chase-up is to ignore that fallback.  The shadow branch
    therefore re-collects only the explicit caps.
  - The lift is applied **only** to the simulator's
    `limit_price` argument; `survival_features` still records
    the original `price` so Cox training labels the strategy's
    real intent, not the lifted ceiling.
- **Files**:
  - `backend/services/trader_orchestrator/order_manager.py` —
    moved `_resolve_execution_price_bounds` call to top of
    `submit_execution_leg` (was duplicated in live branch only),
    added shadow-side `shadow_limit_price` lift block before the
    `ensemble_estimate(...)` call.
  - `backend/tests/test_trader_order_manager_live.py` — new test
    `test_shadow_buy_with_chase_up_lifts_simulator_limit_so_asks_above_mid_fill`
    that runs the same fixture twice (chase=False vs chase=True)
    and asserts the no-chase path returns
    `limit_price_not_executable` while the chase path produces
    a real `executed` result.  This pins the regression so the
    next refactor cannot silently re-introduce the no-op.
- **Verified**:
  - `pytest tests/test_trader_order_manager_live.py` — 17/17
    pass on the running `worker-trading` container.
  - 6-minute live soak after redeploy: `Execution submission`
    fail count for `Sandbox - Traders Copy Trade` dropped from
    82 → 1 in the same window; `selected` decisions rose from
    ~1/hour to 9/6min (~1.5/min); `Tail-End` produced its first
    real `trader_order` since 2026-05-07 (cancelled by its own
    `max_probability=0.905` cap when the market drifted above
    that — strategy-level rejection, **not** the
    `limit_price_not_executable` simulator artifact).
- **Live-mode caveat**: This is a **shadow-only** path; the live
  branch was already correct (live broker enforces
  `max_execution_price` from
  `_resolve_execution_price_bounds`).  The patch does **not**
  weaken live-mode price discipline.
- **Rollback**: `git revert` the order_manager.py changes (the
  test file's new test will then fail and surface the
  regression).  No DB-only rollback exists for code paths.

### Status

OPEN — chase-up now functional for shadow.  Remaining blockers
on `Sandbox - Traders Copy Trade` per 30-min soak (in order of
volume): `Source notional floor`, `Adverse entry drift limit`
(despite `max_entry_drift_pct=15`, this gate is the
**strategy-level** symmetric drift check, not the risk-limit
one), `Stacking guard` (already-occupied markets — copy trader
seeing the same wallet's repeated trades), `Minimum exit
notional feasibility` (driven by current
`max_trade_notional_usd=5` ⇒ exit notional often <$2; raising
to 25 unblocks this for Copy Trade but increases shadow capital
at risk per trade).  Next investigation: why
`SessionExecutionResult.orders_written=1` appears in
`worker-trading` logs but `trader_orders`/`simulation_trades`
remain empty for the Copy Trade cycles ending 17:27–17:28 —
suggests a shadow commit or persistence path that the
chase-up patch did not exercise.  Tracking separately.

## 2026-05-08 ~20:15 UTC — shadow execute_signal commit fix (code patch, not DB)

- **Surface**: code (`backend/services/trader_orchestrator/session_engine.py`)
- **Applied via**: edit + `./deploy/sync_remote.sh`
- **Why**: After the chase-up fix, `Execution submission` blocker
  collapsed but `trader_orders` / `execution_sessions` /
  `trader_positions` stayed at **0 rows for 25+ minutes**, while
  `Sandbox - Traders Copy Trade` accumulated `Risk blocked:
  trader_open_positions (next=14 max=12)` ~80% of decisions.
  Worker-restart cleared the block once (70 → 3 phantom keys),
  but it grew back to 13 within ~20s of the next selected burst.
  Deep-dive into the call graph found the root cause:
  - `submit_order` (`workers/trader_orchestrator_worker.py:1596`)
    in shadow mode opens a per-call session
    (`async with AsyncSessionLocal() as submit_session`) and never
    commits it.
  - `execute_signal` flushes the new
    `ExecutionSession` / `ExecutionSessionLeg` /
    `TraderOrder` / `ExecutionSessionOrder` rows via
    `_persist_execution_projection` but only **flushes**, never
    commits (live's `_commit_pre_submit_projection` is the only
    explicit commit and it runs only when
    `entry_submit_placeholders` is non-empty).
  - On `async with` exit, `AsyncSession.close()` rolls back every
    pending insert.  The caller still receives
    `SessionExecutionResult(status="executed",
    orders_written=1, created_orders=[…])` and faithfully calls
    `hot_state.upsert_active_order(...)` for each row — pinning a
    phantom open-position key in memory against a now-empty DB.
    After ~14 such bursts, `trader_open_positions` cap fires and
    blocks every subsequent decision until the worker is
    restarted.  Silent failure: no `transient DB error`, no
    `_commit_with_retry` failure surfaced, because the worker
    session is a **different** session from `submit_session` and
    its commit succeeds (it has nothing to commit).
- **Patch**: insert an explicit `await self.db.commit()` inside
  `_persist_execution_projection` after all flushes /
  `set_trade_signal_status` / `sync_trader_position_inventory`,
  before event-bus publishes.  On `DBAPIError` the commit
  rolls back, logs `execution_session persist commit failed`
  with full diagnostic context (session_id, trader_id, mode,
  trader_order_count, error_class, error), and re-raises so the
  caller sees the failure (worker treats it as a transient DB
  error and retries the signal next cycle).  On success a single
  `execution_session persisted` INFO line is emitted when
  `trader_orders` is non-empty so production has a permanent
  fingerprint of working persistence.

### Code changes

| File | Change |
|---|---|
| `backend/services/trader_orchestrator/session_engine.py` | Added `from sqlalchemy.exc import DBAPIError`, `from utils.logger import get_logger`, module logger; in `_persist_execution_projection` after the inventory-sync loop and before event-bus publishes — `await self.db.commit()` wrapped in `try/except DBAPIError` with rollback + structured ERROR log + re-raise; INFO log on success when `trader_orders` is non-empty. |
| `backend/tests/test_execution_session_engine.py` | New regression test `test_execute_signal_shadow_persists_with_commit_so_async_session_close_does_not_rollback` that drives `mode="shadow"` end-to-end with mocked submit_execution_wave and asserts `db.commit_calls >= 1` plus persisted `TraderOrder` / `ExecutionSessionOrder` rows.  Without the patch this test fails (commit_calls == 0). |

### Verification

After deploy + orchestrator unpause, 2-minute soak:
- 10 selected decisions, 7 blocked, 4 skipped (vs. 0 selected
  pre-fix, all blocked on `trader_open_positions`).
- 10 `trader_orders` rows committed (`status=open`,
  `total_notional_usd=$191`).
- 10 `execution_sessions` rows committed (`status=completed`).
- 13 `trader_positions` rows committed (`status=open`,
  `total_notional_usd=$215`).
- `execution_session persisted` INFO line ratio ≈ 1 per selected
  decision.  Zero `execution_session persist commit failed`
  occurrences.
- `trader_open_positions` blocker dropped to 0.  Remaining gate
  blocks (`Adverse entry drift limit`, `Entry price ceiling`,
  `Entry drift from signal`, `Minimum exit notional feasibility`,
  `Execution submission`) are all expected normal-population
  blockers, not the systemic phantom-cap issue.

### Live-mode interaction

`_commit_pre_submit_projection` (live placeholders) still runs
unchanged.  The new commit at end of
`_persist_execution_projection` is a **second** commit on the
same session for live; this is safe because the second commit
only persists incremental changes flushed after the pre-submit
commit.  Empty-pending commit is a no-op.

### Rollback

```bash
# 1. revert the session_engine.py changes
git revert <commit-sha>
# 2. push to local checkout
./deploy/sync_remote.sh
```

The new test `test_execute_signal_shadow_persists_with_commit_so_async_session_close_does_not_rollback`
will fail on revert and surface the regression immediately.

### Status

OPEN — fix deployed and verified.  Remaining work:
- Watch `simulation_trades` table — currently 0 rows ever (this
  table appears unused for the new pipeline; may be legacy for
  the older simulation engine).  The new path writes to
  `trader_orders` + `trader_positions` + `execution_sessions`,
  which is consistent with the architecture in
  `docs/plans/architecture/trader-pipeline.md`.
- Soak for 24h to confirm phantom positions stay at 0 across
  market hours.

### 2026-05-10 ~07:47 UTC — Plan 0021: orchestrator auto-resume in shadow

- **Surface**: `backend/main.py::_reset_orchestrator_boot_state` (code,
  not DB — the change is shipped in the image; it changes how the
  function reads/writes `trader_orchestrator_control` on each
  backend startup).
- **Applied via**: `./deploy/sync_remote.sh` (commit `fd93339f`).
- **Why**: Every backend container restart was hard-pausing the
  trader orchestrator (`is_enabled=false, is_paused=true,
  selected_account_id=null`), forcing operator to click Resume +
  Start in the UI even after a routine redeploy. This is correct
  for live-mode (never auto-resume risk) but operationally noisy
  for shadow bots which hold no real money and benefit from
  surviving a redeploy unchanged.
- **Expected effect**: backend restart with prior state
  `mode='shadow' AND is_enabled=true AND is_paused=false` now
  preserves it (only `live_arm` and `live_preflight` are nulled,
  which must never survive a process restart). Live mode and any
  operator-stopped/paused state still hard-reset.
- **Verification (this redeploy)**:
  - Pre-deploy: `mode=shadow, is_enabled=t, is_paused=f,
    selected_account_id="08fb2d1e-3bb1-4cd5-bd22-db3efbe4085e"`.
  - Post-deploy (immediate, post-`docker compose down/up`):
    same row — mode/enabled/paused/account preserved,
    `live_arm=null, live_preflight=null`. Snapshot updated to
    `current_activity="Cycle[scheduled:general] monitoring open
    orders=12"` within seconds (the first cycle ran without
    operator intervention).
- **Regression tests**: 6 unit tests in
  [`backend/tests/test_main_lifespan_smoke.py`](../../backend/tests/test_main_lifespan_smoke.py)
  pin both branches plus the live-flag-clearing invariant. Run
  via `bash scripts/run_tests_remote.sh tests/test_main_lifespan_smoke.py`
  (note: also bind-mount `backend/main.py` if testing local edits
  before redeploy).
- **Rollback**:
  ```bash
  git revert fd93339f
  ./deploy/sync_remote.sh
  ```
  Old hard-reset behaviour returns; orchestrator goes back to
  pause-on-every-restart.
- **Status**: SHIPPED — verified on 2026-05-10 redeploy. No
  operator action required to bring sandbox bots back after future
  redeploys.

### 2026-05-10 ~11:18 UTC — Plan 0022: quiet `missing_polymarket_credentials` reseeder spam

- **Surface**: `backend/workers/trader_reconciliation_worker.py::_reseed_wallet_state_cache_from_rest`
  (code, not DB — ships in the image).
- **Applied via**: `./deploy/sync_remote.sh` (commit `37c9964f`).
- **Why**: Plan 0018 closing analysis surfaced that the reseeder
  loop was emitting ~720 `WARNING` lines per hour on this Sandbox-
  only deployment because all four `app_settings.polymarket_*`
  credential fields are NULL. The cache is a live-mode-only
  dependency, so the warnings carried zero operational signal —
  pure log noise that hid real degradation.
- **Expected effect**: per-cycle skip warnings demoted to `DEBUG`
  when `live_execution_service.get_last_init_error()` is the literal
  string `"missing_polymarket_credentials"`. One `WARNING`
  announcement at the moment the loop enters this state, one when
  it exits. Other init-error strings (transient HTTP, gamma
  timeouts) keep the existing per-cycle `WARNING` because those
  are real degradation, not configuration absence.
- **Verification (this redeploy)**:
  - Pre-deploy: 120 `WalletStateCache reseeder skipped` lines per
    10-min window in `worker-trading`.
  - Post-deploy (5-min window after restart settle): **0**
    per-cycle skip warns; **1** "demoting per-cycle warnings to
    DEBUG" announcement (transition detector fired once on entry
    into the quiet state, exactly as designed).
- **Regression tests**: 5 unit tests in
  [`backend/tests/test_wallet_cache_reseeder_quiet_mode.py`](../../backend/tests/test_wallet_cache_reseeder_quiet_mode.py)
  pinning entry announcement, steady-state silence, exit
  announcement, non-sentinel WARN preservation, and re-entry
  announcement repetition. Run via
  `bash scripts/run_tests_remote.sh tests/test_wallet_cache_reseeder_quiet_mode.py`.
- **Rollback**:
  ```bash
  git revert 37c9964f
  ./deploy/sync_remote.sh
  ```
  Per-cycle WARNs return.
- **Status**: SHIPPED — verified 2026-05-10. The fix becomes a
  no-op the moment Polymarket credentials are added to
  `app_settings`: the next reseeder cycle observes `init_error=None`,
  emits the resume announcement, and standard logging returns.

### 2026-05-10 ~12:05 UTC — Plan 0023: broaden binary-market outcome normalisation

- **Surface**: `backend/services/traders_copy_trade_signal_service.py::_resolve_market_snapshot`
  (code, not DB — ships in the image).
- **Applied via**: `./deploy/sync_remote.sh` (commit `7770d8d1`).
- **Why**: Plan 0018's binary-market outcome normaliser only fired
  when the gamma `outcomes` list contained a literal "Yes" or "No"
  label. Crypto BTC up/down markets (`["Up","Down"]`) and other
  binary-but-non-Yes/No vocabularies skipped normalisation, the
  outcome leaked through as the original label (e.g. `"down"`),
  the strategy emitted `direction=""`, the `_resolve_leg_direction`
  fallback returned bare `"buy"`, and the simulator's defensive
  widening could not find a `tokens[]` list in the live_market
  payload (only `selected_token_id` singular). Result: every
  backfill cycle (~5 s) raised `Unsupported direction 'buy'` for
  the affected orders, sustaining the
  `shadow_ledger_backfill_failed` warn stream that Plan 0018 was
  expected to silence (~50/h on the Sandbox bot).
- **Expected effect**: any 2-token Polymarket market is binary by
  construction (the two tokens are the YES and NO sides of a
  single condition regardless of label vocabulary). Drop the
  `{"yes","no"} & lowered` guard so every 2-token market
  canonicalises to `Yes`/`No` by token-position. Multi-outcome
  single-market structures (>2 tokens) still skip normalisation.
- **Verification (this redeploy)**:
  - Pre-deploy: **2** `shadow_ledger_backfill_failed` events in
    the 10-min window for Sandbox bot
    (`trader=61dcbeb2b9bc42bd9e9635a09ae5e0c3`).
  - Post-deploy (10-min window after restart settle): **0**.
    The two known-stuck pre-fix orders
    (`247669fc155a4d7abc5f8ee9cd68bc04`,
    `08bce1226e574413a4bbb70e05d1f8c7`) are `status='resolved_loss'`
    and fell out of the backfill candidate query
    (`_ACTIVE_ORDER_STATUSES = {submitted, executed, completed,
    open}`) after the orchestrator restart — no manual SQL drain
    was needed. New copy-trade signals on Up/Down markets now
    emit `direction='buy_yes'`/`'buy_no'` directly via the
    strategy's existing canonical branch.
- **Regression tests**: 2 new + 3 pre-existing tests in
  [`backend/tests/test_traders_copy_trade_signal_service.py`](../../backend/tests/test_traders_copy_trade_signal_service.py)
  pin Up/Down + Arsenal/Field normalisation plus the multi-outcome
  passthrough. All 14 tests pass via
  `bash scripts/run_tests_remote.sh tests/test_traders_copy_trade_signal_service.py`.
- **Rollback**:
  ```bash
  git revert 7770d8d1
  ./deploy/sync_remote.sh
  ```
  The `{"yes","no"} & lowered` guard returns and the up/down
  stuck-order pattern returns with it.
- **Status**: SHIPPED — verified 2026-05-10. Plan 0018's
  shadow_ledger_backfill_failed stream is now fully silenced.

### 2026-05-10 ~10:08 UTC — conservative live-risk limits on Sandbox Traders Copy Trade

- **Surface**: `traders.risk_limits_json` for trader
  `61dcbeb2b9bc42bd9e9635a09ae5e0c3` (Sandbox - Traders Copy Trade,
  mode=shadow).
- **Applied via**: `PUT /api/traders/{id}` through loopback
  (basic-auth bypassed). Audit recorded as
  `trader_config_revisions.operator='operator-claude'`,
  `reason='Phase 0 conservative live-risk limits — Sandbox
  copy-trade prep'`.
- **Why**: Operator wants to flip the Sandbox bot to `mode=live`
  in the near future. The 217-terminal-order audit on the same
  date showed the bot is barely profitable (+$12 net P&L, 44%
  win rate) with losses concentrated in ~5 leader wallets and
  the sports/esports prop-bet market topic. Conservative caps
  applied **now in shadow** to observe how often each cap fires
  before the live flip. Pruning the leader pool itself is parked
  in plan 0024 (backlog) — needs ≥ 200 more terminal orders to
  be statistically firm.
- **Changes** (5 fields, all in `risk_limits`):

  | field | before | after |
  |---|---:|---:|
  | `max_position_notional_usd` | 100 | **5** |
  | `max_trade_notional_usd` | 100 | **5** |
  | `max_gross_exposure_usd` | 2000 | **100** |
  | `max_daily_loss_usd` | 1000 | **100** |
  | `max_daily_spend_usd` | 5000 | **200** |

  All other risk fields preserved (max_open_orders=500,
  max_open_positions=500, allow_averaging=false,
  halt_on_consecutive_losses=true, max_consecutive_losses=4,
  circuit_breaker_drawdown_pct=12, max_entry_drift_pct=15,
  portfolio.enabled=false). Strategy params untouched.

- **Interaction analysis** (so no surprises):
  - **Effective per-position cap = $5** (orchestrator
    `max_position_notional_usd=5` dominates strategy
    `max_position_size=1000`).
  - **Max simultaneous open positions ≈ 20** (`gross / per-position
    = 100 / 5 = 20`). The trader still has
    `max_open_orders=500` / `max_open_positions=500` but the
    gross-exposure cap is now the single binding constraint.
  - **Sandbox currently has 49 open positions** (from the same
    audit). Risk manager does NOT close them; it blocks NEW
    orders until existing drain to ≤ 20. Expect a multi-hour
    "frozen on new entries" period as positions resolve naturally.
    This is intended.
  - **Daily-loss cap = $100** is tighter than the global
    `app_settings.global_risk.max_daily_loss_usd=500` — the
    tighter wins, so the trader-level value is what fires.
  - **`circuit_breaker_drawdown_pct=12`** is now mostly theoretical
    on this scale: 12% of $100 = $12, but daily loss cap of $100
    will fire long before that on a sustained losing streak.
  - **`portfolio.min_order_notional_usd=10`** is dormant
    (`portfolio.enabled=false`). DO NOT enable portfolio mode
    without lowering this to ≤ 5, otherwise every order will be
    rejected as below the portfolio min-order floor.
  - **`proportional_multiplier=1.0`** in strategy params still
    tries to copy 1:1 with leader, but final size clamps to $5
    for any leader trade > $5 (i.e. almost all). Sizing-edge
    information is lost but the cap correctly enforces. May
    revisit and switch to a smaller multiplier if operator wants
    fixed-size bets visible as such.
  - **No global `live_risk_clamps` for this trader** — `max_open_orders_cap`
    etc. are absent from `app_settings.global_runtime.live_risk_clamps`,
    so trader-level values pass through unchanged.

- **Verification**:
  ```
  max_pos | max_trade | max_gross | max_daily_loss | max_daily_spend
       5  |    5      |   100     |     100        |     200
  ```
  Updated_at: `2026-05-10 10:08:34`. Audit row in
  `trader_config_revisions` (id=`edb50a24...`).

- **Rollback** (if monitoring shows the caps are too tight or
  some unexpected interaction surfaces):
  ```bash
  ssh polyhome-1 'curl -fsS -X PUT http://127.0.0.1:8888/api/traders/61dcbeb2b9bc42bd9e9635a09ae5e0c3 \
    -H "Content-Type: application/json" \
    -d "{\"requested_by\":\"operator\",\"reason\":\"Rollback Phase 0 caps\",\"risk_limits\":{...PRE-CHANGE FULL DICT...}}"'
  ```
  Pre-change full dict is recorded in `trader_config_revisions.trader_before_json`
  for revision id `edb50a24fd8f4a90b7335b6242a15beb` —
  copy-paste from there to restore exactly.

- **Status**: APPLIED — bot continues cycling in shadow. Operator
  to monitor over the next 24-48 h: how often each cap fires,
  whether the cap-induced "frozen on entries" period collapses
  cleanly as positions drain, whether realised P&L stabilises
  with the smaller per-position notional.

### 2026-05-10 ~10:12-11:24 UTC — Phase 0 cap change cascade-failed; rolled back + halt_on_consecutive_losses disabled

This entry documents a multi-step recovery from the
2026-05-10 ~10:08 Phase 0 cap change above. The change itself was
reverted; this is the post-mortem + the residual workaround.

**Symptom**: ~4 minutes after Phase 0 caps applied at 10:08, bot
auto-paused via `circuit_breaker_pause` ("4 consecutive losses,
limit=4"). Subsequent `circuit_breaker_safe_exit` force-closed
83 shadow positions at current market prices — many of those
realised losses they wouldn't have hit otherwise. After two
operator-driven resumes (10:51 and 11:16), the breaker re-tripped
within 18-19 s each time.

**Initial diagnosis (PARTIALLY WRONG, kept here for the record)**:
I claimed the consecutive-losses counter was "stale" because the
breaker re-tripped despite `loss_streak_reset_at` advancing on
operator_resume and despite zero `trader_orders` being closed in
the last 30 minutes. I disabled `halt_on_consecutive_losses=false`
as an escalation workaround, expecting the bot would still keep
failing.

**Corrected diagnosis (after operator asked about RPC)**: not a
stale counter. The actual cause is the
`uq_trader_position_identity` constraint
(`(trader_id, mode, market_id, direction)`) firing on attempts to
re-open positions on markets where a previous position closed
during `circuit_breaker_safe_exit`. The constraint covers all
statuses, so even closed positions block new opens on the same
(market_id, direction). The signal-processing layer counts the
resulting `IntegrityError` as a "failed signal", which feeds the
consecutive-losses counter as a loss. So 5 IntegrityError-failures
in a row tripped the breaker, not 5 actual losing trades. This is
the same defect-class I incorrectly closed earlier this session
("constraint not reproducing" — verified by checking `COUNT(*) > 1`
on the tuple, which always returned zero **because the constraint
blocks the second INSERT before the row gets in**, so duplicates
never accumulate to be counted).

**Actions taken** (in order):

1. **10:08 UTC** — Phase 0 caps applied (max_position 5,
   max_gross 100, max_daily_loss 100, max_trade 5, max_daily_spend 200).
   See preceding entry above for full dict.
2. **10:12 UTC** — `circuit_breaker_pause` (4 consecutive losses).
   `circuit_breaker_safe_exit` force-closed 83 positions.
3. **10:51 UTC** — operator resumed via UI. Re-tripped after 18 s.
4. **11:14-11:17 UTC** — first rollback attempt: PUT pre-change
   risk_limits (full dict from
   `trader_config_revisions.trader_before_json` of revision
   `edb50a24...`) + `is_paused=false`. Bot resumed, re-tripped
   `circuit_breaker_pause` (5 consecutive losses) at 11:17:05 —
   only 19 s after resume. Audit revision id (rollback): see
   `trader_config_revisions ORDER BY created_at DESC LIMIT 1
   WHERE reason ILIKE '%ROLLBACK%'`.
5. **11:21 UTC** — second escalation: PUT
   `halt_on_consecutive_losses=false` + resume. All other
   risk_limits remain at the rollback (pre-Phase-0) state.
   Bot stayed alive.
6. **11:30+ UTC verification** — 0 new circuit_breaker events
   since the escalation. Decision flow recovered: 24 selected,
   61 skipped, 1 failed, 136 blocked over a 5-min window.
   IntegrityError occurrences: 2 per 5 min (rare, not a
   systematic flood), confirming the constraint does fire but
   only sporadically — it is the breaker amplifying that, not
   constant failure.

**Current state of `risk_limits`** (post-escalation, what's
actually live):
- All Phase 0 numeric caps **REVERTED** to pre-change values
  (max_position 100, max_gross 2000, max_daily_loss 1000,
  max_trade 100, max_daily_spend 5000).
- `halt_on_consecutive_losses = false` ← WORKAROUND, not a
  permanent state.
- `circuit_breaker_drawdown_pct = 12.0` and
  `max_daily_loss_usd = 1000.0` remain as the active backstops.
- `max_consecutive_losses = 4` value persists but is dormant
  while halt is off.

**Rollback** (restore the workaround back to default protection):
```bash
ssh polyhome-1 'curl -fsS -X PUT http://127.0.0.1:8888/api/traders/61dcbeb2b9bc42bd9e9635a09ae5e0c3 \
  -H "Content-Type: application/json" \
  -d "{\"requested_by\":\"operator\",\"reason\":\"Restore halt_on_consecutive_losses=true\",\"risk_limits\":{...current dict with halt=true...}}"'
```

**Real follow-up needed (out of scope for this entry)**:
- The `uq_trader_position_identity` constraint should NOT block
  re-opens on the same `(market_id, direction)` once the
  previous position is terminal. Either change to a partial
  unique index `WHERE status='open'`, or change
  `_persist_execution_projection` to UPDATE the existing row
  on conflict instead of INSERT-ing a new one. This is the
  defect that cascaded into the breaker storm.
- The signal-processing layer should not count
  `IntegrityError`-failures as losses for the consecutive-loss
  counter — they are infrastructure failures, not P&L
  outcomes. Failed signals should bump a separate
  "infrastructure errors" counter with its own threshold.
- Both follow-ups warrant an actual plan once the operator is
  ready to invest the cycles. The constraint fix needs an
  Alembic migration; the counter-classification fix is a
  smaller code change in the orchestrator's loss-streak
  bookkeeping.

**Lessons learned (logged to feedback memory under `feedback_audit_existing_ui_before_planning.md` extension and a new `feedback_risk_knob_interactions.md` to be written)**:
- A risk-knob change in isolation must include a written
  dimensional analysis of every gate that consumes the changed
  field, plus every sibling safety net (consecutive-loss
  counter, drawdown breaker, halt-on-* flags, force-flatten
  triggers). I shipped Phase 0 without that and it cost the
  bot a forced 83-position flatten plus three failed restarts
  before stabilising.
- When a "diagnosis" is offered, validate it against logs
  before acting. I called the counter "stale" without
  cross-checking the worker-trading log — which clearly showed
  the IntegrityError chain.

---

### 2026-05-10 — Pruning losing leaders is already shipped in the UI (operator-corrected; plans 0024/0025 deleted as redundant)

- **Pool tab**
  ([`frontend/src/components/DiscoveryPanel.tsx:1552-1605`](../../frontend/src/components/DiscoveryPanel.tsx))
  has per-wallet **Blacklist**, **Manual exclude**, and
  **Unblacklist** buttons. Clicking sets
  `discovered_wallets.source_flags.pool_blacklisted` (or
  `pool_manual_exclude`) and triggers
  `smart_wallet_pool.recompute_pool()` within ~60 s. Pool view
  is sortable by `total_pnl` and filterable by min P&L /
  win-rate, so the operator can find losers visually.
  Backend route:
  `POST /api/discovery/pool/members/{address}/blacklist` /
  `DELETE` for unset
  ([`backend/api/routes_discovery.py:2471-2500`](../../backend/api/routes_discovery.py)).
- **Tracked → Groups tab** has a "Manual Group" creator
  ([`frontend/src/components/RecentTradesPanel.tsx:880-960`](../../frontend/src/components/RecentTradesPanel.tsx)).
  A bot's `traders_scope.modes` can be set to `["group"]` with
  `group_ids=[<UUID>]` to scope copy-trade to ONLY a curated
  set of trusted leaders. Cleaner than "blacklist N losers"
  when the operator already knows who the good ones are.
- **Caveat**: the Pool tab shows wallet-**global** P&L (across
  all of Polymarket), not per-bot scoped. For our 217-order
  audit the global vs bot-specific signals correlated well
  enough that the existing UI is sufficient. If operator
  notices systematic mismatch later (e.g. a wallet good
  globally but consistently bad for our specific bot), revisit
  whether a per-bot analytics tile is justified — but only
  after that pattern actually shows up in production data.

### 2026-05-10 ~12:20 UTC — Plan 0024: UPSERT in `sync_trader_position_inventory` eliminated IntegrityError race

- **Surface**: `backend/services/trader_orchestrator_state.py::sync_trader_position_inventory`
  (code, not DB — ships in the image).
- **Applied via**: `./deploy/sync_remote.sh` (commits `bcafd9c2`
  + the closing-commit). Plan archived at
  [`docs/plans/completed/0024-upsert-trader-position-inventory.md`](../plans/completed/0024-upsert-trader-position-inventory.md).
- **Why**: Closes the cascade-failure root cause from the
  2026-05-10 ~10:12-11:24 UTC entry (Phase 0 caps tripping
  circuit_breaker). The IntegrityError on
  `uq_trader_position_identity` was firing whenever
  `circuit_breaker_safe_exit` left a closed row that the next
  copy-trade signal collided with. Failed-signal IntegrityErrors
  were counted by `halt_on_consecutive_losses` as actual losses
  → breaker tripped → safe_exit closed more positions →
  more collisions.
- **Fix**: replaced `session.add(TraderPosition(...))` with
  `pg_insert(TraderPosition.__table__).on_conflict_do_update(constraint="uq_trader_position_identity", set_={...})`
  in the `for identity, bucket in grouped.items():` loop. The
  conflict branch overwrites the colliding row with the same
  fields the previous UPDATE branch wrote (re-opens closed
  positions, refreshes sizing/timing, merges payload_json in
  Python before the UPSERT). Established UPSERT pattern in same
  file (TraderSignalConsumption line 6706, TraderSignalCursor
  line 6982).
- **Verification (this redeploy)**:
  - Pre-deploy: **4 IntegrityError / 15 min** in worker-trading log.
  - Post-deploy 5-min window: **0**.
  - Post-deploy 30-min window: **0** (zero `uq_trader_position_identity`
    occurrences across the entire observation period).
  - Decision flow remained healthy: 11 selected / 446 skipped /
    386 blocked over 30 min, normal copy-trade `min_notional` /
    `entry_drift` skip distribution.
- **`halt_on_consecutive_losses=true` restored** at 11:48 UTC
  via `PUT /api/traders/61dcbeb2...`. After 30 min of
  observation: **zero** `circuit_breaker_pause` events on the
  trader. The pre-fix workaround from the previous entry is
  now properly retired.
- **Regression tests**: 2 new in
  [`backend/tests/test_trader_live_provider_reconciliation.py`](../../backend/tests/test_trader_live_provider_reconciliation.py)
  pin (a) reopen-after-close case and (b) no-IntegrityError when
  the in-memory snapshot misses a row that exists in DB. All 33
  tests in the file pass via
  `bash scripts/run_tests_remote.sh tests/test_trader_live_provider_reconciliation.py`.
- **Rollback**:
  ```bash
  git revert bcafd9c2
  ./deploy/sync_remote.sh
  ```
  IntegrityError race returns; would also need to flip
  `halt_on_consecutive_losses=false` again as the workaround.
- **Status**: SHIPPED — verified 2026-05-10. Cascade-failure
  root cause from the 10:12 entry is now fully closed.

### 2026-05-10 ~13:24 UTC — Sandbox bot: raise `max_consecutive_losses` 4 → 12 (operator-applied)

**Surface**: `traders.risk_limits_json.max_consecutive_losses` for
trader `61dcbeb2b9bc42bd9e9635a09ae5e0c3` (Sandbox - Traders Copy
Trade, mode=shadow).

**Applied via**: UI (operator-driven). Audit recorded as
`trader_config_revisions` row created `2026-05-10 13:24:27.727054`,
no `operator` set (UI does not pass operator name yet).

**Why**: Post Plan 0024 the IntegrityError-as-loss source is gone,
so `halt_on_consecutive_losses=true` now reflects only **real**
P&L losses. With Sandbox's measured win rate of 44.2% (per the
217-order audit), the probability of 4 losses in a row is
`(1 - 0.442)^4 = 9.7 %`. At ~30 cycles/h with one trip
realising losses on every open position via
`circuit_breaker_safe_exit`, this triggered the breaker ~3 times
per hour and produced large force-flatten cascades each time
(observed: 68 positions flattened in the 13:00:08 trip). Operator
chose to raise the streak threshold rather than disable the
breaker entirely; this is a CRITICAL knob — full walkthrough
below.

#### Step 1 — Direct gate impact

| Field | Before | After | Direct gate(s) | Pre threshold | Post threshold |
|---|---:|---:|---|---|---|
| `max_consecutive_losses` | 4 | 12 | `trader_loss_streak` ([`risk_manager.py:119-130`](../../backend/services/trader_orchestrator/risk_manager.py)) | `trader_consecutive_losses < 4 → pass` | `trader_consecutive_losses < 12 → pass` |
| `max_consecutive_losses` | 4 | 12 | Auto-pause + `circuit_breaker_safe_exit` ([`trader_orchestrator_worker.py:5155-5240`](../../backend/workers/trader_orchestrator_worker.py)) | Trip when `streak ≥ 4` | Trip when `streak ≥ 12` |

Trip probability at 44.2 % win rate:
- Pre: `(1 - 0.442)^4 = 9.7 %` per 4-trade window
- Post: `(1 - 0.442)^12 = 0.27 %` per 12-trade window
- Reduction factor: **~36×**

#### Step 2 — Indirect-metric impact

`n/a — matrix confirms zero indirect consumers` (the streak
counter feeds only the `trader_loss_streak` gate plus the
auto-pause path; no derived metrics anywhere else).

#### Step 3 — Live data simulation

```sql
-- Recent trip frequency on Sandbox bot (last 24h, before this change):
SELECT count(*) AS trips_24h
FROM trader_events
WHERE trader_id = '61dcbeb2b9bc42bd9e9635a09ae5e0c3'
  AND event_type = 'circuit_breaker_pause'
  AND created_at > now() - interval '24 hours';
-- Result: ~5 trips/24h pre-change (mostly clustered post Phase 0
-- cap experiments). The math above predicts trip frequency drops
-- to <1/12-13h post-change at steady-state win rate.
```

#### Step 4 — Compound-effect checklist

- [ ] `max_position_notional_usd` — n/a; per-position size unchanged.
- [ ] `max_trade_notional_usd` — n/a; per-trade cap unchanged.
- [ ] `max_gross_exposure_usd` — n/a; gross cap unchanged.
- [x] **`max_open_orders` / `max_open_positions`** — **COMPOUND BIG**.
  When the breaker eventually does trip, `circuit_breaker_safe_exit`
  force-flattens **all** currently-open positions at market
  prices. Higher streak threshold = more cycles between trips =
  more positions accumulated before each trip. Observed at
  4-limit: ~68 positions per trip. At 12-limit: extrapolated
  to ~150-200 positions per trip. Per-trip realized loss could
  be 2-3× larger even if frequency drops 36×. **Bounded by**
  `max_open_positions=500` (current cap). Worth lowering this to
  ~100-150 in a separate walkthrough to bound safe_exit blast
  radius.
- [x] **`max_daily_loss_usd`** — **COMPOUND**. Larger per-trip
  flatten could blow daily cap in ONE event: 150 positions ×
  $5 unfavorable closing = $750. Daily cap is $1000 → trip
  consumes 75 % of daily budget instantly → bot is paused for
  the rest of the UTC day even if trip itself is rare. Net
  daily exposure: per-day expected losses likely LOWER than
  4-limit baseline (3 trips × ~$340 = $1020/h vs 0.077 trips ×
  ~$750 = $58/h), but **single worst-case is closer to the
  cap**. Acceptable for shadow mode; revisit if flipping live.
- [ ] `circuit_breaker_drawdown_pct` — DEAD CODE per matrix; ignore.
- [x] **`halt_on_consecutive_losses`** — **direct sibling**, both
  knobs read together at the gate. Currently `true` (restored
  after Plan 0024). Change is meaningful only because halt is
  enabled. If operator later sets halt=false, this number
  becomes informational only.
- [x] **`circuit_breaker_safe_exit`** — see compound notes
  above; this is the trigger event whose blast radius scales
  with open-position count.
- [ ] `block_new_orders` — n/a.
- [ ] `traders.is_paused` / `traders.is_enabled` — n/a (operator
  also resumed the bot in the same UI step; bot is now
  `is_paused=false`).
- [ ] `worker_control.is_paused` / `worker_control.is_enabled` — n/a.
- [ ] `allow_taker_limit_buy_above_signal` — n/a.

Strategy-param compound (outside matrix scope):
- **`max_position_size`** (Copy Trade strategy_param, default
  1000.0): no effect (orchestrator-level
  `max_position_notional_usd=100` already dominates).
- **`max_copy_drawdown_pct`** (Copy Trade strategy_param,
  default 100.0): no effect (no derived `trader_drawdown_pct`
  metric depends on streak).

#### Step 5 — Rollback (< 30 s)

```bash
ssh polyhome-1 'curl -fsS -X PUT http://127.0.0.1:8888/api/traders/61dcbeb2b9bc42bd9e9635a09ae5e0c3 \
  -H "Content-Type: application/json" \
  -d "{\"requested_by\":\"operator\",\"reason\":\"Rollback max_consecutive_losses 12 → 4\",\"risk_limits\":{...full dict with max_consecutive_losses: 4...}}"'
```

Pre-change full dict is in `trader_config_revisions` (latest row
where `created_at < 2026-05-10 13:24:27`,
`trader_before_json -> 'risk_limits'`).

#### Status

APPLIED + bot resumed. Operator decision; my recommendation
agreed with this direction (option A from the diagnostic
conversation).

**Operational watch-items**:
1. Monitor `circuit_breaker_pause` event frequency over next
   24h. Expected: ≤1 trip vs pre-change ~3-5/h.
2. When the next trip does fire, record how many positions
   `circuit_breaker_safe_exit` flattens and the resulting
   daily P&L delta. If ≥ $750 in one trip, consider lowering
   `max_open_positions` to bound blast radius (would be its
   own walkthrough).
3. Before flipping the bot to `live` mode, revisit this knob —
   12-loss streak at $5 live position size = $60 worst-case
   streak (acceptable), but the safe_exit-blast compound
   becomes more material with real money.

### 2026-05-10 ~16:30 UTC — Tail-End cancelled-order verdict (Plan 0033)

- **Surface**: `traders.strategy_params_json.max_probability` for trader
  `388da687054c4b4a858ea152fff04900` (`Sandbox - Tail-End`, mode=shadow).
- **Applied via**: nothing — this is a **measurement-only** journal
  entry, no knob was touched by this plan. The recommendation below is
  for the operator to apply later (or not).
- **Why**: 14-day audit (2026-04-26..2026-05-10): 33 `cancelled` vs 9
  `executed` `trader_orders`. **All 33** carried
  `payload_json#>>'{leg,reason}' = 'limit_price_not_executable'` from
  the Cox-PH ensemble. Entry-price boundary at 0.900 / 0.905 was
  perfectly sharp — the 9 executed orders all had
  `entry_price ≤ 0.8865`, the 33 cancellations all had
  `entry_price ∈ {0.85, 0.865, …, 0.9035, 0.905}` with 16 stacked at
  exactly the cap. Plan 0033 was opened to determine whether the
  simulator was wrong (recommendation: tune Cox-PH) or whether the
  config was the gate (recommendation: tune `max_probability`).

#### Cap reduction in code (the `min(...)` line that does the damage)

From [`backend/services/trader_orchestrator/order_manager.py:962-980`](../../backend/services/trader_orchestrator/order_manager.py:962):

```python
explicit_buy_caps = [
    _valid_execution_bound(leg.get("max_execution_price")),
    _valid_execution_bound(metadata_for_caps.get("max_execution_price")),
    _valid_execution_bound(params.get("max_execution_price")),
    _valid_execution_bound(params.get("max_entry_price")),
    _valid_execution_bound(params.get("max_probability")),
    _derive_min_upside_price_cap(params.get("min_upside_percent")),
]
tightest_explicit_cap = min(
    (cap for cap in explicit_buy_caps if cap is not None),
    default=None,
)
```

For Tail-End's config (`max_probability=0.905`, `min_upside_percent=6`,
others None) the cap collapses to `min(0.905, 100/(100+6)=0.9434) =
0.905`. Effective `shadow_limit_price` fed to the Cox-PH ensemble is
0.905 — even though `strategy_context.max_entry_price` (the chase-up
target) is `0.94775`.

#### Bucket counts (Task 3 output)

Bucket definitions and full per-row classification live in
[`docs/plans/work-artifacts/0033-bucket-classification.md`](../plans/work-artifacts/0033-bucket-classification.md).

| Bucket | Count | Share of evidenced rows | Share of all 33 |
|---|---:|---:|---:|
| **A — config-driven** (book ask in `(max_probability, ctx_max_entry]` window) | 25 | 92.6 % | 75.8 % |
| **B — simulator pessimism** (book ask ≤ shadow_limit, but Cox-PH said no) | 0 | 0.0 % | 0.0 % |
| **C — book really wasn't there** (book ask > ctx_max_entry; chase-up wouldn't help) | 2 | 7.4 % | 6.1 % |
| **Indeterminate** (no microstructure snapshot ±15s, no public CLOB taker BUY in window) | 6 | — | 18.2 % |

Per-band slice (entry-price clustering at the cap):

| Entry band | Total | A | B | C | Indet. |
|---|---:|---:|---:|---:|---:|
| 0.85 – 0.870 | 3 | 0 | 0 | 0 | 3 |
| 0.871 – 0.890 | 6 | 1 | 0 | 0 | 5 |
| 0.891 – 0.900 | 8 | 8 | 0 | 0 | 0 |
| 0.901 – 0.905 | 16 | 16 | 0 | 2 | 0 |

The 6 Indeterminate rows are all from the 2026-05-07 10:02 batch
(market-microstructure recorder hadn't been provisioned for those
tokens yet) plus one isolated row from 2026-05-09 06:50; CLOB
post-hoc trades didn't cover them either. They are not informative
about either simulator or config — they're observability gaps, not
verdict-relevant data.

The 2 Bucket-C rows are momentary spread blowouts (425 bps and 1538
bps wide), where even a 5 % chase-up wouldn't have crossed; nothing
to do.

The single CLOB-corroborated case (id `70515907...`, market 2125964
"BTC > $80k", 2026-05-07 10:03:39) saw a real public taker BUY at
exactly `0.91` inside the 6-second FAK window — matching the
microstructure snapshot's `best_ask=0.91` and confirming Bucket A:
`shadow_limit=0.895` < ask < `ctx_max_entry=0.94225`.

#### Verdict — Verdict 1: simulator is correct, config is the gate

> 25 of 27 evidenced cancellations (92.6 %, well above the 70 %
> threshold) had `book_best_ask` strictly inside the chase-up
> window `(max_probability, ctx_max_entry_price]`. The Cox-PH
> ensemble correctly returned `fill_probability=0` for the
> `shadow_limit_price=0.905` it was handed. The `min(...)` cap
> reduction at line 970 collapsed the chase-up target down onto
> the entry-band cap, which is the actual block.

#### Recommendation (operator action — no follow-up plan needed)

Two equivalent fixes; both eliminate the same pathology:

1. **Operator config (one-line tweak via Bots UI):** raise
   `max_probability` for `Sandbox - Tail-End` from `0.905` to
   something that does not collide with the `+5 %` chase-up
   target — e.g. `0.97`. This restores the original intent
   (`max_probability` was meant as a "signal-too-good-to-be-true"
   guard at signal-emission time, not as an execution-price cap).
   **`max_probability` is a CRITICAL-tier knob per the matrix** —
   the change itself requires the
   [walkthrough template](#walkthrough-template-for-critical-knob-changes).
2. **Code refactor (separate plan if pursued):** split the entry-
   band cap (`max_probability`, evaluated at signal-emission time
   in the strategy) from the execution-price cap (`max_execution_price`,
   evaluated at order-submit time in `order_manager._resolve_execution_price_bounds`
   and the chase-up branch). Today they share the same `min(...)`
   reduction at lines 329-336 and 962-980, so any operator setting
   one knob unintentionally tightens the other.

Per Plan 0033 / Task 5 close-out: **no follow-up plan opened**. Per
the plan's decision rule, Verdict 1 is satisfied by an operator
config tweak. If the operator instead chooses path (2) — the code
refactor — that decision should be recorded as a new plan with the
CRITICAL-knob walkthrough.

#### Forensic artefacts (kept under git)

- [`docs/plans/work-artifacts/0033-tailend-cancelled-orders-2026-05-10.csv`](../plans/work-artifacts/0033-tailend-cancelled-orders-2026-05-10.csv) — Task 1 raw dump.
- [`docs/plans/work-artifacts/0033-tailend-clob-window-trades.csv`](../plans/work-artifacts/0033-tailend-clob-window-trades.csv) — Task 2 CLOB-window join.
- [`docs/plans/work-artifacts/0033-book-snapshot-join.csv`](../plans/work-artifacts/0033-book-snapshot-join.csv) — book microstructure join (the canonical evidence; CLOB trades only corroborated 1 of 33 because Tail-End markets are too thin for organic taker activity).
- [`docs/plans/work-artifacts/0033-bucket-classification.md`](../plans/work-artifacts/0033-bucket-classification.md) — Task 3 narrative.
- [`docs/plans/work-artifacts/0033-bucket-classification.sql`](../plans/work-artifacts/0033-bucket-classification.sql) — re-runnable join.
- [`docs/plans/work-artifacts/0033-fetch-clob-window.py`](../plans/work-artifacts/0033-fetch-clob-window.py) — re-runnable CLOB scraper (gamma + data-api, no auth).

#### Status

VERDICT FILED, no knob changed. Operator may apply the
`max_probability` tweak via the Bots UI at their discretion; that
tweak — when made — will be its own runtime-tweaks entry with the
full CRITICAL walkthrough.

### 2026-05-10 — Plan 0035 cap-split simulation (24 / 24 Bucket-A rows recovered)

Replayed the 33 cancelled `Sandbox - Tail-End` orders through the
post-fix `_chase_up_execution_caps` reducer (Plan 0035 path 2,
not the operator-config path 1). Script:
[`scripts/simulate_0035_chase_up_caps.py`](../../scripts/simulate_0035_chase_up_caps.py)
(deletable after this entry; preserved here for reproducibility).

| Bucket | Recovered | Total | Note |
|---|---:|---:|---|
| A — config-gated chase would help | 24 | 24 | All recovered: `post_fix_shadow_limit = ctx_max_entry_price ≥ book_best_ask` for every Bucket-A row in the canonical book-snapshot CSV |
| C — book above chase cap | 0 | 2 | Unchanged: spread-blowout cases where even the chase-up target was below `best_ask` |
| Indeterminate (no snapshot) | — | 7 | No book snapshot to evaluate against |

The script output and the bucket totals match the Plan 0033 doc's
Verdict 1 logic directly: every row that the Plan 0033 evidence
classified as "cap collapse blocked an otherwise-valid fill" recovers
under the post-fix reducer. The two Bucket-C rows remain uncrossable
as expected — those are real spread blowouts, not config bugs.

Note on the count: Plan 0033's bucket-classification.md narrative
quoted "25 / 27 evidenced rows" in Bucket A; the canonical CSV
(`0033-book-snapshot-join.csv`) actually has 24 / 26 (one row that
the narrative attributed to Bucket A is `no_book_snapshot` in the
CSV, so it sits in Indeterminate). The CSV is the artefact of
record. 24 / 24 is the full recovery rate for evidenced Bucket-A
rows.

This is a code-side simulation, not a runtime tweak. The actual
production rollout is tracked in Plan 0035 / Task 5; verification
SQL lives there.

## 2026-05-10 ~21:00 UTC — Plan 0039: Polymarket CLOB V2 cutover landed in `wallet_ws_monitor` + `ctf_execution`

- **Surface**: code change rsynced via `./deploy/sync_remote.sh`,
  not a DB tweak. Logged here because it un-broke the entire
  copy-trade pipeline that had been silent since Polymarket's
  2026-04-28 V1→V2 exchange cutover, and because the rollback
  recipe is the same shape as a runtime knob (`git revert` +
  redeploy).
- **Applied via**: code patch (Plan 0039 commits, all carry
  `Plan: 0039` trailer) → `./deploy/sync_remote.sh`.
- **Why**: `worker-trading` was issuing `eth_getLogs` against the
  V1 exchange addresses (`0x4bFb…982E`,
  `0xc5D563…220f80a`) and the V1 `OrderFilled` topic
  (`0xd0a08e8c…f6`). Polymarket cut those contracts dead on
  2026-04-28. Live trading moved to `0xE111…996B` (CTF Exchange
  V2) + `0xe2222d…0F59` (Neg Risk V2) and a new `OrderFilled`
  signature (`0xd543adfd…d8ee`, ABI: 4 indexed topics + 7 data
  words including `side` / `tokenId` / `builder` / `metadata`).
  Result before fix: zero rows in `wallet_monitor_events` for
  24 + h, copy-trade decisions silent for trader
  `Focused - 0x10c95474a8`.
- **Expected effect**: `wallet_monitor_events` row rate jumps
  from 0 / h to tens / min; `Trade detected` log lines reappear
  in `worker-trading`; CTF `setApprovalForAll` covers BOTH V2
  exchange operators (CTF Exchange V2 + Neg Risk V2), no
  `silently no-op approve` failure mode for negrisk markets.
- **Verification command**:

  ```bash
  ssh polyhome-1 "cd /home/polyhome/homerun && \
    docker compose exec -T postgres psql -U homerun -d homerun -t -c \
      \"SELECT count(*) FROM wallet_monitor_events \
        WHERE detected_at > now() - interval '10 minutes'\""
  # Pre-deploy: 0
  # Post-deploy (10 min after redeploy): 236
  #   (132 BUY + 104 SELL across the 48 tracked source wallets)
  ```

### Changes (code surface, not DB)

| Path | Before | After |
|---|---:|---:|
| `wallet_ws_monitor.CTF_EXCHANGE_ADDRESSES` | `(0x4bFb…982E, 0xc5D563…220f80a)` (V1) | renamed to `POLYMARKET_EXCHANGE_ADDRESSES_V2 = (0xE111…996B, 0xe2222d…0F59)` |
| `wallet_ws_monitor.ORDER_FILLED_TOPIC` | `0xd0a08e8c…f6` (V1) | `0xd543adfd…d8ee` (V2) |
| `wallet_ws_monitor._parse_order_filled_log` | accepted V1 4t+5w + 2t+7w | V2-only: 4t+7w (`side`, `tokenId`, `makerAmt`, `takerAmt`, `fee`, `builder`, `metadata`); rejects V1 shapes |
| `wallet_ws_monitor._determine_trade_side_and_details` | V1 asset-id-0-as-USDC heuristic | V2 `side` byte + leader-as-maker / leader-as-taker inversion |
| `wallet_ws_monitor._handle_log` | filtered taker-side hits where maker ∉ V1 exchange set | removed — V2 contracts never appear as maker/taker |
| `WalletTradeEvent.builder/metadata` | n/a | new `bytes32` hex fields plumbed through for future referral attribution |
| `ctf_execution.CTFExecutionService.CTF_EXCHANGE` | `0x4bFb41…D8B8982E` (V1, single operator) | replaced by `POLYMARKET_EXCHANGE_V2` + `POLYMARKET_NEG_RISK_EXCHANGE_V2` class attrs sourced from `py_clob_client_v2.config.get_contract_config(137)` and pinned by assert |
| `ctf_execution.ensure_exchange_approval` | one `setApprovalForAll(V1 exchange, true)` | iterates over the two V2 operators; aggregates per-operator results in `payload.approvals[]`; aborts on first failure |
| `live_execution_service.initialize` docstring | n/a | comment paragraph documents that submit-side is automatically V2 (SDK `__resolve_version()` returns `2` against live CLOB API after the cutover); reproducer command included |

### Rollback

```bash
# 1. Revert the V2 migration commits.
ssh polyhome-1 'cd /home/polyhome/homerun && git log --grep="Plan: 0039" --oneline'
git -C /Users/dtsym/Work/Splunk/_Project-X/homerun revert <SHA-range from above>
# 2. Re-rsync to the server.
./deploy/sync_remote.sh
# 3. Effect: V1 constants return → wallet_ws_monitor receives zero
#    OrderFilled events again → wallet_monitor_events rate drops to
#    0 within ~30 s → copy-trade pipeline goes silent. No other
#    pipeline regresses (ClobClient still resolves V2 transparently
#    via the SDK's __resolve_version() — submit-side is unaffected
#    by reverting our wallet-monitor changes).
# 4. Verify rollback: same SQL query as the verification command;
#    expect count(*) = 0 within 5 minutes.
```

### Regression coverage

`backend/tests/test_wallet_ws_monitor.py` (V2 fixtures + V1
rejection assertions) and `backend/tests/test_ctf_execution.py`
(V2 operator address pinning + `ensure_exchange_approval`
end-to-end behaviour). Both files green at deploy time:

```bash
bash scripts/run_tests_remote.sh tests/test_wallet_ws_monitor.py tests/test_ctf_execution.py
# 48 passed in 2.32s
```

