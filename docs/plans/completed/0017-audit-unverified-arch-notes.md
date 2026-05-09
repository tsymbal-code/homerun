# Plan: Real-diff audit of `<unverified>` architecture notes

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0017` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Plan 0013 introduced the `Last verified: YYYY-MM-DD` discipline
marker on every architecture note; eight notes still carry the
placeholder `<unverified>` because no real diff against code has
been performed since the marker was installed. This plan does
that work for all eight in one coordinated pass.

For each note: (1) verify the cited `file:line` references still
exist, (2) check declared classes / functions / signatures /
table columns still match production code, (3) flag genuine drift
(public surface that has changed) and fix it in-place, (4) bump
the marker to today's date once the diff is performed. Notes that
turn out to be fully accurate get only a date bump.

Out of scope:
- Adding new sections (those go through dedicated arch-note
  plans).
- Re-running `/sync-docs` against deeper history (that is plan-0014
  tooling, not this plan's work).

The eight notes in this audit:

```
backend-architecture.md
copy-trade-pipeline.md
database-and-migrations.md
llm-provider-layer.md
market-filter.md
settings-and-secrets.md
system-overview.md
testing.md
```

"Done" looks like: every note above carries
`Last verified: 2026-05-09` with a brief annotation describing
what was diffed; any genuine drift surfaced by the diff has been
patched in the same commit.

## Context / References

- [`docs/plans/README.md`](README.md) — Ralphex format, `Plan:` trailer convention
- [`docs/plans/architecture/`](architecture/) — directory under audit
- [`.claude/commands/sync-docs.md`](../../.claude/commands/sync-docs.md) — the methodology this plan applies manually
- [`.claude/agents/arch-note-writer.md`](../../.claude/agents/arch-note-writer.md) — subagent role this plan exercises
- Plans 0013 (introduced the marker), 0014 (introduced `/sync-docs` + the `Last verified` discipline)

## Validation Commands

- `for f in backend-architecture copy-trade-pipeline database-and-migrations llm-provider-layer market-filter settings-and-secrets system-overview testing; do grep -q 'Last verified: 2026-05-09' docs/plans/architecture/$f.md || echo "MISSING $f"; done | grep -c MISSING; test $? -eq 1`
- `grep -L 'Last verified: 2026-05-09\|Last verified: 2026-05-08\|Last verified: <unverified>' docs/plans/architecture/*.md | head; test $(grep -L 'Last verified:' docs/plans/architecture/*.md | wc -l) -eq 0`

### Task 1: `backend-architecture.md` audit

- [x] Diff the note against `backend/main.py` lifespan, `backend/api/routes_*.py` listing, `backend/services/strategy_loader.py`, `backend/services/data_source_loader.py`, `backend/services/event_bus.py`, plug-in patterns. Flag drift; patch in-place if surfaced.
- [x] Bump `Last verified:` to `2026-05-09` with a one-line annotation describing the audit.
- [x] Mark completed

### Task 2: `copy-trade-pipeline.md` audit

- [x] Diff against `backend/services/traders_copy_trade_signal_service.py` (post-Plan-0009/0010 publish path), `backend/services/signal_bus.py`, `backend/services/intent_runtime.py`, `backend/services/strategy_signal_bridge.py`, `backend/services/wallet_ws_monitor.py`. Confirm the post-fix publish-via-`bridge_opportunities_to_signals` semantics is faithfully recorded; patch any stale "in-memory event-bus only" claims.
- [x] Bump `Last verified:` to `2026-05-09`.
- [x] Mark completed

### Task 3: `database-and-migrations.md` audit

- [x] Diff against current migration head in `backend/alembic/versions/`, `backend/models/database.py` for major-table column lists, the `_column_names` idempotent-add helper, the `migrate` one-shot service in `docker-compose.yml`. Flag drift on table schemas and migration head.
- [x] Bump `Last verified:`.
- [x] Mark completed

### Task 4: `llm-provider-layer.md` audit

- [x] Diff against `backend/services/ai/llm_provider.py` enum values, the `detect_provider` prefix table, `LLMSettings` Pydantic in `backend/api/routes_settings.py`, `AppSettings` LLM columns in `backend/models/database.py`. Flag drift on provider list, prefix table, key-save lifecycle.
- [x] Bump `Last verified:`.
- [x] Mark completed

### Task 5: `market-filter.md` audit

- [x] Diff against `backend/services/scanner.py` `_apply_market_tag_whitelist`, `_filter_tradable_markets`, `MarketTagsSeen` in `backend/models/database.py`, `Settings → Scanner` UI mapping in `frontend/src/components/SettingsPanel.tsx`. Flag drift; cross-link to `market-quality-and-prioritization.md` and `crypto-fast-binary-lane.md` is already in place per plan 0015.
- [x] Bump `Last verified:`.
- [x] Mark completed

### Task 6: `settings-and-secrets.md` audit

- [x] Diff against `backend/utils/secrets.py` (Fernet keyed off `APP_SECRETS_KEY`), `AppSettings` encrypted columns, the sandbox-account model (`simulation_accounts`, `selected_account_id` semantics — the same field that was the second blocker we hit during the 2026-05-08 trading-flow diagnostic), `routes_settings.py` apply-update flow.
- [x] Bump `Last verified:`.
- [x] Mark completed

### Task 7: `system-overview.md` audit

- [x] Diff against `docker-compose.yml` services + ports, `backend/workers/host.py` plane manifests, the high-level data-flow ladder. Confirm the "Where to look next" table mirrors the actual `architecture/` directory (Plan 0015 added five rows; verify all are present and link-resolvable).
- [x] Bump `Last verified:`.
- [x] Mark completed

### Task 8: `testing.md` audit

- [x] Diff against `backend/pyproject.toml` pytest configuration (60 s timeout, async test mode), `backend/tests/conftest.py`, the test-suite layout (no frontend tests; backend uses `pytest-asyncio`). Confirm the strategy-slug-bound test prohibition still matches `agents.md` § What NOT to Do.
- [x] Bump `Last verified:`.
- [x] Mark completed

### Task 9: Close-out

- [x] Run all Validation Commands locally; all pass.
- [x] `git log --grep='Plan: 0017'` shows the commit set.
- [x] `git mv docs/plans/0017-audit-unverified-arch-notes.md
      docs/plans/completed/`.
- [x] Update the row in `plan-control-index.md` to point at the
      `completed/` path.
- [x] Push to `origin/main`.
- [x] Mark completed
