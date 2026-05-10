# Plan: Flag three additional `TRADER_RISK_DEFAULTS` knobs as dead-code in the UI

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Plan 0036 (HIGH-tier knob audit, 2026-05-10) confirmed three more
fields in `TRADER_RISK_DEFAULTS` are **schema-only** — UI exposes
them, defaults / validators / persistence layers all run, but no
runtime gate / decision / order-manager call ever reads them:

- `slippage_bps` (default 35.0 bps)
- `max_spread_bps` (default 75.0 bps)
- `use_dynamic_sizing` (default `True`)

These join the five fields previously flagged by Plan 0031
(`circuit_breaker_drawdown_pct`, `max_daily_spend_usd`,
`retry_limit`, `retry_backoff_ms`, `order_ttl_seconds`), bringing
the dead-code count in `TRADER_RISK_DEFAULTS` from 5 (20% of 25)
to 8 (32%).

The remediation is identical to Plan 0031: **don't remove the
knobs from the UI; flag them visually with a red background and
a tooltip stating they are dead code.** The frontend rendering
pathway (red-tinted background + `(deprecated)` label suffix +
`title` tooltip) was already wired by Plan 0031 — this plan only
needs to annotate the schema and extend the matrix Dead-code
section. **No frontend changes required.**

The change is two-layer:

1. **Backend** — `strategy_sdk.py` `TRADER_RISK_FIELDS_SCHEMA`
   gets a `dead_code: True` annotation on the three new entries
   (lines 442, 443, 451) plus a Python comment matching the
   format introduced by Plan 0031.

2. **Docs** — `_common-bot-parameters.md` § Dead code in
   `TRADER_RISK_DEFAULTS` extends from 5 to 8 entries; the count
   in the closing summary line bumps from `5 з 25 = 20%` to
   `8 з 25 = 32%`. The HIGH-tier matrix entries for the three
   knobs already carry the `confirmed dead` audit marker from
   Plan 0036 — they stay as-is (cross-link added).

This plan is U-tier (frontend visual change, but actually
zero-frontend-edit because the pathway is reusable) with a
secondary B (one-line backend schema annotation per dead knob).
Per the CRITICAL knob touch policy from plan 0028: **no Task
touches a CRITICAL knob**, so the walkthrough requirement does
not fire. plan-validator rule #11 marks PASS-by-default.

## Out of scope

- **Removing the dead-code fields from the UI / schema /
  defaults.** Operator decision (carried over from 0031): visual
  flag only.
- **Wiring runtime consumers** for the three dead-code fields
  (i.e. making them actually work). That is a separate B plan
  if/when the operator decides per-trader bps / dynamic-sizing
  semantics are wanted.
