# Architecture: worker-news plane

This is the news-and-weather plane: one of the three worker
processes Homerun launches alongside `worker-trading` and
`worker-discovery`. It owns the heavy ML stack
(sentence-transformers + FAISS, ~2 GB heap), the LLM-driven news
workflow, the weather pipeline, and the Cox-PH fill-model trainer.
Crucially, it **never executes trades** — its outputs reach the
trading plane through `trade_signals` rows in Postgres.

For the LLM-vs-classical-ML map this plane embodies, read
[`ai-and-llm.md`](ai-and-llm.md) first. For the trader pipeline
that consumes its outputs, see
[`trader-pipeline.md`](trader-pipeline.md).

## Purpose

This plane is responsible for:

1. Pulling news articles and event data from external feeds (RSS,
   GDELT, ACLED, OpenSky, etc.) and persisting them in
   `news_articles`.
2. Building and refreshing the **FAISS embedding index** of active
   markets so news articles can be matched to markets by semantic
   similarity (sentence-transformers).
3. Running the **news workflow orchestrator** — a budgeted pipeline
   that combines classical ML retrieval with optional LLM stages
   (event extraction, reranking, edge estimation) to produce
   `NewsWorkflowFinding` and `NewsTradeIntent` rows.
4. Bridging high-conviction `NewsTradeIntent` rows into
   `trade_signals` (`source='news'`) so traders on the trading
   plane can consume them.
5. Running the **weather pipeline** (`worker-news` also owns
   `weather_worker`) and the **Cox-PH fill-model trainer**
   (`cox_trainer_worker`), both of which need pandas/scipy and
   would bloat the trading plane.

It does **not**:

- Execute trades (`initialize_live_execution: False`).
- Open feed-driven WebSocket sessions (Polymarket / Kalshi /
  Binance live feeds live on `worker-trading`).
- Consume `trade_signals` (orchestrator on `worker-trading`).
- Run the position-mark loop or reconciliation worker.

## Key files

