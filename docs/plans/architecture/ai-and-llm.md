# Architecture: AI & LLM in Homerun

This is the bird's-eye view of **how AI is used end-to-end** in
Homerun: which decisions are made by an LLM, which decisions are made
by classical ML (embeddings + ranking), where research happens, and
how a "winning market" is identified before a position is opened.

It is a map, not a deep-dive. Each component has a more detailed file
under `services/ai/`, `services/news/`, or this directory; the
"Where to look next" table at the bottom routes you there. The closest
neighbour notes are
[`llm-provider-layer.md`](llm-provider-layer.md) (the LLM transport
layer) and [`trader-pipeline.md`](trader-pipeline.md) (what happens
**after** AI has produced a signal).

## TL;DR — what AI actually decides

Homerun runs as **classical-ML-first, LLM-second**. The hot path
(scanner → strategy → orchestrator → trader → order) is deterministic
Python and runs without a single LLM call. AI is used in three
narrow, opt-in roles:

1. **News-driven probability estimation** — for one strategy
   (`news_edge`) the LLM converts a news article into an estimated
   `P(YES)` for a related market; the price gap drives the trade.
2. **Per-opportunity second opinion** — the `OpportunityJudge` scores
   any opportunity on viability / resolution / execution / efficiency
   axes and is consulted on demand from the UI or the scanner.
3. **Research and supervision** — UI-driven helpers (Market
   Analyzer, Resolution Analyzer, AI Copilot) and an autonomous
   supervisor (Cortex) that watches the fleet, recalls long-term
   memories, and can pause traders or tighten risk clamps.

Every LLM call is logged to `LLMUsageLog` with cost, model, and
purpose. Every feature has a flag in `AppSettings`. The whole layer
is throttled by a single `ai_max_monthly_spend` ceiling and a
per-cycle call cap on the news pipeline.

## Two AI tracks running side-by-side

Homerun mixes two very different families of AI in one product, and
keeping them apart is the prerequisite for reading the rest of this
note:

### Track A — Classical ML (embeddings, indexes, rankers)

Cheap, deterministic, runs every news cycle. **No LLM tokens spent.**

| Component | What it does | LLM? |
|---|---|---|
| [`services/news/semantic_matcher.py`](../../../backend/services/news/semantic_matcher.py) | sentence-transformers + FAISS K-NN over (article, market) pairs; TF-IDF fallback when FAISS is unavailable | No |
| [`services/news/market_watcher_index.py`](../../../backend/services/news/market_watcher_index.py) | rebuilds the embedding index of active markets each cycle | No |
| [`services/news/article_clusterer.py`](../../../backend/services/news/article_clusterer.py) | groups duplicate stories before they enter the funnel | No |
| [`services/news/hybrid_retriever.py`](../../../backend/services/news/hybrid_retriever.py) | weighted blend of semantic / keyword / event scores | No |
| [`services/news/reranker.py`](../../../backend/services/news/reranker.py) — *entity-overlap pre-filter* (lines 34–76) | regex-based NER throws away obviously mismatched pairs (e.g. baseball article × basketball market) before any LLM is called | No |
| [`services/fill_simulator/`](../../../backend/services/fill_simulator/) | Cox-PH fill model used in shadow mode to decide whether a limit price would have executed | No (statistical) |

These components sit in the data path of every news cycle and are
why Homerun can run at zero LLM cost when the operator turns AI off.

### Track B — LLM-powered (token-based reasoning)

Used selectively, behind feature flags, with budget gates.

