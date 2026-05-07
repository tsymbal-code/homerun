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
  oracle-history TTL cache). Parked in
  [`backlog/0004-...`](backlog/0004-optimize-worker-trading-cpu-hotspots.md);
  evaluated only if a profile *after* this plan still shows them
  ≥ 10 % each.

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

- [ ] Create [`docs/plans/architecture/market-filter.md`](architecture/market-filter.md)
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
- [ ] Link the note from
  [`docs/plans/README.md`](README.md) "Architecture notes"
  list.
- [ ] Mark completed

### Task 2: DB migration — `market_tags_seen` + `app_settings` columns

- [ ] Generate a new alembic revision under
  `backend/alembic/versions/<sha>_market_tag_filter.py`. The
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
- [ ] Local dry-run: `alembic upgrade head --sql > /tmp/m.sql`,
  visually inspect.
- [ ] Run `alembic upgrade head` against the live DB on
  `polyhome-1` per the deploy recipe in
  [`deploy/AGENTS.md`](../../deploy/AGENTS.md).
- [ ] Verify schema:
  `ssh polyhome-1 "docker compose exec -T postgres psql -U homerun -d homerun -c '\d market_tags_seen'"`.
- [ ] Mark completed

### Task 3: Backend — tag aggregator hook in ingest

- [ ] Add `backend/services/market_tag_aggregator.py` with one
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
- [ ] Hook the call into
  [`scanner.py:980`](../../backend/services/scanner.py)
  `_filter_tradable_markets` **at the top**, before the
  tradable-only loop. Catch and log exceptions — aggregator
  failure must **not** break ingest.
- [ ] Add a configurable batch flag
  `MARKET_TAG_AGGREGATOR_ENABLED` (default `True`) so the
  feature can be disabled at runtime if needed.
- [ ] Unit test in `backend/tests/services/test_market_tag_aggregator.py`:
  feed a fixture list of markets with overlapping tags;
  assert correct upsert counts and that re-running advances
  `last_seen` and `occurrences`.
- [ ] Mark completed

### Task 4: Backend — tag whitelist filter

- [ ] In
  [`scanner.py:937`](../../backend/services/scanner.py)
  `_is_market_tradable`, add a final clause: if
  `app_settings.market_filter_tags` is non-empty, require
  `set(market.tags) | set(market.event_tags)` ∩ filter_tags
  to be non-empty. If the intersection is empty, return
  `False` with a structured reject reason
  `("market_filter_tags_no_match", filter_tags)` so the
  decision is visible in the `_filter_tradable_markets`
  diagnostics.
- [ ] Cache the resolved filter on the
  `MarketUniverseCache` (or equivalent in scanner) and refresh
  it from `AppSettings` on the same cadence as the rest of the
  scanner config (every refresh cycle is fine; we don't need
  millisecond freshness).
- [ ] Unit test in
  `backend/tests/services/test_scanner_market_filter.py`:
  - Empty filter ⇒ all otherwise-tradable markets pass.
  - Non-empty filter ⇒ markets with intersecting tags pass,
    markets without are rejected with the correct reason.
  - Filter intersects against both `market.tags` and
    `event.tags`.
- [ ] Mark completed

### Task 5: Backend — API endpoints

- [ ] Add `GET /settings/market-filter/available-tags` in
  [`backend/api/routes_settings.py`](../../backend/api/routes_settings.py).
  Returns
  `{tags: [{name: str, last_seen: datetime, occurrences: int}], total: int}`
  ordered by `occurrences` desc, filtered by
  `last_seen > now() - interval '24 hours'`. Limit 1000.
- [ ] Extend `ScannerSettingsModel` (pydantic) with
  `market_filter_tags: list[str] = Field(default_factory=list)`
  and a validator stripping whitespace, lowercasing, deduping.
- [ ] `GET /settings/scanner` returns the new field;
  `PUT /settings/scanner` accepts and persists it. Persistence
  goes through the existing `AppSettings` update path.
- [ ] Reject unknown tags? **No** — accept any string. A tag
  the operator types might not be in
  `market_tags_seen` yet but appear next ingest. Do dedupe
  case-insensitively and trim.
- [ ] API roundtrip test in
  `backend/tests/api/test_routes_settings_scanner_filter.py`:
  PUT with two tags, GET back, observe persisted values.
- [ ] Mark completed

### Task 6: Frontend — `Settings → Scanner` tag-filter section

