# Architecture: Backend

The backend is one Python codebase deployed in two roles: as the
FastAPI **API plane** (one process, container `homerun-backend`) and
as three independent **worker planes** (containers
`homerun-worker-{trading,news,discovery}`). All four roles share the
same source tree, the same Postgres, and the same Redis.

For the wider runtime topology see [System Overview](system-overview.md).

## Purpose

This layer owns:

1. The HTTP/WebSocket surface the frontend talks to (`/api/*`, `/ws`).
2. Long-running asyncio loops that scan markets, run strategies,
   ingest news/weather, train the fill model, discover wallets,
   reconcile orders, and drive the live-execution service.
3. The plug-in patterns that turn DB rows into running Python:
   strategies, data sources, discovery profiles.
4. The bridges between in-process events
   (`event_bus`/`event_dispatcher`) and cross-process state
   (Redis pub/sub, Postgres rows).

It does **not** own the schema (see
[Database & Migrations](database-and-migrations.md)), the secret
storage policy (see [Settings & Secrets](settings-and-secrets.md)),
or any LLM call shape (see [LLM Provider Layer](llm-provider-layer.md)).

## Source tree

```
backend/
├── main.py                  # FastAPI app, lifespan, router mounting, /ws
├── config.py                # settings dataclass, runtime overrides loader
├── alembic/                 # 130+ migrations, alembic.ini, env.py
├── api/
│   ├── routes_*.py          # one router per domain (~40 files)
│   ├── settings_helpers.py  # apply_update_request — Pydantic→ORM mapping
│   └── websocket.py         # ConnectionManager, channels, topics
├── models/
│   └── database.py          # all SQLAlchemy models in one ~3.5k-line file
├── services/
│   ├── ai/                  # llm_provider, agent, skills, tools
│   ├── strategies/          # built-in strategy classes (one file each)
│   ├── trader_orchestrator/ # session_engine, fast_submit, risk_manager
│   ├── simulation/          # execution_simulator, fill_models
│   ├── fill_simulator/      # Cox-PH model code
│   ├── strategy_loader.py   # AST-validated hot-reload of DB-stored Python
│   ├── data_source_loader.py
│   ├── ws_feeds.py          # Polymarket CLOB / Kalshi WS clients
│   ├── polymarket_user_feed.py
│   ├── binance_feed.py
│   ├── live_execution_service.py
│   ├── live_execution_adapter.py
│   ├── shared_state.py      # cross-coro snapshots
│   ├── event_bus.py / event_dispatcher.py
│   ├── redis_client.py
│   └── ...                  # ~120 service modules
├── workers/
│   ├── host.py              # plane bootstrap: which loops run where
│   └── *.py                 # one worker loop per file (scanner.py, etc.)
└── utils/
    ├── secrets.py           # encrypt_secret / decrypt_secret (Fernet)
    ├── logger.py
    └── ...
```

Two design choices to be aware of upfront:

- **`models/database.py` is intentionally one file**, not a package.
  It contains every SQLAlchemy model in declared order. Alembic's
  `env.py` imports it as a single unit, and that fixed order matters
  for reflection and migration generation.
- **`services/` is flat by default** with a few sub-packages
  (`ai/`, `strategies/`, `trader_orchestrator/`, `simulation/`,
  `fill_simulator/`). Cross-service imports are common and acceptable
  — there is no enforced layering between services.

## API plane (`main.py`)

The FastAPI app is created at module top-level, with a
`@asynccontextmanager` lifespan that runs the full startup sequence
([main.py:237-500](../../../backend/main.py)):

1. **Database bootstrap** — `init_database()` creates the schema on a
   fresh database or runs `alembic upgrade head` on an existing one.
   This is the same code path the `migrate` service runs as a
   one-shot, so the API is idempotent against whatever the migration
   container did.
2. **Redis client** — `redis_client.start()` connects with a soft
   failover to in-memory pub/sub (the API stays usable even if Redis
   is down).
3. **Cross-plane subscriptions** — wallet state, trader events, and
   signal arrival channels are subscribed to so the UI can fan them
   out via the `/ws` endpoint.
4. **Strategy and data-source registries** — `strategy_loader.refresh_all_from_db(...)`
   and `data_source_loader.refresh_all_from_db()` warm-load the
   plug-in catalog from Postgres, filtered by the relevant
   `source_key`s for this process role.
5. **Event bus + dispatcher** — local pub/sub for in-process worker
   events.
6. **Runtime settings overrides** — `config.apply_runtime_settings_overrides()`
   pulls operator-tunable values from `AppSettings` and applies them
   over the file/env defaults. Same call is rerun whenever
   settings are saved.
7. **Orchestrator state reset** — trader orchestrator is started
   paused, in shadow mode, with no live orders armed.
8. **Market cache + AI layer** — `market_cache.warm_load()` and
   `LLMManager.initialize()`.
9. **FeedManager (API plane only)** — Polymarket CLOB + Kalshi WS
   feeds for *price push to the UI*. The user-channel (live
   trading) feed lives only in the trading worker plane.
