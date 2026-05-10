# Architecture: Testing

The repository has two test surfaces with very different maturity:
the **backend has 195 pytest files** with real Postgres integration
and a 60 s global timeout enforced in CI; the **frontend has no test
runner at all** and relies on `tsc --noEmit` plus `npm run build`
for static safety. This note maps both, plus the CI workflows that
run them.

## Purpose

This layer answers two operational questions:

1. *"How do I add a test for this change?"* — what framework, where
   to put the file, which fixtures to lean on, when to use a real
   Postgres versus a mock.
2. *"How do I trust this is exercised in CI?"* — which workflow
   runs it, what timeouts apply, what the failure mode looks like
   on a PR.

It does **not** prescribe coverage targets, write strategies for
new test types (e.g. introducing Vitest on the frontend), or define
what "good enough" means — those are decisions for individual
plans.

## Key files

| Path | What it holds |
|---|---|
| [backend/pyproject.toml](../../../backend/pyproject.toml) | `[tool.pytest.ini_options]` — `asyncio_mode = "strict"`, `timeout = 60`, registered markers `unit` / `db` / `slow` (Plan 0019) |
| [backend/tests/conftest.py](../../../backend/tests/conftest.py) | 364 lines: two autouse fixtures + ~14 domain fixtures |
| [backend/tests/postgres_test_db.py](../../../backend/tests/postgres_test_db.py) | `build_postgres_session_factory()` — per-test isolated DB via asyncpg |
| [backend/tests/test_main_lifespan_smoke.py](../../../backend/tests/test_main_lifespan_smoke.py) | Plan 0019 — verifies `import main`, `app` shape, full `lifespan` startup/shutdown via subprocess against a throwaway DB |
| [backend/tests/test_alembic_roundtrip.py](../../../backend/tests/test_alembic_roundtrip.py) | Plan 0019 — head migration `downgrade → upgrade` round-trip on a stamped DB |
| [backend/tests/test_*.py](../../../backend/tests/) | 197 test files, all `test_<subsystem>.py` |
| [scripts/run_tests_remote.sh](../../../scripts/run_tests_remote.sh) | Plan 0019 — operator helper: `bash scripts/run_tests_remote.sh [pytest-args...]` runs the suite on `polyhome-1` against the live Postgres |
| [.github/workflows/ci.yml](../../../.github/workflows/ci.yml) | Backend lint + tests (with `pytest-cov` summary, Plan 0019) + frontend tsc + frontend build |
| [.github/workflows/sloppy.yml](../../../.github/workflows/sloppy.yml) | Code quality scan, fail-below threshold 60 |
| [.github/workflows/greencheck.yml](../../../.github/workflows/greencheck.yml) | Auto-fix attempt on CI failure |
| [Makefile](../../../Makefile) | Top-level test targets (backend pytest, frontend tsc) |
| [frontend/package.json](../../../frontend/package.json) | `dev`, `build`, `preview` only — no test script |

## Backend: pytest

### Framework

- **pytest** with **pytest-asyncio** in `asyncio_mode = "strict"` —
  every async test must carry `@pytest.mark.asyncio`. The mode is
  enforced project-wide; tests that omit the marker silently skip
  on older versions and fail on newer ones.
- **pytest-timeout** with global `timeout = 60` from
  [`pyproject.toml`](../../../backend/pyproject.toml). CI re-applies
  the same `--timeout=60` defensively. There are no per-test
  overrides today, so any test that legitimately needs >60 s must
  decompose itself.
- **`unittest.mock`** for `AsyncMock` / `MagicMock`. No
  `pytest-mock` — call sites import directly:
  `from unittest.mock import AsyncMock, MagicMock`.

### Naming and layout

All backend tests live flat in `backend/tests/`:

- **`test_<subsystem>.py`** — the dominant shape. One test file
  matches one service / route / worker.
- **`test_<feature>_<lifecycle>.py`** — long flows broken by phase
  (e.g. `test_trader_position_lifecycle_resolution.py`,
  `test_trader_position_lifecycle_rapid_exit.py`).
- **`test_<workflow>_<aspect>.py`** — orchestrator scenarios
  (`test_trader_orchestrator_decision_gates.py`,
  `test_trader_orchestrator_worker.py`).

**Markers (Plan 0019).** Three markers are registered in
[`pyproject.toml`](../../../backend/pyproject.toml):

- `unit` — pure logic, no external IO. Default for unmarked tests
  once the marker sweep lands.
- `db` — requires real Postgres; uses `build_postgres_session_factory`
  or hits the live engine.
- `slow` — wall-clock > 5 s under normal load.