- **Group D dead knobs from Plan 0036 audit**
  (`scanner_max_opportunities_total/_per_strategy`). They live
  in `SettingsPanel.tsx` (Settings → Scanner panel), which has
  no `dead_code` rendering pathway today. Tracked as a follow-up
  proposal in
  [`plan-control-index.md`](plan-control-index.md#follow-up-plan-proposals-drafts-not-yet-ided)
  ("Wire or move scanner_max_opportunities_*").

## Context / References

- [Plan 0031 — Flag dead-code `risk_limits` knobs in UI with red background](completed/0031-flag-dead-code-knobs-in-ui.md)
  — the original five-knob plan; this one extends it.
- [Plan 0036 — Per-entry audit of the HIGH-tier knob matrix](completed/0036-high-knob-matrix-per-entry-audit.md)
  — the audit that surfaced these three knobs as `confirmed
  dead` (Group A entries in `_common-bot-parameters.md`).
- [`backend/services/strategy_sdk.py:442-451`](../../backend/services/strategy_sdk.py)
  — `TRADER_RISK_FIELDS_SCHEMA` location for the three lines
  to annotate.
- [`frontend/src/components/StrategyConfigForm.tsx`](../../frontend/src/components/StrategyConfigForm.tsx)
  — `ParamField` interface + `ConfigField` renderer (no edit
  needed; reads `dead_code` from schema and applies the
  `bg-rose-500/15 border-rose-400/55 text-rose-300` wrapper).
- [`docs/strategies/_common-bot-parameters.md` § Dead code in `TRADER_RISK_DEFAULTS`](../strategies/_common-bot-parameters.md#dead-code-in-trader_risk_defaults)
  — section to extend from 5 to 8 entries.

## Validation Commands

- `grep -c '"dead_code": True' backend/services/strategy_sdk.py` —
  expect exactly 8 (5 from Plan 0031 + 3 from this plan).
- `grep -nE '\b(slippage_bps|max_spread_bps|use_dynamic_sizing)\b.*dead_code' backend/services/strategy_sdk.py` —
  expect 3 lines.
- `grep -c '^- \*\*`' docs/strategies/_common-bot-parameters.md | sort -u` —
  ensure documentation render is unbroken (sanity).
- `grep -A1 '### Dead code in `TRADER_RISK_DEFAULTS`' docs/strategies/_common-bot-parameters.md | head -20` —
  the section now lists 8 bullets.
- After deploy: open Bot → Risk Limits flyout, verify
  `Slippage Guard (bps)`, `Max Spread (bps)`, and
  `Dynamic Position Sizing` render with the same red-tinted
  background as the five existing dead-code fields. If the
  operator declines redeploy, defer the visual smoke to next
  operator-driven deploy.

### Task 1: Annotate the three entries in `TRADER_RISK_FIELDS_SCHEMA`

- [x] Edit
  [`backend/services/strategy_sdk.py`](../../backend/services/strategy_sdk.py)
  `TRADER_RISK_FIELDS_SCHEMA`. For each of these three lines,
  add `"dead_code": True` to the dict and a two-line Python
  comment immediately above (matching the format used by Plan
  0031 — `# Dead code as of 2026-05-10 — see docs/strategies/_common-bot-parameters.md` /
  `# § Dead code in TRADER_RISK_DEFAULTS.  Schema-only; no runtime consumer.`):
  - Line 442 — `slippage_bps`
  - Line 443 — `max_spread_bps`
  - Line 451 — `use_dynamic_sizing`
- [x] Mark completed

### Task 2: Extend the matrix Dead-code section

- [x] In
  [`docs/strategies/_common-bot-parameters.md` § Dead code in `TRADER_RISK_DEFAULTS`](../strategies/_common-bot-parameters.md#dead-code-in-trader_risk_defaults)
  insert three new bullets (between the existing five) for
  `slippage_bps`, `max_spread_bps`, `use_dynamic_sizing`. Each
  bullet follows the same pattern as the existing five:
  default + schema citation + one-paragraph reason why the field
  has no runtime consumer + cross-link to the HIGH-tier matrix
  entry (which already has the audit verdict from Plan 0036).
- [x] Update the closing summary line from `5 з 25` → `8 з 25`
  (and `20% dead code` → `32% dead code`).
- [x] Drive-by fix: refresh stale Plan-0031 line citations in the
  same Dead-code section (5 bullets had drift up to +17 lines
  from the original 2026-05-10 commit; my edit shifted them
  further) and bump the UI-marker paragraph's
  `strategy_sdk.py:435+` → `:437+`. In-place corrections only;
  no semantic change.
- [x] Mark completed

### Task 3: Validate the change

- [x] Run `grep -c '"dead_code": True' backend/services/strategy_sdk.py` —
  result `8` (5 from Plan 0031 + 3 new).
- [x] Run `grep -nE '\b(slippage_bps|max_spread_bps|use_dynamic_sizing)\b.*dead_code' backend/services/strategy_sdk.py` —
  three matches at lines 444, 447, 457 (drifted from 442/443/451
  due to the inserted 6 comment lines, exactly as expected).
- [x] Confirm no frontend file was modified — `git diff
  --name-only frontend/` empty. Rendering pathway from Plan 0031
  picks up `dead_code` from schema automatically because
  `StrategySDK.trader_risk_fields_schema()` does
  `dict(field)` per entry → all keys including `dead_code`
  reach the API consumer
  (`backend/services/trader_orchestrator/config_schema.py:360`
  → `routes_strategies.py:1227` → frontend
  `StrategyConfigForm.tsx:387` `if (!props.field.dead_code)`).
- [x] Mark completed

### Task 4: Close

- [x] All check-boxes above are `[x]`.
- [x] `git mv docs/plans/0038-flag-three-trader-risk-knobs-as-dead.md
  docs/plans/completed/`.
- [x] Update [`plan-control-index.md`](plan-control-index.md):
  link target to `completed/0038-...md`.
- [x] Mark completed
