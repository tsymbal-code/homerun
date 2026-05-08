# Plan: Agent onboarding baseline — Cursor rules + Claude Code skills

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0012` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

The repo today has solid human-facing docs (`CLAUDE.md`,
`agents.md`, `deploy/AGENTS.md`, `docs/plans/`) but very thin
machine-facing scaffolding for AI coding agents. `.claude/` has only
a `launch.json`; `.cursor/rules/` has a single always-on rule. The
result is that every new chat repeats the same onboarding work, and
every new agent has to be told manually about Ralphex format, the
SSH-only diagnostic loop, and the LLM-vs-ML split documented in
[`ai-and-llm.md`](architecture/ai-and-llm.md).

This plan installs the missing scaffolding so a fresh AI session in
either Cursor or Claude Code arrives pre-loaded with the right
context for whatever file or topic it touches:

- **Cursor:** per-area auto-attach `.mdc` rules under
  `.cursor/rules/` (backend, frontend, migrations, plans, AI/LLM,
  strategies).
- **Claude Code:** a `settings.json` with a curated permissions
  allowlist and a single reminder hook; a small `commands/`
  directory with one slash command (`/sync-docs`); an `agents/`
  directory with three focused subagents (plan validator, arch-note
  writer, commit-trailer checker).
- **`agents.md`** gets a short "Where AI agents start" section that
  links to all of the above so the codex itself stays the canonical
  index.

"Done" looks like: opening a chat in Cursor inside `backend/` auto-
attaches the backend rule; opening Claude Code lets the operator run
`/sync-docs` and dispatch the three subagents without typing
prompts; `agents.md` lists every artefact this plan creates.

No runtime code changes. No DB schema changes. No strategy or
worker changes.

## Context / References

- [`README.md`](README.md) — Ralphex plan format, policy header rules
- [`agents.md`](../../agents.md) — agent codex this plan extends
- [`CLAUDE.md`](../../CLAUDE.md) — Claude Code entry point
- [`.cursor/rules/homerun.mdc`](../../.cursor/rules/homerun.mdc) — existing always-on Cursor rule
- [`architecture/ai-and-llm.md`](architecture/ai-and-llm.md) — context the AI/LLM rule must point at
- Cursor MDC rule format: `.mdc` with YAML frontmatter (`description`, `globs`, `alwaysApply`)
- Claude Code custom commands and subagents: `.claude/commands/<name>.md` and `.claude/agents/<name>.md`
- Plan 0013 (architecture doc gap audit) depends on this plan's `/sync-docs` command and on `Last verified` discipline introduced here

## Validation Commands

- `python -c "import json; json.load(open('.claude/settings.json'))"`
- `test $(find .cursor/rules -name '*.mdc' | wc -l) -ge 7`
- `test $(find .claude/commands -name '*.md' | wc -l) -ge 1`
- `test $(find .claude/agents -name '*.md' | wc -l) -ge 3`
- `grep -L 'description:' .cursor/rules/*.mdc; test $? -eq 1`
- `grep -q '/sync-docs' agents.md && grep -q '.cursor/rules' agents.md && grep -q '.claude/agents' agents.md`

### Task 1: Cursor auto-attach rules per area

Extend `.cursor/rules/` with scoped `.mdc` files. Each new file uses
`alwaysApply: false` plus a `globs:` matcher so it loads only when a
matching file is in the chat context. Each rule is short (≤ 60
lines), points at the canonical doc instead of restating it, and
notes the "do not do this" footguns most often hit by AI agents in
that area.

- [x] Add `.cursor/rules/backend.mdc` (globs: `backend/**/*.py`).
      Anchors: async patterns, FastAPI route shape, AsyncSessionLocal
      retry pattern, Pydantic v2 (no `@validator`, no `class Config`),
      structured logging via `utils.logger.get_logger`. Link to
      [`backend-architecture.md`](architecture/backend-architecture.md).
- [x] Add `.cursor/rules/frontend.mdc` (globs:
      `frontend/src/**/*.{ts,tsx}`). Anchors: Jotai vs react-query
      split, shared WebSocket singleton, mandatory
      `normalizeUtcTimestampsInPlace`, shadcn/ui imports, no extra
      WS connections. Link to
      [`frontend-architecture.md`](architecture/frontend-architecture.md).
- [x] Add `.cursor/rules/migrations.mdc` (globs:
      `backend/alembic/versions/**/*.py`). Anchors: `_column_names`
      idempotent guard, `down_revision` chain, no `op.execute`
      destructive ops, downgrade is `pass`. Link to
      [`database-and-migrations.md`](architecture/database-and-migrations.md).
- [x] Add `.cursor/rules/plans.mdc` (globs: `docs/plans/**/*.md`).
      Anchors: mandatory Plan policy header (verbatim copy), Tasks
      checkbox rule, `Mark completed` last item, `Plan: <NNNN>`
      trailer convention. Link to
      [`README.md`](README.md).
- [x] Add `.cursor/rules/ai-llm.mdc` (globs: `backend/services/ai/**`,
      `backend/services/news/**`). Anchors: budget gates
      (`ai_max_monthly_spend`, `news_workflow_cycle_llm_call_cap`),
      feature toggles (`ai_enabled`, per-feature flags),
      LLM-vs-non-LLM split, `LLMUsageLog.purpose` discipline. Link
      to [`ai-and-llm.md`](architecture/ai-and-llm.md) and
      [`llm-provider-layer.md`](architecture/llm-provider-layer.md).
- [x] Add `.cursor/rules/strategies.mdc` (globs:
      `backend/services/strategies/**/*.py`). Anchors:
      `BaseStrategy` contract, `detect` vs `detect_async`,
      `evaluate`/`should_exit` ExitDecision dataclass, never test by
      strategy slug. Link to
      [`trader-pipeline.md`](architecture/trader-pipeline.md).
- [ ] Verify: open one file from each area in Cursor and confirm the
      matching rule appears in the rule pill. (Manual check —
      operator-driven.)
- [x] Mark completed

### Task 2: `.claude/settings.json` — permissions and one reminder hook

Create the settings file so common read-only operations and the
`ssh polyhome-1` diagnostic loop don't prompt for approval each
time. Keep the allowlist tight: read-only inspection only; nothing
that can mutate code, server state, or external services.

- [x] Create `.claude/settings.json` with `permissions.allow` covering:
      `Bash(git status*)`, `Bash(git diff*)`, `Bash(git log*)`,
      `Bash(git show*)`, `Bash(ls*)`, `Bash(find*)`, `Bash(rg*)`,
      `Bash(wc*)`, `Bash(cat:*)` (where harness allows), and the
      SSH read-only diagnostic forms documented in
      [`deploy/AGENTS.md`](../../deploy/AGENTS.md):
      `Bash(ssh polyhome-1 'docker compose ps*)`,
      `Bash(ssh polyhome-1 'docker compose logs*)`,
      `Bash(ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/health*)`,
      `Bash(ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/api/*)`.
- [x] Add `permissions.deny` for the well-known footguns:
      `Bash(curl http://localhost:*)`, `Bash(curl http://127.0.0.1:*)`
      (anything that targets the local checkout — `CLAUDE.md`'s
      "single most important fact"), `Bash(rm -rf data/*)`,
      `Bash(git push --force*)`.
- [x] Add a single `UserPromptSubmit` hook that injects a reminder
      block when the user prompt mentions `localhost`, `psql`,
      `docker compose up`, or `curl http://`. The reminder points at
      `CLAUDE.md` § "The single most important fact" and the SSH
      command catalog. Hook script lives at
      `.claude/hooks/remind-ssh.sh`, runs in <100 ms, only writes to
      stdout (no side effects).
- [x] Verify settings parse: `python -c "import json;
      json.load(open('.claude/settings.json'))"`.
- [x] Mark completed

### Task 3: `/sync-docs` slash command

The single command the operator asked for: given a window of
recent commits, identify which files changed, map them to the
architecture notes that cover those areas, surface concrete
discrepancies, and offer to refresh the `Last verified` markers
(introduced in plan 0013) where the note still matches reality.

- [x] Create `.claude/commands/sync-docs.md` with a prompt template
      that accepts an optional `$1` argument (number of commits to
      review; default 20). The template instructs the agent to:
      1. Run `git log --oneline -n $1` and `git diff --name-only`
         for that window.
      2. Build a map `{changed_file → owning architecture note}`
         using the `Where to find more` table in `CLAUDE.md` and
         `system-overview.md` `Where to look next`.
      3. For each owning note, scan it against the actual code at
         the cited file:line refs and report:
         (a) lines in the note that no longer match,
         (b) new public surface in the code not yet documented,
         (c) `Last verified` date if present.
      4. Propose specific edits as a checklist; do NOT apply them
         without operator confirmation.
      5. Where the note still matches reality, offer to bump
         `Last verified: YYYY-MM-DD` to today.
- [x] Document the command in `agents.md` (Task 5) and `CLAUDE.md`
      (Task 6).
- [ ] Smoke-test: invoke `/sync-docs 5` against the most recent 5
      commits and confirm the agent produces a coherent report.
      (Manual check.)
- [x] Mark completed

### Task 4: Subagents under `.claude/agents/`

Three narrow, read-mostly subagents. Each is one Markdown file with
YAML frontmatter (`name`, `description`, `tools`) plus a system
prompt. Tools are restricted to the minimum each role needs.

- [x] Add `.claude/agents/plan-validator.md`. Tools: `Read`, `Bash`
      (read-only git + ls), `Grep`. Role: given a plan filename or
      ID, validate it against `docs/plans/README.md` —
      mandatory policy header verbatim, Tasks-only checkboxes,
      `Mark completed` as last item of each task, Validation
      Commands runnable, every referenced file exists. Output: a
      pass/fail report; do NOT edit the plan.
- [x] Add `.claude/agents/arch-note-writer.md`. Tools: `Read`,
      `Write`, `Edit`, `Grep`, `Bash` (read-only). Role: write or
      update one `docs/plans/architecture/<topic>.md` from the
      Ralphex skeleton (Purpose / Key files / Contracts /
      Dependencies / Extension points / Last verified). Refuses to
      include speculative content; only documents what `git grep`
      can confirm. Caller passes the topic and the code paths to
      cover.
- [x] Add `.claude/agents/commit-trailer-checker.md`. Tools: `Bash`
      (read-only git). Role: given a SHA or a commit range, verify
      that every commit carries exactly one `Plan: <NNNN>` trailer
      OR is on the small whitelist of trailer-exempt operations
      (typo fix, dependency bump from a script). Output: list of
      offenders; do NOT rewrite history.
- [ ] Smoke-test each subagent on a real recent example. (Manual.)
- [x] Mark completed

### Task 5: `agents.md` — "Where AI agents start" section

Per the operator's preference, no `backend/AGENTS.md` /
`frontend/AGENTS.md` sub-files. Instead, a single section near the
top of the existing root `agents.md` lists every artefact this plan
creates so a fresh agent has one canonical pointer. Bonus: the
section also serves as the discoverability map for plan 0013's
new architecture notes.

- [x] Add a `## Where AI agents start` section to `agents.md`,
      placed immediately after the deployment-topology block.
      Content: a tight bulleted index of `.cursor/rules/*.mdc` (with
      one-line each), `.claude/commands/sync-docs.md`,
      `.claude/agents/{plan-validator, arch-note-writer,
      commit-trailer-checker}.md`, and the policy that every new
      Cursor rule or Claude artefact gets one line here.
- [x] Cross-check that the section is reachable from
      `CLAUDE.md` (Task 6 ensures the link).
- [x] Mark completed

### Task 6: Index sync and cross-link sweep

After Tasks 1–5 land the artefacts, the index files (`CLAUDE.md`,
`plan-control-index.md`) need to learn about them. Also: Plan 0011
landed a new architecture note that is not in
`plan-control-index.md` Cross-references yet
(`copy-trade-pipeline.md`); fold it in with the rest.

- [x] Update `CLAUDE.md` "Where to find more" table with rows for
      `.cursor/rules/`, `.claude/commands/sync-docs.md`,
      `.claude/agents/`, and (forward-link) `architecture/ai-and-llm.md`
      already added.
- [x] Update `docs/plans/plan-control-index.md` Cross-references
      list to include `ai-and-llm.md`, `copy-trade-pipeline.md`,
      and the new `.cursor/rules/` and `.claude/` directories.
- [x] Add a row for plan 0012 to the Index table in
      `plan-control-index.md` (category D, prerequisites —).
- [x] Mark completed

### Task 7: Close-out

- [x] Run all Validation Commands locally; all pass.
- [x] `git log --grep='Plan: 0012'` shows the full commit set.
- [x] `git mv docs/plans/0012-agent-onboarding-baseline.md
      docs/plans/completed/`.
- [x] Update the row in `plan-control-index.md` to point at the
      `completed/` path.
- [x] Mark completed