Only the two new smoke tests
([`test_main_lifespan_smoke.py`](../../../backend/tests/test_main_lifespan_smoke.py),
[`test_alembic_roundtrip.py`](../../../backend/tests/test_alembic_roundtrip.py))
are marked today. The existing 195 files predate the marker registry;
applying markers across them is a separate planned sweep. Once that
lands, CI can split into a fast `unit` job and a slow `db`/`slow`
job. Filtering subsets in the meantime: `pytest -m "not slow"` runs
everything except the explicitly-marked slow tests, useful for fast
local feedback.

The two largest files are
[`test_trader_orchestrator_worker.py`](../../../backend/tests/test_trader_orchestrator_worker.py)
(~6 700 lines) and `test_trader_position_lifecycle_resolution.py`
(~4 800 lines). They're long on purpose — each documents one
state machine end-to-end through dozens of branches. Splitting
them is a refactor plan, not lightly done.

### conftest.py — the two autouse fixtures

[backend/tests/conftest.py:17](../../../backend/tests/conftest.py)
`_seed_wallet_state_cache_for_tests` (autouse) pre-seeds
`WalletStateCache` so orchestrator tests don't skip on freshness
checks. Without it, the orchestrator's "is the cache warm?" gate
would treat the test environment as "not warm" and short-circuit.

[backend/tests/conftest.py:52](../../../backend/tests/conftest.py)
`_block_test_wallet_db_writes` (autouse) intercepts persistence
calls for the canonical test wallet addresses (`0x1234…`,
`0xdeadbeef…`, `0x0000…`) and turns them into no-ops. This is
defence-in-depth — a test that accidentally writes a fake wallet
into a real DB would corrupt operator data.

If a test legitimately needs to verify DB persistence for a test
wallet, it must opt out by setting
`HOMERUN_ALLOW_TEST_WALLET_DB_WRITES=1` in its environment. No test
in the current tree does this.

The other ~10 fixtures are passive — sample dicts, parsed model
instances (`sample_market`, `sample_event`, `sample_opportunity`),
and pre-mocked clients (`mock_polymarket_client`, `mock_strategy`).
They're imported by name, not invoked autouse.

### Real-Postgres integration tests

Some tests cannot work against in-memory mocks because they
exercise SQLAlchemy's reflection or asyncpg-specific behaviour.
[backend/tests/postgres_test_db.py:74](../../../backend/tests/postgres_test_db.py)
`build_postgres_session_factory()` handles that:

1. Connects as the admin user via asyncpg.
2. Issues `CREATE DATABASE homerun_test_<prefix>_<uuid>`.
3. Creates a fresh `AsyncEngine` against that DB and runs
   `Base.metadata.create_all`.
4. Returns a session factory the test can use.
5. On teardown, drops the database.

About 10 test files use this — `test_data_source_catalog_and_sdk.py`,
`test_discovery_weather_upserts.py`, and other places where the
contract under test *is* the SQL.

In CI these tests work because
[ci.yml:45-58](../../../.github/workflows/ci.yml) starts a
`postgres:16-alpine` service container with health checks.

**Locally there is no Postgres** (see [CLAUDE.md](../../../CLAUDE.md)
"single most important fact"). The operator-facing recipe for
running pytest against the live remote stack is
[`scripts/run_tests_remote.sh`](../../../scripts/run_tests_remote.sh)
(Plan 0019): SSH into `polyhome-1`, `docker compose run --rm --no-deps`
a throwaway backend container with `backend/tests/` and
`backend/pyproject.toml` bind-mounted in (the runtime image excludes
`tests/` by `.dockerignore`), pointed at the running Postgres
service. Throwaway databases are created by
`build_postgres_session_factory` using the `homerun` DB user (which
is superuser + CREATEDB-able on the deployed instance) and dropped
at teardown — they cannot affect operator data.

```bash
bash scripts/run_tests_remote.sh                                # full suite
bash scripts/run_tests_remote.sh tests/test_passwords.py        # one file
bash scripts/run_tests_remote.sh -m 'not slow' tests/           # fast subset
bash scripts/run_tests_remote.sh -k 'lifespan or alembic'       # by name
```

Tests that bypass the helper (an editor running pytest locally with
no DB) fail with `ConnectionRefusedError`, not skip — this is
intentional, see "Known footguns" below.

### Mocking patterns

Three patterns repeat:

1. **`AsyncMock` / `MagicMock`** for service / client boundaries
   (most common).
2. **`monkeypatch`** for live-service interception (the autouse
   wallet-write blocker uses this).
3. **Inline fake classes** where `AsyncMock` would obscure intent —
   e.g. `_FailureProjectionDb` in
   `test_execution_session_engine.py:32-67` defines a fake DB layer
   in 30 lines that's clearer than threading 10 mock specs.

