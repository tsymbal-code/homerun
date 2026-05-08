---
description: Pre-commit audit — staged files, paired-doc coverage, Plan trailer presence. Report-only; does not stage or commit.
argument-hint: "[draft commit message]"
allowed-tools: Bash, Read, Grep, Glob
---

# /pre-commit-check

You are auditing what the operator is **about** to commit, before
the commit happens. Your output is a compact PASS / WARN / FAIL
summary so the operator can fix issues in-place. Do not stage,
commit, push, or modify any file.

`$1` (optional): the draft commit message text. If passed, use it
to detect a `Plan: <NNNN>` trailer or `[no-plan]` opt-out. If not
passed, suggest the operator inspect their draft separately.

## Phase 1 — What is staged

Run, in order, capturing the output:

1. `git status --short` — full picture.
2. `git diff --cached --name-only` — staged paths only.
3. `git diff --cached --stat` — sense of weight per file.
4. `git log --oneline -1` — last commit, useful as a sibling
   reference.

If `git diff --cached` is empty, abort with a single line:
`Nothing staged. Stage with 'git add' first, then re-run.`

## Phase 2 — Paired-doc coverage

For each staged path, look up the doc(s) it should travel with,
using the same mapping as `/sync-docs`:

| Staged path | Paired doc(s) |
|---|---|
| `backend/services/ai/**`, `backend/services/news/**` | `docs/plans/architecture/ai-and-llm.md`, `docs/plans/architecture/llm-provider-layer.md` |
| `backend/services/strategies/<snake>.py` | `docs/plans/architecture/trader-pipeline.md` AND `docs/strategies/<kebab>.md` |
| `backend/services/trader_orchestrator/**` | `docs/plans/architecture/trader-pipeline.md` |
| `backend/services/strategy_reverse_engineer/**` | `docs/plans/architecture/strategy-reverse-engineer.md` |
| `backend/services/fill_simulator/**`, `backend/services/simulation/**`, `backend/services/live_execution_*.py` | `docs/plans/architecture/execution-and-fills.md` |
| `backend/services/ws_feeds.py`, `backend/services/polymarket_user_feed.py`, `backend/services/binance_feed.py`, `backend/api/websocket.py` | `docs/plans/architecture/websocket-and-events.md` |
| `backend/services/insider_detector.py`, `anomaly_detector.py`, `wallet_intelligence.py` | `docs/plans/architecture/wallet-intelligence.md` |
| `backend/services/execution_safety.py`, `execution_tiers.py`, `price_chaser.py`, `token_circuit_breaker.py`, `live_pressure.py`, `position_monitor.py`, `stuck_position_monitor.py`, `market_tradability.py`, `live_market_detector.py` | `docs/plans/architecture/execution-defense.md` |
| `backend/services/crypto_service.py` | `docs/plans/architecture/crypto-fast-binary-lane.md` |
| `backend/services/market_regime.py`, `quality_filter.py`, `market_monitor.py`, `market_prioritizer.py`, `sport_classifier.py`, `category_buffers.py`, `depth_analyzer.py` | `docs/plans/architecture/market-quality-and-prioritization.md` |
| `backend/services/execution_latency_metrics.py`, `latency_tracker.py` | `docs/plans/architecture/execution-and-fills.md` § Observability |
| `backend/workers/**` | the matching plane note (`worker-trading.md` / `worker-news.md` / `worker-discovery.md`) |
| `backend/alembic/versions/**`, `backend/models/database.py` | `docs/plans/architecture/database-and-migrations.md` |
| `backend/api/routes_*.py` | `docs/plans/architecture/backend-architecture.md` |
| `backend/utils/secrets.py` (or `*api_key*` columns on `AppSettings`) | `docs/plans/architecture/settings-and-secrets.md` |
| `frontend/src/**` | `docs/plans/architecture/frontend-architecture.md` (and `websocket-and-events.md` if WS is touched) |
| `docker-compose.yml`, `deploy/**` | `docs/plans/architecture/system-overview.md`, `deploy/AGENTS.md` |

For each paired doc:

- If it is **also staged** in this commit → `PASS` for this row.
- If it is **modified locally but not staged** (`git status` shows
  ` M`) → `WARN: doc modified but not staged`. Suggest
  `git add <path>`.
- If it is **untouched** → `WARN: paired doc not modified`.
  Suggest the operator confirm whether the change really needs no
  doc update; if it does, edit the doc and stage it.

For staged strategies (`backend/services/strategies/<snake>.py`):
also check that the `*_DEFAULT_CONFIG` dict and the class
declarations in the staged module are reflected by the doc's
`## Налаштування за замовчуванням` table (numerically) and the
`## Контракт` block (slug, source_key, subscriptions). Flag any
factual mismatch.

## Phase 3 — Plan trailer

If `$1` was provided, scan it for:

- A line matching `^Plan: [0-9]{4}$` → `PASS`.
- The literal `[no-plan]` anywhere → `PASS` (explicit opt-out for
  typo / deps / hotfix).
- Neither → `WARN: no Plan: trailer detected`. Suggest the next
  free `Plan: <NNNN>` number by listing
  `docs/plans/[0-9]*-*.md docs/plans/{backlog,completed}/[0-9]*-*.md | sort | tail`.

If `$1` was not provided, print:
`Plan trailer: not checked (pass the draft commit message as $1
to enable this check).`

Also report whether the staged set "looks plan-worthy":

- Multiple files across different layers → likely yes.
- One small doc edit, one trivial refactor → likely no, `[no-plan]`
  is fine.

## Output format

```
/pre-commit-check report
Staged: <N> files (<+I/-D> lines)

Paired-doc coverage:
  [ ✓ ] backend/services/strategies/news_edge.py
        ↳ docs/strategies/news-edge.md (staged)
        ↳ docs/plans/architecture/trader-pipeline.md (staged)
  [ ! ] backend/services/ai/opportunity_judge.py
        ↳ docs/plans/architecture/ai-and-llm.md (NOT staged, modified)
        ↳ git add docs/plans/architecture/ai-and-llm.md

Strategy default-config consistency:
  [ ✓ ] news_edge.py vs news-edge.md — defaults match

Plan trailer:
  [ ! ] no `Plan:` trailer or `[no-plan]` keyword in draft
        ↳ next free Plan ID: 0015 (suggest)
        ↳ if off-plan, append `[no-plan]` to the message body

Summary: 1 PASS, 2 WARN, 0 FAIL
```

If everything is clean: `Summary: N PASS, 0 WARN, 0 FAIL — clean to
commit.`

## Footguns

- **Do not modify or stage anything.** If the operator wants the
  fixes applied, they re-run `/sync-docs` or edit by hand.
- **Architecture notes use `Last verified`** — flag the marker
  when its date is not today on a note you would expect this
  commit to refresh, but do not auto-bump.
- **Treat `[no-plan]` as the canonical opt-out.** Do not invent
  alternatives (`Plan: none`, `skip-plan`, etc.) — they will not
  match the `PreToolUse` hook's matcher.
