# Architecture: Market quality and prioritisation

Seven modules upstream of the scanner decide which markets are
worth scanning at all, classify them by regime, assign priority
tiers (HOT / WARM / COLD), apply category-aware risk envelopes,
and validate order-book depth before execution. They are the
**post-tag-filter, pre-strategy** stage of the pipeline — invisible
from [`trader-pipeline.md`](trader-pipeline.md), but every
opportunity flows through them.

The previous gate is [`market-filter.md`](market-filter.md)
(tag whitelist, on ingest). The next gate is the strategies
themselves (per-strategy `detect`/`detect_async`).

## Purpose

This layer is responsible for:

1. **Regime classification** — every market gets a "trending /
   volatile / ranging" label that strategies use to scale
   confidence, position size, stop-loss width, max-hold time.
2. **Quality filtering** — ten structural filters on
   `Opportunity` rows (ROI floor, liquidity floor, leg count, plan
   cost, position size, profit floor, resolution timeframe,
   annualised ROI). Cheap, deterministic, runs before any strategy
   detail.
3. **Anomaly monitoring** — new markets, price dislocations, thin
   books are flagged in real time; alerts feed the prioritiser.
4. **Tier assignment** — HOT / WARM / COLD, driving polling
   cadence (15 s / 60 s / 180 s) and change-detection skip.
5. **Sport classification** — slug → sport category for live-event
   risk handling.
6. **Category buffers** — per-category slippage / liquidity /
   position-size multipliers (sports get +1.5 % slippage; weather
   gets +0.1 %).
7. **Depth validation** — VWAP + slippage estimation before
   submission via `ExecutionEstimator`.

It does **not**:

- Decide whether a strategy should fire. That's the strategy's
  own `detect_async`.
- Filter by tag. That's [`market-filter.md`](market-filter.md).
- Submit orders. That's
  [`execution-and-fills.md`](execution-and-fills.md) +
  [`execution-defense.md`](execution-defense.md).

## Key files

| Path | Lines | Owns |
|---|---|---|
| [`market_regime.py`](../../../backend/services/market_regime.py) | 91 | regime classifier; per-regime execution multipliers |
| [`quality_filter.py`](../../../backend/services/quality_filter.py) | 418 | 10-filter `QualityFilterPipeline`; per-strategy overrides |
| [`market_monitor.py`](../../../backend/services/market_monitor.py) | 960 | `MarketMonitor`: new-market / dislocation / thin-book alerts; crypto schedule prediction |
| [`market_prioritizer.py`](../../../backend/services/market_prioritizer.py) | 537 | `MarketPrioritizer`: 7-signal HOT/WARM/COLD assignment + change-detection skip |
| [`sport_classifier.py`](../../../backend/services/sport_classifier.py) | 149 | regex-based slug → sport mapping with DB persistence |
| [`category_buffers.py`](../../../backend/services/category_buffers.py) | 427 | `CategoryBufferService`: per-category risk multipliers |
| [`depth_analyzer.py`](../../../backend/services/depth_analyzer.py) | 381 | `DepthAnalyzer`: VWAP + slippage check via `ExecutionEstimator` |

## Pipeline order

```
       Tag whitelist (market-filter.md)
                ▼
 ┌─ MarketRegimeClassifier.get_regime
 │     (trending | volatile | ranging)
 │
 ├─ QualityFilterPipeline.evaluate_opportunity
 │     (10 sequential filters; first failure short-circuits)
 │
 ├─ MarketMonitor.ingest_snapshot / get_fresh_opportunities
 │     (new-market / dislocation / thin-book alerts)
 │
 ├─ MarketPrioritizer.classify_market / classify_all
 │     (HOT / WARM / COLD + change-detection skip)
 │
 ├─ SportClassifier.classify_by_slug / get_classification
 │     (slug → sport, sport_category)
 │
 ├─ CategoryBufferService.adjust_*
 │     (slippage, liquidity, size, price-buffer multipliers)
 │
 └─ DepthAnalyzer.check_depth
       (book-depth + VWAP + slippage gate before CLOB call)
                ▼
        Strategy detect_async
```

`scanner.py` (line 25–27, 126) is the integrator: imports
`market_prioritizer`, `quality_filter`, calls them in the scan
loop. `market_runtime.py` is the data conduit (events / markets
catalog) for `MarketMonitor` and `MarketRegimeClassifier`.

All seven modules run on **`worker-trading`**.

## Contracts

### `market_regime.py` — regime classification