- [ ] In
  [`frontend/src/components/SettingsPanel.tsx`](../../frontend/src/components/SettingsPanel.tsx),
  inside the existing Scanner tab, add a new section
  "Market Tag Filter" between "Pool Caps" and the bottom of
  the tab. Section contents:
  - Headline + 1-line explanation ("Limit which markets the
    scanner ingests. Empty list = no filter applied.").
  - Multi-select chip-picker bound to
    `scannerSettings.market_filter_tags`. Use the project's
    existing `Combobox`/chip component if one exists; otherwise
    a simple `<input>` + chip list is acceptable for a first
    cut.
  - The picker's autocomplete dropdown queries
    `/settings/market-filter/available-tags` (react-query,
    5-minute stale time — tag list changes slowly).
  - "Save" / "Reset" buttons reuse the Scanner-tab pattern.
- [ ] Loading / empty / error states for the available-tags
  fetch (empty state: "No tags ingested yet — the list is
  populated from live Polymarket traffic and refreshes within
  one ingest cycle.").
- [ ] Reuse the existing `useMutation` on `/settings/scanner`
  to persist. No new endpoint client needed beyond
  `available-tags`.
- [ ] `npm run typecheck` clean.
- [ ] Manual smoke from the operator's browser: open
  `Settings → Scanner`, see the new section, pick 2 tags,
  save, observe via SSH that `market_catalog` rows are
  dropped accordingly on the next ingest cycle (~30 s).
- [ ] Mark completed

### Task 7: Deploy + verify

- [ ] `./deploy/sync_remote.sh` from local checkout (this is
  the canonical path; do not push directly — see
  [`CLAUDE.md`](../../CLAUDE.md)).
- [ ] After redeploy, run alembic verification (Task 2's last
  step) and confirm worker-trading + worker-discovery came
  back healthy:
  `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose ps'`.
- [ ] Wait for one ingest cycle (~30–60 s) and confirm
  `market_tags_seen` is being populated:
  `select count(*) from market_tags_seen where last_seen > now() - interval '5 minutes'`
  should return non-zero.
- [ ] Set a small test filter via the UI (e.g. `crypto`),
  save, wait one ingest cycle, observe the row count in
  `market_catalog` drops:
  `select count(*) from market_catalog`. Reset to empty
  filter to confirm rollback path.
- [ ] Mark completed

### Task 8: Re-profile worker-trading + decide on backlog 0004

- [ ] Re-apply the temporary `cap_add: [SYS_PTRACE]` per
  [plan 0003 Task 2](completed/0003-profile-worker-trading-hotspots.md).
- [ ] Run a 60 s py-spy capture under steady-state load with
  a non-empty tag filter active (use whatever tags match the
  bots currently running). Save SVG to
  `docs/plans/architecture/worker-trading-profile-<YYYY-MM-DD>-post-filter.svg`.
- [ ] Compare top-10 self-time table to 2026-05-07. Each of
  the three hotspots from plan 0003 (deepcopy, oracle-history,
  `_compute_stability`) should be reduced proportionally to
  the volume cut.
- [ ] Append a "After plan-0005" subsection to the
  "Measured CPU profile" section of
  [`architecture/worker-trading.md`](architecture/worker-trading.md)
  with the new numbers.
- [ ] Decide: if any hotspot is still ≥ 10 %, pull
  [`backlog/0004-...`](backlog/0004-optimize-worker-trading-cpu-hotspots.md)
  back into the active queue (`git mv`). Otherwise leave it
  parked.
- [ ] Revert the cap_add. Confirm bootstrap loop is healthy.
- [ ] Mark completed

### Task 9: Update related architecture notes + close

- [ ] In
  [`architecture/system-overview.md`](architecture/system-overview.md),
  add one sentence pointing to `market-filter.md` from the
  scanner / market-universe paragraph.
- [ ] In
  [`architecture/worker-trading.md`](architecture/worker-trading.md)
  "Recommendation" section, mark step 1 (upstream filter) as
  **DONE — see plan 0005** with the post-filter profile link.
- [ ] In
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md),
  append an entry for the chosen filter values once they
  stabilise (per the journal's append-only rule).
- [ ] `git mv docs/plans/0005-tag-based-market-filter-at-ingest.md
  docs/plans/completed/`.
- [ ] Update [`plan-control-index.md`](plan-control-index.md)
  link target.
- [ ] Mark completed
