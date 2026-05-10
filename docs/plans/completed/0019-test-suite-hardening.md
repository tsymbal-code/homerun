# Plan: Test suite hardening — coverage, markers, smoke tests, remote runner

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0019` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

The backend ships ~1 990 pytest functions across 195 files (see
[architecture/testing.md](architecture/testing.md)). Coverage is wide
but the suite has structural gaps that let real regressions through:

- **No coverage report.** A fix can land without exercising the new
  branch and CI stays green.
- **No `lifespan` startup smoke.** "Backend won't import / won't
  start" is detected only by the operator on redeploy.
- **No Alembic round-trip test.** A broken `downgrade()` is detected
  only when prod needs a rollback.
- **No marker categorisation.** Whole-suite pytest run is monolithic
  — can't filter "fast unit only" before commit.
- **Tests don't ship in the runtime image.** The `.dockerignore`
  excludes `tests/`, and there's no operator-friendly recipe to run
  pytest on the remote stack against the live Postgres.

This plan fills those four gaps with the smallest reasonable surface
change, **without** touching the existing 195 test files (mass-marker
sweep is left for a follow-up plan). "Done" means: operator can run
`bash scripts/run_tests_remote.sh` from the local checkout to execute
the full suite on `polyhome-1` against a throwaway database, CI
publishes a coverage summary, and two new smoke tests guard
startup + migrations.

## Context / References

- [Architecture: Testing](architecture/testing.md) — current state,
  conftest, real-Postgres pattern.
- [backend/tests/conftest.py](../../backend/tests/conftest.py) — two
  autouse fixtures (wallet cache seed, test-wallet write block).
- [backend/tests/postgres_test_db.py](../../backend/tests/postgres_test_db.py)
  — `build_postgres_session_factory()` template for the alembic
  round-trip test.
- [backend/main.py:236](../../backend/main.py) — `@asynccontextmanager
  async def lifespan(app)` — what the smoke test must drive.
- [backend/alembic/env.py](../../backend/alembic/env.py) — async
  Alembic env; `target_metadata = Base.metadata`.
- [backend/.dockerignore](../../backend/.dockerignore) — current
  policy: `tests/` is excluded from the runtime image.
- [.github/workflows/ci.yml](../../.github/workflows/ci.yml) — current
  single-job pytest invocation.

## Validation Commands

- `bash scripts/run_tests_remote.sh tests/test_main_lifespan_smoke.py tests/test_alembic_roundtrip.py`
- `bash scripts/run_tests_remote.sh tests/`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose ps'`
- `git log --grep='Plan: 0019' --oneline`

## Out of scope

Explicitly **not** in this plan (each is its own follow-up):

- Mass-applying `unit`/`db`/`slow` markers to the existing 195 test
  files. We register the markers and document the policy; a sweep
  plan can backfill.
- Splitting the 6 700-line `test_trader_orchestrator_worker.py` into
  per-aspect files.
- Hypothesis property tests for FIFO PnL, Kelly, Cox-PH simulator.
- HTTP contract fixtures (`respx` cassette catalogue) replacing the
  ad-hoc `FakeResponse` pattern.
- Mutation testing (`mutmut` / `cosmic-ray`).
- CI job split (unit-fast vs. db-slow). The marker registry this
  plan adds is the prerequisite; the actual split is a separate
  scheduling decision.

### Task 1: Register pytest markers and add test dependencies

- [x] Add `pytest-cov>=5.0,<7.0`, `hypothesis>=6.100,<7.0`,
      `pytest-xdist>=3.6,<4.0` to
      [backend/requirements.txt](../../backend/requirements.txt)
      under the existing pytest group.  Also added
      `asgi-lifespan>=2.1,<3.0` (initially planned for Task 2 but
      ultimately not used — see deviation note below).
- [x] Extend `[tool.pytest.ini_options]` in
      [backend/pyproject.toml](../../backend/pyproject.toml) with
      `markers = ["unit: …", "db: …", "slow: …"]`.
      `filterwarnings = ["error::DeprecationWarning"]` was **not**
      enabled — the suite emits `DeprecationWarning`s from
      `datetime.utcnow()` calls in legacy alembic migrations and
      from C-extension imports we don't own (SwigPyPacked etc.); a
      promote-to-error mode would force a sweep that's out of
      scope for this plan.
- [x] Mark completed

### Task 2: Lifespan smoke test

- [x] Create
      [backend/tests/test_main_lifespan_smoke.py](../../backend/tests/test_main_lifespan_smoke.py)
      with four tests:
  1. `test_import_app_module` — `import main` must succeed.
  2. `test_app_is_fastapi_with_routes` — `app` is `FastAPI` and
     has > 100 routes registered.
  3. `test_lifespan_is_async_context_manager` — `app.router.lifespan_context`
     is callable.
  4. `test_lifespan_startup_and_shutdown_complete` (`db`+`slow`) —
     allocates a throwaway DB, then drives the full
     `lifespan(app)` context in a **subprocess** with that DB's
     URL in the env. The subprocess approach is necessary because
     `models.database.async_engine` is built at module import time
     against `settings.DATABASE_URL`, so overriding the env var
     after the test runner has already imported anything from
     `models.database` has no effect. The asgi-lifespan dependency
     listed in Task 1 turned out to not be needed —
     `app.router.lifespan_context(app)` is itself an async
     context manager.
