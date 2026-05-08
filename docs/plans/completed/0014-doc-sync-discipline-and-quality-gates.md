# Plan: Documentation hygiene and quality gates for AI agents

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0014` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Plans 0012 and 0013 installed the agent-onboarding scaffolding
(Cursor rules, Claude Code commands and subagents, architecture
notes, the `Last verified` marker). What is still missing is the
**discipline that keeps it true over time**:

- An agent can today change `services/strategies/news_edge.py`
  without touching `docs/strategies/news-edge.md`. Defaults
  silently drift; the operator catches it days later.
- The same is true for `docs/plans/architecture/<topic>.md` — code
  evolves, the note doesn't, the `Last verified` date never gets
  bumped.
- Commits without a `Plan: <NNNN>` trailer slip through; the
  trailer convention from `docs/plans/README.md` is currently
  honour-system.
- `/sync-docs` (introduced in Plan 0012) only audits architecture
  notes — it does not look at strategy docs.

This plan adds the small set of guardrails that make those
discipline failures visible at the moment they happen, not days
later. It is **rules + checks**, not new code paths. No runtime
behaviour change.

"Done" looks like: an agent that touches a strategy file or an
arch-relevant code path is reminded — by the Cursor rule, by the
`agents.md` codex, by `/sync-docs`, and (on commit) by a one-shot
pre-commit check — to update the matching doc and stamp `Last
verified` if appropriate.

## Context / References

- [`docs/plans/README.md`](README.md) — Ralphex format, `Plan:` trailer convention
- [`agents.md`](../../agents.md) — Where AI agents start (the index this plan extends)
- [`CLAUDE.md`](../../CLAUDE.md) — entry point
- [`.cursor/rules/strategies.mdc`](../../.cursor/rules/strategies.mdc) — strategy rules to extend
- [`.cursor/rules/ai-llm.mdc`](../../.cursor/rules/ai-llm.mdc), [`.cursor/rules/backend.mdc`](../../.cursor/rules/backend.mdc), [`.cursor/rules/migrations.mdc`](../../.cursor/rules/migrations.mdc), [`.cursor/rules/frontend.mdc`](../../.cursor/rules/frontend.mdc) — area rules to extend
- [`.claude/commands/sync-docs.md`](../../.claude/commands/sync-docs.md) — slash command to extend with strategy-doc mapping
- [`.claude/agents/plan-validator.md`](../../.claude/agents/plan-validator.md), [`commit-trailer-checker.md`](../../.claude/agents/commit-trailer-checker.md) — existing subagents
- Plans 0012 and 0013 — prerequisites; this plan builds on their artefacts

## Validation Commands

- `grep -q 'docs/strategies/' .cursor/rules/strategies.mdc`
- `grep -q 'Documentation hygiene' agents.md`
- `grep -q 'docs/strategies/' .claude/commands/sync-docs.md`
- `test -f .claude/commands/pre-commit-check.md`
- `bash .claude/hooks/check-plan-trailer.sh < /dev/null >/dev/null 2>&1; test $? -ne 127`
- `for f in docs/plans/architecture/*.md; do grep -q 'Last verified:' "$f" || echo "MISSING: $f"; done | grep -c MISSING; test $? -eq 1`

### Task 1: Strategy doc-sync rule

The single highest-leverage gap: `services/strategies/<slug>.py`
and `docs/strategies/<slug>.md` are paired files, but no rule
binds them. Extend the strategy Cursor rule and the codex section
so an agent reading either file sees the obligation.

- [x] Extend `.cursor/rules/strategies.mdc` with a "Documentation
      sync" section: when adding/changing a strategy under
      `backend/services/strategies/<slug>.py`, the agent must
      check `docs/strategies/<slug>.md` and update it (or flag a
      drift) for any change to: `*_DEFAULT_CONFIG`, class
      docstring, `evaluate`/`should_exit` semantics, the
      `Subscriptions` set, the source key. The rule lists the
      exact filename mapping (kebab-case slug ↔ underscored
      module).
- [x] Add a parallel reminder to `.cursor/rules/plans.mdc` that
      a *new* strategy under `services/strategies/` always lands
      together with a new `docs/strategies/<slug>.md` (Ukrainian).
      The agent **may write and edit these docs directly** —
      operator confirmed full edit permissions on
      `docs/strategies/`. Match the voice and structure of
      sibling docs (`basic.md`, `news-edge.md`,
      `weather-distribution.md`) — Ukrainian prose, the same
      section skeleton (Сутність / Контракт / Логіка детекції /
      Логіка виходу / Налаштування за замовчуванням / Коли НЕ
      працює / Посилання).
- [x] Add an `agents.md` § "Documentation hygiene" subsection
      stating the same obligation in codex form, plus its mirror
      for `docs/plans/architecture/` (every layer change ⇒ note
      update + `Last verified` bump).
- [x] Mark completed

### Task 2: Architecture-doc sync reminder per area rule

`Last verified` exists, but nothing in the per-area Cursor rules
points the agent at it. Each rule already references its
canonical architecture note; add a single explicit obligation.

- [x] Append to `.cursor/rules/backend.mdc`,
      `.cursor/rules/frontend.mdc`,
      `.cursor/rules/migrations.mdc`,
      `.cursor/rules/ai-llm.mdc`,
      `.cursor/rules/strategies.mdc`: a one-paragraph "When you
      change this layer's contract, also update <its
      architecture note> and bump `Last verified` to today's
      date (UTC). Contract = anything documented in the
      ## Contracts or ## Key files section." Each rule names its
      own note explicitly so the obligation is unambiguous.
- [x] Mark completed

### Task 3: Extend `/sync-docs` to cover strategy docs

`/sync-docs` knows about architecture notes but not about
`docs/strategies/`. Add the mapping plus the staleness check.

- [x] Edit `.claude/commands/sync-docs.md` Phase 1 mapping table
      to include the row
      `backend/services/strategies/<slug>.py → docs/strategies/<slug>.md`
      (with the kebab/underscore mapping rule).
- [x] Add a Phase 2 sub-report block "Strategy doc drift" that
      diffs each touched strategy module's `*_DEFAULT_CONFIG` and
      `Subscriptions` against the corresponding `docs/strategies/<slug>.md`
      tables. Treat the operator-written prose as
      authoritative; only flag *factual* mismatches (numeric
      defaults, slug, source key, subscriptions, class name).
- [x] Document in the command file: the agent **may auto-edit**
      `docs/strategies/*.md` (operator confirmed full edit
      permissions). Behaviour mirrors the architecture-note path
      — propose the diff in Phase 2, apply only after operator
      confirmation in Phase 3. Match Ukrainian voice and section
      skeleton when amending.
- [x] Mark completed

### Task 4: `/pre-commit-check` — one-shot pre-commit audit

A short slash command operators run before `git commit`. Lists
staged changes, maps them to the docs they should touch, and
warns if a `Plan: <NNNN>` trailer is missing.

- [x] Create `.claude/commands/pre-commit-check.md` with the
      contract:
      1. Run `git diff --cached --name-only` and `git status --short`.
      2. Map each staged code path to its expected doc neighbour
         (using the same table as `/sync-docs`).
      3. For each mapped doc: report whether it is also staged.
      4. Look at `git config commit.template` and the recent
         commit history; suggest the next free `Plan: <NNNN>`
         number if the staged set looks plan-worthy and no
         `Plan:` trailer is mentioned in the user's draft message
         (caller passes the draft as `$1`).
      5. Print a compact PASS / WARN / FAIL summary so the
         operator can fix before committing.
      Behaviour: report-only. Never run `git add` / `git commit` /
      anything mutating.
- [x] Index it in `agents.md` § "Where AI agents start".
- [x] Mark completed

### Task 5: `PreToolUse` hook nudging on `git commit` without trailer

Soft guardrail (warn, don't block) — same flavour as the
`UserPromptSubmit` SSH reminder from plan 0012. When the agent is
about to run `git commit -m ...` the hook reads the message,
checks for `Plan: <NNNN>` or a whitelisted no-plan keyword, and
either passes through silently or prints a one-paragraph
reminder before the command runs.

- [x] Create `.claude/hooks/check-plan-trailer.sh`. It receives a
      JSON payload with `tool_name` and `tool_input` on stdin
      ([Claude Code hook contract](https://docs.claude.com/en/docs/claude-code/hooks)).
      For `tool_name == "Bash"`, parse `tool_input.command`. If
      it starts with `git commit` and the message body does not
      contain `Plan: ` *and* does not contain `[no-plan]` (the
      explicit opt-out for typo fixes / deps), emit a warning to
      stdout with the trailer convention reminder.
- [x] Wire it into `.claude/settings.json` under `hooks.PreToolUse`
      with `matcher: "Bash"`. The hook **must not** block — exit 0
      regardless of finding; project policy is to nudge, not
      gate (mirroring the `UserPromptSubmit` reminder).
- [x] Smoke-test: `git commit` with `Plan: 0014` → no warning.
      `git commit -m "test"` → warning printed. `git commit -m
      "test [no-plan]"` → no warning. Plus `--amend` and non-Bash
      tool: silent. All 5 cases verified.
- [x] Mark completed

### Task 6: Codex section — Documentation hygiene

A single, dense section in `agents.md` that an agent finds
during the standard onboarding read. Pulls together the
obligations sprinkled across the rules above so the codex stays
the canonical index.

- [x] Add `## Documentation hygiene` to `agents.md`, placed
      immediately after "Where AI agents start". Cover:
      1. Every code-changing commit must update the matching
         doc(s) — strategy → `docs/strategies/<slug>.md`, layer →
         `docs/plans/architecture/<topic>.md`. If the change is
         genuinely doc-irrelevant (refactor with no contract
         change), state so in the commit body.
      2. `Last verified: YYYY-MM-DD` is bumped only after a real
         diff against code; never as a reflex.
      3. Deleting code = deleting its doc paragraph in the same
         commit; orphan paragraphs are bugs.
      4. `Plan: <NNNN>` trailer is mandatory for every plan-
         driven commit. The exempt list (typo fix, deps bump,
         emergency hotfix) is small and noted in
         `docs/plans/README.md` § Commits and traceability.
      5. Pre-flight: run `/sync-docs N` and `/pre-commit-check`
         before committing a non-trivial change.
- [x] Mark completed

### Task 7: Cleanup — orphan/stale checks

Two small audits that surfaced during plan 0014 drafting:

- [x] Delete `docs/UPDATE.md` (operator decision: 2-months
      stale, 0 inbound refs, redundant with desktop-launcher
      docs). Run `grep -rl 'UPDATE\.md' docs/ CLAUDE.md
      agents.md README.md` first; clean up any inbound
      references. Then `git rm docs/UPDATE.md`.
- [x] Verify `docs/plans/architecture/copy-trade-pipeline.md` and
      `docs/plans/architecture/worker-trading.md` got their
      `Last verified` line as part of plan 0011's close-out
      (deferred from plan 0013). If not, add the line as
      `<unverified>` here.
- [x] Run `/sync-docs 30` (manual) to surface any drift the
      pipeline introduced since plan 0013 closed.
- [x] Mark completed

### Task 8: Close-out

- [x] Run all Validation Commands locally; all pass.
- [x] `git log --grep='Plan: 0014'` shows the full commit set.
- [x] `git mv docs/plans/0014-doc-sync-discipline-and-quality-gates.md
      docs/plans/completed/`.
- [x] Update the row in `plan-control-index.md` to point at the
      `completed/` path.
- [x] Mark completed