There is **no VCR / cassette / pytest-recording**. HTTP responses
are mocked inline, typically with a `FakeResponse` class that
mirrors the `httpx.Response` shape used at the call site (see
`test_llm_provider_local.py`).

## Frontend: tsc + build, that's it

[frontend/package.json](../../../frontend/package.json) lists only
three scripts: `dev`, `build`, `preview`. There is no `test`
script. There is no Vitest, no Jest, no Playwright, no Cypress,
no Storybook, no `*.test.tsx` / `*.spec.tsx` anywhere.

The de-facto safety net is two CI steps:

1. **Type checking** via `npx tsc --noEmit`
   ([ci.yml:101](../../../.github/workflows/ci.yml)). This catches
   the majority of regressions in this codebase because the React
   app is heavily typed end-to-end (jotai atoms, react-query keys,
   Pydantic-mirroring `apiSettings.ts`).
2. **Production build** via `npm run build` — fails on any TS
   error or unresolved import that `tsc --noEmit` somehow missed.

This is a deliberate pragmatic choice for the current size of the
frontend, not an oversight. Adding a test runner is a future plan
(category **U** with a strong **D** flavour) and would primarily
target the parts that have logic worth testing — `useRealtimeInvalidation`,
the WebSocket reconnect, Jotai atom reducers — not the panels.

## CI workflows

| Workflow | Triggers | Job(s) |
|---|---|---|
| [`ci.yml`](../../../.github/workflows/ci.yml) | push to main/master, PR, manual dispatch | Backend Lint (ruff), Backend Tests (pytest with postgres:16-alpine service), Frontend Lint (`tsc --noEmit`), Frontend Build (`npm run build`). 15 min job timeout. |
| [`sloppy.yml`](../../../.github/workflows/sloppy.yml) | PR, manual | Sloppy AI code-quality scan, fail-below threshold of 60. |
| [`greencheck.yml`](../../../.github/workflows/greencheck.yml) | CI workflow completion, only on failure | Attempts auto-fix via Claude agent. |
| `docker-publish.yml`, `codeflow-card.yml` | (separate concerns: image publishing, PR cards) | Not in the test pipeline. |

The backend test step is the longest:

```yaml
DATABASE_URL: postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/homerun
python -m pytest tests/ -v --tb=short --timeout=60
```

Note that the `pytest tests/` invocation **does not** select by
marker — every test runs every CI run. Without categorisation,
slow integration tests share the wall-clock budget with fast unit
tests; if total runtime ever blows past the 15-minute job timeout,
the fix path is either to introduce markers + a "fast" job and a
"slow" job, or to parallelise via `pytest-xdist`. Neither is in
place today.

## Validation Commands in plans

A plan's `## Validation Commands` should always include a pytest
invocation and the frontend type check, even when the plan is
backend-only — the cost is small and catches accidental TS-type
breakage in `apiSettings.ts` mirrors. The standard menu:

```
- `docker compose exec backend pytest -q`
- `docker compose exec backend ruff check`
- `docker compose exec backend python -c "<one-liner that imports the new symbol>"`
- `cd frontend && npm run build`
```

For changes that need real Postgres (schema, ORM-level tests),
either add a step that brings up the compose stack
(`docker compose up -d postgres redis`) or run the affected tests
inside the backend container which already has the DB.

