# Architecture: Wallet intelligence and anomaly detection

This is the wallet-scoring + risk-detection stack that drives the
Discovery and Wallets UIs. Three modules totalling ~3 800 lines —
[`insider_detector.py`](../../../backend/services/insider_detector.py) (699),
[`anomaly_detector.py`](../../../backend/services/anomaly_detector.py) (687),
[`wallet_intelligence.py`](../../../backend/services/wallet_intelligence.py) (2 509)
— score wallets on insider-trading patterns, flag statistical / pattern /
timing anomalies, and run the orchestrating analysis (clustering,
tagging, confluence, cross-platform identity matching, whale cohorts).

All three live on the **`worker-discovery`** plane (see
[`worker-discovery.md`](worker-discovery.md)). Their outputs surface
in the Discovery UI via `routes_discovery.py` and `routes_anomaly.py`,
and feed the `traders/*` strategy family
([`copy-trade-pipeline.md`](copy-trade-pipeline.md)).

## Purpose

This stack is responsible for:

1. **`InsiderDetectorService`** — scores every discovered wallet on
   an 11-component formula (timing alpha, pre-news lead, market
   selection edge, drawdown, cluster correlation, etc.) and writes
   `insider_score` + `insider_confidence` + `insider_metrics_json`
   to `discovered_wallets`.
2. **`AnomalyDetector`** — analyses one wallet on demand, runs three
   detector phases (statistical / pattern / timing) and persists
   findings as `DetectedAnomaly` rows plus a rollup
   `anomaly_score` on `tracked_wallets`.
3. **`WalletIntelligence`** orchestrator — the umbrella service
   that runs five sub-systems on a 30-min cadence:
   `ConfluenceDetector`, `EntityClusterer`, `WalletTagger`,
   `CrossPlatformTracker`, `WhaleCohortAnalyzer`. Updates
   composite scores, cluster IDs, tags, and cross-platform
   identities on `discovered_wallets`.

The three are intentionally separate because they answer different
operator questions:

- "Is this wallet trading on insider information?" → InsiderDetector
- "Is this wallet's behaviour statistically impossible?" → AnomalyDetector
- "Which wallets cluster together; who are the whales?" → WalletIntelligence

## Key files

| Path | What it holds |
|---|---|
| [`backend/services/insider_detector.py`](../../../backend/services/insider_detector.py) | `InsiderDetectorService` (line 81); `rescore_wallets()` entry (line 91); 11-component weight table (lines 40–52); `FLAGGED_THRESHOLD=0.72`, `WATCH_THRESHOLD=0.60` (lines 54–59) |
| [`backend/services/anomaly_detector.py`](../../../backend/services/anomaly_detector.py) | `AnomalyDetector` (line 73); `analyze_wallet()` (line 86); `AnomalyType` enum (lines 17–32); `Severity` enum (lines 35–39); statistical detector (lines 327–395), pattern detector (397–469), timing detector (471–486) |
| [`backend/services/wallet_intelligence.py`](../../../backend/services/wallet_intelligence.py) | `WalletIntelligence` orchestrator (lines 2439–2506); `ConfluenceDetector` (line 68); `EntityClusterer` (lines 681–1251); `WalletTagger` (lines 1315–1575); `CrossPlatformTracker` (lines 1606–1863); `WhaleCohortAnalyzer` (lines 1899–2438) |
| [`backend/workers/tracked_traders_worker.py`](../../../backend/workers/tracked_traders_worker.py) | Calls `insider_detector.rescore_wallets()` (line 798) and `wallet_intelligence.run_full_analysis()` (line 252) on schedule |
| [`backend/api/routes_anomaly.py`](../../../backend/api/routes_anomaly.py) | `POST /analyze` (line 82), `GET /anomalies` (line 651), `GET /find-profitable` (line 623) |
| [`backend/api/routes_discovery.py`](../../../backend/api/routes_discovery.py) | `GET /wallet/{address}` (line 1662), `POST /sync/confluence` (line 1840) |

## Contracts

### InsiderDetector — composition

`insider_score` is a weighted average over 11 components. Each
component returns a value in `[0, 1]` or `None` (insufficient data);
only non-`None` components contribute to the final score. Weights
(insider_detector.py:40–52):

