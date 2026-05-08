---
description: Audit recent commits against architecture notes; surface drift; refresh Last verified markers on operator confirmation.
argument-hint: "[N commits, default 20]"
allowed-tools: Bash, Read, Grep, Glob, Edit
---

# /sync-docs

You are auditing the Homerun codebase against its architecture
documentation in `docs/plans/architecture/`. This command produces a
**report**, then — only after the operator confirms — applies the
proposed edits.

`$1` is the number of recent commits to inspect. Default to **20** if
unset or non-numeric.

## Phase 1 — Inspect (read-only)

Run, in order:

1. `git log --oneline -n $1` — capture the window.
2. `git log --name-only --pretty=format: -n $1 | sort -u` — full set of
   touched paths.
3. `git diff --stat HEAD~$1..HEAD 2>/dev/null || true` — visualise the
   weight of the change. Skip silently if the range is invalid (shallow
   clone, fewer commits than requested).

For each touched path, map it to its owning architecture note(s). The
mapping table below is authoritative. When a path matches multiple
rows, report against all of them.

| Path glob | Owning architecture note(s) |
|---|---|
| `backend/services/ai/**` | `ai-and-llm.md`, `llm-provider-layer.md` |
| `backend/services/news/**` | `ai-and-llm.md`, `worker-news.md` (if it exists) |
| `backend/services/strategies/**` | `trader-pipeline.md` (per-strategy: `docs/strategies/<slug>.md`) |
| `backend/services/trader_orchestrator/**` | `trader-pipeline.md` |
| `backend/services/strategy_reverse_engineer/**` | `strategy-reverse-engineer.md` (if it exists), `ai-and-llm.md` |
| `backend/services/fill_simulator/**`, `backend/services/simulation/**`, `backend/services/live_execution_*.py` | `execution-and-fills.md` (if it exists), `trader-pipeline.md` |
| `backend/services/ws_feeds.py`, `backend/services/polymarket_user_feed.py`, `backend/services/binance_feed.py`, `backend/api/websocket.py` | `websocket-and-events.md` (if it exists), `frontend-architecture.md` |
| `backend/workers/**` | `worker-trading.md` / `worker-news.md` / `worker-discovery.md` per plane |
| `backend/alembic/versions/**` | `database-and-migrations.md` |
| `backend/api/routes_*.py` | `backend-architecture.md` |
| `backend/models/database.py` | `database-and-migrations.md`, `settings-and-secrets.md` |
| `backend/utils/secrets.py`, settings touch | `settings-and-secrets.md` |
| `frontend/src/**` | `frontend-architecture.md` |
| `docker-compose.yml`, `deploy/**` | `system-overview.md`, `deploy/AGENTS.md` |

For each owning note that appears in the mapped set:

- Read it.
- For each `file:line` ref or path ref the note cites, run
  `git grep -n` (or `Grep`) and confirm the cited symbol/line still
  exists. Mark mismatches.
- For new public surface in the diff window (new public class, new
  route, new column on a documented table, new `AppSettings` field),
  check whether the note already covers it. Mark gaps.
- Look for an existing `Last verified: YYYY-MM-DD` line. If absent,
  flag it (the marker convention is plan 0013).

## Phase 2 — Report

Print one block per affected note, in this shape:

```
### docs/plans/architecture/<note>.md
Last verified: <date or "missing">

Discrepancies (concrete edits proposed):
  - <bullet>: <what to change, with file:line>

Undocumented new surface:
  - <bullet>: <what to add, with file:line>

Status: [drift detected | in sync | first audit (no marker yet)]
```

After all per-note blocks, print a roll-up:

- `Notes with drift:` <count>
- `Notes in sync (offer to bump Last verified):` <list>
- `Notes never verified (no marker):` <list>

**Do NOT apply any edits in Phase 2.** Stop and ask the operator
which of the three actions to take:

1. Apply only the discrepancy fixes (verbatim, as proposed).
2. Apply discrepancy fixes AND bump `Last verified` on the in-sync
   notes to today's date.
3. Bump `Last verified` only (skip drift fixes — operator wants to
   triage manually).

## Phase 3 — Apply (only after explicit confirmation)

Use `Edit` with exact `old_string` / `new_string` blocks. Never
substitute prose. When bumping `Last verified`:

- If the line exists, replace the date.
- If the line is missing, append it as the last line of the file
  (preceded by a blank line) in the form `Last verified: YYYY-MM-DD`
  where the date is today (UTC).

After applying, re-run Phase 1 quickly on the touched notes to
confirm zero remaining discrepancies, and print:

```
Applied: N edits across M notes. Last verified bumped on K notes.
```

## Footguns

- **Don't conflate the per-strategy operator docs (`docs/strategies/`)
  with architecture notes.** The operator notes are Ukrainian and
  user-facing; do not auto-edit them. Only mention them in the report
  when a strategy slug clearly drifted, and let the operator decide.
- **`git log` against a shallow clone** can miss commits older than the
  fetch depth. If `git log -n $1` returns fewer than `$1` commits and
  `git rev-parse --is-shallow-repository` reports `true`, mention it
  in the report header.
- **Architecture notes can outpace the code by design.** Some notes
  describe future state (e.g. references to a migration that lands in
  the same plan). Trust the operator if they say "this is intentional";
  don't bulldoze the note.
