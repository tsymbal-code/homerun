# Architecture: Execution defence layer

Nine modules form a layered defence between strategy decision and
venue submission. Together they account for ~3 100 lines of code
across `backend/services/`, and an operator debugging "why was this
trade blocked?" has nine candidate gates to check. This note maps
all of them to one page so the diagnosis is single-pass.

The "what happens after a fill" half of execution lives in
[`execution-and-fills.md`](execution-and-fills.md). This note
covers the **submission boundary** — pre-CLOB checks, retry logic,
post-trade circuit breakers, position monitoring, and the
backpressure feedback loop.

The pipeline that **leads** to this layer (decision → selected) is
[`trader-pipeline.md`](trader-pipeline.md).

## Purpose

Each module owns one specific failure mode at the submission
boundary:

| Order in the flow | Module | Failure mode it owns |
|---|---|---|
| 1. pre-decision (cache) | `market_tradability` | venue not accepting orders |
| 2. pre-decision | `live_market_detector` | live vs historical (drives GTD) |
| 3. tier classification | `execution_tiers` | confidence-based price/size/retry policy |
| 4. submit-time gate | `execution_safety` | operator-installed price floor/ceiling violations |
| 5. submit retries | `price_chaser` | partial fills + price drift |
| 6. post-submit, per-token | `token_circuit_breaker` | rapid-fire trade clusters or API error storms |
| 7. async, system-wide | `live_pressure` | downstream backpressure (DB / venue / loop stalls) |
| 8. background, position-side | `position_monitor` | TP/SL trigger, automatic exits |
| 9. background, position-side | `stuck_position_monitor` | exits blocked by venue/oracle conditions |

Together they enforce: prices are within operator policy, retries
do not march off a cliff, one bad token cannot wedge the system,
and stuck positions are visible to the operator within 6 hours.

It does **not** own:

- Risk gates that decide *if* to trade — those are in the
  orchestrator's `risk_manager.py` (see
  [`trader-pipeline.md`](trader-pipeline.md) Stage 5).
- Cox-PH fill probability — that's
  [`execution-and-fills.md`](execution-and-fills.md).
- Venue authentication — that's `live_execution_service.py`.

## Key files

| Path | Lines | Owns |
|---|---|---|
| [`market_tradability.py`](../../../backend/services/market_tradability.py) | 172 | semaphore-gated tradability map |
| [`live_market_detector.py`](../../../backend/services/live_market_detector.py) | 145 | live vs historical classification + GTD |
| [`execution_safety.py`](../../../backend/services/execution_safety.py) | 308 | operator-installed price floors/ceilings |
| [`execution_tiers.py`](../../../backend/services/execution_tiers.py) | 370 | 4-tier confidence-based policy |
| [`price_chaser.py`](../../../backend/services/price_chaser.py) | 440 | retry-with-price-update on partial fills |
| [`token_circuit_breaker.py`](../../../backend/services/token_circuit_breaker.py) | 385 | per-token trip on rapid trades or API errors |
| [`live_pressure.py`](../../../backend/services/live_pressure.py) | 179 | system-wide backpressure publish/read |
| [`position_monitor.py`](../../../backend/services/position_monitor.py) | 295 | shadow position TP/SL daemon |
| [`stuck_position_monitor.py`](../../../backend/services/stuck_position_monitor.py) | 638 | live blocked-exit surveillance + Telegram alerts |

## Contracts

### 1. `market_tradability` — tradability map

```python
async get_market_tradability_map(
    market_ids: Iterable[str], *, now=None, max_concurrency=12
) -> dict[str, bool]
```

Returns `{market_id: True/False}`. Hits a process-level semaphore
([`market_tradability.py:27-34`](../../../backend/services/market_tradability.py))
and an LRU cache (5 000 entries, 3-min TTL). Marks non-tradable on
`resolved=true` / `disputed=true` / CLOB rejecting orders. **Never
raises.**

Lives in-memory only; no DB persistence.

### 2. `live_market_detector` — live status + GTD

```python
async live_market_detector.is_live(token_id: str) -> LiveStatus
```