```
win_rate                       0.10
timing_alpha                   0.16   ← 1h/6h/24h price-move edge
roi                            0.08
brier                          0.12   ← calibration penalty (quadratic)
entry_resolution_edge          0.10   ← win-rate vs implied probabilities
position_concentration         0.07   ← wallet notional / market notional
pre_news_timing                0.12   ← lead-time before news drops
market_selection_edge          0.08   ← low-liq vs high-liq performance
drawdown_behavior              0.05
cluster_correlation            0.08   ← 15m co-trading sync with peers
funding_overlap_proxy          0.04   ← coordinated-funding proxy
                                 ----
                                 1.00
```

Final formula (`rescore_wallets()` lines 277–334):

```
weighted_sum         = Σ(weight × clamp(component, 0, 1))
available_weight_sum = Σ(weight) for non-None components
base_score           = weighted_sum / available_weight_sum
confidence_penalty   = clamp(insider_confidence / 0.75, 0, 1)
insider_confidence   = available_weight_sum × clamp(resolved_sample / 40, 0, 1)
insider_score        = round(clamp(base × confidence_penalty, 0, 1), 6)
```

Classification (lines 626–641):

| Classification | `insider_score` | `confidence` | `sample_size` |
|---|---|---|---|
| `flagged_insider` | ≥ 0.72 | ≥ 0.60 | ≥ 25 |
| `watch_insider` | ≥ 0.60 | ≥ 0.50 | ≥ 15 |
| `none` | otherwise | — | — |

Persisted columns on `discovered_wallets`
([`database.py:2493-2498`](../../../backend/models/database.py)):
`insider_score`, `insider_confidence`, `insider_sample_size`,
`insider_last_scored_at`, `insider_metrics_json`,
`insider_reasons_json`. Indexed on `insider_score`.

Scheduled by `tracked_traders_worker` every 30–60 s with
`stale_minutes=15`, `max_wallets=64` per cycle (default —
`DEFAULT_RESCORE_MAX_WALLETS`).

### AnomalyDetector — types and severities

Ten anomaly types, four severities (anomaly_detector.py:17–39):

```
AnomalyType:
  IMPOSSIBLE_WIN_RATE       — Z>4 on binomial, ≥95% win rate
  UNUSUAL_ROI               — avg ROI > 20%
  PERFECT_TIMING            — (stub)
  STATISTICALLY_IMPOSSIBLE  — zero losses on ≥20 closed positions
  FRONT_RUNNING             — (stub)
  WASH_TRADING              — buy + sell same market within 60s
  COORDINATED_TRADING       — (defined, unused in v1)
  INSIDER_PATTERN           — (defined, unused in v1)
  ARBITRAGE_ONLY            — ≥80% of trades in 1–10% ROI band
  UNUSUAL_SIZE              — (defined, unused in v1)

Severity: LOW | MEDIUM | HIGH | CRITICAL
```

Aggregate score (lines 518–532):
```
weights = {LOW:0.2, MEDIUM:0.4, HIGH:0.7, CRITICAL:1.0}
anomaly_score = min(Σ(score × weight[severity]) / count, 1.0)
```

Persisted to:
- `tracked_wallets` ([`database.py:707`](../../../backend/models/database.py)) —
  `anomaly_score`, `is_flagged`, `flag_reasons`, `last_analyzed_at`
- `detected_anomalies` ([`database.py:946`](../../../backend/models/database.py)) —
  one row per detected anomaly; indexes on `anomaly_type`,
  `wallet_address`, `severity`

API surface: `POST /api/anomaly/analyze`, `GET /api/anomaly/anomalies`,
`GET /api/anomaly/find-profitable` ([`routes_anomaly.py`](../../../backend/api/routes_anomaly.py)).
On-demand only — not run on every wallet automatically.

### WalletIntelligence — five sub-systems

All five run inside `run_full_analysis()` (lines 2455–2485).