| Component | Purpose | Default model |
|---|---|---|
| [`services/ai/opportunity_judge.py`](../../../backend/services/ai/opportunity_judge.py) | LLM-as-judge: scores arbitrage opportunities on 4 axes, returns recommendation | `ai_default_model` (gpt-4o-mini) |
| [`services/ai/resolution_analyzer.py`](../../../backend/services/ai/resolution_analyzer.py) | analyses market resolution rules for ambiguities, edge cases, key dates | `ai_default_model` |
| [`services/ai/news_sentiment.py`](../../../backend/services/ai/news_sentiment.py) | structured sentiment + outcome implications for a topic | `ai_default_model` |
| [`services/ai/market_analyzer.py`](../../../backend/services/ai/market_analyzer.py) | ReAct agent that researches a single market via tools (resolution, news, liquidity, correlation, history) | `ai_default_model` |
| [`services/ai/agent.py`](../../../backend/services/ai/agent.py) | **base ReAct loop** with tool-calling, scratchpad, configurable `max_iterations` — every agent in the system extends this | — |
| [`services/news/reranker.py`](../../../backend/services/news/reranker.py) — *LLM rerank stage* | re-orders top-K (article × market) candidates by semantic relevance after the cheap pre-filter | `news_workflow_model` or `ai_default_model` |
| [`services/news/edge_estimator.py`](../../../backend/services/news/edge_estimator.py) | turns a (article, market) pair into a structured `model_probability_yes` + confidence + reasoning JSON | `news_workflow_model` |
| [`services/ai/skills/`](../../../backend/services/ai/skills/) + [`tools/`](../../../backend/services/ai/tools/) | tool registry for Cortex / Copilot / Market Analyzer (recall/remember, fleet status, pause trader, fetch market, etc.) | — |
| [`services/strategy_reverse_engineer/`](../../../backend/services/strategy_reverse_engineer/) | a separate LLM-heavy pipeline that backtests-and-revises candidate strategies to mimic a wallet's behaviour | `reverse_engineer_*` settings |
| [`services/autoresearch_service.py`](../../../backend/services/autoresearch_service.py) | per-trader / per-strategy auto-research experiments (param tuning, A/B) | — |

Both tracks are wired together in
[`services/news/workflow_orchestrator.py`](../../../backend/services/news/workflow_orchestrator.py),
the central pipeline that produces news-edge signals.

## Component map

```
                                     ┌──────────────────────────────┐
                                     │        LLMManager            │
                                     │  (process-wide singleton)    │
                                     │  llm_provider.py             │
                                     │  - chat / structured_output  │
                                     │  - 9 providers, key rotation │
                                     │  - LLMUsageLog ledger        │
                                     └─────────────┬────────────────┘
                                                   │
            ┌──────────┬────────────┬──────────────┼──────────────┬─────────────┬──────────────┐
            │          │            │              │              │             │              │
            ▼          ▼            ▼              ▼              ▼             ▼              ▼
      Opportunity  Resolution   News-          Market        News-Workflow   Cortex      Strategy-RE
        Judge      Analyzer    Sentiment       Analyzer       Orchestrator   (autonomous   (offline,
                                                                              fleet         heavy
                                                                              supervisor)   pipeline)

            │          │            │              │              │              │            │
            ▼          ▼            ▼              ▼              ▼              ▼            ▼
        opportunity research    research      research        trade_signals    pauses      strategy
        judgments   sessions    sessions      sessions        (news source)    traders /   candidates
        (DB)        (DB)        (DB)          (DB)                              edits       + reports
                                                                                clamps
```

## Data flow: from raw market to a trade decision

The pipeline below shows what happens to a Polymarket / Kalshi market
**when AI is allowed to participate**. Stages with no LLM run
unconditionally; stages with LLM are budget-gated.