`LiveStatus` (line 27): `is_live: bool`, `gtd_seconds: int`
(61 for live, 1 800 for historical), `cache_ttl_seconds: int`,
`checked_at: datetime`.

Cache TTL asymmetric: 60 s for live, 300 s for non-live.
Persists each check to `market_live_status` table
([`database.py:16`](../../../backend/models/database.py): token_id,
is_live, last_checked, gtd_seconds, check_count). `invalidate(token_id)`
forces re-check.

### 3. `execution_safety` — operator price floors/ceilings

```python
assert_buy_entry_price_within_safety_bounds(
    strategy_slug: Optional[str], entry_price: Optional[float]
) -> SafetyAssessment
```

`SafetyAssessment` is a frozen dataclass
([`execution_safety.py:88-109`](../../../backend/services/execution_safety.py)):
`passed: bool`, `reason: str` (stable code:
`"entry_price_below_safety_floor"` /
`"entry_price_above_safety_ceiling"` / `"ok"`),
`message: str`, optional `floor`, `ceiling`, `observed`.

**Always returns; never raises.** This is the single most
important fact about this module — downstream code must check
`.passed`, not catch exceptions.

Floors/ceilings are operator-installed at runtime via
`register_strategy_entry_price_floor(slug, floor)` (line 116).
Storage is module-level dicts (`_STRATEGY_ENTRY_PRICE_FLOORS`,
`_STRATEGY_ENTRY_PRICE_CEILINGS`, lines 79–80) — **in-memory
only**, no DB. Slugs normalised to lowercase. Empty registry by
default; the safety check is a no-op until something is
registered.

Called from `submit_execution_leg()`
([`order_manager.py:748`](../../../backend/services/trader_orchestrator/order_manager.py))
**before** the CLOB call. Violations short-circuit with
`LegSubmitResult(status="skipped", reason="execution_safety_floor")`.

### 4. `execution_tiers` — 4-tier classification

```python
classify_opportunity(
    roi_percent: float, liquidity: float,
    strategy: str, category: Optional[str]
) -> ExecutionTier
```

`ExecutionTier` (line 58): `tier (1-4)`, `name`, `price_buffer`,
`size_multiplier`, `max_retries`, `order_type` (GTC/FOK),
`description`.

Tier matrix (hard-coded, lines 99–136):

| Tier | ROI ≥ | Liq ≥ | Buffer | Size× | Retries | Order |
|---|---|---|---|---|---|---|
| 1 high_conviction | 5% | $20 K | $0.01 | 1.25 | 5 | GTC |
| 2 standard | 3% | $5 K | $0.005 | 1.00 | 4 | GTC |
| 3 cautious | 2% | $1 K | $0.003 | 0.80 | 3 | GTC |
| 4 minimal | else | else | $0.001 | 0.50 | 2 | FOK |

Category buffers stack additively (lines 139–145): SPORTS +0.01,
CRYPTO +0.008, CULTURE +0.005, POLITICS +0.002, WEATHER +0.001.
Persists every classification to `tier_assignments`
([`database.py:31`](../../../backend/models/database.py)) for
analytics.

### 5. `price_chaser` — retry with price updates

```python
await price_chaser.execute_with_chase(
    token_id, side, price, size,
    place_order_fn, get_market_price_fn,
    opportunity_id, tier
) -> dict
```

Returns `{success, final_price, total_filled, attempts,
total_price_adjustment}`. Each retry sequence persists to
`order_retry_logs` ([`database.py:34`](../../../backend/models/database.py)).

`PriceChaseConfig` (line 63):

```
max_retries: 5
price_increment_per_retry: $0.005
max_total_chase: $0.02
max_slippage_percent: 2.0%      ← cap on adjustment relative to original
final_retry_order_type: GTD     ← last attempt switches to GTD
final_retry_gtd_seconds: 61
chase_on_first_retry: True
```

Logic: BUY raises bid each attempt, SELL lowers ask. Final attempt
switches to GTD-61. Adjustments clamped to
`min(max_total_chase, max_slippage_percent × original_price)`.
Final price clamped to `[0.01, 0.99]`. Stops on full fill, dust
threshold (≤0.01 shares), or retries exhausted.

