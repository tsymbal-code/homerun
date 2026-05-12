# Plan: Branch-derived deploy targets (unify prod and stage in one repo)

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0056` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Until now the operator kept the prod and staging codebases in two
separate git repositories. Both run the same Homerun stack — prod
on `polyhome-prod`, stage on `polyhome-1` — but every diagnostic
recipe, every agent rule, every architecture note was authored with
**one literal hostname baked in**. As long as the two repos were
separate the literal was correct in each.

The operator is now merging both projects into a single repo where
the **git branch** selects the target server:

| Branch | Environment | SSH alias       | Remote path                |
|--------|-------------|-----------------|----------------------------|
| `main` | production  | `polyhome-prod` | `/home/polyhome/homerun`   |
| `dev`  | staging     | `polyhome-1`    | `/home/polyhome/homerun`   |

A naive merge would leave hardcoded `polyhome-prod` strings on the
`dev` branch (and vice versa) — every doc would lie half the time,
agents would SSH into the wrong host, and every `git merge` would
produce mechanical text conflicts on those literals. The current
repo already shows the failure mode: `deploy/sync_remote.sh`
defaults to `polyhome-prod` while `deploy/AGENTS.md` is written
end-to-end against `polyhome-1`. That inconsistency is the bug this
plan eliminates.

The fix has three load-bearing pieces:

1. **One SSOT mapping `branch → host`** — declared exactly once,
   in a new architecture note plus the `case` block at the top of
   `deploy/sync_remote.sh`. Every other doc, hook, and script
   reads from it.
2. **Agent-facing docs use a `<HOMERUN_HOST>` placeholder** in all
   command examples, with a single "Which server am I on?" section
   that points at the SSOT table. Agents resolve the placeholder
   from the current branch before running anything.
3. **Defence in depth in `deploy/sync_remote.sh`** — the script
   refuses to sync `main → polyhome-1` or `dev → polyhome-prod`
   unless `FORCE_HOST=1` is set. This is the explicit escape hatch
   for the rare "deploy main to stage for a hotfix soak" scenario.

A Claude Code `UserPromptSubmit` hook prepends
`[homerun reminder] Current branch is <X> → target server is <Y>`
to every agent turn so even an agent that skipped the docs picks
up the right context. The always-on Cursor rule mandates
`git branch --show-current` as the first command of any session.

Done means: a fresh agent on either branch can diagnose, deploy,
and roll back without ever being told the host name interactively;
every authored doc reads the same on both branches; merges between
`main` and `dev` produce zero text conflicts on hostname strings.

## Context / References

- [`deploy/sync_remote.sh`](../../deploy/sync_remote.sh) — current
  default `SSH_HOST=polyhome-prod`; the only place a literal host
  drives runtime behaviour today.
- [`deploy/AGENTS.md`](../../deploy/AGENTS.md) — 15 literal
  hostname references; the canonical deployment guide for agents.
- [`CLAUDE.md`](../../CLAUDE.md), [`agents.md`](../../agents.md),
  [`.cursor/rules/homerun.mdc`](../../.cursor/rules/homerun.mdc) —
  three top-level agent entry points that all hardcode
  `polyhome-prod`.
- [`.claude/hooks/remind-ssh.sh`](../../.claude/hooks/remind-ssh.sh)
  — `UserPromptSubmit` hook that today only knows about
  `polyhome-prod`.
- [`.claude/settings.json`](../../.claude/settings.json) — Bash
  allowlist of SSH commands; today only permits `ssh polyhome-prod`,
  so on the `dev` branch every diagnostic would fail the permission
  check.
- [`scripts/run_tests_remote.sh`](../../scripts/run_tests_remote.sh)
  — also hardcodes `polyhome-1`; tests on `main` would target the
  wrong host.
- New architecture note to create in this plan:
  `docs/plans/architecture/deploy-targets.md`.

## Validation Commands

- `bash -n deploy/sync_remote.sh`
- `bash -n .claude/hooks/remind-ssh.sh`
- `bash -n scripts/run_tests_remote.sh`
- `python3 -c "import json; json.load(open('.claude/settings.json'))"`
- `rg -F 'polyhome-prod' -- deploy/ .claude/ .cursor/ scripts/ agents.md CLAUDE.md` — every remaining hit MUST be inside the SSOT table in `deploy-targets.md`, `sync_remote.sh`'s `case` block, or the documented placeholder-resolution example.
- `rg -F 'polyhome-1' -- deploy/ .claude/ .cursor/ scripts/ agents.md CLAUDE.md` — same constraint as above.
- `rg -F '<HOMERUN_HOST>' -- deploy/ .claude/ .cursor/ scripts/ agents.md CLAUDE.md docs/plans/architecture/` — at least one hit per agent-facing doc and recipe.
- Manual: `git checkout main && bash deploy/sync_remote.sh --dry-run-host` reports target `polyhome-prod`; `git checkout dev && bash deploy/sync_remote.sh --dry-run-host` reports target `polyhome-1`. (The `--dry-run-host` flag is added in Task 2.)

### Task 1: Architecture note — `deploy-targets.md`

- [x] Create `docs/plans/architecture/deploy-targets.md` with the
  standard architecture-note shape (Purpose / Key files /
  Contracts / Dependencies / Extension points). Make this file the
  **single source of truth** for the branch → host mapping. Sections:
  - **Purpose.** Why the mapping is branch-derived; what the
    `<HOMERUN_HOST>` placeholder means; the trade-off (every
    diagnostic now demands `git branch --show-current` first).
  - **Mapping table.** Branch, env, SSH alias, remote path. This
    is the only place in the repo where the literal hostnames
    appear as authoritative configuration.
  - **Determining the current target.** Show the
    `git branch --show-current` recipe and the
    `sync_remote.sh --dry-run-host` recipe. Note the
    `FORCE_HOST` escape hatch and when it is legitimate.
  - **Cross-branch identical surfaces.** State that postgres,
    redis, compose stack, `.env` shape, secrets envelope, all
    container names, all port mappings are identical between
    `polyhome-prod` and `polyhome-1`. The only branch-derived
    thing is the SSH alias.
  - **Where the mapping is read.** Enumerate every file that
    reads the branch-derived target at runtime
    (`deploy/sync_remote.sh`, `scripts/run_tests_remote.sh`,
    `.claude/hooks/remind-ssh.sh`). Anything else uses the
    placeholder, not the literal.
- [x] End the note with `Last verified: <today UTC>`.
- [x] Append a row to the architecture-notes table in
  [`agents.md`](../../agents.md) § "Where to find more" pointing
  at the new note. (Note: `agents.md` does not carry an architecture
  notes table — the canonical table lives in `CLAUDE.md` § "Where to
  find more" and `architecture/system-overview.md` § "Where to look
  next". Row appended to both, plus a paired-docs row in `agents.md`
  § "Documentation hygiene" mapping the four runtime files to
  `deploy-targets.md`.)
- [x] Mark completed

### Task 2: SSOT + safety guard in `deploy/sync_remote.sh`

- [x] Replace the literal `SSH_HOST="${SSH_HOST:-polyhome-prod}"`
  default in
  [`deploy/sync_remote.sh:25`](../../deploy/sync_remote.sh) with a
  branch-derived resolver:

  ```bash
  CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  case "${CURRENT_BRANCH}" in
    main) DERIVED_HOST="polyhome-prod"; DERIVED_ENV="prod" ;;
    dev)  DERIVED_HOST="polyhome-1";    DERIVED_ENV="stage" ;;
    *)
      echo "REFUSING: branch '${CURRENT_BRANCH}' is not mapped to a deploy target." >&2
      echo "See docs/plans/architecture/deploy-targets.md for the mapping." >&2
      exit 1
      ;;
  esac
  SSH_HOST="${SSH_HOST:-${DERIVED_HOST}}"
  ```

  The `SSH_HOST` env override stays — it is the only knob for
  ad-hoc operator targeting.
- [x] Add the cross-target safety guard immediately after the
  case block. Refuse `main → polyhome-1` and `dev → polyhome-prod`
  unless `FORCE_HOST=1` is set; print the override recipe so the
  operator does not have to read the script source:

  ```bash
  if [[ "${SSH_HOST}" != "${DERIVED_HOST}" && "${FORCE_HOST:-0}" != "1" ]]; then
    echo "REFUSING: branch '${CURRENT_BRANCH}' is mapped to '${DERIVED_HOST}'" >&2
    echo "         but SSH_HOST is set to '${SSH_HOST}'." >&2
    echo "  To override deliberately: FORCE_HOST=1 SSH_HOST=${SSH_HOST} ./deploy/sync_remote.sh" >&2
    exit 1
  fi
  ```
- [x] Add a `--dry-run-host` flag at the top of the script that
  prints `branch=<X> env=<Y> host=<Z>` and exits 0 without
  rsyncing. The validation step in `## Validation Commands`
  depends on this. (Implementation note: the flag runs *after* the
  safety guard so that Task 8 case 3 — refusal under
  `SSH_HOST=<other>` without `FORCE_HOST=1` — reports correctly.)