```python
class MarketRegimeClassifier:
    def get_regime(market_id: str) -> str
    # returns "trending" | "volatile" | "ranging" (default if history < 20 points)
```

Regimes are derived from a rolling price-history deque (max 200
points per market) and three thresholds (`config.py`):

| Field | Default | Triggers |
|---|---|---|
| `trending_cumret_threshold` | 0.03 | cumulative return > 3 % AND vol < 2× mean abs return ⇒ `trending` |
| `volatile_stddev_threshold` | 0.015 | stdev(returns) > 1.5 % ⇒ `volatile` |
| `min_points` | 20 | below this ⇒ `ranging` (default) |

Each regime maps to four execution multipliers
(`_REGIME_PARAMS`, lines 9–28): `confidence_multiplier`,
`size_multiplier`, `stop_loss_width`, `max_hold_multiplier`. The
`volatile` regime tightens to 0.5× size and 0.67× hold.

Singleton: `market_regime_classifier` (line 91). Consumed by
strategy SDK helpers when computing per-market execution params.

### `quality_filter.py` — `QualityFilterPipeline`

```python
class QualityFilterPipeline:
    def evaluate_opportunity(
        opp, overrides: Optional[QualityFilterOverrides] = None
    ) -> QualityReport
```

`QualityReport`: `opportunity_id`, `passed: bool`,
`filters: list[FilterResult]`. Each `FilterResult` carries
`filter_name`, `passed`, `reason`, `threshold`, `actual_value` —
this is what surfaces in the UI as the rejection reason.

Ten filters in order (lines 134–415):

| # | Filter | Default | Source |
|---|---|---|---|
| 1 | `min_roi` | 2.5 % | `MIN_PROFIT_THRESHOLD` |
| 2 | `directional_roi_cap` | 120 % (30 % guaranteed) | hard-coded |
| 3 | `plausible_roi` | 30 % (guaranteed-spread only) | `MAX_PLAUSIBLE_ROI` |
| 4 | `max_legs` | 6 | `MAX_TRADE_LEGS` |
| 5 | `leg_liquidity` | $500 × legs | `MIN_LIQUIDITY_PER_LEG` |
| 6 | `min_liquidity` | $1 000 | `MIN_LIQUIDITY_HARD` |
| 7 | `min_position_size` | $50 | `MIN_POSITION_SIZE` |
| 8 | `min_absolute_profit` | $10 at max position | `MIN_ABSOLUTE_PROFIT` |
| 9 | `resolution_timeframe` | 18 months | `MAX_RESOLUTION_MONTHS` |
| 10 | `annualized_roi` | 10 % | `MIN_ANNUALIZED_ROI` |

