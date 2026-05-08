# Architecture: Database & Migrations

The data layer is one PostgreSQL 16 database accessed via async
SQLAlchemy + asyncpg, with three engine pools and one `Base` metadata
shared across the entire codebase. Schema changes go through Alembic
in a versioned linear history. There is no read replica, no sharding,
and no ORM-level multi-tenancy — single-tenant by design.

## Purpose

This layer owns:

1. The schema for every persistent entity in Homerun: settings,
   markets, opportunities, simulation accounts, trader orders,
   strategies, data sources, ML caches, usage logs.
2. The async session factories used by every other layer
   (`AsyncSessionLocal`, `FastAsyncSessionLocal`,
   `AuditAsyncSessionLocal`).
3. Schema evolution via Alembic — both the migration files and the
   `init_database()` bootstrap path that the API and the `migrate`
   compose service share.
4. Connection-level resilience: TCP keepalives, retryable session
   wrapper, statement / lock / idle-in-transaction timeouts.

It does **not** own business logic — every model is a passive
container. Even row mutations sit in service modules
(`simulation.py`, `trader_orchestrator/*`, `strategy_loader.py`),
not on the model classes.

## Key files

| Path | What it holds |
|---|---|
| [backend/models/database.py](../../../backend/models/database.py) | All ORM models in declared order, three async engines, three session factories, `init_database()`, `RetryableAsyncSession`, FastAPI `Depends()` helper `get_db_session` |
| [backend/models/model_registry.py](../../../backend/models/model_registry.py) | `register_all_models()` — explicit registration so Alembic autogenerate sees every model |
| [backend/alembic.ini](../../../backend/alembic.ini) | Alembic config — `script_location`, default URL (overridden at runtime by `config.settings.DATABASE_URL`) |
| [backend/alembic/env.py](../../../backend/alembic/env.py) | Online + offline runners, points at `Base.metadata`, calls `register_all_models()` |
| [backend/alembic/versions/](../../../backend/alembic/versions/) | 130+ migration files; current head is `202605060001` |
| [backend/utils/retry.py](../../../backend/utils/retry.py) | `is_retryable_db_error(exc)` — recognises serialization conflicts, deadlocks, transient connection issues |

## The `Base` and the singleton schema file

`models/database.py` is **one file** containing every model. This is
intentional and supported by two practices:

- **Declared order matters.** Foreign-key referents must precede
  referrers; SQLAlchemy autogenerate uses declared order for
  reflection. Reordering the file is a schema change in disguise.
- **`register_all_models()`** is the safety net. It explicitly imports
  every model class so Alembic autogenerate sees them even if Python
  hasn't otherwise touched their module. Both `init_database()` and
  `alembic/env.py` call it before doing anything.

Splitting the file (a tempting refactor) would require carefully
preserving import order in `model_registry.py` — the file size is
the cost we pay for "just open one file to see the schema."

## Three engines, three session factories

Three engine pools, all on the same database URL, sized differently
for their workloads:

| Factory | Engine pool | Purpose |
|---|---|---|
| `AsyncSessionLocal` | `async_engine` (default size from settings) | Default for almost everything: API request handlers, worker loops, background tasks |
| `FastAsyncSessionLocal` | `fast_async_engine` (smaller, low-latency) | Hot path: scanner ticks, price updates, things that must complete in sub-100ms |
| `AuditAsyncSessionLocal` | `audit_async_engine` (small, dedicated) | Append-only writes: `LLMUsageLog`, `RuntimeStatus` history, anything that should not contend with read traffic |

All three use `class_=RetryableAsyncSession`,
`expire_on_commit=False`. The split exists because long-running
backtest queries on `AsyncSessionLocal` would otherwise starve the
scanner; isolating the hot path on a small pool keeps the loop
predictable.

### Postgres allocation on the production host

The compose `postgres` service is sized for the production host
`polyhome-1` (7.6 GiB total RAM, no swap). The relevant `command:`
flags in [docker-compose.yml](../../../docker-compose.yml) are:

| Setting | Value | Why |
|---|---|---|
| `shared_buffers` | `1536MB` | ~20% of host RAM. Empirically holds the entire working set: cache hit pct stays at 99%+ once buffer pool is warm. |
| `effective_cache_size` | `3GB` | Truthful hint to the planner: shared_buffers + page cache. Setting it above total RAM (the previous `10GB`) biased the planner toward index scans that miss cache. |
| `work_mem` | `16MB` | Caps peak risk: `max_connections × work_mem` = 1.6 GB worst case. |
| `maintenance_work_mem` | `256MB` | VACUUM / CREATE INDEX rare on this DB. |
| `max_connections` | `100` | Observed peak ~78 across all engine pools combined; 100 leaves ~28% headroom. |
| `effective_io_concurrency` | `32` | Tuned for the QEMU-backed SSD on this host (the previous `200` is for NVMe RAID). |
| `autovacuum_max_workers` | `3` | 4-vCPU host — 3 vacuum workers leave headroom for the trader workers. |
| `max_wal_size` | `2GB` | Faster checkpoint recovery than the previous `4GB`; disk space is not the constraint. |
| `synchronous_commit` | `off` | Acceptable for shadow / single-tenant workload; documented for live awareness. |
| `shm_size` (Docker) | `2g` | shared_buffers (1.5 GB) + working room (0.5 GB). |