Tier-aware backoff (line 337): tier ≥ 3 = exponential `0.2 × 2^n`,
tier 2 = flat 0.5 s, tier 1 = 0.3 s.

### 6. `token_circuit_breaker` — per-token trip

```python
record_trade(token_id, size, price, side) -> Optional[TokenTripEvent]
record_api_error(token_id) -> None
is_tripped(token_id) -> tuple[bool, Optional[str]]
trip_token(token_id, reason, details) -> None
clear_trip(token_id) -> None
```

`TokenTripConfig` (line 43):

```
large_trade_threshold_shares: 1500
consecutive_trigger:           2     ← ≥2 large trades in window = trip
detection_window_seconds:     30
trip_duration_seconds:        120
trip_on_api_error:            True
```

Trip triggers:
1. `record_trade()`: ≥ 2 large trades within 30 s →
   reason `"rapid_large_trades"`.
2. `record_api_error()`: ≥ 5 API errors within 30 s →
   reason `"consecutive_api_errors"`.
3. `trip_token()`: manual.

Persists to `token_trips` ([`database.py:18`](../../../backend/models/database.py)):
`token_id`, `reason`, `trade_count`, `triggered_at`, `expires_at`,
`cleared_at`, `was_auto_expired`, `details`. Auto-expires at
`expires_at`. `clear_trip()` is the operator escape hatch.

### 7. `live_pressure` — backpressure publish/read

Module-level functions (no class):

```python
publish_backpressure(component, *, level: float, reason: str)
current_backpressure_level() -> float
maybe_mark_db_pressure(exc, component, ttl_seconds=60) -> bool
is_db_pressure_active() -> bool
backpressure_extra_sleep_seconds(base_interval) -> float
```

Storage: in-memory dicts
(`_BACKPRESSURE_BY_COMPONENT[component] = (level, ts, reason)`).
Levels in `[0.0, 1.0]`; >0.7 means "skip non-essential work."
**Stale entries auto-evict after 30 s** so a crashed publisher
cannot lock the system.

DB pressure is reactive: caught SQLAlchemy exceptions call
`mark_db_pressure(reason, component, ttl_seconds)`; flag persists
for the TTL.

`backpressure_extra_sleep_seconds(base)` returns extra sleep:
level ≤ 0.4 → 0; level 0.7 → `base`; level 1.0 → `3 × base`.
Producers (workers, scanner) read this voluntarily; this is
**advisory, not blocking**.

### 8. `position_monitor` — shadow TP/SL daemon

`PositionMonitor` singleton with `start()`/`stop()`/`get_status()`.
Polls every 15 s
([`position_monitor.py:44`](../../../backend/services/position_monitor.py)).

For each open `simulation_positions` row with `take_profit_price`
or `stop_loss_price` set:

1. Fetch current price (WS cache when fresh < 10 s, else HTTP).
2. If `current ≥ take_profit_price` → exit, reason `"take_profit"`.
3. If `current ≤ stop_loss_price` → exit, reason `"stop_loss"`.
4. PnL = `(current - entry) × quantity`, fee 2 % on winnings.
5. Update `simulation_positions.status`, write
   `simulation_trades`, update `simulation_accounts.current_capital`.

Shadow-mode only. Live positions use `trader_reconciliation_worker`
(see [`execution-and-fills.md`](execution-and-fills.md)).

### 9. `stuck_position_monitor` — blocked-exit surveillance

```python
await scan_stuck_positions(age_hours, session_factory) -> list[dict]
await classify_stuck_position(observation) -> dict
await alert_operator_on_stuck_positions(classified) -> dict
```

Scans `trader_orders` rows where `mode='live'`, `status` in open
lifecycle states, `pending_live_exit.status` in blocked-terminal
states, and `created_at ≤ now - 6 h`.

Blocked-terminal `pending_live_exit.status` values
([`stuck_position_monitor.py:101-111`](../../../backend/services/stuck_position_monitor.py)):
`blocked_persistent_timeout`, `blocked_no_inventory`,
`blocked_retry_exhausted`, `blocked_retry_exhausted_hard`,
`blocked_orderbook_gone`.