| Sub-system | Class line | Purpose | Output table / column |
|---|---|---|---|
| `ConfluenceDetector` | 68 | scans for multiple wallets converging on the same market×side within a tight time window | `market_confluence_signals` ([`database.py:2606`](../../../backend/models/database.py)) |
| `EntityClusterer` | 681 | clusters wallets by behavioural similarity (timing, size, market preference) | `wallet_clusters` ([`database.py:2557`](../../../backend/models/database.py)); `discovered_wallets.cluster_id` |
| `WalletTagger` | 1315 | rule-based behavioural tags (`smart_predictor`, `whale`, `high_risk`, `new_talent`, etc.) | `discovered_wallets.tags` (JSON list) |
| `CrossPlatformTracker` | 1606 | matches Polymarket addresses to Kalshi accounts via behavioural fingerprint | `cross_platform_entities` ([`database.py:2688`](../../../backend/models/database.py)) |
| `WhaleCohortAnalyzer` | 1899 | builds co-trading network graph; identifies whale cohorts | `trader_groups`, `trader_group_members` ([`database.py:2748`](../../../backend/models/database.py)) |

Tag rules (lines 1371–1447):
- `smart_predictor`: total_trades ≥ 100, win_rate ≥ 0.6, total_pnl > 0, anomaly_score < 0.5
- `consistent_winner`: total_trades ≥ 50, win_rate ≥ 0.55, no anomalies
- `whale`: total_pnl > $10 K or membership in top pool
- `high_risk`: max_drawdown > 0.3 or anomaly_score > 0.6
- `new_talent`: days_active < 30, total_trades ≥ 20, win_rate > 0.6, total_pnl > 0

Cadence: `run_full_analysis()` runs ~every 30 min with timeouts per
step (confluence 45 s, tagger ~30 s, clusterer ~20 s, cross-platform
~10 s, cohorts ~15 s; ~2 min total).

### How the three services compose

```
discovered_wallets
   ▲
   │ insider_score, insider_confidence
   │
InsiderDetectorService.rescore_wallets()      — every 30–60s, batch 64
   │
   │ reads:  WalletActivityRollup (45-day window),
   │         NewsWorkflowFinding (pre-news component),
   │         DiscoveredWallet.cluster_id (cluster-correlation component)
   │ writes: discovered_wallets.insider_score / metrics / reasons
   │
   ▼
WalletIntelligence.run_full_analysis()        — every 30 min
   │
   │ reads:  discovered_wallets (incl. insider_score, anomaly_score)
   │ writes: composite_score, cluster_id, tags,
   │         market_confluence_signals, wallet_clusters,
   │         cross_platform_entities, trader_groups
   │
   ▼
AnomalyDetector.analyze_wallet()              — on-demand via API
   │
   │ reads:  Polymarket trade tape (live HTTP),
   │         tracked_wallets.last_analyzed_at
   │ writes: tracked_wallets.anomaly_score / is_flagged,
   │         detected_anomalies rows
```

`WalletIntelligence` consumes `insider_score` and `anomaly_score`
when building tags (high_risk requires `anomaly_score > 0.6`) but
does not call the other two services directly.

## Configuration

`AppSettings` columns ([`database.py:1363-1381`](../../../backend/models/database.py)):

```
discovery_trader_opps_confluence_limit          (default 50)
discovery_trader_opps_insider_limit             (default 40)
discovery_trader_opps_insider_min_confidence    (default 0.62)
discovery_trader_opps_insider_max_age_minutes   (default 180)
discovery_pool_insider_priority_threshold       (default 0.62)
```

Loaded by `tracked_traders_worker` at lines 330–345 via
`_trader_opportunity_intent_settings()`.

Component weights and detector thresholds are **hard-coded** in
their respective modules (insider_detector.py:40–52,
anomaly_detector.py:76–84). Changing them requires a code redeploy
— this is intentional, since they encode the operator's risk model.

## Dependencies (both directions)

**This stack depends on:**

- `discovered_wallets`, `tracked_wallets`, `wallet_trades`
  populated by `worker-discovery`'s
  [`wallet_discovery.py`](../../../backend/services/wallet_discovery.py)
  and [`tracked_traders_worker`](../../../backend/workers/tracked_traders_worker.py).
- `WalletActivityRollup` rows (rolling 45-day window) for
  insider scoring inputs.
- `NewsWorkflowFinding` for the `pre_news_timing` component
  (provided by `worker-news`,
  see [`worker-news.md`](worker-news.md)).