10. **Position-mark seeding** — open positions are seeded into
    `PositionMarkState` and a price-feed callback updates unrealized
    PnL in real time.
11. **Pool watchdog** — background task that monitors PG connection
    exhaustion.

On shutdown the lifespan reverses the order, draining feeds and
flushing the LLM usage log.

### Routers

All routers are mounted at `/api` in [main.py:1059-1095](../../../backend/main.py).
The naming convention is one router per domain in `api/routes_<domain>.py`.
A non-exhaustive list:

- **Markets / opportunities / scanner**: `routes_simulation`,
  `routes_search`, `routes_signals`.
- **Strategies / data sources**: `routes_strategies`,
  `routes_data_sources`, `routes_validation`,
  `routes_strategy_reverse_engineer`.
- **Traders / orchestrator**: `routes_traders`, `routes_trader_sources`,
  `routes_trader_orchestrator`, `routes_orchestrator_live`,
  `routes_operator`.
- **AI**: `routes_ai`, `routes_agents`, `routes_cortex`,
  `routes_autoresearch`.
- **Backtests / ML**: `routes_backtest`, `routes_dataset`,
  `routes_fill_model`, `routes_ml`.
- **Workers / health / maintenance**: `routes_workers`,
  `routes_maintenance`.
- **Settings / providers**: `routes_settings`, `routes_providers`
  (the latter is for **external data providers**, not LLM).

Cross-cutting middleware:

- `CORSMiddleware` — open by default for local dev.
- `InboundAPIRateLimiter` (token bucket per client) at
  [main.py:121-187](../../../backend/main.py).
- A response interceptor that normalises UTC timestamps so the
  frontend can render them consistently.

### WebSocket

The single WebSocket endpoint is `/ws` ([main.py:1112-1128](../../../backend/main.py)).
A `ConnectionManager` ([backend/api/websocket.py](../../../backend/api/websocket.py))
holds the connections and routes outbound messages by **channel** and
**topic**:

- Channels: `core`, `opportunities`, `trading`, `crypto`, `weather`,
  `news`, `events`, `workers`, `signals`, `wallet`.
- Topics: wildcards (`*`, `*:*`) plus named topics
  (`opportunities.summary`, `position_marks`, `prices:{market_id}`).

Clients send `{type: "subscribe", channels: [...], topics: [...]}`
to filter what they receive. Producers (workers, services) push via
`connection_manager.broadcast(message)`. The connection check is
gated by the UI-lock service — locked sessions get a 4403 close code.

## Worker planes (`workers/host.py`)

A worker process is started by `python -m workers.host <plane>`. The
plane name is also written to `HOMERUN_WORKER_PLANE` for downstream
services to read.

[`host.py`](../../../backend/workers/host.py) defines a static
plane-to-loop map:

| Plane | Workers / loops it starts |
|---|---|
| `trading` | `market_universe`, `scanner`, `scanner_slo`, `search_index`, `events`, `trader_reconciliation`, `fast_trader_runtime`, `redeemer`, `fill_simulator_refresh`. Plus runtimes: `trader_orchestrator` (general lane) and `trader_orchestrator_crypto`. Owns Polymarket / Kalshi / Binance / Chainlink WS feeds. |
| `news` | `news_worker`, `weather_worker`, `cox_trainer_worker`. No orchestrator — pure signal generation. Loads the `news_edge` and `weather` strategies only. |
| `discovery` | `discovery_worker`, `tracked_traders_worker`, `provider_import_worker`, `strategy_reverse_engineer_worker`, `backtest_worker`. Loads only `traders`-class strategies. |

Each loop is a long-lived `asyncio.Task` with its own polling cadence.
There is no central scheduler — back-pressure is an emergent property
of `asyncio.sleep`s and Redis backlog.

The trading plane also runs the FeedManager bound to the user
channel, which is why running the trading container twice is unsafe
(see [System Overview](system-overview.md)).

## Plug-in patterns

Two plug-in systems share the same shape: source code is stored as
text in a Postgres row, validated, compiled, and instantiated at
load time. Only the table and base class differ.

### Strategy hot-reload

Source: [services/strategy_loader.py](../../../backend/services/strategy_loader.py).
Backing table: `strategies` (`models/database.py`). Per row:
`slug`, `source_key` (`scanner` / `news` / `weather` / `traders` /
`crypto` / `manual`), `name`, `class_name`, `source_code` (Python
text), `config` (JSON), `enabled`, `status`, `error_message`.

Pipeline:

1. **AST validation** — `ast.parse(source_code)` plus an allow-list of
   imports (`models`, `services.strategies`, `utils`, stdlib, `httpx`,
   `numpy`). Forbidden: `os.system`, `subprocess`, `socket`, `pickle`,
   `eval`, `exec`, `__import__`, `compile`, `open`. Required methods
   are checked: at least one of `detect`, `detect_async`, `evaluate`,
   `should_exit`.