Classification (lines 349–479) uses on-chain RPC truth:

| Classification | Meaning | Alert? |
|---|---|---|
| `missing_chain_inputs` | token_id / condition_id missing | no |
| `chain_unavailable` | RPC failed | no |
| `recovered_externally` | on-chain balance = 0 | no |
| `redemption_pending` | balance > 0, market resolved | no |
| `pending_resolution` | end_date within 7 days | no |
| `transient_client_failure` | last_error not a venue rejection | no |
| `operator_intervention` | balance > 0, end > 7 days, venue rejection | **yes** |

Alerts go to Telegram via `notifier.send_operator_alert()`,
**informational tone**, framed around on-chain truth.
**Never auto-writes positions** — the manual-writeoff endpoint
is the only path that mutates `actual_profit`.

Alert cooldown: 6 h per `order_id`, in-memory `_last_alert_at`
dict (re-alerts on process restart, by design — surfaces silent
crashes).

Invoked from `trader_reconciliation_worker` every 5 min.

## Submission flow — what fires when

```
strategy_loader → strategy.detect_async → opportunity
   │
   │ orchestrator selects opportunity
   ▼
execution_tiers.classify_opportunity                   ← (3) tier policy
   │
   ▼
risk_manager pre-flight gates                           (in trader-pipeline.md)
   │
   ▼
submit_execution_leg() — order_manager.py:487
   │
   ├─ market_tradability.get_market_tradability_map     ← (1)
   ├─ live_market_detector.is_live                       ← (2) sets GTD
   ├─ execution_safety.assert_buy_entry_price_...        ← (4)  ← FAIL = SKIP
   ├─ buy_pre_submit_gate (notional vs inventory)        ← FAIL = SKIP
   │
   ▼
CLOB submission (Polymarket / Kalshi)
   │
   ├─ price_chaser.execute_with_chase                    ← (5) retry loop
   │
   ▼
post-submission monitoring
   │
   ├─ token_circuit_breaker.record_trade                 ← (6) per-token trip
   ├─ live_pressure observers                            ← (7) advisory
   │
   ▼
position open
   │
   ├─ position_monitor (shadow only, every 15 s)         ← (8) TP/SL
   ├─ stuck_position_monitor (live, every 5 min)         ← (9) blocked-exit
```

Hierarchy (which gate can skip the rest):

- **`execution_safety` violation** → `LegSubmitResult.skipped`,
  no further gates fire. Reason: `"execution_safety_floor"` in
  `trader_decision_checks`.
- **`buy_pre_submit_gate` failure** → `LegSubmitResult.skipped`,
  no CLOB call. Reason: `"buy_pre_submit_gate"`.
- **`token_circuit_breaker.is_tripped` true** → upstream callers
  skip new orders for that token until trip expires or operator
  clears it.
- **`live_pressure` is advisory** — readers slow down voluntarily;
  it never short-circuits a decision.

## Configuration

Most thresholds are hard-coded in their modules. Only operator
policy is dynamic:

| Knob | Where | Persistence |
|---|---|---|
| Strategy price floor / ceiling | `register_strategy_entry_price_floor()` | in-memory; redeploy or boot script registers |
| Tier matrix | `execution_tiers.py:99-136` | code-only |
| Category buffers | `execution_tiers.py:139-145` | code-only |
| `PriceChaseConfig` | `price_chaser.py:63` | code-only |
| `TokenTripConfig` | `token_circuit_breaker.py:43` | code-only |
| Stuck-position thresholds | `stuck_position_monitor.py:124+` | code-only |
| Position monitor poll interval (15 s) | `position_monitor.py:44` | code-only |

**`AppSettings` exposes none of these** — by design, since they
encode the operator's risk model rather than tunable behaviour.

## Dependencies (both directions)

**This layer depends on:**

- `polymarket_client` and Kalshi REST for live tradability + book
  data.
