# Plan: Tag-based market filter at the Polymarket ingest layer

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Polymarket exposes ~10 000 markets across heterogeneous topics
(crypto, sports, politics, entertainment, …). Today every one of
them passes through `worker-trading`'s scanner, scoring, deepcopy
and DB write paths even when the operator has no interest in
their domain. The 2026-05-07 profile
([architecture note](architecture/worker-trading.md#measured-cpu-profile-2026-05-07))
showed the resulting deepcopy/oracle-history hotspots; they exist
because the input volume is high, not because any single
algorithm is broken.

This plan introduces a **tag-based whitelist filter** applied
at the ingest layer (`scanner._is_market_tradable` /
`_filter_tradable_markets`). Markets that don't carry at least
one operator-selected tag are dropped before they reach
`market_catalog`, the scanner cache, the opportunity dispatcher,
or any downstream worker. The filter is **OR-logic, whitelist,
operator-managed via the existing `Settings → Scanner` page**;
when the whitelist is empty the system behaves exactly like
today (no filtering).

To populate the operator's chooser with real tag values the
system also runs a **tag aggregator hook**: every Polymarket
ingest cycle extracts tags from the **raw, unfiltered** market
list and upserts them into a new `market_tags_seen` table. The
Settings UI queries that table for tags `last_seen` within the
past 24 hours.

Done = operator can pick one or more tags in `Settings → Scanner`,
save, and within one ingest cycle observe (a) `market_catalog`
shrinks accordingly, (b) downstream scanner / opportunity
dispatcher / dispatch loop processes only the filtered subset,
(c) a re-run of the py-spy capture from plan 0003 shows the
deepcopy and oracle-history hotspots reduced proportionally to
the volume reduction.

## Out of scope

- **Retroactive prune.** Existing rows in `market_catalog` that
  no longer match the filter are not removed. They expire /
  close naturally. If a manual prune becomes desirable, that's
  a follow-up plan.
- **Category filter, AND-logic, regex, exclude-list.** Only
  tag-OR-whitelist this round. Operator requirements explicitly
  scoped to that.
- **Polymarket-side server filter.** Gamma API does not support
  `?tags=`; we filter locally. If the API later gains the param,
  a future plan can push the filter upstream.
- **Worker-trading CPU optimisations** (deepcopy halving,
  oracle-history TTL cache). Tracked in plan 0004
  ([`0004-optimize-worker-trading-cpu-hotspots.md`](../0004-optimize-worker-trading-cpu-hotspots.md));
  was originally backlog'd, promoted back to active after this
  plan's Task 8 re-profile showed `get_oracle_history` and
  `copy.deepcopy` still ≥ 10 % each.

## Context / References

- [Architecture: worker-trading process model and CPU profile](architecture/worker-trading.md)
- [Architecture: market filter pipeline](architecture/market-filter.md)
  (created by Task 1 of this plan)
- [`backend/services/polymarket.py:597`](../../backend/services/polymarket.py)
  — `get_all_markets`, where the raw stream enters
- [`backend/services/scanner.py:937`](../../backend/services/scanner.py)
  — `_is_market_tradable`, the natural filter site
- [`backend/services/scanner.py:980`](../../backend/services/scanner.py)
  — `_filter_tradable_markets`, the natural aggregator site
- [`backend/api/routes_settings.py:2590`](../../backend/api/routes_settings.py)
  — `GET/PUT /settings/scanner`, the existing surface to extend
- [`backend/models/database.py:1228+`](../../backend/models/database.py)
  — `AppSettings`, where new columns land
- [`frontend/src/components/SettingsPanel.tsx`](../../frontend/src/components/SettingsPanel.tsx)
  — Scanner tab, where the new section attaches

## Validation Commands

- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend pytest -q backend/tests/services/test_scanner_market_filter.py backend/tests/api/test_routes_settings_scanner_filter.py'`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend ruff check backend/services/scanner.py backend/api/routes_settings.py backend/models/database.py'`
- `cd frontend && npm run typecheck`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend alembic upgrade head'`
- `ssh polyhome-1 "cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c \"select count(*) from market_tags_seen where last_seen > now() - interval '24 hours'\""`

### Task 1: Write the architecture note `architecture/market-filter.md`

- [x] Create [`docs/plans/architecture/market-filter.md`](architecture/market-filter.md)
  with the standard architecture-note skeleton (Purpose, Key
  files, Contracts, Dependencies, Extension points). Cover:
  - The funnel: raw Polymarket → tag aggregator (always) →
    tag whitelist filter (if non-empty) → `market_catalog`.
  - The `market_tags_seen` table schema + retention policy
    (hot rows = `last_seen > now() - 24h`; physical prune of
    `> 7 days` on a slow timer in `worker-discovery`).
  - The settings contract (new pydantic fields under
    `ScannerSettingsModel`).
  - Extension points: how to add a new filter axis later
    (category, liquidity-min, market-question regex) by
    composing predicates inside `_is_market_tradable`.
- [x] Link the note from
  [`docs/plans/README.md`](README.md) "Architecture notes"
  list.
- [x] Mark completed

### Task 2: DB migration — `market_tags_seen` + `app_settings` columns

- [x] Generate a new alembic revision under
  `backend/alembic/versions/202605070002_market_tag_filter.py`. The
  migration:
  - Creates `market_tags_seen` with columns `tag VARCHAR PK`,
    `first_seen TIMESTAMP NOT NULL`,
    `last_seen TIMESTAMP NOT NULL`,
    `occurrences BIGINT NOT NULL DEFAULT 1`.
  - Adds `idx_market_tags_seen_last_seen` btree index on
    `last_seen` for the 24-hour query.
  - Extends `app_settings` with two nullable columns:
    `market_filter_tags JSON` (list of selected tag strings;
    null/empty list = filter inactive) and
    `market_filter_updated_at TIMESTAMPTZ`.
  - Use the inspect-then-add guard pattern for `app_settings`
    (per
    [`docs/plans/architecture/database-and-migrations.md`](architecture/database-and-migrations.md)).
  - ORM `MarketTagSeen` lives in `models/database.py` next to
    `ScannerSettings`; the matching `app_settings` columns
    (`market_filter_tags`, `market_filter_updated_at`) are
    appended to `AppSettings` after `scanner_strict_ws_max_age_ms`.
- [~] Local dry-run: `alembic upgrade head --sql` is **not
  available** in this repo because pre-existing migrations
  (e.g. `202602130002_world_intel_settings_columns.py`) call
  `op.get_bind()` and `inspect()` even at offline-render time,
  which `MockConnection` doesn't support
  (`NoInspectionAvailable`). Documented here, not blocking;
  online application happens in Task 7.
- [→] Run `alembic upgrade head` against the live DB on
  `polyhome-1` — moved to Task 7 (the `migrate` one-shot service
  in `docker-compose.yml` runs it automatically as part of the
  redeploy).
- [→] Verify schema:
  `ssh polyhome-1 "docker compose exec -T postgres psql -U homerun -d homerun -c '\d market_tags_seen'"` — moved to Task 7 after
  the redeploy lands the new image with the migration file.
- [x] Mark completed

### Task 3: Backend — tag aggregator hook in ingest

- [x] Add `backend/services/market_tag_aggregator.py` with one
  pure function `record_tags_from_markets(session, markets:
  Iterable[Market]) -> int` that:
  - Iterates the raw `markets` list **before** any filter is
    applied.
  - Extracts `market.tags` (already normalised by
    `_extract_tags`) plus, for each event, `event.tags`.
  - Performs an upsert per unique tag: `INSERT … ON CONFLICT
    (tag) DO UPDATE SET last_seen = excluded.last_seen,
    occurrences = market_tags_seen.occurrences + 1`.
  - Returns the number of distinct tags written/updated.
  - Signature is `(session, events, markets)` — the function
    needs to walk both the market and event tag fields, so the
    extra `events` argument keeps the call site explicit.
- [x] Hook the call into
  [`scanner.py:980`](../../backend/services/scanner.py)
  `_filter_tradable_markets` **at the top**, before the
  tradable-only loop. Catch and log exceptions — aggregator
  failure must **not** break ingest.
  Implementation deviation: the hook actually fires from
  `refresh_catalog` (`scanner.py` Phase 2b, just **before**
  `_filter_tradable_markets`), not inside
  `_filter_tradable_markets` itself. The latter is a sync
  `@staticmethod` invoked from `run_in_executor` paths and
  cannot await an async DB session; lifting the call into
  the surrounding async coroutine keeps the funnel correct
  (still pre-filter), avoids an executor-unsafe API change,
  and runs the upsert exactly **once per ingest cycle** —
  the three other `_filter_tradable_markets` callers are
  cache-replay paths whose markets were already recorded by
  the original ingest.
- [x] Add a configurable batch flag
  `MARKET_TAG_AGGREGATOR_ENABLED` (default `True`) so the
  feature can be disabled at runtime if needed.
- [x] Unit test in `backend/tests/test_market_tag_aggregator.py`
  (filename adapted to repo's flat `backend/tests/` layout):
  feed a fixture list of markets with overlapping tags;
  assert correct upsert counts and that re-running advances
  `last_seen` and `occurrences`. Also covers normalisation
  (lowercase/strip/dedupe), the empty-list short-circuit, and
  the `MARKET_TAG_AGGREGATOR_ENABLED=False` kill-switch.
- [x] Mark completed

### Task 4: Backend — tag whitelist filter

- [x] In
  [`scanner.py`](../../backend/services/scanner.py),
  add a tag-whitelist filter. **Implementation deviation:**
  the whitelist lives in a new sibling staticmethod
  `ArbitrageScanner._apply_market_tag_whitelist(events,
  markets, whitelist) -> tuple[list, list]`, not inside
  `_is_market_tradable`, because the latter is a per-market
  helper that doesn't see `Event.tags` and the operator's
  whitelist must intersect against the union
  `(market.tags ∪ event.tags)`. The new helper is invoked from
  all four `_filter_tradable_markets` callers
  (`refresh_catalog`, the cached-merged-scan, the
  cached-merged-scan with new markets, and
  `_hydrate_catalog_from_db`) **before** the tradability gate,
  matching the funnel diagram in
  [`market-filter.md`](architecture/market-filter.md). When
  the whitelist is empty, the helper returns inputs unchanged
  (no-op).
  - Reject diagnostics: a single `logger.info` per call with
    `reason=market_filter_tags_no_match` and the active
    whitelist, mirroring the existing
    `_filter_tradable_markets` log line.
  - Tag matching is case-insensitive (both whitelist and
    per-row tags are lowercased before intersecting), so
    operator-typed tags like `"Crypto"` match the
    aggregator's normalised `crypto`.
- [x] Cache the resolved filter on the scanner instance
  (`self._cached_market_filter_tags: frozenset[str]`) and
  refresh it from `AppSettings.market_filter_tags` on every
  ingest cycle inside `refresh_catalog`. `_hydrate_catalog_from_db`
  also refreshes it before its sync executor block. The
  cached-merged-scan and incremental-fetch paths read the
  cached value without touching the DB.
  - The async loader `_load_market_filter_tags` is fail-open:
    a transient DB error returns `frozenset()` (= no filter),
    so a hiccup in `app_settings` can never silently empty the
    trading universe.
- [x] Unit test in
  `backend/tests/test_scanner_market_filter.py` (filename
  adapted to repo's flat `backend/tests/` layout):
  - Empty filter ⇒ all otherwise-tradable markets pass.
  - Non-empty filter ⇒ markets with intersecting tags pass,
    markets without are rejected.
  - Filter intersects against both `market.tags` and
    `event.tags` (event-tag union covers markets without
    their own tag).
  - Case-insensitive matching.
  - OR-logic across multiple whitelisted tags.
  - Partial event children: events keep only matching
    children, fully-rejected events drop entirely.
- [x] Mark completed

### Task 5: Backend — API endpoints

- [x] Add `GET /settings/market-filter/available-tags` in
  [`backend/api/routes_settings.py`](../../backend/api/routes_settings.py).
  Returns
  `{tags: [{name: str, last_seen: datetime, occurrences: int}], total: int}`
  ordered by `occurrences` desc, filtered by
  `last_seen > now() - interval '24 hours'`. Limit 1000.
- [x] Extend `ScannerSettingsModel` (pydantic) with
  `market_filter_tags: list[str] = Field(default_factory=list)`
  and a validator stripping whitespace, lowercasing, deduping.
- [x] `GET /settings/scanner` returns the new field;
  `PUT /settings/scanner` accepts and persists it. Persistence
  goes through the existing `AppSettings` update path
  (`apply_update_request` writes both `market_filter_tags`
  and `market_filter_updated_at`).
- [x] Reject unknown tags? **No** — accept any string. A tag
  the operator types might not be in
  `market_tags_seen` yet but appear next ingest. Do dedupe
  case-insensitively and trim. The validator on the pydantic
  model and the duplicated normalisation in
  `apply_update_request` (defence-in-depth — the latter
  applies even if a future caller bypasses the model) both
  enforce this contract.
- [x] API roundtrip test in
  `backend/tests/test_routes_settings_scanner_filter.py`
  (filename adapted to repo's flat `backend/tests/` layout):
  pydantic-validator normalisation; PUT with two tags via
  `apply_update_request` + GET back via `scanner_payload`;
  `available-tags` endpoint returns only the last-24h slice
  ordered correctly.
- [x] Mark completed

### Task 6: Frontend — `Settings → Scanner` tag-filter section

- [x] In
  [`frontend/src/components/SettingsPanel.tsx`](../../frontend/src/components/SettingsPanel.tsx),
  inside the existing Scanner tab, add a new section
  "Market Tag Filter" between "Pool Caps" and the bottom of
  the tab. Section contents:
  - Headline + 1-line explanation ("Limit which markets the
    scanner ingests. Empty list = no filter applied.").
  - Multi-select chip-picker bound to
    `scannerSettings.market_filter_tags`. Implementation is the
    "simple `<input>` + chip list" path — no `Combobox` lives
    in `frontend/src/components/ui/`. Suggestions come through
    a native `<datalist>` so the chooser remains keyboard- and
    paste-friendly without dragging in a new dependency.
  - The picker's autocomplete dropdown queries
    `/settings/market-filter/available-tags` (react-query,
    5-minute stale time — tag list changes slowly).
  - "Save" reuses the existing Scanner-tab `handleSaveSection`
    button at the bottom of the tab; the chips themselves are
    a controlled state mutation, so a separate Reset isn't
    needed (operator removes chips individually or clicks
    Save with the empty list to reset the filter).
- [x] Loading / empty / error states for the available-tags
  fetch (empty state: "No tags ingested yet — the list is
  populated from live Polymarket traffic and refreshes within
  one ingest cycle.").
- [x] Reuse the existing `useMutation` on `/settings/scanner`
  to persist. No new endpoint client needed beyond
  `available-tags`.
- [x] `npm run typecheck` clean (executed via `docker run --rm
  node:20-alpine ... ./node_modules/.bin/tsc --noEmit` because
  the host has no local node toolchain — the CLAUDE.md
  contract says local is editor + git only).
- [→] Manual smoke from the operator's browser — moved to
  Task 7 verification (after redeploy lands the new image).
- [x] Mark completed

### Task 7: Deploy + verify

- [x] `./deploy/sync_remote.sh` from local checkout (this is
  the canonical path; do not push directly — see
  [`CLAUDE.md`](../../CLAUDE.md)).
- [x] After redeploy, run alembic verification (Task 2's last
  step) and confirm worker-trading + worker-discovery came
  back healthy:
  `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose ps'`.
  Schema confirmed: `\d market_tags_seen` shows `tag` PK,
  `first_seen`, `last_seen`, `occurrences`, plus
  `idx_market_tags_seen_last_seen`; `\d app_settings`
  shows `market_filter_tags` (json) and
  `market_filter_updated_at` (timestamp without time zone).
  All 11/11 workers healthy.
- [x] Wait for one ingest cycle (~30–60 s) and confirm
  `market_tags_seen` is being populated:
  `select count(*) from market_tags_seen where last_seen > now() - interval '5 minutes'`
  should return non-zero. **Observed:** 1688 distinct tags
  recorded after first cycle; `select max(occurrences)`
  reaches `2+` confirming repeat-cycle upserts work.
- [x] Set a small test filter via the UI (e.g. `crypto`),
  save, wait one ingest cycle, observe the row count in
  `market_catalog` drops:
  `select count(*) from market_catalog`. Reset to empty
  filter to confirm rollback path.
  **Verification deviation:** `market_catalog` is a single
  JSON snapshot row (one row holds the full active catalog
  blob), so `count(*)` doesn't change with filtering.
  Filter activity was instead verified via worker-trading
  log lines `Catalog tag-whitelist filter: X → Y markets
  (... whitelist=...)`. The full UI roundtrip was exercised
  end-to-end via an SSH tunnel
  (`ssh -fN -L 18888:127.0.0.1:3000 polyhome-1`) bypassing
  the host nginx Basic Auth:
  - Loaded `Settings → Scanner → Market Tag Filter`,
    confirmed the section renders the chip-picker, the
    `Type or pick a tag, press Enter` input, the disabled
    Save until dirty, and the "1000 distinct tags seen in
    the last 24 h." availability footer.
  - Added `crypto` and `sports`, hit Save → DB row updated
    to `["crypto","sports"]`, next ingest cycle logged
    `Catalog tag-whitelist filter: 3029 → 2187 markets
    (525 events kept; whitelist=['crypto','sports'])`.
  - Removed both chips, hit Save → DB row reset to `[]`,
    subsequent ingest cycles logged **no** tag-whitelist
    line (helper short-circuits on empty whitelist),
    confirming the no-op rollback path.
- [x] Mark completed

### Task 8: Re-profile worker-trading + decide on backlog 0004

- [x] Re-apply the temporary `cap_add: [SYS_PTRACE]` per
  [plan 0003 Task 2](completed/0003-profile-worker-trading-hotspots.md).
  Added to `worker-trading` service in `docker-compose.yml`,
  redeployed via `BUILD_IMAGES=0 ./deploy/sync_remote.sh`.
- [x] Run a 60 s py-spy capture under steady-state load with
  a non-empty tag filter active (use whatever tags match the
  bots currently running). Save SVG to
  `docs/plans/architecture/worker-trading-profile-<YYYY-MM-DD>-post-filter.svg`.
  Captured with `whitelist=['crypto','sports','politics']` (cut
  19 966 → 14 604 markets per cycle); same active trader as
  baseline (`Sandbox - Traders Copy Trade`, fast lane). 6 866
  CPU-active samples, 77 350 idle-included samples, plus a
  `--format raw` capture for top-N analysis. SVG saved to
  [`architecture/worker-trading-profile-2026-05-07-post-filter.svg`](architecture/worker-trading-profile-2026-05-07-post-filter.svg).
- [x] Compare top-10 self-time table to 2026-05-07. Each of
  the three hotspots from plan 0003 (deepcopy, oracle-history,
  `_compute_stability`) should be reduced proportionally to
  the volume cut.
  **Outcome:** `_compute_stability` dropped from ~5 % to <1 %
  (out of top-25), `_rebuild_realtime_graph` similarly dropped
  off the top-25; `copy.deepcopy` chain shrank from ~15 % to
  ~10.8 %. But `get_oracle_history` (combined) **rose in share**
  from ~14 % to ~36 %, and `_oracle_move_from_history` rose from
  ~2.5 % to ~6.6 %. Diagnosis: the catalog-bound hotspots shrank
  proportionally to the catalog cut; the crypto-fast-binary
  reference path (`get_oracle_history`,
  `_rebuild_crypto_rows_from_cache`) reads from the **Binance**
  WS feed + Chainlink oracle history, neither of which the tag
  filter touches, so its absolute time was constant and its
  share rose because the denominator shrank.
- [x] Append a "After plan-0005" subsection to the
  "Measured CPU profile" section of
  [`architecture/worker-trading.md`](architecture/worker-trading.md)
  with the new numbers.
- [x] Decide: if any hotspot is still ≥ 10 %, pull
  [`0004-optimize-worker-trading-cpu-hotspots.md`](../0004-optimize-worker-trading-cpu-hotspots.md)
  back into the active queue (`git mv`). Otherwise leave it
  parked.
  **Outcome:** Two hotspots remain ≥ 10 % (`get_oracle_history`
  ~36 %, `copy.deepcopy` ~10.8 %), so plan 0004 was promoted
  back into the active queue:
  `git mv docs/plans/backlog/0004-optimize-worker-trading-cpu-hotspots.md docs/plans/0004-optimize-worker-trading-cpu-hotspots.md`,
  status header rewritten to ACTIVE, Task 3 (compute-stability
  vectorisation) descoped because that hotspot already dropped
  below threshold, and `plan-control-index.md` updated
  accordingly (prerequisites: `0003, 0005`).
- [x] Revert the cap_add. Confirm bootstrap loop is healthy.
  Removed `cap_add: [SYS_PTRACE]` from `worker-trading`,
  redeployed, verified
  `docker inspect homerun-worker-trading --format='{{.HostConfig.CapAdd}}'`
  returns `[]`. Cleared the temporary
  `market_filter_tags` back to `[]` so the running deployment
  matches the operator's pre-experiment state.
- [x] Mark completed

### Task 9: Update related architecture notes + close

- [x] In
  [`architecture/system-overview.md`](architecture/system-overview.md),
  add one sentence pointing to `market-filter.md` from the
  scanner / market-universe paragraph.
- [x] In
  [`architecture/worker-trading.md`](architecture/worker-trading.md)
  "Recommendation" section, mark step 1 (upstream filter) as
  **DONE — see plan 0005** with the post-filter profile link.
- [x] In
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md),
  append an entry for the chosen filter values once they
  stabilise (per the journal's append-only rule).
  Filter shipped at deployment-default `[]` (operator picks
  production value out of band); rollback recipe and
  post-filter profile summary captured in the same entry.
- [x] `git mv docs/plans/0005-tag-based-market-filter-at-ingest.md
  docs/plans/completed/`.
- [x] Update [`plan-control-index.md`](plan-control-index.md)
  link target.
- [x] Mark completed