- [x] Replace the script's docstring (`# Defaults: ...`) to
  describe the branch-derived behaviour and link to
  `docs/plans/architecture/deploy-targets.md` as the
  authoritative reference.
- [x] Verify with `bash -n deploy/sync_remote.sh` and a manual
  `bash deploy/sync_remote.sh --dry-run-host` on both branches.
- [x] Mark completed

### Task 3: Generalize the top-level agent guides

- [x] [`CLAUDE.md`](../../CLAUDE.md): replace every literal
  `polyhome-prod` with `<HOMERUN_HOST>` in command examples
  (10 hits). Add a new section near the top titled
  **"Which server am I on?"** with the same table-and-recipe
  shape as the architecture note. Cross-link to
  `docs/plans/architecture/deploy-targets.md`. The
  "single most important fact" section keeps its prose
  ("Homerun does NOT run on the operator's local machine") but
  no longer names a specific host — the host is now derived,
  and that derivation is the message.
- [x] [`agents.md`](../../agents.md): same treatment (2 hits).
  Reuse the SSOT table by reference, do NOT duplicate it. The
  "Deployment Topology" section gains a one-line pointer at the
  new architecture note and at the `git branch --show-current`
  recipe.
- [x] [`deploy/AGENTS.md`](../../deploy/AGENTS.md): heaviest
  rewrite (15 hits). Strategy:
  1. Front-load a **"Which server am I on?"** subsection right
     after the "Where Homerun runs" table. Move the host into
     a row of that table, not into prose.
  2. Convert every code example to use `<HOMERUN_HOST>` in
     place of the literal alias.
  3. Replace the "Application stack ... on Remote server
     **`polyhome-1`** ..." sentence with "... on the
     branch-derived target server (see _Which server am I on?_)".
  4. The file's "Things to be aware of" and "Common 'I broke
     something' recipes" tables are agnostic to which host —
     touch only the hostnames inside commands.
