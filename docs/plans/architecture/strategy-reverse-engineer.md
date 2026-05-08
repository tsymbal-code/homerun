# Architecture: Strategy reverse-engineer pipeline

A separate LLM-heavy pipeline that takes a Polymarket wallet
address and either writes an analytical PDF report on its
behaviour, or **iteratively synthesises a `BaseStrategy` subclass**
that mimics it. It runs on the `worker-discovery` plane (one job at
a time, atomic claim), uses its own tool registry distinct from
Cortex / Copilot, and lands its output as a row in
`strategy_reverse_engineer_jobs` plus a candidate `Strategy` that
the operator can promote.

This is not the same as Cortex (the autonomous fleet supervisor) or
the AutoResearch service (per-trader / per-strategy experiments).
The high-level placement is in
[`ai-and-llm.md`](ai-and-llm.md) § "Research and supervision";
this note is the deep dive.

## Purpose

This pipeline is responsible for:

1. Profiling a wallet — how often it trades, in what categories,
   with what edge, against which markets.
2. **`report_mode="report"`** — running a deterministic analytics
   path (statistics + LLM-drafted narratives) and rendering a PDF.
3. **`report_mode="strategy_seed"`** — running an LLM agent loop
   that proposes candidate `BaseStrategy` subclasses, backtests
   them, scores them against the wallet's actual trades, and
   iterates until either `target_score` is reached or
   `max_iterations` is exhausted.
4. Bringing in external data on demand: polybacktest snapshots
   for the wallet's market universe.
5. Persisting every iteration with full audit trail (cost, tokens,
   score breakdown, LLM critique, divergence summary).

It does **not**:

- Auto-deploy synthesised strategies. Promotion is explicit:
  `promote_to_strategy_library()` creates a `Strategy` row with
  `enabled=False`, and the operator must enable it manually.
- Run on the trading or news planes — only on `worker-discovery`.
- Touch the user's funds. It only proposes code; nothing it
  writes can place an order until the operator promotes and
  enables the strategy.

## Key files

| Path | What it holds |
|---|---|
| [`backend/services/strategy_reverse_engineer/service.py`](../../../backend/services/strategy_reverse_engineer/service.py) | `enqueue_job()` (line 34), `claim_next_queued_job()` (line 141, atomic `WITH FOR UPDATE SKIP LOCKED`), `run_job()` (line 187, dispatcher for the two modes), `promote_to_strategy_library()` (line 354) |
| [`backend/services/strategy_reverse_engineer/agent.py`](../../../backend/services/strategy_reverse_engineer/agent.py) | `run_reverse_engineer_agent(job_id)` (line 110). System prompt at lines 45–107. Wraps `services.ai.agent.Agent` with `max_iterations=max(state["max_iterations"]*4, 30)`, `temperature=0.2` |
| [`backend/services/strategy_reverse_engineer/tools.py`](../../../backend/services/strategy_reverse_engineer/tools.py) | `build_tools(ctx)` (line 76). 13 `AgentTool` definitions; each closes over `ReverseEngineerContext` |
| [`backend/services/strategy_reverse_engineer/scoring.py`](../../../backend/services/strategy_reverse_engineer/scoring.py) | `score_backtest_against_wallet()` (line 93). Composite of overlap, side_agreement, pnl_correlation, frequency_match |
| [`backend/services/strategy_reverse_engineer/wallet_profile.py`](../../../backend/services/strategy_reverse_engineer/wallet_profile.py) | `fetch_and_profile_wallet()`, `profile_trades()` |
| [`backend/services/strategy_reverse_engineer/analytical_report_runner.py`](../../../backend/services/strategy_reverse_engineer/analytical_report_runner.py) | `run_analytical_report(job_id)` (line 37) — deterministic path for `report_mode="report"` |
| [`backend/services/strategy_reverse_engineer/report_writer.py`](../../../backend/services/strategy_reverse_engineer/report_writer.py) | LLM-drafting per PDF section |
| [`backend/services/strategy_reverse_engineer/market_resolution.py`](../../../backend/services/strategy_reverse_engineer/market_resolution.py) | resolves wallet trades into `market_id`s |
| [`backend/workers/strategy_reverse_engineer_worker.py`](../../../backend/workers/strategy_reverse_engineer_worker.py) | `start_loop()` — claims one job at a time, default 5 s poll |
| [`backend/api/routes_strategy_reverse_engineer.py`](../../../backend/api/routes_strategy_reverse_engineer.py) | REST routes (prefix `/strategy-reverse-engineer`) |
| [`backend/services/external_data/polybacktest_client.py`](../../../backend/services/external_data/polybacktest_client.py) | the only client for fetching historical book data |

## Contracts

### `enqueue_job` signature