2. **Compile + instantiate** — a fresh `types.ModuleType` is filled
   via `exec(code, namespace)` with no `__builtins__` of its own,
   then the class is instantiated with `(strategy_type=slug,
   config=config_dict)`.
3. **Register** — the instance is keyed by slug in `self._loaded`
   and subscribed to the relevant events on
   `event_dispatcher`.
4. **Errors** — invalid or failing strategies persist their
   `error_message` and `status="error"` into the DB row. The UI
   surfaces this in the **Strategies** tab. The loader keeps running.

The catalog is fully replaced on every `refresh_all_from_db()`. There
is no partial reload — instead, the loader holds a lock so workers
block on refresh rather than running mid-unload.

The same `source_key` filter lets each plane load only its slice:
the trading plane skips `news` / `weather` to keep the heap small.

### Data source hot-reload

Source: [services/data_source_loader.py](../../../backend/services/data_source_loader.py).
Backing table: `data_sources`. Per row: `slug`, `source_key`,
`source_kind` (`python` / `rss` / `rest_api`), `name`, `class_name`,
`source_code`, `config`, `enabled`, `status`, `error_message`.

The validation flow is identical to strategies but the contract is
`BaseDataSource` instead of `BaseStrategy`. Built-in source kinds
(`rss`, `rest_api`, `twitter`, `chainlink`, `binance_ws`) are
selected by `source_kind`; `python` runs operator-supplied code
through the same AST guard.

A loaded source's `fetch_async()` returns dicts that get persisted
to `data_source_records`, and strategies receive them either via
the `EventType.DATA_SOURCE_UPDATE` event or by calling
`StrategySDK.get_data_records(source_slug=...)` directly.

## Cross-process messaging

Three transports are in use, each with a distinct purpose:

| Transport | Where | Use case |
|---|---|---|
| Postgres rows + polling | Everywhere | Durable state. Opportunities, positions, settings, strategy code. Workers do their own polling cadence; there is no change-data-capture. |
| Redis pub/sub | `services/redis_client.py`, `signal_bus_redis_bridge.py`, `wallet_rtds_feed.py`, the trader-events bridge | Volatile cross-plane signals. Examples: a worker publishes "wallet X had a trade" → API plane bridges it to the `/ws` `wallet` channel. |
| `event_bus.py` + `event_dispatcher.py` | Within one process | Strategies subscribe to events like `MARKET_DATA_REFRESH`, `DATA_SOURCE_UPDATE`. No serialisation overhead, no cross-plane reach. |

Picking the right transport is rarely ambiguous: durable state goes
to Postgres, cross-plane fan-out to Redis, intra-process strategy
events to the event bus.

## Dependencies (both directions)

**This layer depends on:**

- Postgres + Redis services (both healthchecked before the API
  starts; the API gates on `service_healthy` in compose).
- `APP_SECRETS_KEY` env var (no startup if missing — encryption can't
  initialise).
- External venues for live mode: Polymarket CLOB, Kalshi REST/WS,
  Polygon RPC, Chainlink Data Streams, Binance.
- HuggingFace (one-time, optional) for the news / sentence-transformer
  model download. Disabled by default beyond the first boot.

**Depended on by:**

- The frontend (`/api/*`, `/ws`).
- The desktop launcher (`gui.py` and `scripts/launchers/*`).
- The Telegram notifier, when configured.
- External operators using the Swagger UI at `/docs` for ad-hoc
  scripting (sandbox account creation today goes through this path).

## Extension points

| When you want to… | Touch |
|---|---|
| Add a new HTTP route | New `api/routes_<domain>.py`, include it in `main.py` router list. |
| Add a new WebSocket channel/topic | Extend `_message_channel()` / `_message_topic()` in `api/websocket.py` and the producer that broadcasts. |
| Add a new background loop | New file in `workers/`, register it in the right plane in `workers/host.py`. Don't put background loops in `main.py`. |
| Add a built-in strategy | New file in `services/strategies/`. Restart workers (or reload from UI). |
| Add a per-purpose AI model | Update `AppSettings.llm_model_assignments` JSON; consumer reads it and passes `model=` to `manager.chat`. |
| Add a new external feed (a new venue) | Mirror the structure of `binance_feed.py` or `ws_feeds.py`; register the start in the trading plane's `host.py`. |

## Known footguns

- **Don't run two trading planes.** The Polymarket user-channel WS
  drops both. compose enforces one; only one process should ever set
  `HOMERUN_WORKER_PLANE=trading`.
- **`asyncio.sleep(0)` is not yielding.** Several scanner code paths
  use `await asyncio.sleep(0)` to give the loop a tick — this works
  but obscures intent. New code should prefer
  `await asyncio.sleep(0.001)` or explicit cooperative points.
- **Don't spawn LLM calls inside the WebSocket broadcast path.** That
  path holds a connection-set lock; long calls there starve every
  other client.
- **`init_database()` is idempotent but slow on first run** because
  alembic walks the full migration list. Containers wait on the
  `migrate` service to complete to avoid duplicate work.

Last verified: <unverified>