Per-strategy overrides
([`QualityFilterOverrides`](../../../backend/services/quality_filter.py#L43)) —
strategies set `BaseStrategy.quality_filter_overrides` and the
pipeline merges them per call. Useful for relaxing ROI floors on
high-volume markets.

Singleton: `quality_filter` (line 418). Imported in
[`scanner.py:27`](../../../backend/services/scanner.py).

### `market_monitor.py` — `MarketMonitor`

```python
async get_fresh_opportunities() -> list[NewMarketAlert]
async ingest_snapshot(events, markets) -> int
```

`AlertType` enum (lines 82–86): `NEW_MARKET`, `NEW_EVENT`,
`PRICE_DISLOCATION`, `THIN_BOOK`. `Urgency` (lines 89–92):
`HIGH`, `MEDIUM`, `LOW`.

Thresholds (lines 37–62):

```
NEW_MARKET_WINDOW_SECONDS         = 300    (5 min "new" flag TTL)
THIN_BOOK_LIQUIDITY_THRESHOLD     = $500
DISLOCATION_SPREAD_HIGH           = 0.05   (sum prices ± from 1.0)
DISLOCATION_SPREAD_MEDIUM         = 0.03
DISLOCATION_SPREAD_LOW            = 0.015
MAX_PRICE_HISTORY                 = 60     (rolling snapshots per market)
REGISTRY_RETENTION_SECONDS        = 86400  (24 h)
```

`MarketSnapshot` (lines 95–166) carries `first_seen_at`,
`initial_prices`, `current_prices`, `price_stability_score` (0
unstable → 1 equilibrated), `is_new`, `has_thin_book`. Stability
recomputes via `_compute_stability()` =
`1 - (avg_delta / 0.10)` clamped to `[0, 1]`.

Crypto schedule prediction
([`CryptoMarketSchedule`](../../../backend/services/market_monitor.py#L202),
line 622): regex-detects recurring BTC/ETH 15 m / 1 h markets in
question text and tracks `last_seen_creation` to predict the next
creation timestamp. Used by the prioritiser for crypto-imminence
signals.

No DB persistence — entirely in-memory; 24 h registry retention.

### `market_prioritizer.py` — `MarketPrioritizer`

```python
def classify_market(market, now=None) -> MarketTier
def classify_all(markets, now=None) -> dict[MarketTier, list[Market]]
def has_market_changed(market) -> bool
def get_changed_markets(markets) -> list[Market]
def get_hot_market_ids() -> set[str]
def should_trigger_fast_scan() -> bool
```

`MarketTier`: `HOT` (~15 s polling), `WARM` (~60 s), `COLD`
(~180 s). State per market in `MarketPriorityState` dataclass
(lines 44–59) — kept in `_states` dict.

Tier decision (lines 135–210) — seven signals contribute
`hot_signals` and `warm_signals`:

| # | Signal | Hot weight | Warm weight |
|---|---|---|---|
| 1 | age ≤ `HOT_TIER_MAX_AGE_SECONDS` (300 s) | +2 | — |
| 1' | age ≤ `WARM_TIER_MAX_AGE_SECONDS` (1 800 s) | — | +1 |
| 2 | `price_stability < 0.3` | +2 | — |
| 2' | `price_stability < 0.6` | — | +1 |
| 3 | thin book (< $500) | +1 | — |
| 4 | `has_monitor_alert` | +2 | — |
| 5 | last price change < 60 s | +1 | — |
| 5' | last change < 300 s | — | +1 |
| 6 | volume > $200 K | — | +1 always |
| 7 | unchanged ≥ `COLD_TIER_UNCHANGED_CYCLES` (5) | demote to COLD | — |

Final: `HOT` if `hot_signals ≥ 2`; `WARM` if `hot_signals ≥ 1`
or `warm_signals ≥ 2`; `COLD` otherwise.

Change detection: 12-char MD5 fingerprint of rounded outcome
prices (resolution 0.001) compared against
`last_price_fingerprint`. Unchanged COLD markets are skipped
entirely on the next scan iteration.

Singleton: `market_prioritizer` (line 537). Imported in
[`scanner.py:25`](../../../backend/services/scanner.py).

### `sport_classifier.py` — `SportClassifier`

```python
def classify_by_slug(token_id, slug) -> Optional[SportClassification]
def get_classification(token_id) -> Optional[SportClassification]
async load_from_db()
async persist_classification(classification)
```

`SportClassification` (lines 60–65): `token_id`, `sport`,
`sport_category`, `extra_buffer`, `is_live_sport`.

`SPORT_PATTERNS` (lines 37–56) — 17 regex patterns mapping
slug → sport. Examples:

| Pattern | Sport ID | Category | Extra buffer |
|---|---|---|---|
| `(?i)\batp\b|(?i)\bwta\b|tennis` | `atp_tennis` / `wta_tennis` | `tennis` | 0.01 |
| `(?i)\bligue1\b` | `soccer_ligue1` | `soccer` | 0.01 |
| `(?i)\bnba\b` | `nba` | `basketball` | 0.008 |
| `(?i)\bufc\b\|\bbox` | `mma` / `boxing` | `mma` / `boxing` | 0.01 |
| `(?i)\bf1\b` | `motorsport` | `motorsport` | 0.005 |

`is_live_sport = sport_category in {"tennis", "soccer", "basketball", "mma"}`
(lines 84, 104).

DB table `sport_token_classifications`
([`database.py:21-33`](../../../backend/models/database.py)):
`token_id` PK, `sport`, `sport_category`, `extra_buffer`,
`classified_at`, `source_slug`. Indexed on `sport`.

Singleton: `sport_classifier` (line 149). Cache loaded on startup
via `load_from_db()`.

### `category_buffers.py` — `CategoryBufferService`

```python
def get_profile(category) -> CategoryRiskProfile
def adjust_slippage_tolerance(base, category) -> float
def adjust_min_liquidity(base, category) -> float
def adjust_position_size(base, category) -> float
def get_price_buffer(category) -> float
async log_buffer_application(opportunity_id, category, adjustments)
```

`CategoryRiskProfile` (lines 38–71): `category`, `display_name`,
`extra_slippage_tolerance`, `price_buffer`,
`min_liquidity_multiplier`, `position_size_multiplier`,
`volatility_rating`, `description`.

`DEFAULT_PROFILES` (lines 129–210):

| Category | +Slip | +Buffer | Liq× | Size× | Volatility |
|---|---|---|---|---|---|
| `SPORTS` | 1.5 % | $0.01 | 1.5× | 0.7× | very_high |
| `CRYPTO` | 1.0 % | $0.008 | 1.3× | 0.8× | high |
| `CULTURE` | 0.5 % | $0.005 | 1.1× | 0.9× | medium |
| `POLITICS` | 0.2 % | $0.002 | 1.0× | 1.0× | low |
| `WEATHER` | 0.1 % | $0.001 | 1.0× | 1.0× | low |
| `ECONOMICS` | 0.3 % | $0.003 | 1.1× | 0.95× | medium |
| `TECH` | 0.3 % | $0.003 | 1.0× | 0.95× | medium |
| `FINANCE` | 0.5 % | $0.005 | 1.2× | 0.85× | medium |

Unknown categories → neutral profile (all multipliers 1.0).

Audit table `category_buffer_logs`
([`database.py:79-99`](../../../backend/models/database.py)):
records `base_*` vs `adjusted_*` per opportunity for analytics.

Singleton: `category_buffer_service` (line 427). Called from the
orchestrator when building the execution plan.

### `depth_analyzer.py` — `DepthAnalyzer`

```python
async check_depth(
    token_id, side, target_price, required_size_usd,
    trade_context=None
) -> DepthCheckResult
async calculate_depth_at_price(token_id, side, price) -> float
async get_executable_price(token_id, side, size) -> float
```

`DepthCheckResult` (lines 42–53): `has_sufficient_depth`,
`available_depth_usd`, `required_depth_usd`, `best_price`,
`vwap_price`, `slippage_percent`, `checked_at`.

Defers to `ExecutionEstimator`
([`backend/services/optimization/execution_estimator.py`](../../../backend/services/optimization/execution_estimator.py))
with hard-coded `ExecutionEstimatorConfig`:

```
fee_bps                       = 0.0
latency_ms                    = 350
displayed_depth_factor        = 0.88
min_depth_factor              = 0.20
max_book_age_ms               = 10000
stale_depth_decay             = 0.55
adverse_selection_multiplier  = 0.70
```

Decision (line ~230):
```
has_sufficient_depth = (
    available_depth_usd >= max(MIN_DEPTH_USD, required_size_usd)
    AND fill_probability >= 0.995
)
```

Default `MIN_DEPTH_USD = 200`. Persists every check to
`depth_checks` audit table
([`database.py:61-76`](../../../backend/models/database.py)).

Singleton: `depth_analyzer` (line 381). Called from the
orchestrator before submission; the result also feeds the Cox-PH
fill model (see [`execution-and-fills.md`](execution-and-fills.md)).

## Configuration

`AppSettings` / `config.py` columns this layer reads:

```
# Quality filter
MIN_PROFIT_THRESHOLD          0.025
MIN_LIQUIDITY_HARD            $1 000
MIN_POSITION_SIZE             $50
MIN_ABSOLUTE_PROFIT           $10
MIN_ANNUALIZED_ROI            10 %
MAX_RESOLUTION_MONTHS         18
MAX_PLAUSIBLE_ROI             30 %
MAX_TRADE_LEGS                6
MIN_LIQUIDITY_PER_LEG         $500

# Tiering
HOT_TIER_MAX_AGE_SECONDS      300
WARM_TIER_MAX_AGE_SECONDS     1 800
COLD_TIER_UNCHANGED_CYCLES    5
THIN_BOOK_LIQUIDITY_THRESHOLD $500
MIN_LIQUIDITY                 $1 000
CRYPTO_PREDICTION_WINDOW_SECONDS  (configurable)

# Depth
MIN_DEPTH_USD                 $200
```

`CategoryBufferService` profiles are hard-coded in
`DEFAULT_PROFILES`. `MarketRegimeClassifier` thresholds are
hard-coded in the constructor. Sport patterns are hard-coded in
`SPORT_PATTERNS`. Changes to any of these require a code redeploy.

## Dependencies (both directions)

**This layer depends on:**

- `market_runtime.py` — feeds events/markets snapshots into
  `MarketMonitor.ingest_snapshot`.
- `polymarket_client` — order-book fetches for `DepthAnalyzer`.
- `ExecutionEstimator` — VWAP + fill-probability engine
  (`backend/services/optimization/`).

**Depended on by:**

- `scanner.py` — imports `market_prioritizer` and
  `quality_filter` directly.
- `trader_orchestrator` — invokes `category_buffer_service` when
  sizing orders; `depth_analyzer` before submit.
- Cox-PH covariate engineering
  ([`fill_simulator/survival_features.py`](../../../backend/services/fill_simulator/survival_features.py)) —
  consumes `depth_analyzer` outputs.
- The strategy SDK — consumes `market_regime_classifier`
  multipliers per market.

## Extension points

| When you want to… | Touch |
|---|---|
| Add a new quality filter | Append to the pipeline list (line ~134) and emit a `FilterResult` |
| Add per-strategy override field | Extend `QualityFilterOverrides` (line 43) and consume it at the matching filter site |
| Add a new tier | Extend `MarketTier` enum + `_classify` logic; ensure scanner cadence map handles the new value |
| Add a new sport regex | Add a tuple to `SPORT_PATTERNS` (sport_classifier.py:37); persist via `persist_classification()` |
| Add a new category buffer profile | Add a `CategoryRiskProfile` to `DEFAULT_PROFILES` (category_buffers.py:129) |
| Tighten depth requirements | `MIN_DEPTH_USD` constructor arg of `DepthAnalyzer` |

## Known footguns

- **Stale market_monitor cache.** Registry retention is 24 h;
  beyond that, snapshots silently age out without an alert.
  Surface registry size in the heartbeat.
- **`MarketPrioritizer` defaults to WARM.** If a market has no
  `MarketPriorityState`, it lands as WARM — not COLD. Don't
  assume COLD is the default.
- **Quality filter too strict.** Raising `MIN_LIQUIDITY_HARD`
  past production levels silently kills opportunity flow; the UI
  shows zero rejections (because no filter even got far enough).
  Always check `quality_filter`'s rejection histogram.
- **Sport classifier mis-categorise.** Regex on slug only —
  market question text is ignored. ATP qualifier markets with
  `qualifier-atp-...` slug match correctly; markets with no `atp`
  in the slug do not. Falls back to no-classification, neutral
  category.
- **Category buffers applied late.** Multipliers fire at
  execution-plan build time, not at quality-filter time. A
  trade that passed the `min_position_size` filter at $50 may
  be reduced below $50 by SPORTS' 0.7× multiplier. Mind the
  re-check.
- **Depth analyzer rejects on `fill_probability < 0.995`.** The
  threshold is conservative; thin books that would still fill at
  best ask get rejected. Lower at your own risk.
- **`ExecutionEstimator` config hard-coded.** No per-market
  tuning; volatile markets eat the same `displayed_depth_factor`
  as stable ones.
- **Regime classifier cache unbounded.** `_prices: dict[str, deque]`
  grows with every market seen; no cleanup. Long-running processes
  drift up in RSS — restart resets.
- **Price fingerprint at 0.001 resolution.** Sub-cent moves are
  classified as "unchanged"; ultra-high-frequency strategies need
  finer granularity (or to bypass the prioritiser via HOT-tier).
- **`COLD_TIER_UNCHANGED_CYCLES = 5`.** With 60 s polling, a
  market is demoted to COLD after 5 minutes of price stability.
  Acceptable for human time scales; aggressive for fast markets.

## Test coverage

- `backend/tests/test_scanner_pipeline.py` — integration:
  quality_filter + market_prioritizer + scanner
- `backend/tests/test_market_regime.py`
- `backend/tests/test_quality_filter.py`
- `backend/tests/test_market_monitor.py`
- `backend/tests/test_market_prioritizer.py`
- `backend/tests/test_sport_classifier.py`
- `backend/tests/test_category_buffers.py`
- `backend/tests/test_depth_analyzer.py`

## Where to look next

| Topic | File |
|---|---|
| Tag whitelist (the gate before this layer) | [`market-filter.md`](market-filter.md) |
| What happens after this layer (strategy detection → execution) | [`trader-pipeline.md`](trader-pipeline.md) |
| Submission-side gates (defence layer) | [`execution-defense.md`](execution-defense.md) |
| Cox-PH fill simulator (consumes `depth_analyzer` outputs) | [`execution-and-fills.md`](execution-and-fills.md) |
| Three-plane runtime (`worker-trading` hosts this layer) | [`system-overview.md`](system-overview.md), [`worker-trading.md`](worker-trading.md) |

Last verified: 2026-05-08