- [x] DB-marker added; the lifespan-driving test skips cleanly when
      Postgres is unreachable.
- [x] Mark completed

### Task 3: Alembic round-trip test

- [x] Create
      [backend/tests/test_alembic_roundtrip.py](../../backend/tests/test_alembic_roundtrip.py).
      **Scope deviation** from the original task: the first
      iteration tried `upgrade base→head → downgrade -1 → upgrade head`,
      which surfaced a chronic anti-pattern in
      [`202602130001_baseline_schema.py`](../../backend/alembic/versions/202602130001_baseline_schema.py)
      — the baseline calls `Base.metadata.create_all(bind=op.get_bind())`,
      which materialises *every* current ORM column at revision 1,
      so later `op.add_column(...)` migrations fail with
      `DuplicateColumnError` (e.g. `block_new_orders` on the
      `traders` table). The deployed DB was originally stamped
      before those columns existed and has never re-played
      migrations from base.
      Fixing baseline replayability is its own larger plan, so
      this test was rescoped to the **head migration only**:
      stamp the throwaway DB at head, downgrade to head's
      explicit parent, re-upgrade. Catches the most common
      regression (a new migration whose `downgrade()` raises or
      isn't symmetric with its `upgrade()`).
      Also discovered: `command.downgrade(cfg, "-1")` in the
      Python API does **not** honour the CLI's relative-offset
      syntax — we compute `head_script.down_revision` explicitly.
      The chronic baseline issue is documented in
      [architecture/testing.md](architecture/testing.md) "Known
      footguns" so a future plan can pick it up.
- [x] Marker: `db` + `slow` applied; throwaway DB dropped on
      teardown (verified post-run: `select count(*) from pg_database
      where datname like 'homerun_test_alembic%'` returns `0`).
- [x] Mark completed

### Task 4: Remote test runner script

- [x] Create
      [scripts/run_tests_remote.sh](../../scripts/run_tests_remote.sh)
      with the contract from the original task. Two refinements
      landed during verification:
  - Also bind-mount `backend/pyproject.toml:/app/backend/pyproject.toml:ro`
    so changes to `[tool.pytest.ini_options]` (markers, timeout,
    asyncio_mode) take effect without forcing a full image
    rebuild.
  - Quote `pytest` arguments via `printf %q` so things like
    `-k 'lifespan or alembic'` survive the SSH transport.
- [x] Document the script in
      [deploy/AGENTS.md](../../deploy/AGENTS.md) under a new
      "Running tests against the live stack" section.
- [x] Mark completed

### Task 5: CI coverage report

- [x] Update [.github/workflows/ci.yml](../../.github/workflows/ci.yml)
      `Run tests` step:
  - Installed `pytest-cov`, `pytest-xdist`, `hypothesis`, and
    `asgi-lifespan` alongside the existing pytest deps.
  - Appended `--cov=services --cov=workers --cov=api --cov=models
    --cov=strategies --cov-report=term-missing:skip-covered
    --cov-report=xml:coverage.xml` to the pytest invocation.
  - Added an `Upload coverage artifact` step that publishes
    `coverage.xml` (retention 14 d, `if-no-files-found: warn`).
  - **No** `--cov-fail-under` set; first run establishes a baseline.
- [x] Mark completed

### Task 6: Verify on remote

- [x] `bash scripts/run_tests_remote.sh tests/test_main_lifespan_smoke.py`
      passes (4 tests, 22.7 s — the slow lifespan-driving test
      dominates wall-clock).
- [x] `bash scripts/run_tests_remote.sh tests/test_alembic_roundtrip.py`
      passes (1 test, 4.7 s — note: first attempt skipped with
      "too many clients already" because the running stack used
      97/100 connections; retried clean. A future plan should make
      `build_postgres_session_factory` a politer tenant by default
      via `NullPool`).
- [x] `bash scripts/run_tests_remote.sh tests/test_main_lifespan_smoke.py
      tests/test_alembic_roundtrip.py tests/test_passwords.py`
      → 8 passed combined.
- [x] Marker filtering verified: `bash scripts/run_tests_remote.sh
      tests/test_main_lifespan_smoke.py -m "not slow"` selects 3
      and deselects 1 (3.3 s vs. 28 s for the unfiltered run).
- [x] Cleanup confirmed: `select count(*) from pg_database where
      datname like 'homerun_test_alembic%' or datname like
      'homerun_test_lifespan%'` returns `0` after both tests.
- [x] Mark completed

### Task 7: Update architecture note + close plan

- [x] Update
      [docs/plans/architecture/testing.md](architecture/testing.md):
      registered markers documented; `scripts/run_tests_remote.sh`
      recipe documented; two new smoke tests added to the "Key
      files" table; chronic baseline-migration `create_all`
      anti-pattern added to "Known footguns" (so a future plan
      can pick up the replayability cleanup); `Last verified`
      bumped to 2026-05-10.
- [x] `git mv docs/plans/0019-test-suite-hardening.md
      docs/plans/completed/0019-test-suite-hardening.md`.
- [x] Update [plan-control-index.md](plan-control-index.md): change
      the row's link target to `completed/`.
- [x] `git log --grep='Plan: 0019'` shows the full commit chain.
- [x] Mark completed
