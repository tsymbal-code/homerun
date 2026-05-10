# Plan: Default Ralphex plan-format rule — CRITICAL knob touch policy

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Sibling to plan 0027 (the agent memory rule) at the
plan-format layer. Plan 0027 fires **before** an agent mutates
a CRITICAL knob — it forces the matrix consultation and
walkthrough at apply-time. This plan fires **earlier**, at
plan-writing time: when an agent (or operator) is drafting any
new Ralphex plan that **proposes** to mutate a CRITICAL knob,
the plan itself must be designed around the walkthrough
requirement, not bolted on afterward.

The bind is added to
[`docs/plans/README.md`](README.md) — the canonical plan
format reference every new plan cites in its policy header.
A new section, "**CRITICAL knob touch policy**", states:

1. Trigger — any plan whose Tasks include changes to one of
   the 15 CRITICAL-tier knobs from the
   [interaction matrix](../strategies/_common-bot-parameters.md#knob-interaction-matrix--critical-tier).
2. Plan-time obligations:
   - The plan's `## Context / References` section must link
     the matrix entries for every CRITICAL knob it touches.
   - Every Task that applies a CRITICAL change has, as one of
     its check-boxes, "fill the
     [walkthrough template](../operational/runtime-tweaks.md#walkthrough-template-for-critical-knob-changes)
     in `runtime-tweaks.md` (5 steps, numeric)".
   - The plan's `## Out of scope` should explicitly note
     whether HIGH/MEDIUM-tier knobs are also touched (so the
     reader knows what was *not* walked through).
3. Plan-validator hint — `plan-validator` agent gets a
   one-paragraph addition to its checklist: "Plan touches
   CRITICAL knob? confirm walkthrough check-box exists in
   relevant Tasks."

Done = `docs/plans/README.md` has the new section, the
`plan-validator` agent's instructions reference it, and a
template snippet for the walkthrough check-box is provided so
plan authors copy-paste rather than reinvent.

This plan is **documentation-only**. No code, no schema, no
runtime behaviour. It binds the plan-writing process, not the
plan-execution process (that's 0027's job).

## Out of scope

- **HIGH/MEDIUM tier knob coverage** — same scope as 0025/26/27.
- **Auto-validation of plans** that propose CRITICAL changes
  (e.g. CI step that lints task check-boxes). Phase 4 candidate.
- **Existing closed plans** are not retrofitted. Past plans in
  `completed/` stay verbatim.
- **Plans that touch CRITICAL knobs only via rollback recipes**
  (e.g. plan 0024 has a rollback that hits a knob) — the rule
  binds **forward changes**, not rollbacks of unrelated work.

## Context / References

- [Plan 0025 — Knob interaction matrix](completed/0025-knob-interaction-matrix-critical-tier.md)
  — Phase 1; defines the 15 CRITICAL knobs.
- [Plan 0026 — Walkthrough template](completed/0026-runtime-tweaks-walkthrough-template-critical.md)
  — Phase 2; the 5-step skeleton plans now reference.
- [Plan 0027 — Agent memory rule](completed/0027-agent-memory-rule-critical-knob-walkthrough.md)
  — Phase 3; the apply-time enforcement.
- [`docs/plans/README.md`](README.md) — file extended by this plan.
- [`.claude/agents/plan-validator.md`](../../.claude/agents/plan-validator.md)
  — agent definition that gets the new check (if it exists; if
  not, this task is skipped and noted in the plan-format doc
  instead).

## Validation Commands

- `grep -q '## CRITICAL knob touch policy' docs/plans/README.md`
- `grep -q 'docs/strategies/_common-bot-parameters.md#knob-interaction-matrix' docs/plans/README.md`
- `grep -q 'walkthrough template' docs/plans/README.md`
- `test -f .claude/agents/plan-validator.md && grep -q 'CRITICAL knob' .claude/agents/plan-validator.md` — only required if the agent definition exists.

### Task 1: Draft the README section

- [x] Decide anchor: `## CRITICAL knob touch policy`, placed
  immediately after the existing `## Plan file format
  (Ralphex)` section and before `## Working a plan`.
- [x] Write the section body covering the three obligations
  (Context links, per-task walkthrough check-box, Out of scope
  HIGH/MEDIUM disclosure).
- [x] Add a copy-paste-ready check-box snippet that plan
  authors drop into the relevant Task block. Example:

  ```markdown
  - [ ] Fill the [walkthrough template](../operational/runtime-tweaks.md#walkthrough-template-for-critical-knob-changes)
    in `runtime-tweaks.md` for this change: Step 1 (direct gate
    impact), Step 2 (indirect-metric impact), Step 3 (live SQL
    simulation), Step 4 (compound-effect checklist), Step 5
    (rollback recipe). Numeric values only; `n/a` allowed only
    when the matrix entry confirms zero impact.
  ```

- [x] State the trigger explicitly: "If any Task in this plan
  performs a `PUT` / `POST` / `UPDATE` / `psql` / UI Save that
  mutates a knob from this list: …" then list the 15 CRITICAL
  knobs (cite the matrix as the canonical list, do not
  duplicate inline — drift risk).
- [x] Mark completed

### Task 2: Insert the section into `docs/plans/README.md`

- [x] Edit
  [`docs/plans/README.md`](README.md): insert the new section
  in the chosen position. Mirror the file's voice (direct,
  no nonsense, file:line citations).
- [x] Mark completed

### Task 3: Update `plan-validator` agent (if it exists)

- [x] Check whether `.claude/agents/plan-validator.md` exists.
- [x] If yes — append a check to its instructions:
  "When validating a plan, scan the plan's Tasks for any UI
  Save / `PUT` / `psql UPDATE` / `POST` step that targets a
  knob in the CRITICAL-tier matrix
  (`docs/strategies/_common-bot-parameters.md#knob-interaction-matrix--critical-tier`).
  If found, the corresponding Task must include the
  walkthrough check-box. Flag absent walkthroughs as a fail."
- [x] If no — note in `docs/plans/README.md` that the
  validator agent integration is deferred until that agent
  exists.
- [x] Mark completed

### Task 4: Cross-link from existing matrix + template

- [x] In
  [`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md)
  "How to use this matrix" subsection, add a sentence:
  "Plans that propose changes to any of the listed knobs
  must additionally satisfy the
  [CRITICAL knob touch policy](../plans/README.md#critical-knob-touch-policy)
  in the plan-format spec."
- [x] In
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  Walkthrough template intro, add a sentence pointing at the
  same anchor for plan authors who land on the template via
  the journal.
- [x] Mark completed

### Task 5: Close

- [x] All check-boxes above are `[x]`.
- [x] `git mv docs/plans/0028-ralphex-plan-rule-critical-knob-touch.md
  docs/plans/completed/`.
- [x] Update [`plan-control-index.md`](plan-control-index.md):
  link target to `completed/0028-...md`.
- [x] Mark completed
