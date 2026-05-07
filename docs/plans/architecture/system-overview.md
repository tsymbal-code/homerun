# Architecture: System Overview

This is the bird's-eye view of Homerun. Every other architecture note
in this directory zooms into one slice of what's described here. Read
this first.

## Purpose

Homerun is a full-stack platform for building, backtesting, and
executing prediction-market trading systems against Polymarket and
Kalshi. The codebase is one repository deployed as multiple
long-running services, all coordinated by a single Postgres + Redis
pair.

The system has two operating modes that share the same code path:

- **Shadow / Sandbox** — strategies and traders run, opportunities
  are detected, but execution flows through a microstructure-aware
  fill simulator into a per-account ledger
  (`SimulationAccount`). No real money moves. This is the default.
- **Live** — execution flows through `live_execution_service` to
  Polymarket CLOB or Kalshi. Gated by an explicit account toggle in
  the UI plus credentials in `AppSettings`.

## Runtime topology

```
                     ┌─────────────────────────┐
                     │  Browser (React + Vite) │
                     │   localhost:3000        │
                     └─────────────┬───────────┘
                                   │ /api/*  /ws
                                   ▼
                     ┌─────────────────────────┐
                     │  homerun-frontend       │
                     │  nginx :3000            │
                     │  proxies /api /ws → :8000
                     └─────────────┬───────────┘
                                   │
                                   ▼
                     ┌─────────────────────────┐
                     │  homerun-backend        │
                     │  FastAPI / uvicorn :8000│
                     │  + WebSocket /ws        │
                     └──┬──────────────────┬───┘
                        │                  │
                        ▼                  ▼
            ┌────────────────┐    ┌────────────────┐
            │  postgres :5432│    │  redis :6379   │
            └────────┬───────┘    └────────┬───────┘
                     │                     │
       ┌─────────────┼─────────────────────┤
       ▼             ▼                     ▼
 ┌──────────┐  ┌──────────┐         ┌──────────┐
 │ worker-  │  │ worker-  │         │ worker-  │
 │ trading  │  │ news     │         │discovery │
 │ (hot path│  │(ML heavy)│         │(REST    │
 │ live exec│  │          │         │ heavy)  │
 │ + WS)    │  │          │         │          │
 └────┬─────┘  └────┬─────┘         └────┬─────┘
      │             │                     │
      ▼             ▼                     ▼
   Polymarket    News feeds            Wallet
   Kalshi        Weather               discovery
   Binance       Cox trainer           Backtests
   (WebSocket)
```

All services are defined in [docker-compose.yml](../../../docker-compose.yml).
The `migrate` service is one-shot and runs `alembic upgrade head`
before `backend` and the worker planes start.

## Services and their jobs