| Path | What it holds |
|---|---|
| [`backend/workers/host.py:159-193`](../../../backend/workers/host.py) | `_PLANE_CONFIGS["news"]` — the plane manifest: which worker modules start, which strategy `source_keys` load, `initialize_live_execution=False` |
| [`backend/workers/news_worker.py`](../../../backend/workers/news_worker.py) | `_run_loop()` — coordinator. Reads control + `AppSettings`, leases the cycle, calls the orchestrator, emits intents to signals |
| [`backend/workers/weather_worker.py`](../../../backend/workers/weather_worker.py) | weather sources → `WeatherForecast`/`WeatherWorkflowFinding`/`WeatherTradeIntent` |
| [`backend/workers/cox_trainer_worker.py`](../../../backend/workers/cox_trainer_worker.py) | `train_and_persist()` every `HOMERUN_COX_TRAIN_INTERVAL_SECONDS` (default 6 h). Lives here because it needs pandas/scipy. Inference is read-side only and runs hot on `worker-trading` |
| [`backend/services/news/workflow_orchestrator.py`](../../../backend/services/news/workflow_orchestrator.py) | `WorkflowOrchestrator.run_cycle()` — the seven-stage pipeline (`CycleBudget`, `StageBudgetTracker` at lines 69–150) |
| [`backend/services/news/feed_service.py`](../../../backend/services/news/feed_service.py) | `news_feed_service.fetch_all()` — driver per registered `DataSource` (RSS / REST) |
| [`backend/services/news/article_clusterer.py`](../../../backend/services/news/article_clusterer.py) | `ArticleClusterer.cluster()` — dedupe by similarity before downstream stages spend tokens |
| [`backend/services/news/market_watcher_index.py`](../../../backend/services/news/market_watcher_index.py) | FAISS + sentence-transformers index. Lazy-loads the model on first cycle (~15–30 s warmup); `NEWS_FAISS_THREADS=1` to avoid PyTorch thread-local segfaults |
| [`backend/services/news/semantic_matcher.py`](../../../backend/services/news/semantic_matcher.py) | embedding-based article ↔ market matching |
| [`backend/services/news/hybrid_retriever.py`](../../../backend/services/news/hybrid_retriever.py) | weighted blend of semantic + keyword + event scores |
| [`backend/services/news/reranker.py`](../../../backend/services/news/reranker.py) | optional LLM rerank stage (after a regex-based entity-overlap pre-filter at lines 34–76) |
| [`backend/services/news/event_extractor.py`](../../../backend/services/news/event_extractor.py) | optional LLM event extraction (~50 % of cycle's LLM budget) |
| [`backend/services/news/edge_estimator.py`](../../../backend/services/news/edge_estimator.py) | optional LLM edge estimation (~20 %) — produces `WorkflowFinding.model_probability_yes` |
| [`backend/services/news/edge_detector.py`](../../../backend/services/news/edge_detector.py) | the simpler `NewsEdge` dataclass for the in-process strategy path |
| [`backend/services/news/intent_generator.py`](../../../backend/services/news/intent_generator.py) | `IntentGenerator.generate()` — `NewsWorkflowFinding` → `NewsTradeIntent` |
| [`backend/services/strategy_signal_bridge.py`](../../../backend/services/strategy_signal_bridge.py) | `bridge_opportunities_to_signals(source="news")` — the only path from this plane into `trade_signals` |

## Contracts

### Cycle budget (LLM)

`CycleBudget` ([`workflow_orchestrator.py:69`](../../../backend/services/news/workflow_orchestrator.py)) carries:

```python
llm_available: bool                    # global ai_enabled + provider OK + workflow_enabled
global_spend_remaining_usd: float      # ai_max_monthly_spend - month-to-date
cycle_spend_cap_usd: float             # news_workflow_cycle_spend_cap_usd
hourly_spend_cap_usd: float            # news_workflow_hourly_spend_cap_usd
hourly_news_spend_usd: float           # rolling spend in last hour
cycle_llm_call_cap: int                # news_workflow_cycle_llm_call_cap
llm_calls_used: int = 0
llm_calls_skipped: int = 0
estimated_cycle_spend_used_usd: float = 0.0
```

`reserve_calls(N)` (line 86) returns the number of calls the stage
actually gets, applying all four ceilings. `StageBudgetTracker`
(line ~132) splits the cycle cap roughly **50 % event extraction,
30 % reranking, 20 % edge estimation**, reallocating leftovers
downstream.

### Output rows

| Table | What it holds | Cited in |
|---|---|---|
| `news_articles` | raw + clustered articles (in-memory cache + DB persist) | [`database.py:826`](../../../backend/models/database.py) |
| `news_workflow_findings` | per `(article × market)` finding with all retrieval/rerank/edge fields | [`database.py:874`](../../../backend/models/database.py) |
| `news_trade_intents` | execution-ready intents (`signal_key` deduped) | [`database.py:914`](../../../backend/models/database.py) |
| `news_workflow_snapshots` | worker status + stats (`last_scan_at`, `next_scan_at`, `degraded_mode`) | [`shared_state.py:61`](../../../backend/services/news/shared_state.py) |
| `weather_*` | weather pipeline equivalents | `database.py` |
| `trade_signals` (source='news') | bridged via `emit_news_intent_signals()` | [`news_worker.py:514`](../../../backend/workers/news_worker.py) |
| `llm_usage_log` | every LLM call, with `purpose='news_workflow:event_extraction'` / `:rerank` / `:edge_estimation` | `database.py:2223` |

### `AppSettings` columns this plane reads

```
news_workflow_enabled                       1565   master switch
news_workflow_auto_run                      1566
news_workflow_scan_interval_seconds         1584
news_workflow_top_k                         1567   retrieval breadth
news_workflow_rerank_top_n                  1568
news_workflow_similarity_threshold          1569
news_workflow_keyword_weight                1570
news_workflow_semantic_weight               1571
news_workflow_event_weight                  1572
news_workflow_min_keyword_signal            1576
news_workflow_min_semantic_signal           1577
news_workflow_min_edge_percent              1578
news_workflow_min_confidence                1579
news_workflow_require_verifier              1573
news_workflow_require_second_source         1580
news_workflow_market_min_liquidity          1574
news_workflow_market_max_days_to_resolution 1575
news_workflow_orchestrator_enabled          1581   findings → intents
news_workflow_orchestrator_min_edge         1582
news_workflow_orchestrator_max_age_minutes  1583
news_workflow_model                         1585   per-pipeline model override
news_workflow_cycle_spend_cap_usd           1586   $ per cycle
news_workflow_hourly_spend_cap_usd          1587   $ per rolling hour
news_workflow_cycle_llm_call_cap            1588   call count per cycle
news_workflow_cache_ttl_minutes             1589
news_workflow_max_edge_evals_per_article    1590
news_rss_feeds_json                         1591
news_gov_rss_feeds_json                     1592
events_acled_api_key                        1595
events_opensky_username/password            1597-1598
events_aisstream_api_key                    1599
events_cloudflare_radar_token               1600
events_gdelt_news_enabled                   1619
weather_workflow_*                          1627-1641
```

## Dependencies (both directions)

**This plane depends on:**

- `LLMManager` (`services.ai`) for all LLM stages. Off-mode
  (`ai_enabled=False`) gracefully degrades to pure ML retrieval —
  findings still appear, intents do not.
- `sentence-transformers` + `faiss` (or TF-IDF fallback when FAISS
  is absent on Windows). ~2 GB resident set when warm.
- External feeds: registered `DataSource` rows pointing at
  RSS/REST/Twitter sources.
- Postgres schema in [`database-and-migrations.md`](database-and-migrations.md).
- The same `bridge_opportunities_to_signals` plumbing that the
  trading plane uses for in-process publishes — but this plane's
  bridge call is purely a DB write.

**Depended on by:**

- `worker-trading` orchestrator, which polls `trade_signals` for
  `source='news'` rows.
- The frontend News tab (`/api/news/*` endpoints, `news_update`
  WebSocket message type).
- `worker-trading` again for **Cox-PH model artifacts** —
  `cox_trainer_worker.train_and_persist()` is the producer; the
  active model row in `fill_probability_models` is consumed by
  `services/fill_simulator/cox_inference.py` on the trading hot
  path. See [`execution-and-fills.md`](execution-and-fills.md).

## Extension points

| When you want to… | Touch |
|---|---|
| Add a new news source | Insert a `DataSource` row of kind `rss`/`api` and let `feed_service.fetch_all()` pick it up |
| Add a new LLM stage to the cycle | Edit `WorkflowOrchestrator.run_cycle()` and budget it via `StageBudgetTracker`; do **not** call the LLM unbudgeted |
| Change the intent threshold | `news_workflow_orchestrator_min_edge`, `news_workflow_min_confidence` |
| Train a new Cox-PH model | `cox_trainer.train_and_persist(window_days=N)` — bumps `generation` on promotion |
| Tighten/loosen news-stale gate | `news_workflow_orchestrator_max_age_minutes` (default 120) |

## Known footguns

- **First cycle is slow.** Sentence-transformers + FAISS lazy-load
  takes 15–30 s. The cycle has a 30 s timeout
  ([`workflow_orchestrator.py:475`](../../../backend/services/news/workflow_orchestrator.py)) — on
  cold-start the first cycle can time out and the next succeeds.
  Look for `degraded_mode=true` in `news_workflow_snapshots`.
- **`NEWS_FAISS_THREADS=1` is load-bearing.** Removing it triggers
  PyTorch thread-local segfaults on FAISS rebuild.
  ([`host.py:46`](../../../backend/workers/host.py)).
- **Budget exhaustion is silent on the trade path.** When
  `ai_max_monthly_spend` is reached, LLM stages skip, findings
  still write but with no `model_probability_yes` →
  `edge_percent=null` → no `NewsTradeIntent` → news-edge bots stop
  trading. Diagnose via `LLMUsageLog` totals
  + `CycleBudget.llm_calls_skipped`.
- **`quality_passed=null` on news signals.** The async quality
  filter on this plane processes signals after they land. Bots
  with `firehose_require_qualified_source=true` reject `null`-state
  signals; if this plane is overloaded, signals stay `null`
  indefinitely. See
  [`trader-pipeline.md`](trader-pipeline.md) Step 3.
- **RSS throttling.** `feed_service.fetch_all()` returns empty,
  cycle reports zero clusters → no findings. Check the
  `DataSource.last_error` column.
- **Backpressure deferral.** When the trading plane reports DB
  pressure, the news cycle is **deferred**, not killed
  ([`news_worker.py:275`](../../../backend/workers/news_worker.py)). Persistent
  backpressure manifests as `next_scan_at` drifting forward
  without a corresponding `last_scan_at` advance.
- **No Redis publishes.** This plane never publishes to Redis. The
  contract with the trading plane is a Postgres-only handoff via
  `trade_signals`. If you "need a Redis nudge," you're solving the
  wrong problem — fix the polling on the trading plane instead.

## Test coverage

- `backend/tests/test_news_worker_loop.py` — main loop control
- `backend/tests/test_news_workflow_routes.py` — API routes
- `backend/tests/test_news_workflow_accuracy_guards.py` — budget
  guardrails, LLM call discipline
- `backend/tests/test_news_intent_generator.py` — `IntentGenerator`
- `backend/tests/test_news_article_clusterer.py`
- `backend/tests/test_news_feed_quality_guards.py`
- `backend/tests/test_news_feed_routes.py`
- `backend/tests/test_news_matching_precision_eval.py` — retrieval
  precision against `news_eval_dataset.jsonl`
- `backend/tests/test_weather_workflow_orchestrator_tradability.py`

## Where to look next

| Topic | File |
|---|---|
| AI/LLM end-to-end map (which stage is LLM, which is ML) | [`ai-and-llm.md`](ai-and-llm.md) |
| LLM transport layer (providers, key save) | [`llm-provider-layer.md`](llm-provider-layer.md) |
| What happens after `trade_signals` row exists | [`trader-pipeline.md`](trader-pipeline.md) |
| Cox-PH inference (consumer side, on `worker-trading`) | [`execution-and-fills.md`](execution-and-fills.md) |
| Three-plane runtime overview | [`system-overview.md`](system-overview.md) |
| `worker-discovery` plane (the third plane) | [`worker-discovery.md`](worker-discovery.md) |

Last verified: 2026-05-08