```python
async def enqueue_job(
    wallet_address: str,
    label: Optional[str] = None,
    report_mode: str = "report",            # 'report' | 'strategy_seed'
    data_source_kind: str = "auto",         # 'auto' | 'recording_session' | 'provider_dataset' | 'live'
    recording_session_ids: Optional[list[str]] = None,
    provider_dataset_ids: Optional[list[str]] = None,
    llm_model: Optional[str] = None,
    max_iterations: Optional[int] = None,
    target_score: Optional[float] = None,
    max_cost_usd: Optional[float] = None,
    max_wallet_trades: Optional[int] = None,
) -> StrategyReverseEngineerJob
```

`None` defaults are filled from `AppSettings` at claim time
([`service.py:108-133`](../../../backend/services/strategy_reverse_engineer/service.py)):
`reverse_engineer_max_iterations` (default 10),
`reverse_engineer_target_score` (default 0.7),
`reverse_engineer_max_cost_usd` (no cap by default),
`reverse_engineer_max_wallet_trades` (default 50_000), and the
per-purpose model from `llm_model_assignments['strategy_reverse_engineer']`.

### `report_mode` semantics

| Mode | Path | Output |
|---|---|---|
| `"report"` | `analytical_report_runner.run_analytical_report()` | PDF with wallet stats + LLM narrative; `best_strategy_code = NULL`. Predictable cost |
| `"strategy_seed"` | `agent.run_reverse_engineer_agent()` | Iterative agent loop; populates `best_strategy_code` / `best_strategy_class` / `best_score`. Cost varies with iteration count |

### `data_source_kind` semantics

| Kind | Behaviour |
|---|---|
| `"auto"` | No explicit `token_ids`; the engine reads whatever microstructural data is available within the wallet's time window. Best-effort; coverage gaps possible for non-crypto markets |
| `"recording_session"` | Operator picks recording session IDs; the engine unions their `token_ids` and time range |
| `"provider_dataset"` | Operator picks polybacktest dataset IDs from the catalog |
| `"live"` | Reserved; not yet implemented |

### Tool registry

13 tools, all closures over `ReverseEngineerContext`
([`tools.py:46-68`](../../../backend/services/strategy_reverse_engineer/tools.py)):

| Tool | Max calls | Role |
|---|---|---|
| `describe_objective` | 2 | Job name + dataset scope + ground rules |
| `wallet_profile_summary` | 3 | Trade counts, hour cadence, top markets |
| `wallet_market_coverage` | 6 | Covered vs uncovered markets |
| `polybacktest_find_markets` | 6 | Resolve event slugs → `market_id`s |
| `polybacktest_import` | 8 | Blocking import; updates `dataset_scope` and aliases |
| `strategy_sdk_reference` | 3 | `BaseStrategy` contract + a worked example |
| `list_existing_strategies` | 3 | Filter by `source_key` |
| `get_strategy_source` | 8 | Read full source by `strategy_id` |
| `dataset_query` | 15 | Paginated Data Lab query (microstructure, book_delta, …) |
| `dataset_sample_token` | 10 | N evenly-spaced snapshots for a token |
| `wallet_trades_window` | 10 | Slice wallet trades by limit/offset/market_id |
| `submit_strategy_candidate` | 50 | **Side-effect:** persist iteration, run backtest, score, return breakdown |
| `finalize_best` | 2 | Promote the best/override iteration as the winner |

The agent must call `submit_strategy_candidate` to score; the
loop terminates when either `score >= target_score` or
`current_iteration >= max_iterations` (count of submissions, not
of LLM turns).

### DB tables

```
strategy_reverse_engineer_jobs (database.py:3564-3643)
  id, wallet_address, label, report_mode, data_source_kind,
  recording_session_ids_json, provider_dataset_ids_json,
  llm_model, max_iterations, target_score, max_cost_usd,
  max_wallet_trades,
  status (queued | profiling | importing_data | running |
          completed | failed | cancelled),
  progress, current_iteration, activity, error,
  wallet_profile_json, wallet_trade_count,
  wallet_window_start, wallet_window_end,
  best_iteration_id, best_score, best_strategy_code,
  best_strategy_class, best_backtest_run_id,
  total_input_tokens, total_output_tokens, total_cost_usd,
  promoted_strategy_id (FK -> strategies; NULL until promoted),
  created_at, started_at, finished_at

strategy_reverse_engineer_iterations (database.py:3646-3700+)
  id, job_id (CASCADE), iteration, status,
  strategy_code, strategy_class,
  backtest_run_id, score, score_breakdown_json,
  divergence_summary, llm_critique, notes, error,
  input_tokens, output_tokens, cost_usd, duration_ms,
  created_at, completed_at
```

### REST routes (prefix `/strategy-reverse-engineer`)

