# Plan Control Index

Per-plan summary: purpose, prerequisites, category. Use this file to
avoid implementing plans out of order and to pick the right next
plan.

This index is for **ordering and prerequisites**, not status. Each
plan file under `docs/plans/` carries its own status via the
checkboxes inside its tasks; once every task is `- [x]`, the plan is
moved to [`completed/`](completed/) (see
[`README.md`](README.md) for the full lifecycle).

For tracing which commits belong to which plan, every commit
produced while executing a plan carries a `Plan: <NNNN>` git
trailer. Run `git log --grep='Plan: 0001'` to recover the full
commit history for a plan. The convention is described in
[`README.md`](README.md#commits-and-traceability).

## Categories

- **F** — Foundational. Changes a public contract (DB schema,
  Pydantic API surface, `BaseLLMProvider` / `BaseStrategy` /
  `BaseDataSource` interface, `LLMManager` signatures, settings
  envelope). Other plans likely block on these. Keep small and
  reviewed carefully.
- **I** — Integration. Adds a new connector that follows an existing
  contract — new LLM provider, new data source kind, new venue
  adapter. Mostly DB columns + thin glue + UI tile.
- **U** — UI feature. Frontend-only or frontend-led work that doesn't
  change backend contracts. Adds a panel, view, modal, keyboard
  shortcut, theme, etc.
- **B** — Backend feature. New worker loop, new background process,
  new strategy, new background analytic. Touches services / workers
  but not public contracts.
- **R** — Refactor / hardening. Restructures existing code without
  user-visible behaviour change. Should ship behind tests, not behind
  a flag.
- **D** — Documentation / tooling. Plans whose deliverable is a
  document, a CI check, a script, a Ralphex prompt template. No
  runtime behaviour change.

When a plan touches more than one category, pick the most blocking
one (F > I > B > U > R > D) and note the secondary in the per-plan
notes.

## Index

| ID   | Title                                                | Category | Prerequisites |
| ---- | ---------------------------------------------------- | -------- | ------------- |
| 0001 | [Add NVIDIA NIM as LLM provider](0001-add-nvidia-nim-provider.md) | I        | —             |

When adding a row: keep this table sorted by ID ascending. Don't
re-number plans — gaps in IDs are normal and expected (deleted or
abandoned plans leave their numbers reserved).

## Per-plan notes

Only notes that aren't obvious from the title. All plans must follow
[`README.md`](README.md) (file format, validation commands,
"Mark completed" final task, move to `completed/` on close).

- **Plan 0001 — Add NVIDIA NIM as LLM provider.** Pure integration
  on top of the existing OpenAI-compatible delegate pattern (same
  shape as xAI / DeepSeek / OpenRouter). Reads
  [`architecture/llm-provider-layer.md`](architecture/llm-provider-layer.md)
  and [`architecture/settings-and-secrets.md`](architecture/settings-and-secrets.md)
  as binding context. No public contracts change; the only schema
  delta is two nullable columns on `app_settings`. Safe to land in
  isolation.

## Ordering decision tree (for agents picking the next plan)

0. Read [`README.md`](README.md) and the relevant
   [`architecture/`](architecture/) note(s) before doing anything
   else. The plan can't be evaluated without that context.
1. Filter the index to rows whose every Prerequisite is either `—`
   or already in [`completed/`](completed/). If no such row exists,
   stop — the queue is blocked and a new plan must be written first.
2. From the eligible set, prefer in this order:
   1. **F** plans (foundational — they unblock everything else).
   2. The lowest-numbered plan in any other category. Numbers
      reflect creation order, which is a rough proxy for the
      operator's intent.
3. Before starting, check that the plan's Validation Commands run
   green on `main`. If they don't, the failure is unrelated tech
   debt and the plan must record it (either fix as part of the plan
   or carve out a separate plan and link it as a prerequisite).
4. While executing: flip checkboxes as work lands; never batch
   completions. Every commit must carry a `Plan: <NNNN>` trailer
   (see
   [`README.md`](README.md#commits-and-traceability)). If new
   sub-tasks emerge, append them under the right `### Task N:`
   rather than starting a new plan unless they're genuinely out of
   scope (see the plan's own `## Out of scope` section).
5. On close: every `- [ ]` becomes `- [x]`, including the trailing
   `- [ ] Mark completed` of each task and the final
   architecture-update / `git mv ... completed/` task. Only then
   move the file.

## How to add a plan

1. Pick the next free ID by listing this directory:
   `ls docs/plans/[0-9]*.md docs/plans/completed/[0-9]*.md | tail`.
   Don't reuse IDs.
2. Create `<NNNN>-<verb>-<subject>.md` from the format in
   [`README.md`](README.md).
3. If the plan needs context that isn't yet in
   [`architecture/`](architecture/), write the architecture note
   first as a separate commit, then write the plan that links to
   it.
4. Append a row here with category and prerequisites.
5. Add a per-plan note **only** if there is something
   non-obvious — don't restate the title.

## Cross-references

- Plan format and lifecycle: [`README.md`](README.md)
- Architecture notes that plans cite:
  [`architecture/`](architecture/) (start with
  [`system-overview.md`](architecture/system-overview.md))
- Completed plans archive: [`completed/`](completed/)
