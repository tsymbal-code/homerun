# Plan: Knob interaction matrix — CRITICAL tier

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Phase 1 of the three-layer fix proposed by the operator on
2026-05-09 to stop the recurring class of "agent applies a
risk-knob change with dimensionally-wrong analysis" failures
seen ~3× in this session (most recently around the
`max_daily_loss_usd`/`circuit_breaker_drawdown_pct` confusion).

The hypothesis: memory-rules can't substitute for missing
**knowledge** of how each safety knob propagates through gates,
derived metrics and compound effects. This plan captures that
knowledge, with file:line citations and exact formulas, in a
durable doc artefact future agents and operators consult before
mutating live values.

Done = the existing
[`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md)
gains a new "Knob interaction matrix — CRITICAL tier" section
covering ~15 knobs of the highest blast-radius class, each with
the four fields the operator's proposal specified:

1. **Direct consumer(s)** — gate name, file:line, exact formula
2. **Indirect consumer(s)** — derived metrics keyed off the knob
3. **Sibling/compound effects** — how this knob interacts with
   other knobs already in the matrix
4. **Tier classification** — CRITICAL / HIGH / MEDIUM, so the
   eventual Layer 3 walkthrough template (Phase 2) can scale
   friction proportionally

This plan is **documentation-only**. No code change. No
runtime behaviour change. Phase 2 (Layer 3 template enforcing
walkthrough on every CRITICAL change) and Phase 3 (Layer 2
memory rule pointing at this doc) are deferred to follow-up
plans.

## Out of scope

- **HIGH / MEDIUM / LOW tier knobs** are tier-classified inline
  but not exhaustively documented in this round. Strategy
  params (e.g. `min_probability`, `min_upside_percent`) are
  covered today in per-strategy docs under
  [`docs/strategies/`](../strategies/) and are MEDIUM at most.
- **Drift tests** that assert the documented formulas match
  current code behaviour. Phase 4 candidate, separate plan.
- **The Layer 3 walkthrough template** in
  [`runtime-tweaks.md`](../operational/runtime-tweaks.md) —
  Phase 2.
- **The Layer 2 memory rule** that points agents at this
  matrix — Phase 3.
- **Runtime-behaviour changes** of any kind. This plan only
  edits one Markdown file plus a small note in the plan
  control index.

## Context / References

- Operator analysis (2026-05-09 session) that led to the
  three-layer fix design.
- [`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md)
  — the existing file this plan extends. Has the
  `risk_limits_json` schema reference, Ukrainian voice, and
  links to per-strategy docs.
- [`backend/services/risk_manager.py`](../../backend/services/risk_manager.py)
  — primary evaluator of `risk_limits` knobs.
- [`backend/services/strategy_sdk.py`](../../backend/services/strategy_sdk.py)
  — `TRADER_RISK_DEFAULTS`, validation, and shape contract.
- [`backend/services/trader_orchestrator/decision_gates.py`](../../backend/services/trader_orchestrator/decision_gates.py)
  — secondary readers (e.g. `max_market_data_age_ms`).
- [`backend/services/strategies/traders_copy_trade.py`](../../backend/services/strategies/traders_copy_trade.py)
  — example of an indirect consumer (`copy_drawdown` gate
  derives from `trader_drawdown_pct`, which derives from
  `max_daily_loss_usd`).
- [`backend/api/routes_workers.py`](../../backend/api/routes_workers.py)
  — `worker_control` writes (`pause/start` API).

## Validation Commands

This plan ships no code, so validation reduces to documentation
invariants on the produced section:

- `grep -c '\.py:' docs/strategies/_common-bot-parameters.md` —
  expect ≥ 30 file:line citations after the matrix lands
  (~2 per CRITICAL knob × 15 knobs).
- `grep -E '^### (CRITICAL|HIGH)' docs/strategies/_common-bot-parameters.md`
  — every matrix entry has a tier-classified header.
- `grep -q 'Knob interaction matrix' docs/strategies/_common-bot-parameters.md`
  — section anchor exists.
- `grep -c '^Last verified:' docs/strategies/_common-bot-parameters.md`
  — exactly 1 marker present.
- `cd frontend && npm run typecheck` — sanity (no frontend touch).

### Task 1: Identify the CRITICAL knob list and tier-classify

The operator's proposed criterion is "state-flipping, wide
blast radius, hard to reverse." Concrete shortlist (≈15
CRITICAL entries):

- [x] **Position-size caps:** `max_position_notional_usd`,
  `max_trade_notional_usd`, `max_gross_exposure_usd`,
  `min_exit_notional`.
- [x] **Open-state caps:** `max_open_orders`,
  `max_open_positions`.
- [x] **Loss / drawdown caps:** `max_daily_loss_usd`,
  `circuit_breaker_drawdown_pct`,
  `circuit_breaker_safe_exit`.
- [x] **Streak halts:** `halt_on_consecutive_losses`,
  `max_consecutive_losses`.
- [x] **Manual halt flags:** `block_new_orders` (per-trader),
  trader `is_paused` / `is_enabled`, orchestrator
  `is_paused` / `is_enabled`.
- [x] **Live execution toggles:** `allow_taker_limit_buy_above_signal`
  (state-changes shadow simulator behaviour),
  `force_flatten`-class triggers.
- [x] Confirm the list before writing entries; commit a short
  note in this task documenting the final scope. If a knob is
  excluded as MEDIUM/HIGH, record the tier rationale.
- [x] Mark completed

### Task 2: Map each CRITICAL knob to direct consumers

For every knob in Task 1's list:

- [x] Find the gate(s) that read the knob via `git grep -n`.
  Capture file:line.
