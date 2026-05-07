# Plans

`docs/plans/` is the working directory for active plans against the
Homerun codebase. The structure and file format intentionally mirror
the [Ralphex](https://ralphex.com/docs/) convention so that, if
desired, Ralphex can be pointed at this tree without further
adaptation.

## Directory layout

```
docs/plans/
├── README.md                        # this file — format, lifecycle, conventions
├── plan-control-index.md            # ordering, prerequisites, categories
├── architecture/                    # reference docs that plans cite
│   └── <topic>.md                   # one layer per file (LLM, sandbox…)
├── completed/                       # archive of finished plans
│   └── <NNNN>-<plan-slug>.md
├── <NNNN>-<plan-slug>.md            # active plan
└── ...
```

- **Active plans** live at the top level of `docs/plans/`. Every active
  plan has at least one open task (`- [ ]`). The directory is sorted
  by sequence number, but the actual ordering rules — categories,
  prerequisites, which plan to pick next — live in
  [`plan-control-index.md`](plan-control-index.md).
- **Completed plans** move to `docs/plans/completed/` the moment all
  their tasks are checked off (`- [x]`). The file content does not
  change — only its directory. That move is the boundary between
  in-progress and done.
- **Architecture notes** live in `docs/plans/architecture/` and
  contain no tasks. Each note describes one slice of the system (LLM,
  sandbox/shadow mode, scanner pipeline, etc.) including who depends
  on it. Plans link to these notes from a `## Context / References`
  section.

## Plan file format (Ralphex)

The required skeleton:

```markdown
# Plan: <human-readable name>

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview
One or two paragraphs: what we're doing, why, and what "done" looks like.

## Context / References
- [Architecture: Foo Layer](architecture/foo-layer.md)
- [models/database.py:1252](../../backend/models/database.py)

## Validation Commands
- `docker compose exec backend pytest -q`
- `docker compose exec backend ruff check`
- `cd frontend && npm run typecheck`

### Task 1: <short title>
- [ ] Specific step with a file reference
- [ ] Another specific step
- [ ] Mark completed

### Task 2: ...
- [ ] ...
- [ ] Mark completed
```

The blockquote at the top is the **Plan policy header**. It is
mandatory on every plan. It exists so that any agent — human or
LLM — picking up the plan cold knows where the rules live without
having to be told. Don't paraphrase it; copy it verbatim from the
template, adjust nothing.

Rules:

1. The plan title is `# Plan: ...`. The filename is a kebab-case slug
   prefixed with a zero-padded sequence number that reflects the
   order in which the plan was created — for example
   `0001-add-nvidia-nim-provider.md`, `0002-...`. The full slug
   (number plus name) becomes the git branch name automatically when
   the plan is run via Ralphex. Numbers are not gaps-checked: deleted
   or aborted plans leave their numbers reserved.
2. `- [ ]` / `- [x]` checkboxes are allowed **only inside `### Task N:`
   sections**. Overview, Context, Validation Commands stay
   checkbox-free.
3. Task numbers can be integers or fractional (`### Task 2.5:`,
   `### Task 2a:`). `### Iteration N:` is accepted as a synonym.
4. By Ralphex convention, the last item of each task is
   `- [ ] Mark completed` — checked off only when every preceding step
   in that task has been done and validated.
5. `## Validation Commands` is machine-readable. One shell command per
   bullet — runnable end-to-end by a CI agent or by Ralphex itself to
   confirm the plan didn't regress the repo.

## Working a plan

1. **Create.** Copy a sibling plan as a template or start fresh. The
   filename is `<NNNN>-<verb>-<subject>.md` (`0002-add-…`,
   `0003-migrate-…`, `0004-refactor-…`). Pick the next free number
   by listing the directory; do not reuse numbers from completed or
   abandoned plans. After the file is in place, append a row to
   [`plan-control-index.md`](plan-control-index.md) with the plan's
   category and prerequisites.
2. **Architecture first.** If the plan touches a layer that has no
   note in `architecture/` yet, write the architecture note first
   (contracts, key files, dependencies) and then link it from the
   plan. That way the next plan in the same area doesn't re-derive
   the same context.
3. **Execution.** Flip `- [ ]` to `- [x]` as work progresses. If new
   sub-tasks emerge, append them under the appropriate `### Task N:`
   or insert a `### Task N.5:` between existing ones.
4. **Closure.** Once no `- [ ]` remains, move the file to
   `docs/plans/completed/` (`git mv …`). Do not edit it after that —
   the plan becomes a historical record.

## Architecture notes (`architecture/`)

A note is a **reference**, not a plan. It contains no tasks; it
describes the **current** state of a layer. When a plan changes
something in that layer, update the matching note as part of the
plan's closing tasks (typically `### Task N: Update architecture
notes`).

Recommended note structure:

```markdown
# Architecture: <Layer Name>

## Purpose
What this layer does, and what it deliberately does NOT do (boundaries).

## Key files
| Path | What it holds |
|---|---|

## Contracts
Interfaces, Pydantic models, DB columns.

## Dependencies (both directions)
- Depends on: …
- Depended on by: …

## Extension points
Where to look when adding a new case (new provider, new strategy, …).
```

Architecture notes do **not** move to `completed/`. They live
permanently and grow with the codebase.

## Commits and traceability

Every commit produced while executing a plan carries a `Plan:` git
trailer that points back to the plan ID. The trailer is the
canonical bidirectional link:

- **Commit → plan**: read the trailer.
- **Plan → commits**: `git log --grep='Plan: <NNNN>'`.

### Trailer format

The last line(s) of the commit message body, after a blank line:

```
Add NVIDIA delegate over OpenAI-compatible provider

Wires NvidiaProvider into LLMManager and adds the prefix-strip
branch in _normalize_model_name_for_provider.

Plan: 0001
```

Rules:

- One trailer per plan touched by the commit. A commit that closes
  work across two plans gets two trailers:
  ```
  Plan: 0001
  Plan: 0003
  ```
  Don't comma-separate IDs on a single line — `git interpret-trailers`
  treats each line as one trailer, and `git log --grep` matches on
  the ID alone.
- The trailer carries only the **ID** (`0001`), not the slug.
  Slugs can be re-titled; IDs are permanent once allocated.
- Architecture-note updates that land as part of a plan's execution
  also carry that plan's trailer. The `architecture/` notes are
  part of the plan's deliverable, not standalone work.
- Commits that touch the repo without serving any plan — emergency
  hotfixes, doc typos, dependency bumps from a script — may omit the
  trailer. If you find yourself doing that more than once or twice,
  the work probably warrants its own plan.

### Retrieval cheatsheet

```bash
# All commits for a single plan, oldest first.
git log --reverse --grep='Plan: 0001'

# All plan-tagged commits across the repo, last 30 days.
git log --since='30 days ago' --grep='^Plan: ' --extended-regexp

# Just the commit hashes for a plan (e.g. to cherry-pick).
git log --reverse --grep='Plan: 0001' --format='%H'

# Which plan(s) does this commit belong to?
git show -s --format='%(trailers:key=Plan,valueonly,separator=%x2C )' <SHA>
```

These commands are stable across `git rebase`, squash merges, and
branch deletions: as long as the commit message survives, the
mapping survives.

### Closed plans and history

When a plan moves to [`completed/`](completed/), no commit-list is
appended to the plan file itself. Maintaining that list manually
would drift; instead, the file remains as it was at close, and
`git log --grep='Plan: <NNNN>'` is the source of truth for "what
landed in this plan." Closed plans are immutable historical
records — don't rewrite them to add commit references.

### Multi-commit plans

A plan typically lands across many commits (one per task or
sub-task is reasonable). Squashing to a single commit-per-plan is
not required and usually harmful — granular commits make `git
bisect` and review feasible. The trailer pattern works at any
granularity: 1 commit per plan or 30, the grep query is the same.

## Conventions

- **Documents are written in English.** Conversation with the human
  operator stays in their preferred language (Ukrainian), but every
  Markdown file in this tree, every code comment, and every commit
  message is English.
- File and slug naming: kebab-case with a zero-padded sequence
  prefix (`<NNNN>-<slug>.md`). The number reflects creation order,
  not priority — sorting the directory listing gives you the
  history.
- One concern per plan. Cross-cutting work is split into multiple
  plans that reference one another.

## Current artefacts

Active plans (canonical order, prerequisites and categories live in
[`plan-control-index.md`](plan-control-index.md)):

_None at the moment — see [`completed/`](completed/) for finished plans._

Architecture notes:

- [System Overview](architecture/system-overview.md)
- [Backend Architecture](architecture/backend-architecture.md)
- [Frontend Architecture](architecture/frontend-architecture.md)
- [Settings & Secrets](architecture/settings-and-secrets.md)
- [Database & Migrations](architecture/database-and-migrations.md)
- [Testing](architecture/testing.md)
- [LLM Provider Layer](architecture/llm-provider-layer.md)
- [Trader Pipeline & Diagnostics](architecture/trader-pipeline.md)

Completed plans: see [completed/](completed/).
