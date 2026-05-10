# Plan: Make Alembic migrations replayable from base on a fresh DB

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0020` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Plan 0019's alembic round-trip test surfaced that `alembic upgrade
base→head` against a fresh database fails with
`DuplicateColumnError` (e.g. `column "block_new_orders" of relation
"traders" already exists`).

Root cause: the baseline migration
[`202602130001_baseline_schema.py`](../../backend/alembic/versions/202602130001_baseline_schema.py)
calls `Base.metadata.create_all(bind=op.get_bind())`, which
materialises every current ORM column at revision 1 — including
columns added by later migrations. Subsequent migrations that
`op.add_column(...)` those same columns then collide with the
baseline.

Production was originally stamped at baseline before any of those
columns existed and has never re-played from base, so the operator
has never hit this. But it means:

- A fresh developer / CI database cannot bootstrap by running
  `alembic upgrade head`.
- The Plan 0019 round-trip test had to be rescoped to head-only
  (stamp + downgrade -1 + upgrade head), which only catches new
  migrations going forward — not historical breakage.

**Fix strategy.** Keep the lazy `create_all` baseline (don't try to
reconstruct historical schema), but make every later
`op.add_column` / `op.create_table` / `op.create_index` idempotent
via the existing `alembic_helpers.column_names()` /
`table_names()` / `index_names()` checks. The pattern is already
established in 96 of ~130 migrations. This plan retrofits the
remaining ~13 unguarded ones.

Add ergonomic helpers `safe_add_column` / `safe_create_table` /
`safe_create_index` so future migration authors don't have to
re-implement the guard pattern by hand.

Extend the round-trip test with a new `test_alembic_replay_base_to_head`
case that does the full `upgrade base → head` against an empty DB.
Once it passes, the "Known footgun" entry in
[`architecture/testing.md`](architecture/testing.md) can be removed.

"Done" looks like: `bash scripts/run_tests_remote.sh
tests/test_alembic_roundtrip.py` passes both round-trip and replay
cases against an empty database, and the architecture note no
longer flags this as a chronic issue.

## Context / References

- [Plan 0019 — Test suite hardening](completed/0019-test-suite-hardening.md)
  surfaced the issue and documented it.
- [Architecture: Testing](architecture/testing.md) "Known footguns"
  — entry to be removed once this plan closes.
- [backend/alembic_helpers.py](../../backend/alembic_helpers.py)
  already provides `column_names()`, `table_names()`,
  `index_names()`. This plan extends it with safe wrappers.
- [backend/alembic/versions/202602130001_baseline_schema.py](../../backend/alembic/versions/202602130001_baseline_schema.py)
  — the baseline. Untouched by this plan.
- [backend/tests/test_alembic_roundtrip.py](../../backend/tests/test_alembic_roundtrip.py)
  — the test extended in Task 4.
- Unguarded migrations identified by scanning
  `backend/alembic/versions/` (Task 3 enumerates them).

## Validation Commands

- `bash scripts/run_tests_remote.sh tests/test_alembic_roundtrip.py`
- `bash scripts/run_tests_remote.sh tests/test_alembic_roundtrip.py -k replay`
- `git log --grep='Plan: 0020' --oneline`

## Out of scope

- Reconstructing the historical schema as it stood at baseline
  time. The `create_all` lazy baseline stays.
- Making **data migrations** (`INSERT INTO ...`) idempotent. Some
  seed migrations may still emit duplicate-key errors on a re-run.
  This plan only makes **schema** ops idempotent; data idempotency
  is a separate decision and a follow-up plan if needed.
- Auditing migration `downgrade()` correctness across the chain.
  Most use the project convention "Explicit downgrade support is
  intentionally omitted for migration safety" (no-op). The
  round-trip test only exercises the head migration's downgrade.
- Squashing the migration chain into a single new baseline. That
  would require re-stamping production and is far higher risk.

### Task 1: Extend `alembic_helpers` with safe op wrappers

- [x] Add to [backend/alembic_helpers.py](../../backend/alembic_helpers.py):
  - `safe_add_column(table_name, column)` — calls `op.add_column`
    only if the column doesn't already exist.
  - `safe_create_table(table_name, *columns, **kwargs)` — calls
    `op.create_table` only if the table doesn't exist. Passes
    through any keyword arguments (constraints, etc.).
  - `safe_create_index(index_name, table_name, columns, **kwargs)`
    — calls `op.create_index` only if the index doesn't exist.
    Passes through `unique=`, `postgresql_where=`, etc.
- [x] Each helper logs (via standard alembic logger) when it
      skips, so a re-run produces an audit trail in the
      migration logs.
- [x] Mark completed

### Task 2: Identify the unguarded migrations

This is the explicit list from a `grep -L` scan of
`backend/alembic/versions/` against `column_names`/`table_names`/
`index_names` references. Update the list in this task if the scan
reveals more once Task 3 lands.

**Unguarded `op.add_column`:**
- `202604260002_trader_block_new_orders.py`
- `202604280001_add_redeemer_settings.py`
- `202604300004_polymarket_default_collateral.py`
- `202605010001_latency_fallback_defaults.py`
- `202605010003_trader_event_verbosity.py`
- `202605040001_provider_datasets_and_reverse_engineer.py`
- `202605060001_backtest_run_jobs.py`

**Unguarded `op.create_table`:**
- `202604290001_trader_order_verification_table.py`
- `202604300001_strategy_persistent_state.py`
- `202604300003_book_delta_and_fill_models.py`
- `202604300005_backtest_runs.py`
- `202605010002_recording_sessions.py`
- `202605010004_global_search_index.py`
- `202605040001_provider_datasets_and_reverse_engineer.py` (same file as above)

**Unguarded `op.create_index`:** (subset of files above plus)
- `202604260003_autoresearch_strategy_scope.py`

Total unique files to touch: ~13.

- [x] Mark completed

### Task 3: Sweep the unguarded migrations

For each file in Task 2's list:

- [x] Replace `op.add_column` / `op.create_table` /
      `op.create_index` with their `safe_*` counterparts.
- [x] Add `from alembic_helpers import …` at the top of each
      modified file.
- [x] Do **not** modify `downgrade()` — keep the project convention.
- [x] **Additional fix surfaced during verification**:
      [`202605010004_global_search_index.py`](../../backend/alembic/versions/202605010004_global_search_index.py)
      uses raw `op.execute("CREATE TABLE search_index ...")`
      (because of the `tsv TSVECTOR GENERATED ALWAYS AS (…) STORED`
      column that SQLAlchemy can't easily express). Added
      `IF NOT EXISTS` to the `CREATE TABLE` and `CREATE INDEX`
      statements, plus a `column_names("search_index")` guard
      that runs `ALTER TABLE … ADD COLUMN tsv …` if missing.
      Necessary because the ORM `SearchIndex` model deliberately
      omits `tsv` (it's Postgres-managed), so baseline's
      `Base.metadata.create_all` creates the table without `tsv`,
      and the GIN index would otherwise fail with
      `UndefinedColumnError`.
- [x] **Test runner script extension**:
      [`scripts/run_tests_remote.sh`](../../scripts/run_tests_remote.sh)
      now bind-mounts `backend/alembic/`, `backend/alembic.ini`,
      and `backend/alembic_helpers.py` (read-only) so migration
      changes take effect inside the throwaway test container
      without rebuilding the image.
- [x] Mark completed

### Task 4: Extend the round-trip test with a replay case

- [x] Add `test_alembic_replay_base_to_head_on_empty_db` to
      [backend/tests/test_alembic_roundtrip.py](../../backend/tests/test_alembic_roundtrip.py).
      **Implementation deviation**: the in-process route
      (`command.upgrade` against a shared async connection) hits
      an alembic ``assert self._transaction is not None`` deep in
      ``MigrationContext`` — first inside the ~130-migration chain
      (cumulative state issue), and definitively in
      `202603120001_db_hot_path_indexes` which uses
      ``context.autocommit_block()`` for ``CREATE INDEX
      CONCURRENTLY``. Switched to a subprocess that shells out to
      ``python -m alembic upgrade head``, which gives alembic the
      standalone connection lifecycle its env.py expects and
      matches what production's ``init_database`` does on cold
      start. Reads the post-replay revision via a separate async
      engine and asserts it matches script-head.
- [x] The existing head-only round-trip test stays — cheap
      regression guard for new migrations.
- [x] Mark completed

### Task 5: Verify on remote

- [x] `bash scripts/run_tests_remote.sh tests/test_alembic_roundtrip.py`
      — both tests pass.
- [x] `bash scripts/run_tests_remote.sh tests/test_main_lifespan_smoke.py
      tests/test_alembic_roundtrip.py` — combined run still passes.
- [x] No leftover throwaway databases:
      `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose
      exec -T postgres psql -U homerun -d homerun -c "select
      count(*) from pg_database where datname like
      '\''homerun_test_alembic%'\''"'` returns `0`.
- [x] Mark completed

### Task 6: Update architecture note + close plan

- [x] Update
      [docs/plans/architecture/testing.md](architecture/testing.md):
  - Remove the "migration chain is not replayable from base"
    footgun (now resolved by this plan).
  - Add a "use the `safe_*` helpers in new migrations" note to
    the "When you want to…" extension-points table.
  - Bump `Last verified` to today, reference Plan 0020.
- [x] `git mv docs/plans/0020-make-alembic-migrations-replayable.md
      docs/plans/completed/0020-make-alembic-migrations-replayable.md`.
- [x] Update [plan-control-index.md](plan-control-index.md): add
      a row + per-plan note, link target points at `completed/`.
- [x] `git log --grep='Plan: 0020'` shows the full commit chain.
- [x] Mark completed