```
┌─ ingest ────────────────────────────────────────────────────────────────────┐
│ Polymarket REST + WebSocket                                                 │
│ Kalshi REST + WebSocket                                                     │
│ Binance WebSocket (BTC/ETH ref price)                                       │
│ News feeds: GDELT, NewsAPI, RSS, Twitter                                    │
│ Weather / sports / on-chain data sources                                    │
└────────────────────────────┬────────────────────────────────────────────────┘
                             │
                             ▼
                Tag-based market filter (no AI)             ← STAGE 0
                scanner._apply_market_tag_whitelist
                see [market-filter.md]
                             │
                             ▼
┌────────────────────────────┴───────────────────────────────────────────────┐
│ For news-edge specifically, worker-news runs the news pipeline:            │
│                                                                            │
│   Article ingest                                                           │
│     │                                                                      │
│     ▼                                                                      │
│   article_clusterer (no AI)                                                │
│     │                                                                      │
│     ▼                                                                      │
│   event_extractor (LLM, budget-gated)         ← optional, ~50% of cycle    │
│     │                                              budget                  │
│     ▼                                                                      │
│   semantic_matcher: FAISS K-NN over markets (no AI)                        │
│     │                                                                      │
│     ▼                                                                      │
│   hybrid_retriever: keyword + semantic + event blend (no AI)               │
│     │                                                                      │
│     ▼                                                                      │
│   reranker entity-overlap pre-filter (regex, no AI)                        │
│     │                                                                      │
│     ▼                                                                      │
│   reranker LLM stage (LLM, budget-gated)      ← optional, ~30% of cycle    │
│     │                                              budget                  │
│     ▼                                                                      │
│   edge_estimator (LLM)                        ← optional, ~20% of cycle    │
│     produces model_probability_yes,              budget                    │
│     confidence, reasoning, evidence                                        │
│     │                                                                      │
│     ▼                                                                      │
│   NewsWorkflowFinding rows  (per (article, market) pair)                   │
│     │                                                                      │
│     ▼                                                                      │
│   NewsTradeIntent rows      (only when edge_percent ≥ orchestrator_min     │
│     │                        AND confidence ≥ min_confidence AND           │
│     │                        ≥ min_supporting_articles AND ≤ max_age)      │
│     ▼                                                                      │
│   trade_signals row (source='news', strategy_type='news_edge')             │
└────────────────────────────┬───────────────────────────────────────────────┘
                             │
                             ▼
                trade_signals (any source)                     ← STAGE 1
                             │
                             ▼
              quality_filter sets quality_passed (no AI)        ← STAGE 2
                             │
                             ▼
            orchestrator routes signal to matching trader      ← STAGE 3
                             │
                             ▼
            strategy.detect_async — for news_edge:
              re-checks min_edge_percent, min_confidence,      ← STAGE 4
              min_supporting_articles, min_supporting_sources,
              max_signal_age_minutes, require_verifier
                             │
                             ▼
        [optional] llm_verify_trades: orchestrator-level LLM   ← STAGE 4a
        verification gate (off by default)
                             │
                             ▼
         risk_manager gates → trader_decisions row             ← STAGE 5
                             │
                             ▼
         execution_session → trader_orders / simulation_trades ← STAGE 6
```

The "From signal to order" half (stages 1–6) is the trader pipeline
described in [`trader-pipeline.md`](trader-pipeline.md). This note
focuses on what happens **before** stage 1 — the AI work that turns
raw market data and news into a `trade_signals` row.

## "Winning market" identification — what each layer decides

A market becomes a candidate for a position by clearing a chain of
filters. Each filter has a clear, persisted contract. Read the chain
top-to-bottom: a market that fails any rung does not appear in the
next.

### Rung 1 — Tag whitelist (every market)

A market enters the catalog only if its tags pass
`scanner._apply_market_tag_whitelist`. This is purely category-based
(politics / sports / crypto / etc.), no AI involvement. Configured
in **Settings → Scanner**. Funnel details:
[`market-filter.md`](market-filter.md).

### Rung 2 — Strategy detection (per-strategy gates)

The market is fed to whatever strategies subscribe to its source. The
strategy itself is the first hard filter; its `detect`/`detect_async`
returns `Opportunity` rows or nothing. **Most strategies do not use
LLM here.** The exceptions:

- [`news_edge`](../../strategies/news-edge.md) — see Rung 2a below.
- Some strategies expose an *optional* `llm_verify_trades` hook in
  the orchestrator (default off); when enabled, the LLM is consulted
  before submission. See `trader-pipeline.md` line 156 for the wiring.

### Rung 2a — News-edge LLM gate (only for `news_edge`)

For a market to clear the `news_edge` strategy:

| Field | Source | Default | Set in |
|---|---|---|---|
| `model_probability_yes` | LLM (`edge_estimator` / `edge_detector`) | — | per call |
| `market_price_yes` | live order book mid | — | live |
| `edge_percent` | `\|model_prob - market_price\| × 100` | — | per finding |
| `confidence` | LLM-self-reported | — | per call |
| `min_edge_percent` | strategy gate | `5.0` | `NEWS_EDGE_DEFAULT_CONFIG` |
| `min_confidence` | strategy gate | `0.45` | same |
| `orchestrator_min_edge` | trade-intent gate (stricter) | `10.0` | same |
| `min_supporting_articles` | strategy gate | `2` | same |
| `min_supporting_sources` | strategy gate | `2` | same |
| `max_signal_age_minutes` | strategy gate | `60` | same |
| `require_verifier` | strategy gate | `True` | same |

