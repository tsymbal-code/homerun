# Plan: Agent memory rule — CRITICAL knob walkthrough enforcement

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Phase 3 of the three-layer fix from 2026-05-09. Phase 1 (plan
0025) delivered the [knob interaction matrix](../strategies/_common-bot-parameters.md#knob-interaction-matrix--critical-tier);
Phase 2 (plan 0026) delivered the [walkthrough template](../operational/runtime-tweaks.md#walkthrough-template-for-critical-knob-changes).
This plan installs the enforcing memory rule: every Claude
Code agent session for this project must consult the matrix
**and** fill the walkthrough **before** any PUT / UPDATE /
SQL change to a CRITICAL-tier safety knob.

The rule is the third memory file in
`/Users/dtsym/.claude/projects/-Users-dtsym-Work-Splunk--Project-X-homerun/memory/`
alongside the existing `feedback_*.md` files. Same flat
format. Same `MEMORY.md` index entry. The text is
deliberately **pointer-as-imperative**: it cites the exact
file paths and section anchors, not abstract guidance like
"think before changing." The user's earlier critique of
abstract memory rules ("я знову напишу поверхневий аналіз і
знову помилюсь") drives the wording — this rule enforces a
process, not a value.

Done = a new `feedback_critical_knob_walkthrough.md` file
exists in the memory directory with the directive, plus a
matching one-line index entry in `MEMORY.md`. The directive
includes:

1. The trigger condition (any change to a knob from the
   CRITICAL list).
2. The mandatory pre-action: open the matrix and the
   walkthrough template.
3. The mandatory artefact (a filled walkthrough in a
   `runtime-tweaks.md` entry).
4. The hard stop: if the walkthrough cannot be filled with
   numeric values, the agent stops and asks the operator
   instead of pencil-whipping.

This plan is **memory-only**. No code, no schema, no
runtime behaviour change. It does not move agent autonomy —
the operator's instruction "агент має повну свободу" remains
in force. The rule binds **how** the agent applies, not
**whether**.

## Out of scope

- **Other agents (Cursor rules, Cline, etc.).** Memory file
  is Claude Code-specific. If parallel rules for other tools
  are wanted, they ship in a separate plan.
- **Auto-enforcement** (e.g. a pre-tool-use hook that scans
  the proposed action for CRITICAL knob field names and
  injects the matrix into context). Useful, but a separate
  Phase 4 candidate.
- **Updating existing memory files.** The three pre-existing
  `feedback_*.md` rules (docs language, plans must include
  tests, audit existing UI before planning) stay verbatim.
- **HIGH/MEDIUM-tier knob coverage.** Phase 3 binds the same
  CRITICAL tier the matrix and template cover. Extending
  later requires updating the matrix first.

## Context / References

- [Plan 0025 — Knob interaction matrix](completed/0025-knob-interaction-matrix-critical-tier.md)
- [Plan 0026 — Walkthrough template](completed/0026-runtime-tweaks-walkthrough-template-critical.md)
- Existing memory dir:
  `/Users/dtsym/.claude/projects/-Users-dtsym-Work-Splunk--Project-X-homerun/memory/`
- Existing `MEMORY.md` index pattern (3 entries, flat
  format, file-path link + one-line `— description`).

## Validation Commands

- `test -f /Users/dtsym/.claude/projects/-Users-dtsym-Work-Splunk--Project-X-homerun/memory/feedback_critical_knob_walkthrough.md`
- `grep -q 'feedback_critical_knob_walkthrough.md' /Users/dtsym/.claude/projects/-Users-dtsym-Work-Splunk--Project-X-homerun/memory/MEMORY.md`
- `grep -q 'docs/strategies/_common-bot-parameters.md' /Users/dtsym/.claude/projects/-Users-dtsym-Work-Splunk--Project-X-homerun/memory/feedback_critical_knob_walkthrough.md`
- `grep -q 'docs/operational/runtime-tweaks.md' /Users/dtsym/.claude/projects/-Users-dtsym-Work-Splunk--Project-X-homerun/memory/feedback_critical_knob_walkthrough.md`

### Task 1: Draft the directive

- [x] Decide on the directive's voice — match existing
  `feedback_*.md` files (English, terse, imperative). One of
  the existing files is a good template: open
  `feedback_audit_existing_ui_before_planning.md` and mirror
  its structure (single section, ~10–20 lines, ends with
  the recurring failure pattern).
- [x] Specify the **trigger condition** explicitly: "any
  PUT/UPDATE/SQL/UI action that changes one of the 15
  CRITICAL-tier knobs listed in the interaction matrix."
- [x] Specify the **mandatory pre-action**: open the matrix
  entry for the knob, read the direct/indirect/compound
  sections.
- [x] Specify the **mandatory artefact**: a 5-step
  walkthrough block in `runtime-tweaks.md` filled with
  numeric values. Cite the template anchor.
- [x] Specify the **hard stop**: if any step's numeric value
  cannot be derived from the matrix or live data, the agent
  stops and asks the operator. No prose-only filler.
- [x] Mark completed

### Task 2: Write the directive file

- [x] Create
  `/Users/dtsym/.claude/projects/-Users-dtsym-Work-Splunk--Project-X-homerun/memory/feedback_critical_knob_walkthrough.md`
  with the wording from Task 1.
- [x] Mark completed

### Task 3: Update the memory index

- [x] Append a one-line entry to
  `/Users/dtsym/.claude/projects/-Users-dtsym-Work-Splunk--Project-X-homerun/memory/MEMORY.md`
  in the existing format:
  `- [Critical knob walkthrough enforcement](feedback_critical_knob_walkthrough.md) — recurring failure: applied risk-knob tweaks with dimensionally wrong analysis (3× in 2026-05-08/09); now requires consulting the interaction matrix + filling the walkthrough template before any change.`
- [x] Mark completed

### Task 4: Self-test the rule by re-reading it

- [x] After writing, read the directive end-to-end one more
  time. Confirm:
  - The trigger condition is **specific** (not "any change",
    but "the 15 listed CRITICAL knobs").
  - The pre-action is a **file path**, not an abstract
    intent ("open `_common-bot-parameters.md` matrix entry",
    not "consider interactions").
  - The artefact is a **named template** with numeric fields.
  - The hard stop is **unambiguous** ("if you cannot fill
    Step 3 with a query result, stop and ask").
- [x] If any of the above reads as soft / abstract, rewrite.
  This is the pencil-whipping defence at the rule level.
- [x] Mark completed

### Task 5: Close

- [x] All check-boxes above are `[x]`.
- [x] `git mv docs/plans/0027-agent-memory-rule-critical-knob-walkthrough.md
  docs/plans/completed/`.
- [x] Update [`plan-control-index.md`](plan-control-index.md):
  link target to `completed/0027-...md`.
- [x] Mark completed