| METHOD | Path | Purpose |
|---|---|---|
| `POST` | `/jobs` | `enqueue_job` from a `CreateJobRequest` Pydantic body (validated wallet, modes, bounded params) |
| `GET` | `/jobs` | List with filters: `wallet_address`, `status`, `limit` |
| `GET` | `/jobs/{job_id}` | One job |
| `POST` | `/jobs/{job_id}/cancel` | `queued`/`running` → `cancelled` |
| `DELETE` | `/jobs/{job_id}` | Hard-delete job + iterations |
| `GET` | `/jobs/{job_id}/iterations` | All iterations |
| `GET` | `/jobs/{job_id}/iterations/{iteration_id}` | One iteration |
| `POST` | `/jobs/{job_id}/promote` | `promote_to_strategy_library(...)` — creates `Strategy` row with `enabled=False` |
| `GET` | `/jobs/{job_id}/report.pdf` | PDF render (renderer chosen by `report_mode`) |

## Dependencies (both directions)

**This pipeline depends on:**

- `LLMManager` for every agent + report-writer call.
- The polybacktest external SaaS for historical book data (no
  on-prem alternative today).
- The Strategy SDK in `services/strategies/base.py` — the agent's
  `BaseStrategy` template + introspection are tied to it.
- `worker-discovery` plane being up. If the plane is down, jobs
  sit in `queued` indefinitely.

**Depended on by:**

- The "Reverse-engineer" UI tab.
- The Strategy Library (when a job is promoted, a row appears
  there with `enabled=False`).
- `AppSettings.llm_model_assignments['strategy_reverse_engineer']`
  for per-purpose model override.

## Extension points

| When you want to… | Touch |
|---|---|
| Tighten cost discipline | Wire `max_cost_usd` into `_process_iteration` so that exceeding the cap aborts the loop. (Currently the field is read but not enforced — see Footguns.) |
| Add a new tool | Append to `build_tools()` with a `max_calls` budget appropriate for its cost. Keep tool I/O small (≤ 8 KB results) — the system prompt assumes truncation |
| Support a new `data_source_kind` | Branch in `_resolve_dataset_scope()` ([`agent.py:388-460`](../../../backend/services/strategy_reverse_engineer/agent.py)); add column-level support if the kind needs persistent ID lists |
| Change the scoring weights | `scoring.py:93-100` — composite weights for overlap / side_agreement / pnl_correlation / frequency_match |
| Switch model for this purpose | Set `AppSettings.llm_model_assignments['strategy_reverse_engineer'] = "<model>"`. No code change needed |

## Known footguns

- **`max_cost_usd` is read but not enforced.** The field is loaded
  in `_load_defaults()` but no current code path aborts the loop
  when total cost crosses the cap. Treat the cap as an *intent*,
  not a guarantee, until this is wired in. Open a plan to fix
  before running unsupervised against expensive models.
- **LLM hallucinates strategy code.** Even with the system prompt
  emphasising "pure Python, no I/O imports," the agent can
  generate code that fails to import. The submit pipeline catches
  syntax/import errors and marks the iteration `failed`, but the
  agent may then loop trying near-identical variations.
- **Empty polybacktest data.** A successful import that yields
  zero snapshots is silent; backtest produces zero fills; score
  is zero on every candidate. `wallet_market_coverage` exposes
  this — the agent should filter, not iterate.
- **Wallet with too few trades.** Below ~10 trades, scoring
  signal is too weak to differentiate candidates; every score is
  near zero. There is no pre-validation in `enqueue_job`; jobs
  succeed but the result is uninformative.
- **One-at-a-time queue blocks on long jobs.** A job that
  exhausts its iterations without finalising holds the worker for
  its full duration. Operators can `cancel` from the UI; raising
  `max_iterations` is usually safer than spinning a parallel
  worker process.
- **Promotion is decoupled from approval.** `promote_to_strategy_library()`
  creates a `Strategy` row with `enabled=False`. If an operator
  reflexively flips `enabled=True` without reviewing the code, an
  LLM-authored strategy reaches live market data. The default off
  state is the only guard.

## Test coverage

No dedicated test files for `services/strategy_reverse_engineer/`
were found at scan time. The shared infrastructure
(`services.ai.agent`, `services.strategies.base`,
`services.external_data.polybacktest_client`) is covered, but the
agent loop, scoring, and analytical-report path do not yet have
unit tests of their own. Filed as future work; do not write
strategy-slug-bound tests (per `agents.md` § What NOT to Do).

## Where to look next

| Topic | File |
|---|---|
| Where this fits in the AI map | [`ai-and-llm.md`](ai-and-llm.md) |
| The plane this runs on | [`worker-discovery.md`](worker-discovery.md) |
| `BaseStrategy` contract the agent targets | [`backend-architecture.md`](backend-architecture.md) (Plug-in patterns) |
| Promotion path → live trading | [`trader-pipeline.md`](trader-pipeline.md) |
| Per-purpose model assignment | [`llm-provider-layer.md`](llm-provider-layer.md) |

Last verified: 2026-05-08