A `NewsWorkflowFinding` is produced when the first six gates pass; a
`NewsTradeIntent` (and therefore a `trade_signals` row) only when the
intent gates also pass. Code: `services/strategies/news_edge.py:126`,
config defaults at lines 59–68.

The `news_workflow_*` columns on `AppSettings` (lines 1565–1590,
[`database.py`](../../../backend/models/database.py)) override these
strategy defaults at runtime — operators tune them in the news
panel without code changes.

### Rung 3 — Opportunity Judge (optional, per-opportunity)

Once an `Opportunity` exists (from any strategy), the
[`OpportunityJudge`](../../../backend/services/ai/opportunity_judge.py)
can score it on demand:

```python
class OpportunityJudge:                       # line 295
    async def judge_opportunity(              # line 312
        self,
        opportunity: Opportunity,
        resolution_analysis: dict | None = None,
        ml_prediction: dict | None = None,
        model: str | None = None,
        force_llm: bool = False,
    ) -> dict
```

Returns a structured JSON with `overall_score`, four sub-axes
(`profit_viability`, `resolution_safety`, `execution_feasibility`,
`market_efficiency`), `recommendation` (`strong_execute` / `execute`
/ `review` / `skip` / `strong_skip`), `reasoning`, and `risk_factors`.
Persisted to `opportunity_judgments` for the UI's
"AI Analysis" tab.

The judge is **gated by `ai_opportunity_scoring`** (default `True`)
and **not** invoked automatically for every opportunity by default —
the scanner uses an ML classifier first and consults the LLM only on
ambiguous cases or when the operator explicitly clicks "Judge".

### Rung 4 — Risk manager (no AI)

The orchestrator's risk gates decide `selected` / `skipped` /
`blocked`; this is plain Python (per-trader exposure, max orders per
cycle, kill-switch, self-crossing-quote, etc.). No AI involvement.
See `trader-pipeline.md` Step 6.

### Rung 5 — Execution simulator (Cox-PH, no LLM)

In shadow mode, the Cox-PH fill simulator decides whether the chosen
limit price would have executed at the chosen size. This is a
trained statistical model, not an LLM. See
[`fill_simulator/`](../../../backend/services/fill_simulator/).

A market that has cleared all five rungs is now an open shadow
position or a submitted live order.

## Research and supervision (LLM-driven, off the trade hot path)

These components do **not** sit in the trade pipeline. They run on
demand or on schedule and produce information the operator (or
Cortex) consumes.

### Market Analyzer — single-market deep research

[`services/ai/market_analyzer.py`](../../../backend/services/ai/market_analyzer.py)
is a ReAct agent (built on `agent.Agent`, `max_iterations=8`) with
tools that fetch market metadata, search news, inspect order book,
detect correlated markets, and pull historical prices. Invoked from
the UI via `POST /api/ai/markets/analyze`. Each run creates a row in
`research_sessions`.

### Resolution Analyzer — rules audit

[`services/ai/resolution_analyzer.py`](../../../backend/services/ai/resolution_analyzer.py)
turns the market's resolution criteria text into a structured audit
(`clarity_score`, `risk_score`, `ambiguities`, `edge_cases`,
`key_dates`, `resolution_likelihood`, `recommendation`). Cached for
24 h in `resolution_analyses`. Useful before holding through
resolution. UI: `POST /api/ai/markets/{id}/analyze-resolution`.

### News Sentiment — topic-level mood

[`services/ai/news_sentiment.py`](../../../backend/services/ai/news_sentiment.py)
does a Google-News RSS pull plus a structured sentiment call,
returning `overall_sentiment`, `sentiment_score` (-1..1),
`confidence`, `market_impact`, `key_takeaways`. Used on demand from
the UI and as a sub-tool inside Market Analyzer. Distinct from the
news-edge pipeline: this is *generic topic mood*, not market-pair
edge estimation.

### AI Copilot & Chat — operator-facing assistants