- `wallet_state_cache` for inventory checks (read-only).
- `notifier` for Telegram alerts (`stuck_position_monitor`).
- Polygon RPC for on-chain truth (`stuck_position_monitor`
  classification).

**Depended on by:**

- `submit_execution_leg()` in
  [`order_manager.py`](../../../backend/services/trader_orchestrator/order_manager.py).
- `trader_reconciliation_worker` (invokes
  `stuck_position_monitor.scan_stuck_positions()`).
- The shadow simulator
  ([`execution_simulator.py`](../../../backend/services/simulation/execution_simulator.py)) —
  only `position_monitor` matters here.
- The Bots / Operator Alerts UI surfaces — alert payloads flow
  through to Telegram and the in-app drawer.

## Extension points

| When you want to… | Touch |
|---|---|
| Add a new safety gate | New module + call in `submit_execution_leg()`; persist a `trader_decision_checks` row with a stable `check_name`. |
| Add a new tier | Extend the `TIERS` dict in `execution_tiers.py`; ensure `price_chaser` consumes the new `max_retries`. |
| Add a new circuit-breaker reason | New trip method on `TokenCircuitBreaker`; persist the new `reason` value to `token_trips`. |
| Add a new stuck-position classification | New branch in `classify_stuck_position()` (line 349); decide if it warrants an alert. |
| Surface a new pressure component | Call `publish_backpressure(component, level, reason)` from the saturation source; readers pick it up automatically. |

## Known footguns

- **Token tripped permanently.** If `clear_trip()` is never
  called and `was_auto_expired` is false, the token blocks until
  the process restarts. Surface `get_active_trips()` in the
  heartbeat.
- **Stuck-position alerts re-fire on process restart.** In-memory
  cooldown does not survive restarts — by design (surfaces silent
  crashes), but expect duplicate Telegram messages after a
  redeploy.
- **Safety floor stale after a UI edit.** The floor is consulted
  per-call; in-flight orders that already passed the gate proceed.
- **Tier mismatch on stale liquidity.** Tier-1 assigned to a
  market whose order-book has since drained — `price_chaser` burns
  all 5 retries before giving up. No mid-flight tier downgrade.
- **`price_chaser` on empty book.** Loop runs `max_retries`
  times even with zero depth; mitigations live in `place_order_fn`
  (raise an exception) or upstream `depth_analyzer` (see
  [`market-quality-and-prioritization.md`](market-quality-and-prioritization.md)).
- **DB pressure ignored.** `live_pressure` is advisory; if a
  worker doesn't read it, it doesn't slow down. Audit each new
  worker for `backpressure_extra_sleep_seconds()` usage.
- **Stuck-scan FIFO trap.** With > 500 stuck rows, the scan
  always sees the same first 500 (LIMIT enforced for worker
  health). Operators must check the queue separately.
- **`position_monitor` shadow-only.** Live TP/SL is enforced by
  the orchestrator's exit logic, **not** by this module. Naming
  is misleading; respect the boundary.

## Test coverage

- `backend/tests/test_execution_safety.py` — registry,
  case-insensitivity, override semantics
- `backend/tests/test_market_tradability.py`
- `backend/tests/test_live_market_detector.py`
- `backend/tests/test_execution_tiers.py`
- `backend/tests/test_price_chaser.py`
- `backend/tests/test_token_circuit_breaker.py`
- `backend/tests/test_position_monitor.py`
- `backend/tests/test_stuck_position_monitor.py` — classification
  routing, on-chain truth, alert cooldown

## Where to look next

| Topic | File |
|---|---|
| Pipeline that produces the decision this layer consumes | [`trader-pipeline.md`](trader-pipeline.md) |
| Cox-PH fill simulator + live execution beyond submit | [`execution-and-fills.md`](execution-and-fills.md) |
| Pre-scanner gates (regime, depth, prioritisation) | [`market-quality-and-prioritization.md`](market-quality-and-prioritization.md) |
| Live feeds and price caches | [`websocket-and-events.md`](websocket-and-events.md) |
| `worker-trading` process model | [`worker-trading.md`](worker-trading.md) |

Last verified: 2026-05-08