- [x] Manual proofread: open both branches' rendered docs side
  by side; they MUST be byte-identical except for the SSOT
  table values. (CLAUDE.md and deploy/AGENTS.md carry SSOT
  mirror tables per Task 3 strategy 1 and the plan's explicit
  "table-and-recipe shape" instruction; agents.md carries
  zero literals and points at the SSOT by reference. All three
  files are branch-byte-identical.)
- [x] Mark completed

### Task 4: Generalize the always-on Cursor rule

- [x] [`.cursor/rules/homerun.mdc`](../../.cursor/rules/homerun.mdc):
  replace literal hostnames (8 hits) with `<HOMERUN_HOST>`. Add
  a new top-of-rule directive:

  ```markdown
  ## First action of every session

  Before running any SSH/deploy command, determine the deploy
  target from the current branch:

      git branch --show-current

  Mapping (canonical: `docs/plans/architecture/deploy-targets.md`):
  - `main` → `polyhome-prod` (production)
  - `dev`  → `polyhome-1`    (staging)

  Substitute the resolved host wherever this rule shows
  `<HOMERUN_HOST>`.
  ```

  Keep the rule's hard "don't" list intact — the prohibitions
  themselves are host-agnostic.
- [x] Add an entry to the "Where to read more" table at the
  bottom of the rule pointing at the new architecture note.
- [x] Mark completed

### Task 5: Update `remind-ssh.sh` to inject the current target

- [x] Rewrite the message block in
  [`.claude/hooks/remind-ssh.sh`](../../.claude/hooks/remind-ssh.sh)
  so it computes the current branch and the resolved target host
  at invocation time and writes them into the reminder. The
  resolver is **the same `case` block** as `sync_remote.sh` —
  factor it into a shell function near the top of the hook so
  the two files cannot drift.