`POST /api/ai/chat-sessions/*` plus the React Copilot drawer expose
a free-form chat session backed by `Agent` with the project's tool
registry. Useful for "explain why this opportunity was rejected" or
"summarise positions opened today." Sessions persist in
`chat_sessions`; tool calls are visible in the transcript.

### Cortex — autonomous fleet supervisor

[`services/ai/agent.py`](../../../backend/services/ai/agent.py) +
[`tools/cortex_tools.py`](../../../backend/services/ai/tools/) +
[`api/routes_cortex.py`](../../../backend/api/routes_cortex.py).

Cortex is a scheduled ReAct agent (`cortex_interval_seconds`,
default 300 s) with a **mandate**: read the fleet's recent
performance, compare against persisted memories, and either record
new observations or — if `cortex_write_actions_enabled=True` —
mutate trader / strategy state via tools.

Tools available to Cortex (subset; full list in
`services/ai/tools/cortex_tools.py`):

| Tool | Effect |
|---|---|
| `cortex_recall` | fetch the most relevant memories for the current run |
| `cortex_remember` | save an observation / lesson / rule / preference |
| `cortex_expire_memory` | mark a memory stale |
| `cortex_get_fleet_status` | snapshot of traders and strategies |
| `cortex_pause_trader` | pause a trader (write action) |
| `cortex_enable_strategy` | toggle a strategy on a trader (write action) |
| `cortex_update_risk_clamps` | tighten / loosen risk limits (write action) |
| `cortex_get_autoresearch_status` | check ongoing autoresearch experiments |

Persistence:

- **`cortex_memory`** (`database.py:2266`) — observations, lessons,
  rules, preferences with `importance`, `access_count`, expiry.
- **`cortex_run_log`** (`database.py:2299`) — every run's full
  trace, actions taken, learnings saved, tokens, cost, model.

Configuration columns on `AppSettings` (lines 1644–1652): see the
table in "Budget, model assignment, kill switches" below. UI route:
`POST /cortex/stream` (Server-Sent Events for live progress);
memories CRUD at `/cortex/memory[/{id}]`.

### AutoResearch — per-trader / per-strategy experiments

[`api/routes_autoresearch.py`](../../../backend/api/routes_autoresearch.py)
+ [`services/autoresearch_service.py`](../../../backend/services/autoresearch_service.py)
runs scoped LLM-driven experiments: tune one trader's parameters,
A/B-test a strategy's params, or kick off a strategy-level study.
Endpoints are scoped per resource (`/autoresearch/stream/{trader_id}`,
`/autoresearch/strategy/{strategy_id}/stream`,
`/autoresearch/strategy/{strategy_id}/params/stream`). Each endpoint
streams via SSE.

### Strategy Reverse-Engineer — wallet-mimicry pipeline

[`services/strategy_reverse_engineer/`](../../../backend/services/strategy_reverse_engineer/)
is a separate, heavy LLM pipeline that takes a wallet address and
either:

- writes an analytical report (`report_mode="report"`), or
- iterates through `submit_strategy_candidate` → `polybacktest_*` →
  `score` → revise loops until a candidate matches the wallet
  (`report_mode="strategy_seed"`), then registers it as a
  `Strategy` row.

Bounded by `reverse_engineer_max_iterations`,
`reverse_engineer_target_score`, `reverse_engineer_max_cost_usd`,
`reverse_engineer_max_wallet_trades` on `AppSettings`. Runs on
`worker-discovery`, never on `worker-trading`.

## Budget, model assignment, kill switches

Three nested controls bound spend, top-down:

### 1. Master AI switch

| Column | Default | Effect |
|---|---|---|
| `ai_enabled` | `False` | Hard off — every AI subsystem skips its LLM stage. |

This is the kill switch. With it off, `news_edge` never produces
edges (no `model_probability_yes`), opportunity judging is unavailable,
Cortex does not fire. The non-LLM ML track (semantic matcher,
clusterer, hybrid retriever) still runs.

### 2. Per-feature toggles