| Service | Image / entrypoint | Job |
|---|---|---|
| `postgres` | `postgres:16-alpine` | Single source of truth for everything: settings, strategies, opportunities, trades, positions, model cache, usage logs. Tuned for the 7.6 GiB production host (`shared_buffers=1.5GB`, `effective_cache_size=3GB`, `max_connections=100`, `work_mem=16MB`, `synchronous_commit=off`). `pg_stat_statements` is loaded and the matching extension is created in the `homerun` database. See [Database & Migrations](database-and-migrations.md#postgres-allocation-on-the-production-host) for the full table. |
| `redis` | `redis:7-alpine` | Volatile pub/sub: cross-plane signals, wallet state deltas, trader-event broadcast. No persistence (`--save ""` `--appendonly no`); restart wipes Redis but never loses durable state. |
| `migrate` | backend image, runs `init_database()` | One-shot. Creates schema on a fresh DB or runs `alembic upgrade head` on an existing one. The same code path also runs at FastAPI startup, so docker and the desktop launchers share one bootstrap. |
| `backend` | uvicorn `main:app` :8000 | API plane. FastAPI app, `/api/*` routes, WebSocket fan-out at `/ws`, FeedManager (Polymarket CLOB + Kalshi for price push to UI), market cache, position-mark loop, AI manager. **Does not** run worker loops. |
| `worker-trading` | `python -m workers.host trading` | Hot path. Scanner, market universe, events, fast trader runtime, trader orchestrator (general + crypto), reconciliation, redeemer, fill-simulator refresh. Owns the **exclusive** Polymarket user-channel WebSocket. |
| `worker-news` | `python -m workers.host news` | News intelligence + weather + Cox-PH fill model trainer. Loads sentence-transformers + FAISS (~2 GB heap), kept off the trading hot path. |
| `worker-discovery` | `python -m workers.host discovery` | Wallet discovery, tracked traders, provider import, strategy reverse-engineer agent, backtest worker. REST-bound; isolated to keep its retries from stalling the orchestrator event loop. |
| `frontend` | nginx :3000 | Serves the built Vite bundle and proxies `/api`, `/ws` to `backend:8000`. The React app itself is a static SPA. |

## Why three worker planes

The split is load-bearing and documented in
[docker-compose.yml:18](../../../docker-compose.yml) and
[backend/workers/host.py:18-21](../../../backend/workers/host.py).
Three independent reasons stack:

1. **Polymarket exclusivity.** The Polymarket user-channel WebSocket
   does not allow two simultaneous connections per API key — running
   the trading plane twice drops both sessions. The compose file is
   designed to launch exactly one trading worker.
2. **Memory isolation.** News + weather pull `sentence-transformers`
   and FAISS (~2 GB). Loading them into the trading plane would slow
   GC and increase event-loop pause variance on the live execution
   path.
3. **Event-loop discipline.** Discovery does many concurrent REST
   calls (~90 in-flight tasks during a typical wallet sweep). A 2026
   soak test showed 5–8 s loop stalls when this ran inside the
   trading plane. Splitting it out brings p99 active-tasks back below
   100.

`HOMERUN_WORKER_PLANE` (`trading` | `news` | `discovery`) is the
runtime switch each worker reads to gate plane-specific behaviour
(which feeds to open, which strategies to load).

## Data flow at a glance

1. **Inbound feeds** (trading plane) — Polymarket CLOB WebSocket,
   Kalshi WebSocket, Binance WebSocket, Chainlink direct REST. These
   land in `services/ws_feeds.py`, `polymarket_user_feed.py`,
   `binance_feed.py`. Updates fan out to: `PriceCache` (in-memory
   bid/ask), `WalletStateCache` (real-time order/trade state), and
   the position-mark loop.
2. **Strategy detection** — workers call `strategy_loader` which
   loads Python class strategies stored in the DB and dispatches them
   on relevant events (`market_data_refresh`, `news_update`,
   `data_source_update`). Strategies emit `Opportunity` rows.
   The market universe fed into this stage is gated by a tag-based
   ingest filter (`scanner._apply_market_tag_whitelist`) configured
   from `Settings → Scanner`; see
   [`market-filter.md`](market-filter.md) for the funnel.
3. **Opportunities → UI** — opportunities go through `shared_state`
   and surface in the React **Opportunities** tab. The WebSocket
   `opportunities` channel pushes incremental updates so the UI
   doesn't have to poll.
4. **Execution.** When a trader is started:
   - `mode='shadow'` → `simulation_service.execute_opportunity` or
     `record_orchestrator_shadow_fill` writes to `SimulationTrade` /
     `SimulationPosition`. Capital is taken from the selected
     `SimulationAccount`.
   - `mode='live'` → `live_execution_service` signs and submits to
     Polymarket CLOB (via py-clob-client) or Kalshi REST.
5. **Reconciliation & PnL** — the reconciliation worker keeps
   `TraderOrder` / `TraderPosition` in sync with venue truth, the
   redeemer claims winnings on resolved markets, and the
   position-mark loop updates unrealized PnL using `PriceCache`.
6. **AI loop (optional)** — `services/ai/*` calls `LLMManager` for
   judging opportunities, scoring news, running Cortex agent
   research. Spend is capped per month (`AppSettings.ai_max_monthly_spend`)
   and logged in `LLMUsageLog`. End-to-end map of which decisions are
   LLM vs classical ML (and how news-edge identifies a "winning
   market"): [AI & LLM](ai-and-llm.md).

## Cross-plane communication

Three channels carry state between planes:

| Channel | Transport | What rides on it |
|---|---|---|
| Settings + opportunities + strategy code | Postgres (`AppSettings`, `Opportunity`, `Strategy`, `DataSource`) | Durable, polled or change-fed by services. Strategy hot-reload uses this. |
| Real-time signals + wallet deltas | Redis pub/sub | Trader-event broadcast (UI fan-out via WebSocket bridge), wallet-state subscriptions, signal bus. |
| In-process events | `services/event_bus.py` + `event_dispatcher.py` | Within one process: a strategy reacts to `MARKET_DATA_REFRESH` without going through Redis. |

A worker plane that writes opportunities to Postgres and the API
plane that reads them never share a Python heap; the contract is
purely the database row plus a Redis nudge for "go look".

## Default ports and how to reach them

From the host (defaults in `.env`):

| Port | Service | Purpose |
|---|---|---|
| `3000` | frontend | UI (React SPA + nginx proxy to API) |
| `8000` (or `BACKEND_PORT`, e.g. `8888`) | backend | FastAPI; Swagger at `/docs`, health at `/health/live`, WebSocket at `/ws` |
| `5432` | postgres | Bound to `127.0.0.1` only |
| `6379` | redis | Bound to `127.0.0.1` only |

The backend container always reaches its sister postgres/redis on
the docker network (`postgres:5432`, `redis:6379`) regardless of host
port mapping. The frontend container always proxies to `backend:8000`
internally.

## Two deployment paths

The same backend code runs in two modes:

1. **Desktop launcher** — `scripts/launchers/Homerun.{command,bat,desktop}`
   shells into a tkinter app (`gui.py`) that boots a local Postgres
   (via Docker), creates a venv, installs npm deps, and runs the
   API + workers as host processes. Best for an operator running
   Homerun on the same machine.
2. **Docker compose** — `docker-compose.yml` ships pre-built images
   from GHCR and runs everything in containers. Best for VPS, NAS,
   or "leave it running" setups. **This is the path documented for
   plan validation.**

Both paths share the same `init_database()` bootstrap and the same
worker plane split. They differ only in process supervision.

## Boundaries and what this layer doesn't do

- **Authentication** is single-tenant. There is no user table, no
  RBAC. The optional UI lock (`UI Lock` in Settings) is a session-level
  pin code, not multi-user auth. Anyone who can reach `:3000` and
  `:8000` controls the whole stack — keep them on loopback or behind
  a VPN.
- **No queue broker** (RabbitMQ / Kafka / Celery). Background work is
  driven by long-lived asyncio loops in worker processes. Redis
  pub/sub is the closest thing to a queue, and it is fire-and-forget
  by design.
- **No sharding.** One Postgres instance per deployment. Throughput
  comes from PG tuning, not horizontal scale.
- **No CDN / object storage.** All artefacts (model weights, cached
  ML outputs) live under `./data/` volumes mounted into the backend
  and worker containers.

## Where to look next

| If you're touching… | Read |
|---|---|
| API routes, FastAPI lifespan, worker startup | [Backend Architecture](backend-architecture.md) |
| React UI, atoms, WebSocket client, state | [Frontend Architecture](frontend-architecture.md) |
| `AppSettings`, encrypted secrets, runtime hot-reload | [Settings & Secrets](settings-and-secrets.md) |
| Schema, Alembic migrations, `AsyncSessionLocal` patterns | [Database & Migrations](database-and-migrations.md) |
| AI & LLM end-to-end (what decisions, what's classical ML, how news-edge picks a market) | [AI & LLM](ai-and-llm.md) |
| Adding/changing an LLM provider | [LLM Provider Layer](llm-provider-layer.md) |