For LLM-provider plans (like
[0001-add-nvidia-nim-provider.md](../0001-add-nvidia-nim-provider.md)),
the `pytest -q backend/tests/test_settings.py backend/tests/test_llm_provider.py`
shorthand exists today; verify the file exists before pinning it
in a Validation Commands list (some test files referenced from
plans haven't been written yet — pytest will silently report "no
tests ran", not "missing file").

## Dependencies (both directions)

**This layer depends on:**

- Postgres 16 (in CI as a service container; locally as a process).
- Python 3.12, the `pytest` / `pytest-asyncio` / `pytest-timeout`
  trio.
- `DATABASE_URL` env var pointing at a writable Postgres.
- The `HOMERUN_ALLOW_TEST_WALLET_DB_WRITES` opt-out for a small
  set of tests that need to write fake wallets.

**Depended on by:**

- Every plan's `## Validation Commands` section.
- The CI gates that protect `main`.
- The Sloppy auto-fix loop, which runs against the test suite to
  decide if its patches are safe.

## Extension points

| When you want to… | Touch |
|---|---|
| Add a test for a new service module | New `backend/tests/test_<module>.py`, lean on existing fixtures in `conftest.py`, mark async tests with `@pytest.mark.asyncio`. |
| Add a test that needs a real DB | Use `build_postgres_session_factory()` from `postgres_test_db.py`. Don't connect to the operator's DB directly. |
| Mock an external HTTP service | Inline `FakeResponse` class or `AsyncMock` against the client. Don't introduce VCR / cassettes — the project hasn't adopted that pattern. |
| Add a long-running test | Mark it `@pytest.mark.slow`. The global timeout (60 s) still applies; if the test legitimately needs longer, decompose it. |
| Add a test that needs the FastAPI app to boot end-to-end | Follow the subprocess pattern in [`test_main_lifespan_smoke.py`](../../../backend/tests/test_main_lifespan_smoke.py). The engine in `models.database` is created at import time, so overriding `DATABASE_URL` mid-test does nothing — the smoke test launches a fresh `python -c` subprocess with the throwaway URL in its env. |
| Add a test for a new alembic migration | Extend [`test_alembic_roundtrip.py`](../../../backend/tests/test_alembic_roundtrip.py) or model after it. Two cases run automatically: (a) `test_head_migration_downgrade_upgrade_roundtrip` stamps at head, downgrades one revision, re-upgrades; (b) `test_alembic_replay_base_to_head_on_empty_db` runs the full chain from base on a fresh DB. |
| Add a new alembic migration | Use `safe_add_column` / `safe_create_table` / `safe_create_index` from [`alembic_helpers`](../../../backend/alembic_helpers.py) instead of the raw `op.*` variants — the baseline's lazy `Base.metadata.create_all` already materialises every current ORM-mapped table/column, so non-idempotent ops would break the replay test. For raw `op.execute("CREATE …")`, add `IF NOT EXISTS` (or guard with `column_names()` for ALTER patterns). |
| Add frontend tests | Out of band — this requires its own plan to choose a runner (Vitest is the obvious default in a Vite project), wire it into CI, and seed the first batch of tests. |
| Add coverage reporting | Out of band — there is no coverage tool configured today. |

## Known footguns

- **Idempotent migrations: use the `safe_*` helpers.** Plan 0020
  made the chain replayable from `base` by retrofitting ~13
  schema-additive migrations to use
  [`alembic_helpers.safe_add_column`](../../../backend/alembic_helpers.py)
  / `safe_create_table` / `safe_create_index`, which skip
  no-op'ed when the baseline's lazy `Base.metadata.create_all`
  already materialised the target. New migrations should use
  these helpers, not raw `op.add_column` / `op.create_table` /
  `op.create_index` — `safe_*` is the canonical pattern.
  Verified by [`test_alembic_replay_base_to_head_on_empty_db`](../../../backend/tests/test_alembic_roundtrip.py),
  which runs the entire chain against a fresh empty DB.
- **The 60 s global timeout is not negotiable in-tree.** A test that
  hits it doesn't fail with "timeout" in CI logs; it fails with the
  test framework killing the worker, which can leak DB sessions.
  Decompose long tests rather than override.
- **Real Postgres is mandatory locally for ~10 tests.** Running
  `pytest tests/` against a developer machine without Postgres on
  `127.0.0.1:5432` produces dozens of confusing
  `ConnectionRefusedError`s. The fix is to bring up the compose
  stack or to run pytest inside the backend container.
- **The autouse wallet-write blocker is silent.** If a test
  expects `await session.commit()` to persist a test-wallet row,
  it gets a no-op — and the next assertion against that row will
  fail with "no rows found". The error message points at the
  assertion, not at the autouse fixture. When debugging, check
  whether the wallet address matches the blocked prefixes before
  anything else.
- **No frontend tests means no detection of UI regressions** beyond
  TypeScript errors. A plan that ships a behavioural change in
  React must include a manual smoke-test checklist as a task; do
  not assume CI catches it.
- **Sloppy and greencheck are AI-driven helpers, not test gates.**
  A green CI without a passing Sloppy run is mergeable. Don't
  treat their badges as primary signal.
- **Fixture ordering is fragile.** Two autouse fixtures touch live
  service internals before each test. Adding a third autouse
  fixture should be done with care; prefer named fixtures opted
  into per test.

Last verified: 2026-05-10 (Plan 0020: extended `test_alembic_roundtrip.py` with `test_alembic_replay_base_to_head_on_empty_db` (full chain replay; verified passing on polyhome-1 — 16 s wall-clock against fresh throwaway DB). Added `safe_add_column` / `safe_create_table` / `safe_create_index` to `alembic_helpers.py`, retrofitted ~13 unguarded migrations + the raw-SQL `search_index` migration. The "migration chain not replayable from base" footgun is now resolved; bind-mounts in `scripts/run_tests_remote.sh` extended to include `alembic/` and `alembic_helpers.py` so migration changes take effect without an image rebuild. Plan 0019 baseline still verified: 4 lifespan smoke tests + head-only round-trip pass.)