| Column | Default | Disables |
|---|---|---|
| `ai_resolution_analysis` | `True` | Resolution Analyzer endpoint and cache |
| `ai_opportunity_scoring` | `True` | Opportunity Judge calls |
| `ai_news_sentiment` | `True` | News sentiment endpoint |
| `news_workflow_enabled` | `True` | Whole news pipeline (track A and B) |
| `news_workflow_orchestrator_enabled` | `True` | Conversion of findings → `NewsTradeIntent` (signals stop reaching traders) |
| `cortex_enabled` | `False` | Cortex agent loop |
| `cortex_write_actions_enabled` | `False` | Cortex can read/observe but cannot mutate |

### 3. Spend ceilings

| Column | Default | What it limits |
|---|---|---|
| `ai_max_monthly_spend` | `50.0` USD | Global monthly LLM cost ceiling. Read by `LLMManager` against `LLMUsageLog` running sum. |
| `news_workflow_cycle_spend_cap_usd` | `0.25` USD | One news cycle |
| `news_workflow_hourly_spend_cap_usd` | `2.0` USD | News pipeline per rolling hour |
| `news_workflow_cycle_llm_call_cap` | `30` | Max LLM calls in one news cycle |
| `news_workflow_max_edge_evals_per_article` | `6` | Per-article fan-out cap on edge estimation |

### 4. Model selection

Defaults:

| Column | Default | Used by |
|---|---|---|
| `ai_default_model` | `gpt-4o-mini` | All consumers unless overridden |
| `ai_premium_model` | `gpt-4o` | High-stakes / explicit "premium" requests |
| `news_workflow_model` | `null` (falls back to default) | News pipeline (rerank + edge estimation) |
| `cortex_model` | `null` (falls back to default) | Cortex |
| `llm_model_assignments` | `{}` | Per-purpose JSON map: `{"news_edge": "...", "opportunity_judge": "...", ...}` |
| `llm_enabled_features` | `{}` | Per-feature on/off JSON map |

The LLM-provider routing layer behind these strings is documented in
[`llm-provider-layer.md`](llm-provider-layer.md). Adding a provider
requires no changes here — the feature/budget settings see all
providers through the unified `LLMManager`.

### 5. CycleBudget — the news pipeline's own enforcer

[`services/news/workflow_orchestrator.py:69`](../../../backend/services/news/workflow_orchestrator.py)
defines `CycleBudget`. Every news cycle constructs one with:

- `llm_available` — global flag (combines `ai_enabled` + provider
  status + `news_workflow_enabled`)
- `global_spend_remaining_usd` — `ai_max_monthly_spend` minus
  month-to-date spend
- `cycle_spend_cap_usd`, `hourly_spend_cap_usd`,
  `cycle_llm_call_cap`

Stages call `budget.reserve_calls(N)`; the budget returns how many
calls the stage actually gets. A `StageBudgetTracker` divides the
cycle cap across event extraction (~50%), reranking (~30%), and edge
estimation (~20%), reallocating leftovers downstream.

## Usage tracking and observability

Every `LLMManager.chat` / `structured_output` call writes a row to
**`llm_usage_log`** (`database.py:2223`):

```
id, provider, model,
input_tokens, output_tokens, cost_usd,
purpose,           -- 'opportunity_judge', 'resolution_analysis',
                   --   'news_sentiment', 'market_analysis',
                   --   'cortex_agent', 'strategy_reverse_engineer',
                   --   'news_workflow:edge_estimation', etc.
session_id,        -- FK → research_sessions when applicable
requested_at, latency_ms, success, error
```

Indexes: `(provider)`, `(model)`, `(requested_at)`,
`(requested_at, success)`, `(purpose)`.

Surfaces:

- **AI Activity tab** (`AIActivityView.tsx`) — request count, token
  totals, estimated cost vs `ai_max_monthly_spend`, per-model
  breakdown, success rate, latency p50/p95.
- **`GET /api/ai/usage`** and **`GET /api/ai/usage/log`** — JSON
  endpoints that power the UI.
- **`research_sessions`** (`database.py:2000+`) — top-level row per
  agent run with `thinking_trace`, `tool_calls`, `result_json`,
  `cost_usd`. Linked from `LLMUsageLog.session_id`.
- **`cortex_run_log`** (`database.py:2299`) — Cortex-specific run
  history with full thinking log and a list of actions taken.

To answer "did the AI cost spike yesterday?":