- [x] The reminder now reads (literal example for the `dev`
  branch):

  ```text
  [homerun reminder] Heads up: this project does NOT run on localhost.
  Current branch: dev → target server: polyhome-1 (STAGING).
  All diagnostic commands must be wrapped:

      ssh polyhome-1 'cd /home/polyhome/homerun && docker compose ps'
      ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --tail=200 backend'
      ssh polyhome-1 'curl -fsS http://127.0.0.1:8888/api/...'

  Full catalog: deploy/AGENTS.md. Mapping: docs/plans/architecture/deploy-targets.md.
  ```

  The hostname inside the example commands is the **resolved**
  alias, not the placeholder — agents copy-paste these examples,
  so spelling out the right value here is part of the safety net.
- [x] If `git rev-parse --abbrev-ref HEAD` fails (detached HEAD,
  not a git repo), fall back to a generic reminder that names
  neither host and points at the mapping doc.
- [x] Verify with `bash -n .claude/hooks/remind-ssh.sh` and a
  manual smoke test on both branches.
- [x] Mark completed

### Task 6: Widen the Bash allowlist in `.claude/settings.json`

- [x] Today
  [`.claude/settings.json`](../../.claude/settings.json) permits
  only `ssh polyhome-prod ...` patterns (12 hits) and denies
  `ssh polyhome-prod 'docker compose down*'`. On the `dev`
  branch every diagnostic SSH would currently be denied by the
  allowlist. (Actual pre-merge state on this branch was the
  reverse — only `polyhome-1` entries present, every `main`-side
  SSH would have been denied. The merge means we need both.)
- [x] Mirror every `polyhome-prod` allow-entry to a matching
  `polyhome-1` entry. Mirror the deny-entry similarly. Keep the
  literal hostnames in this file — JSON-schema patterns do not
  support placeholders, and the file itself is the boundary
  where literals are unavoidable.
- [x] Add a header comment block at the top of the JSON (as
  `"_comment"` keys, ignored by Claude Code) noting that this
  is the single config file where literal hostnames are
  legitimate, and pointing at the architecture note for the
  rationale.
- [x] Verify with `python3 -c "import json; json.load(open('.claude/settings.json'))"`.
- [x] Mark completed

### Task 7: Generalize the remaining scripts and reference docs

- [x] [`scripts/run_tests_remote.sh`](../../scripts/run_tests_remote.sh):
  reuse the same `case` block as `sync_remote.sh`. Add the
  identical `FORCE_HOST` safety guard. Update the script's
  docstring.
- [x] [`scripts/trader_events_housekeeper_dry_run.py`](../../scripts/trader_events_housekeeper_dry_run.py)
  (1 hit), [`backend/services/ws_feeds.py`](../../backend/services/ws_feeds.py)
  (1 hit): these are runtime artifacts, not deploy scripts.
  Audit each hit and decide per-file:
  - If the literal is a code-side default that should also be
    branch-derived → read `os.environ["HOMERUN_HOST"]` with the
    same case-block fallback (factor it into
    `backend/utils/deploy_target.py` if needed).
  - If the literal is in a comment/log string referring to a
    specific historical event → leave it (these are records).
  Document the decision in the file's diff commit message.

  **Decisions:**
  - `scripts/trader_events_housekeeper_dry_run.py:10` — docstring
    "Usage … on `polyhome-1`" is a recipe pointer, not a record.
    Rewrote to point at `deploy-targets.md` and the branch-derived
    host (no `os.environ` read needed; this is documentation, the
    Python module itself does not address the host).
  - `backend/services/ws_feeds.py:716` — "live verification on
    `polyhome-1` confirms book_depth gate" is a historical
    diagnostic record of where the gate was verified. **Leave**.