If the cache hit ratio drops below 95% sustained, bump
`shared_buffers` to `2GB`. Don't go below 1 GB on PG 16 — planner
overhead becomes noticeable.

### `pg_stat_statements`

`shared_preload_libraries=pg_stat_statements` is loaded by the
postgres container. The matching SQL view is created once via
`CREATE EXTENSION IF NOT EXISTS pg_stat_statements;` and persists in
the `homerun` database afterwards. It is the canonical slow-query
view — sample query:

```sql
SELECT query, calls, round(mean_exec_time::numeric, 1) AS mean_ms
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 10;
```

Stats reset on Postgres restart. Wait at least 10 minutes after a
redeploy before reading them for meaningful aggregation.

### Connection settings (per-connection)

`async_engine`'s `connect_args` set:

- `timezone='UTC'` — all timestamps are UTC. The frontend
  re-localises them via the axios interceptor.
- `statement_timeout` — caps any single query (default 60 s, settable
  via `DATABASE_STATEMENT_TIMEOUT_MS`).
- `lock_timeout` — caps how long we wait for a row lock (default 5 s).
- `idle_in_transaction_session_timeout` — reaps abandoned txns
  (default 120 s).
- `tcp_keepalives_idle/interval/count` — server-side keepalive so
  Postgres reaps half-dead clients within ~90 s.

A SQLAlchemy `connect` event listener
([database.py:4736](../../../backend/models/database.py)) also turns
on TCP keepalive on the **client** raw socket, since asyncpg doesn't
expose a connect-time keepalive parameter. This makes both ends of
the connection notice partition / NIC failure within ~90 s.

## `RetryableAsyncSession`

Defined in [database.py:119](../../../backend/models/database.py).
It extends `AsyncSession` with two behaviours:

1. **Cancellation-safe close.** A coroutine cancellation during
   `commit()` / `rollback()` could leak a half-open connection back
   to the pool; this wrapper schedules a deferred `invalidate()` so
   the connection is destroyed instead of reused.
2. **Selective retry hook.** `is_retryable_db_error()` from
   `utils/retry.py` recognises serialization failures, deadlocks,
   asyncpg `ConnectionDoesNotExistError`, and a few PG-error codes.
   Services like the simulation account creator
   ([simulation.py:80](../../../backend/services/simulation.py))
   use it explicitly to retry transient lock conflicts under
   write-heavy load.

The session does **not** retry every operation automatically — the
contract is that callers detect `OperationalError` and decide.
Auto-retrying writes would risk double-application; the explicit
opt-in keeps semantics clear.

## `init_database()` and the migration boot path

[`init_database()`](../../../backend/models/database.py) at
`models/database.py:5504` is the single bootstrap entry point. It:

1. Calls `register_all_models()` to load every model class.
2. Opens a connection on `async_engine` with up to 30 retries
   (1 s apart) — handles the race where Postgres is still booting
   when the API starts.
3. Runs `command.upgrade(alembic_cfg, "head")` against that
   connection, which applies any pending migrations.

This same function is the entrypoint of:

- The `migrate` compose service:
  `python -c "import asyncio; from models.database import init_database; asyncio.run(init_database())"`
- The FastAPI lifespan startup
  ([main.py:271](../../../backend/main.py)).

Because both share the path, you cannot end up in a state where the
container says the schema is up to date but the API disagrees.
`alembic upgrade head` is idempotent — calling it twice at the same
revision is a no-op.

`get_db_session` (line 5537) is the FastAPI `Depends()` helper. Every
route that needs a session injects it; its `finally` block guarantees
rollback on cancel and close in all cases.

## Alembic conventions

The repository follows a few project-specific conventions on top of
vanilla Alembic:

### Filenames

`<YYYYMMDDNN>_<short_slug>.py` for date-stamped migrations:

```
202605060001_backtest_run_jobs.py
202605070001_add_nvidia_nim_columns.py
```

The `NN` suffix lets multiple migrations land on the same date.
Hash-style names (`9069a6422cfd_…`) appear when migrations come from
upstream branches and have been merged with a `merge_migration_heads`
revision (`77a2c87e00b2_merge_migration_heads.py`).

### Linear history

The repo aims to keep a linear chain. When two branches add
migrations independently and both reach `main`, a merge migration
unifies them
([`77a2c87e00b2_merge_migration_heads.py`](../../../backend/alembic/versions/77a2c87e00b2_merge_migration_heads.py)).
Before starting a plan that adds a migration, run:

```bash
docker compose exec backend alembic heads
```

If there is more than one head, the plan must include a merge
migration step (or be rebased) — never let the tree fork.

### The "guard for existing column" pattern

Many repository migrations (especially those that re-establish
columns that drifted between schema and migration history) use a
defensive guard:

```python
def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    existing = _column_names("app_settings")
    columns = [
        sa.Column("nvidia_api_key", sa.String(), nullable=True),
        sa.Column("nvidia_base_url", sa.String(), nullable=True),
    ]
    for col in columns:
        if col.name not in existing:
            op.add_column("app_settings", col)
```

[`202603200001_add_openrouter_columns.py`](../../../backend/alembic/versions/202603200001_add_openrouter_columns.py)
is the canonical reference for this pattern. Use it for any migration
that adds nullable columns to `app_settings` — this table accumulates
columns and the guard makes re-applies safe.

### `downgrade()` is usually a no-op

Most repository migrations leave `downgrade()` as `pass`. Operator
deployments are forward-only; a real downgrade would risk losing
operator-stored values. Plans should not rely on `alembic downgrade`
working.

## Async patterns in the rest of the codebase

The recurring shape, used everywhere:

```python
from models.database import AsyncSessionLocal

async with AsyncSessionLocal() as session:
    result = await session.execute(select(Model).where(...))
    entity = result.scalar_one_or_none()
    if entity is None:
        entity = Model(...)
        session.add(entity)
    entity.field = value
    await session.commit()
```

Conventions to follow:

- **One session per logical unit of work.** Don't pass sessions
  across coroutines; create a fresh `async with AsyncSessionLocal()`
  in each.
- **`expire_on_commit=False` is the project default.** That means
  attribute access after commit returns the in-memory value without
  a refetch. Combine with explicit `await session.refresh(obj)`
  when fresh server-side data is needed.
- **Read-modify-write under contention** must use
  `is_retryable_db_error(exc)` and bounded retries. The simulation
  account creator is the reference implementation.
- **For FastAPI route handlers**, prefer
  `session: AsyncSession = Depends(get_db_session)` over creating
  sessions in the body — that integrates with FastAPI's request
  lifecycle (cancellation propagates to rollback).

## Indices and explain plans

Hot tables carry composite indices declared on the model:

```python
__table_args__ = (
    Index("idx_position_account", "account_id"),
    Index("idx_position_market", "market_id"),
)
```

When adding a new query path, add an index in the same migration as
the schema change. The Postgres tuning in
[docker-compose.yml](../../../docker-compose.yml) (low
`random_page_cost=1.1`, modest `effective_io_concurrency=32` matching
the QEMU SSD) assumes indexed access patterns; full table scans on
million-row tables will hit `statement_timeout`.

## Dependencies (both directions)

**This layer depends on:**

- `config.settings.DATABASE_URL` — the asyncpg-style URL.
- `cryptography` (for `utils/secrets.py`, used by encrypted columns).
- Postgres 16 (compose pins this; older versions will work for most
  things, but PG 16 features like `pg_stat_statements` are loaded by
  the compose config).

**Depended on by:**

- Every service in `backend/services/`.
- Every API route in `backend/api/`.
- Every worker in `backend/workers/`.
- The `migrate` container in compose.
- The desktop launcher (`gui.py` runs the same `init_database()`
  before starting the API).

## Extension points

| When you want to… | Touch |
|---|---|
| Add a column to an existing table | Add it to the model in `database.py`, generate or hand-write an Alembic migration with `op.add_column` (use the guard pattern for `app_settings`), set `down_revision` to the current head. |
| Add a new table | Add the model class to `database.py`, register it in `model_registry.py`, generate the migration. Add appropriate indices in the same migration. |
| Add a new query in a hot loop | Use `FastAsyncSessionLocal` rather than `AsyncSessionLocal`. Cap with `statement_timeout`. Add an index covering the WHERE clause if the table has more than ~10k rows. |
| Add an audit / append-only log | Use `AuditAsyncSessionLocal`. Don't add it to the default pool — bursts will starve other readers. |
| Detect transient failures | `from utils.retry import is_retryable_db_error` and wrap with bounded backoff. Don't retry inside the open transaction; rollback first. |

## Known footguns

- **Don't autogenerate migrations on a stale head.** Run
  `alembic upgrade head` first, then `alembic revision --autogenerate`
  — otherwise the generated diff includes prior changes.
- **Don't `import` from `models.database` lazily inside hot loops.**
  The first import triggers engine creation and pool warm-up.
  Always module-level.
- **Don't assume `expire_on_commit` defaults to True.** It's
  explicitly `False` here. After commit, `obj.field` returns the
  in-memory value, not a refetched one. If you need server defaults
  populated on the object, use `await session.refresh(obj)`.
- **Don't open a session inside another open session's context.**
  Nested `async with AsyncSessionLocal()` calls each grab a separate
  pool connection. Under load, that doubles the pool footprint and
  invites deadlocks.
- **`op.add_column` on `app_settings` without the guard pattern** is
  fragile because the table has accumulated drift over time. Always
  use the inspect-then-add pattern for `app_settings`.
- **Migration filenames are sortable but not strictly chronological.**
  Two contributors creating `202605070001_*.py` simultaneously will
  conflict. Prefer the `YYYYMMDDNN` form and bump `NN` to avoid
  collisions; if you collide, rebase rather than write a merge
  migration.

Last verified: <unverified>