```sql
select date_trunc('hour', requested_at) as h, purpose,
       sum(input_tokens + output_tokens) as toks,
       round(sum(cost_usd)::numeric, 4) as usd
from llm_usage_log
where requested_at > now() - interval '24 hours'
group by 1, 2 order by 1 desc, usd desc;
```

## Known footguns

- **Hallucinated probabilities.** `model_probability_yes` from a
  small/fast model can be confidently wrong. The defence stack is
  `min_confidence=0.45`, `min_supporting_articles=2`,
  `min_supporting_sources=2`, plus the optional `require_verifier`
  second pass. Lowering any of these turns news-edge into noise.
- **News already in the price.** A story that has been on the news
  cycle for hours is reflected in the market. The
  `max_signal_age_minutes=60` gate is the lower bound; consider
  tightening to 15–30 min during fast-moving events.
- **Single-source bias.** A single high-authority outlet
  (Bloomberg / Reuters / a known crypto Twitter account) is *not*
  enough by itself; `min_supporting_sources=2` is the default for a
  reason. Operators sometimes drop it during testing and forget to
  raise it.
- **`quality_passed=null` blocks news-edge bots.** The asynchronous
  quality filter on `worker-news` may not have caught up; bots with
  `firehose_require_qualified_source=true` reject `null` signals.
  See `trader-pipeline.md` § Footguns for the full diagnostic.
- **Budget exhaustion is silent on the trade path.** When
  `ai_max_monthly_spend` is reached, news-pipeline LLM stages are
  skipped — the *non*-LLM stages keep producing findings, but with
  no `model_probability_yes`, edge cannot be computed and no signal
  appears. The symptom is "news_edge stopped trading" with no error
  log; check `LLMUsageLog` totals first.
- **Cortex with `write_actions_enabled` can pause traders.** This
  is by design, but easy to forget. The first sign is `is_paused=true`
  appearing on a trader the operator did not pause manually — check
  `cortex_run_log.actions_taken`.
- **`ai_enabled=False` does not stop the news-pipeline ML side.**
  The semantic matcher, FAISS index, clusterer, and hybrid retriever
  still run on `worker-news`. They are part of the *non*-LLM track.
  To stop everything news-related, also flip `news_workflow_enabled`
  off.
- **`ai_premium_model` is opt-in.** Code paths that want premium
  reasoning have to ask for it explicitly; nothing escalates to it
  on its own. Setting it without a code consumer is harmless but
  also useless.

## Where to look next

| Topic | File |
|---|---|
| LLM transport: providers, routing, key save lifecycle | [`llm-provider-layer.md`](llm-provider-layer.md) |
| What happens after a `trade_signals` row exists | [`trader-pipeline.md`](trader-pipeline.md) |
| News-edge strategy operator reference (Ukrainian) | [`../../strategies/news-edge.md`](../../strategies/news-edge.md) |
| Tag-based market intake filter | [`market-filter.md`](market-filter.md) |
| Settings, secrets, encrypted API keys | [`settings-and-secrets.md`](settings-and-secrets.md) |
| Database schema, migrations | [`database-and-migrations.md`](database-and-migrations.md) |
| Three-plane runtime (`worker-trading`, `worker-news`, `worker-discovery`) | [`system-overview.md`](system-overview.md) |
| News pipeline plane (where most of Track A + B above runs) | [`worker-news.md`](worker-news.md) |
| Wallet-mimicry agent loop (deep dive of Strategy Reverse-Engineer) | [`strategy-reverse-engineer.md`](strategy-reverse-engineer.md) |
| Live feeds, UI `/ws`, Redis pub/sub | [`websocket-and-events.md`](websocket-and-events.md) |
| AutoResearch (per-trader / per-strategy LLM experiments) | [`backend/services/autoresearch_service.py`](../../../backend/services/autoresearch_service.py) — has its own routes at `/api/autoresearch/*`; no dedicated arch-note yet |
| Cortex tool registry and skills | [`backend/services/ai/skills/`](../../../backend/services/ai/skills/), [`backend/services/ai/tools/`](../../../backend/services/ai/tools/) — referenced from this note's "Cortex" section |

Last verified: 2026-05-08
