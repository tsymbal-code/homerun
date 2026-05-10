# Plan: Mandatory walkthrough template for CRITICAL knob changes in `runtime-tweaks.md`

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Phase 2 of the three-layer fix from 2026-05-09. Plan 0025
shipped Phase 1 (the [knob interaction matrix](../strategies/_common-bot-parameters.md#knob-interaction-matrix--critical-tier)).
This plan adds Layer 3 — a **mandatory walkthrough template**
inside [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md).
Whenever a runtime tweak changes a CRITICAL-tier knob, the
journal entry is **incomplete** without:

1. **Pre/post threshold values** for every direct gate the knob
   feeds (numeric, not prose).
2. **Pre/post values for every derived metric** the knob shifts
   (e.g. `trader_drawdown_pct` for `max_daily_loss_usd`).
3. **A concrete pre-change SQL/curl simulation** against
   recent live data showing the predicted block-rate / fill-rate
   delta — not abstract reasoning.
4. **A compound-effect checklist** ticking off which sibling
   knobs in the matrix were considered.
5. **A rollback recipe** that reverts in < 30 seconds.

The template is **forced-numeric** by design: prose-only fields
("no impact expected") get rejected at review. The grep-based
invariant in Validation Commands enforces this at audit time.

Done = `runtime-tweaks.md` has a new `## Walkthrough template
for CRITICAL knob changes` section with the skeleton, an
example entry that walks through a hypothetical
`max_daily_loss_usd: 300 → 100` change, and a one-line
cross-reference from
[`_common-bot-parameters.md`](../strategies/_common-bot-parameters.md)
"How to use this matrix" placeholder pointing at the live
template.

This plan is **documentation-only**. No code, no schema, no
runtime behaviour change. Phase 3 (memory rule that points
agents at this template before any PUT) is plan 0027.

## Out of scope

- **CI enforcement** of the walkthrough requirement (e.g.
  pre-commit hook that lints `runtime-tweaks.md` entries).
  Phase 4 candidate.
- **Auto-generation of pre/post numbers** from live data via
  a backend endpoint. Useful for dashboards, but a separate
  plan.
- **Walkthroughs for HIGH/MEDIUM-tier knobs.** This plan
  binds CRITICAL tier only — same scope as the Phase 1
  matrix. HIGH-tier walkthroughs may follow if usage shows
  value.
- **Retrofitting walkthroughs to past entries.** Existing
  journal entries (2026-05-07 sandbox-bots, etc.) stay
  unchanged — append-only journal rule. Walkthrough applies
  to **new** entries from the merge of this plan onward.

## Context / References

- [Plan 0025 — Knob interaction matrix](completed/0025-knob-interaction-matrix-critical-tier.md)
  — Phase 1; defines the 15 CRITICAL knobs the template
  binds against.
- [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  — file extended by this plan. Section "CRITICAL-tier knob
  changes — consult the interaction matrix first" already
  added by Plan 0025; this plan adds the **template body**
  it currently references but doesn't fully spell out.
- [`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md)
  "How to use this matrix (Phase 2 placeholder)" — the
  forward-reference placeholder this plan satisfies.

## Validation Commands

- `grep -q '## Walkthrough template for CRITICAL knob changes' docs/operational/runtime-tweaks.md`
- `grep -c '^### Step ' docs/operational/runtime-tweaks.md` — at least 5 (template has 5 mandatory steps).
- `grep -q 'Worked example' docs/operational/runtime-tweaks.md` — the example entry is present.
- `grep -q 'Phase 2 placeholder' docs/strategies/_common-bot-parameters.md` — placeholder still anchors the reference, OR is replaced with the live link.

### Task 1: Draft the walkthrough template skeleton

- [x] Decide the section anchor name. Recommend
  `## Walkthrough template for CRITICAL knob changes` placed
  immediately after the existing "CRITICAL-tier knob changes
  — consult the interaction matrix first" callout (added by
  plan 0025) and before "## Why this lives in git".
- [x] Define the 5 mandatory steps with forced-numeric
  scaffolds. Skeleton:

  ```markdown
  ### Step 1 — Direct gate impact (numeric)

  For each CRITICAL field changed:

  | Field | Before | After | Direct gate(s) | Pre threshold | Post threshold |
  |---|---:|---:|---|---:|---:|
  | <field> | <num> | <num> | <gate from matrix> | <formula evaluated at "before"> | <formula evaluated at "after"> |

  ### Step 2 — Indirect-metric impact (numeric)

  For each indirect consumer documented in the matrix:

  | Field changed | Derived metric | Pre value (today's data) | Post value (today's data) | Sibling gate that reads it |
  |---|---|---:|---:|---|

  Acceptable to write `n/a — no indirect consumers in matrix` when
  the matrix entry confirms zero indirect consumers.

  ### Step 3 — Live data simulation (SQL or curl, not prose)

  ```sql
  -- "How many decisions in the last 24 h would have been blocked
  --  under the new threshold?"
  SELECT ... FROM trader_decisions WHERE ...
  ```

  Paste the actual query result (1-2 numbers).  Prose-only
  responses are NOT acceptable.

  ### Step 4 — Compound-effect checklist

  Tick every sibling-knob row in the matrix that interacts
  with this change.  At least one row must be ticked OR the
  value `none — verified against matrix on YYYY-MM-DD` is
  written explicitly.

  - [ ] `max_position_notional_usd` — <effect or `n/a`>
  - [ ] `max_gross_exposure_usd` — <effect or `n/a`>
  - [ ] `max_daily_loss_usd` — <effect or `n/a`>
  - [ ] `circuit_breaker_drawdown_pct` — DEAD CODE per matrix; ignore unless matrix changes
  - [ ] `halt_on_consecutive_losses` / `max_consecutive_losses` — <effect or `n/a`>
  - [ ] `max_open_orders` / `max_open_positions` — <effect or `n/a`>
  - [ ] `block_new_orders` / `is_paused` (per-trader) — <effect or `n/a`>
  - [ ] `worker_control` (orchestrator-wide) — <effect or `n/a`>
  - [ ] `allow_taker_limit_buy_above_signal` — <effect or `n/a`>

  ### Step 5 — Rollback recipe (must be < 30 s)

  ```bash
  # exact SQL / curl / UI path that returns the system to the
  # pre-tweak state in under 30 seconds.  Tested before applying.
  ```
  ```

- [x] Note explicitly that `n/a` answers are acceptable **only
  when the matrix entry says so**, not when the writer "doesn't
  expect impact." This is the pencil-whipping defence.
- [x] Mark completed

### Task 2: Write a worked example using `max_daily_loss_usd`

The single example most likely to anchor the template in
operator memory is the dimensional-bug class itself — the
`copy_drawdown` chain. The example walks through a
hypothetical `max_daily_loss_usd: 300 → 100` change:

- [x] **Step 1:** show the two direct gates from the matrix
  (`trader_daily_loss`, `trader_daily_total_loss`) with
  pre/post threshold numbers (-300 → -100).
- [x] **Step 2:** show the `trader_drawdown_pct` indirect
  metric: pre at $30 daily loss = 10% drawdown_pct; post at
  same $30 = 30% drawdown_pct (3× more sensitive). Note
  the `copy_drawdown` gate trips at any
  `max_copy_drawdown_pct < 30`.
- [x] **Step 3:** SQL example that queries
  `trader_decisions` for the last 24 h and projects how many
  would have hit `daily_loss_cap` under the tighter threshold.
  Concrete output line.
- [x] **Step 4:** ticked checklist showing
  `max_copy_drawdown_pct` (strategy_param, not in CRITICAL
  matrix) flagged as compound. CB-drawdown explicitly
  marked DEAD CODE per matrix.
- [x] **Step 5:** one-line UI/SQL revert.
- [x] Mark completed

### Task 3: Insert template + example into `runtime-tweaks.md`

- [x] Edit
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md):
  add the new `## Walkthrough template for CRITICAL knob
  changes` section with the skeleton from Task 1 and the
  worked example from Task 2 (under a `### Worked example —
  hypothetical max_daily_loss_usd: 300 → 100` subheader).
- [x] Reword the existing "CRITICAL-tier knob changes —
  consult the interaction matrix first" callout (added by
  plan 0025) to point at the new template anchor:
  "*…see the [Walkthrough template](#walkthrough-template-for-critical-knob-changes)
  below*".
- [x] Mark completed

### Task 4: Update the placeholder in `_common-bot-parameters.md`

- [x] In
  [`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md),
  the matrix's tail subsection
  `### How to use this matrix (Phase 2 placeholder)` carries
  a forward-reference. Replace its body with a one-paragraph
  summary plus a direct anchor link:
  "*The walkthrough template is now live in
  [`runtime-tweaks.md` — Walkthrough template for CRITICAL
  knob changes](../operational/runtime-tweaks.md#walkthrough-template-for-critical-knob-changes).
  Every new entry that touches a CRITICAL-tier knob must
  fill the 5-step skeleton; prose-only sections are
  rejected at audit.*"
- [x] Drop the "(Phase 2 placeholder)" suffix from the
  subheader.
- [x] Mark completed

### Task 5: Close

- [x] All check-boxes above are `[x]`.
- [x] `git mv docs/plans/0026-runtime-tweaks-walkthrough-template-critical.md
  docs/plans/completed/`.
- [x] Update [`plan-control-index.md`](plan-control-index.md):
  link target to `completed/0026-...md`.
- [x] Mark completed