- Polymarket trade tape via `polymarket_client.get_wallet_trades()`
  for AnomalyDetector live analysis.

**Depended on by:**

- The Discovery UI (`routes_discovery.py` `/wallet/{address}`,
  `/clusters`, `/trader-network`).
- The Anomaly UI (`routes_anomaly.py`).
- The `traders/*` strategy family — `traders_confluence` reads
  `MarketConfluenceSignal`; `traders_copy_trade` reads
  `tracked_wallets.is_flagged`. See
  [`copy-trade-pipeline.md`](copy-trade-pipeline.md).
- Trader risk envelopes — operators may wire
  `discovery_pool_insider_priority_threshold` into their pool
  filters.

## Extension points

| When you want to… | Touch |
|---|---|
| Tweak insider-score weights | `insider_detector.py:40-52` (code redeploy) |
| Add a new anomaly type | `AnomalyType` enum (anomaly_detector.py:17), implement detector method, add weight in score-aggregation |
| Add a new wallet tag | `WalletTagger` (wallet_intelligence.py:1371-1447) — rules are inline |
| Tune confluence sensitivity | `MIN_WALLETS_WATCH`, `SIGNAL_DECAY_MINUTES` constants in `ConfluenceDetector` |
| Change rescore cadence | `tracked_traders_worker.py` — adjust the call interval and `stale_minutes` argument |
| Surface a new metric in the UI | `routes_discovery.py` `/wallet/{address}` payload, then the React side |

## Known footguns

- **Stale `insider_score`.** Default `stale_minutes=15`; if
  `tracked_traders_worker` is paused or backed up, scores drift
  silently. Force refresh with `stale_minutes=0`.
- **High false-positive anomaly rate** on small samples. The
  `MIN_TRADES_FOR_ANALYSIS=10` floor is the only gate. For wallets
  with 11–20 trades, a single hot streak trips
  `IMPOSSIBLE_WIN_RATE`. Raise the floor for a quieter feed.
- **Confluence under-detection.** `MIN_WALLETS_WATCH=2` is
  sensitive but high-recall. Raise to 3–4 for less noise.
- **Cluster overfitting.** `_calculate_pattern_similarity()`
  (line 1073–1078) weights timing similarity heavily; high-frequency
  wallets cluster trivially even when uncorrelated.
- **DB contention on confluence scan.** `_CONFLUENCE_EVENT_ROW_LIMIT=5000`
  can spike Postgres CPU on busy hours; lower to 2 000 if pressure
  is observed.
- **`wallet_trades` empty despite `tracked_wallets.total_trades=N`.**
  `total_trades` is the wallet's lifetime Polymarket history;
  `wallet_trades` is the local crawl cache. Same footgun as in
  [`trader-pipeline.md`](trader-pipeline.md).
- **Insider score and anomaly score are independent.** A wallet can
  have `insider_score=0.85` (flagged) and `anomaly_score=0.0` (no
  detected anomaly); they answer different questions. Don't
  collapse them in dashboards.
- **`PERFECT_TIMING` and `FRONT_RUNNING` are stubs** in v1
  (anomaly_detector.py:479, 482). Code paths exist but produce no
  detections. Surface this in any operator-facing claim.

## Test coverage

- `backend/tests/test_insider_detector.py` — classification
  thresholds, component calculations, Brier calibration
- `backend/tests/test_smart_wallet_pool_and_confluence.py` —
  confluence detection, signal decay
- `backend/tests/test_wallet_intelligence_cross_platform.py` —
  cross-platform entity matching
- `backend/tests/test_routes_discovery_trader_network.py` — API
  contract validation

## Where to look next

| Topic | File |
|---|---|
| The plane this stack runs on | [`worker-discovery.md`](worker-discovery.md) |
| `traders/*` signal family that consumes the outputs | [`copy-trade-pipeline.md`](copy-trade-pipeline.md) |
| News-edge inputs (pre-news timing component) | [`worker-news.md`](worker-news.md), [`ai-and-llm.md`](ai-and-llm.md) |
| Wallet-trade ingest source | [`worker-discovery.md`](worker-discovery.md) |
| Discovery UI walkthrough | `docs/UI_AND_DEMO_MODE.md` |

Last verified: 2026-05-08