- [x] Read the surrounding 30 lines to extract the **exact**
  comparison or formula. Example shape:

  ```
  formula:  trader_daily_pnl_usd <= -max_daily_loss_usd → block
  consumer: backend/services/risk_manager.py:412-418 (as of 2026-05-09)
  gate-name written into trader_decisions.reason: "daily_loss_cap_breached"
  ```

- [x] If the same knob has multiple direct consumers (e.g.
  read in `risk_manager` AND in `decision_gates` AND in
  `live_pressure`), list each separately with its own formula.
  Most CRITICAL knobs have 1-3 direct consumers.
- [x] Mark completed

### Task 3: Map each CRITICAL knob to indirect consumers

This is the step that catches the dimensional bug from the
operator's example (`max_daily_loss_usd` → `trader_drawdown_pct`
→ `copy_drawdown` gate).

- [x] For every knob in Task 1, search downstream services for
  references to derived metrics that mention the knob in their
  computation. Example chain:

  ```
  max_daily_loss_usd
    └─ derives → copy_risk_context.trader_drawdown_pct
                  formula: (-trader_total_daily_pnl_usd / max_daily_loss_usd) × 100
                  consumer: backend/services/strategies/traders_copy_trade.py:587-801
                  gate-name: "copy_drawdown"
                  compound: lowering max_daily_loss_usd N× makes drawdown_pct N× more
                            sensitive → copy_drawdown gate trips N× sooner for
                            same nominal loss
  ```

- [x] When a knob has zero indirect consumers, write
  "no indirect consumers found in <date> grep" so the absence
  is stated, not inferred.
- [x] Mark completed

### Task 4: Document sibling / compound effects

For each knob, identify which **other** CRITICAL knobs in the
matrix interact with it. The compound effects the operator
called out as a class:

- `max_position_notional_usd` × `max_gross_exposure_usd` →
  effective max simultaneous positions
- `halt_on_consecutive_losses` × position size → tighter
  per-position cap means more attempts per hour, streak hits
  faster
- `circuit_breaker_drawdown_pct` × `circuit_breaker_safe_exit`
  → when CB fires, exit-mode determines whether positions
  force-flatten (immediate batch realization → kicks daily
  loss cap in turn)
- `max_open_orders` × strategy entry rate → bot appears
  "frozen" when cap is reached but positions don't drain
  (the wave-2..4 stuck-positions symptom from 2026-05-08)

- [x] Each knob entry has a "Compound with" subsection listing
  ≥ 0 sibling-knob interactions. Use plain language; no need
  for full formulas at this level — file:line citations on the
  direct/indirect consumers carry the precision.
- [x] Mark completed

### Task 5: Write the matrix into `_common-bot-parameters.md`

- [x] Append a new top-level section
  `## Knob interaction matrix — CRITICAL tier` at the end of
  [`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md)
  (before the `## Посилання` references list, if one exists).
  Write a short Ukrainian preamble (~3 paragraphs) mirroring
  the rest of the file's voice:
  - what the matrix is for (зміна safety knob = read this first)
  - tier definitions (CRITICAL / HIGH / MEDIUM)
  - where Phase 2 (walkthrough template) and Phase 3 (memory
    rule) plug in
- [x] Per-knob entries follow the operator's proposed shape:

  ```markdown
  ### CRITICAL — `max_daily_loss_usd`

  **Default:** 100.0 (USD)
  **TRADER_RISK_DEFAULTS:** [`strategy_sdk.py:411-413`](...)

  #### Direct consumers

  | Gate | Formula | File:line | reason-string |
  |---|---|---|---|
  | daily_loss_cap | `trader_daily_pnl_usd <= -max_daily_loss_usd → block` | `risk_manager.py:412-418` | `daily_loss_cap_breached` |

  #### Indirect consumers

  | Derived metric | Formula | Consumer | Gate |
  |---|---|---|---|
  | `trader_drawdown_pct` | `(-trader_total_daily_pnl_usd / max_daily_loss_usd) × 100` | `traders_copy_trade.py:599-801` | `copy_drawdown` |

  #### Compound with

  - **`max_copy_drawdown_pct` (strategy_param):** lowering
    `max_daily_loss_usd` N× makes `trader_drawdown_pct` N× more
    sensitive → `copy_drawdown` gate trips N× sooner for the
    same nominal loss. Operator typically discovers this when
    a "tighten the daily loss" tweak unexpectedly silences the
    copy-trade bot entirely.
  - **`circuit_breaker_drawdown_pct`:** independent base
    (% of `starting_capital`, not of `max_daily_loss_usd`) —
    explicitly NOT compound. Stated here to prevent the
    inverse confusion.
  ```

- [x] Repeat for the other ~14 CRITICAL knobs from Task 1.
- [x] Add a final subsection
  `### How to use this matrix (Phase 2 placeholder)` —
  one paragraph explaining that future
  [`runtime-tweaks.md`](../../operational/runtime-tweaks.md)
  entries on a CRITICAL knob change will require a written
  walkthrough citing this matrix; the template lands in
  Phase 2.
- [x] Mark completed

### Task 6: Cross-reference + close

- [x] Add a one-line link from the existing
  [`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md)
  table-of-contents (top of file) into the new section.
- [x] Add a one-line link from
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  Format section pointing at the matrix as the "consult
  before applying CRITICAL change" reference.
- [x] Append `Last verified: YYYY-MM-DD` at the end of
  `_common-bot-parameters.md` if missing; bump if present.
- [x] `git mv docs/plans/0025-knob-interaction-matrix-critical-tier.md
  docs/plans/completed/`.
- [x] Update [`plan-control-index.md`](plan-control-index.md):
  link target to `completed/0025-...md`.
- [x] Mark completed