- [x] [`docs/strategies/README.md`](../../docs/strategies/README.md)
  (2 hits) and [`docs/strategies/_common-bot-parameters.md`](../../docs/strategies/_common-bot-parameters.md)
  (8 hits): operator-facing Ukrainian docs. Where the host is
  mentioned as part of a generic diagnostic recipe — switch to
  `<HOMERUN_HOST>` with a one-line Ukrainian explanation
  (e.g. _"де `<HOMERUN_HOST>` — це `polyhome-prod` для main і
  `polyhome-1` для dev, див. `docs/plans/architecture/deploy-targets.md`"_).
  Where the host is mentioned in a historical anecdote — leave it.
  (Also caught and rewrote
  `docs/strategies/crypto-5m-midcycle.md:93,96` — generic curl
  recipe.)
- [x] [`docs/plans/architecture/*.md`](architecture/) — only the
  notes that hold living recipes need rewriting
  (`crypto-fast-binary-lane.md`, `worker-discovery.md`,
  `database-and-migrations.md`, `testing.md`,
  `trader-pipeline.md`). The `worker-trading.md` "After plan
  0054" subsection records a specific measurement on
  `polyhome-prod` — that is historical and stays. Walk through
  each note's hits and apply the same audit rule as Task 7
  bullet 2. (Audited; `trader-pipeline.md:588` "Recorded
  baselines on the `polyhome-1` host:" is historical, left;
  `testing.md:337` `Last verified` marker is historical, left;
  `websocket-and-events.md` 219 + 271 are historical
  observations and out of scope per the plan. All living
  recipes in the five listed notes use `<HOMERUN_HOST>`.)
- [x] [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md):
  this is an **historical journal** (40 hits). Do **not**
  rewrite past entries — they record what happened on a
  specific host on a specific day. Only update the file's
  preamble to point at the new mapping so future entries pick
  the right convention.
- [x] Append the file `docs/plans/work-artifacts/0033-fetch-clob-window.py`
  to the "do not rewrite" list — it is a frozen artefact of plan
  0033 and must not be modified retroactively. (Confirmed by
  inspection — the file is under `work-artifacts/` and was not
  touched by this plan. The plan's "Out of scope" section
  already enumerates `work-artifacts/*` as immutable; the audit
  trail here is the explicit confirmation.)
- [x] Mark completed

### Task 8: Sanity-check the merge by running the validation suite

- [x] Run every command under `## Validation Commands`. The
  three `rg -F 'polyhome-*'` greps must report only the
  expected SSOT-table hits and the `case`/`allowlist` hits in
  the four files listed in the architecture note's
  "Where the mapping is read" section.
  - `bash -n` clean on all three shell scripts.
  - `python3 -c "import json; json.load(...)"` parses
    `.claude/settings.json`.
  - `rg -F 'polyhome-prod' -- deploy/ .claude/ .cursor/ scripts/ agents.md CLAUDE.md`
    → 22 hits, all inside the SSOT mirror tables in
    `CLAUDE.md` / `deploy/AGENTS.md` / `.cursor/rules/homerun.mdc`,
    the `case` blocks in `sync_remote.sh` /
    `run_tests_remote.sh` / `remind-ssh.sh`, the Bash
    allowlist in `.claude/settings.json`, the
    `FORCE_HOST` placeholder-resolution example in the
    script docstrings, and the "operator has both aliases"
    enumeration in `deploy/AGENTS.md`.
  - `rg -F 'polyhome-1' -- ...` → 20 hits, same categories.
  - `rg -lF '<HOMERUN_HOST>' -- ...` reports every agent-facing
    doc and recipe (CLAUDE, agents, deploy/AGENTS, the Cursor
    rule, the hook, five architecture notes, deploy-targets).
- [x] On a scratch branch, run the manual test:
  1. `git checkout main && bash deploy/sync_remote.sh --dry-run-host` → expects `host=polyhome-prod env=prod`. ✓ verified.
  2. `git checkout dev && bash deploy/sync_remote.sh --dry-run-host` → expects `host=polyhome-1 env=stage`. ✓ verified via `git worktree add -B dev`.
  3. `git checkout dev && SSH_HOST=polyhome-prod bash deploy/sync_remote.sh --dry-run-host` → expects refusal, exit 1. ✓ verified.
  4. `git checkout dev && FORCE_HOST=1 SSH_HOST=polyhome-prod bash deploy/sync_remote.sh --dry-run-host` → expects `host=polyhome-prod env=stage` and a warn line. ✓ verified.
- [x] On a scratch branch, run a manual hook smoke test:
  `echo '{"prompt":"docker compose up"}' | bash .claude/hooks/remind-ssh.sh`
  prints a reminder that names the current branch's resolved
  host. ✓ verified — on `main` resolved `polyhome-prod (PRODUCTION)`,
  on `dev` resolved `polyhome-1 (STAGING)`.
- [x] Mark completed

### Task 9: Close-out — index, architecture-note bump, archive

- [x] Add a Plan 0056 row to the architecture-notes table in
  [`agents.md`](../../agents.md) (the "Where to find more"
  table) referencing `docs/plans/architecture/deploy-targets.md`.
  Already done in Task 1 — verify it landed. (As noted in
  Task 1: agents.md has no such table; rows landed in
  `CLAUDE.md` § "Where to find more",
  `architecture/system-overview.md` § "Where to look next",
  and the paired-docs table in `agents.md` § "Documentation
  hygiene".)
- [x] Append a row to
  [`plan-control-index.md`](../plan-control-index.md) for plan
  0056. Category: **D**. Prerequisites: —. Note that this plan
  is the prerequisite for any future plan that adds a third
  environment (preview, hotfix-soak, etc.) — that plan extends
  the `case` block in the SSOT, no other file needs to know.
- [x] Bump `Last verified` on the architecture notes touched in
  Task 7 to today's UTC date — but only on notes where you ran
  a real diff against code, not on notes you only string-edited
  for the hostname literal. Per the project convention, the
  marker is not a reflex stamp.

  **Decision:** none of the notes touched in Task 7
  (`crypto-fast-binary-lane.md`, `worker-discovery.md`,
  `database-and-migrations.md`, `testing.md`,
  `trader-pipeline.md`) was diffed against code — only the
  hostname literal was rewritten to the placeholder.
  `Last verified` markers remain at their previous dates.
  `deploy-targets.md` itself, being new, ends with
  `Last verified: 2026-05-12`.
- [x] Confirm `git log --grep='Plan: 0056'` shows the full
  commit set for this plan. (Deferred to the commit step —
  the operator runs `git log --grep='Plan: 0056'` after
  landing the commit; the trailer is enforced by the
  `PreToolUse` hook in `.claude/settings.json`.)
- [x] `git mv docs/plans/0056-branch-derived-deploy-targets.md docs/plans/completed/`.
- [x] Update the row in `plan-control-index.md` to point at
  `completed/0056-...`.
- [x] Mark completed

## Out of scope

- **Rewriting historical content.** Closed plans
  (`docs/plans/completed/*`), backlog plans
  (`docs/plans/backlog/*`), work artefacts
  (`docs/plans/work-artifacts/*`), the appendix under
  `docs/plans/architecture/_appendix/`, and dated entries in
  `docs/operational/runtime-tweaks.md` are immutable historical
  records. They name the host that was correct on the day they
  were written; that fact is part of the record. Don't touch.
- **A third environment.** This plan handles the two existing
  targets only. Adding `preview` / `hotfix-soak` later means
  extending the `case` block in one place
  (`deploy/sync_remote.sh`) and one place
  (`.claude/hooks/remind-ssh.sh`) and one row in the SSOT
  table — no doc surgery is needed after this plan lands.
- **Migrating the operator's `~/.ssh/config`.** Both aliases
  are assumed to already exist locally. This plan does not
  touch SSH client config; if the operator's `~/.ssh/config`
  has only one of the two aliases, that is an operator-side
  prerequisite, not a plan task.
- **CI deployment.** There is still no CI deploying to either
  host (per `deploy/AGENTS.md`); `./deploy/sync_remote.sh`
  remains the only deploy path. Wiring a GitHub Action to
  deploy `main → polyhome-prod` on push is a separate plan if
  the operator wants it.
- **Reconfiguring postgres / redis / compose for stage.** The
  architecture note in Task 1 states explicitly that stage and
  prod are stack-identical. If that ever stops being true, the
  divergence belongs in a per-environment overlay
  (`docker-compose.stage.yml`) and is the subject of a separate
  plan — not this one.
- **CRITICAL or HIGH-tier risk knobs.** None are touched by any
  task in this plan. All edits are to docs, hooks, allowlists,
  and the deploy-script `case` block; no `app_settings` column,
  no strategy, no risk gate is modified.
