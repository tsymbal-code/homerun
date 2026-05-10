# Plan: Flag dead-code `risk_limits` knobs in the UI with red background

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: <NNNN>` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Plans 0025 and 0029 documented five `TRADER_RISK_DEFAULTS` fields
that the operator can edit through the UI but the runtime
ignores entirely:

- `circuit_breaker_drawdown_pct`
- `max_daily_spend_usd`
- `retry_limit`
- `retry_backoff_ms`
- `order_ttl_seconds`

The operator's preferred remediation (2026-05-10): **don't
remove the knobs from the UI; flag them visually with a red
background and a tooltip stating they are dead code.** Removing
them silently would be more confusing if other agents or
operators ever wired them in later; the visual flag is honest
and reversible.

The change is two-layer:

1. **Backend** — `strategy_sdk.py` `TRADER_RISK_FIELDS_SCHEMA`
   gets a `dead_code: True` annotation on those five entries.
   That makes the schema itself the canonical source of truth
   for "which knobs the runtime ignores," visible to every
   consumer (frontend, API clients, future doc generators).

2. **Frontend** — `StrategyConfigForm.tsx` extends `ParamField`
   with the optional `dead_code` flag and wraps any field
   carrying it in a red-tinted container with a hover tooltip
   "Dead-code knob: this field has no runtime effect — see
   matrix." No change to input behaviour (operator can still
   edit and save; it just won't do anything, same as today).

3. **Docs** — `_common-bot-parameters.md` Dead-code section
   (added by 0029) gets a one-paragraph note that the UI now
   visually flags these fields.

This plan is U-tier (frontend visual change) with secondary
B (one-line backend schema annotation per dead knob). Per the
CRITICAL knob touch policy from plan 0028: **no Task touches a
CRITICAL knob**, so the walkthrough requirement does not fire.
plan-validator rule #11 marks PASS-by-default.

## Out of scope

- **Removing the dead-code fields from the UI / schema /
  defaults.** Operator decision: visual flag only.
- **Wiring runtime consumers** for the five dead-code fields
  (i.e. making them actually work). That is a separate B plan
  if/when the operator wants the behaviour.
- **Re-running the survey for additional dead-code candidates.**
  Plan 0032 was earlier proposed for `live_provider_health.*`
  and was declined this round.
- **Per-strategy MEDIUM-tier matrix** (plan 0030, declined).

## Context / References

- [Plan 0025 — Knob interaction matrix CRITICAL](completed/0025-knob-interaction-matrix-critical-tier.md)
  — first dead-code finding (`circuit_breaker_drawdown_pct`).
- [Plan 0029 — HIGH tier expansion](completed/0029-knob-matrix-high-tier-expansion.md)
  — confirmed four additional dead-code knobs.
- [`backend/services/strategy_sdk.py:421`](../../backend/services/strategy_sdk.py)
  — `TRADER_RISK_FIELDS_SCHEMA` location.
- [`frontend/src/components/StrategyConfigForm.tsx`](../../frontend/src/components/StrategyConfigForm.tsx)
  — `ParamField` interface + `ConfigField` renderer.
- [`frontend/src/components/RiskLimitsView.tsx`](../../frontend/src/components/RiskLimitsView.tsx)
  — wrapper that mounts `StrategyConfigForm` inside the
  Bot → Risk Limits flyout.
- [`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md#dead-code-in-trader_risk_defaults)
  — matrix entry for the five dead-code knobs (after 0029
  merge).

## Validation Commands

- `cd frontend && npm run typecheck`
- `grep -c '"dead_code": True' backend/services/strategy_sdk.py` — expect exactly 5
- `grep -q 'dead_code' frontend/src/components/StrategyConfigForm.tsx` — frontend recognises the flag
- `grep -q 'red-tinted background' docs/strategies/_common-bot-parameters.md` — matrix mentions the UI marker
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T backend python -c "from services.strategy_sdk import StrategySDK; dead = [f[\"key\"] for f in StrategySDK.TRADER_RISK_FIELDS_SCHEMA if f.get(\"dead_code\")]; print(sorted(dead))"'` — should return the five expected keys

### Task 1: Annotate the five entries in `TRADER_RISK_FIELDS_SCHEMA`

- [x] Edit
  [`backend/services/strategy_sdk.py`](../../backend/services/strategy_sdk.py)
  `TRADER_RISK_FIELDS_SCHEMA` (≈ line 421-470). Add
  `"dead_code": True` to the dict entries for:
  - `max_daily_spend_usd`
  - `order_ttl_seconds`
  - `retry_limit`
  - `retry_backoff_ms`
  - `circuit_breaker_drawdown_pct`
- [x] Add a short Python comment immediately above each
  annotated entry: `# Dead code as of 2026-05-10 — see matrix
  in docs/strategies/_common-bot-parameters.md.` This makes
  the rationale visible at the schema declaration so the
  next agent reading the file doesn't strip the flag.
- [x] Mark completed

### Task 2: Extend `ParamField` and render the dead-code marker

- [x] In
  [`frontend/src/components/StrategyConfigForm.tsx`](../../frontend/src/components/StrategyConfigForm.tsx),
  add `dead_code?: boolean` to the `ParamField` interface
  (currently around line 7-19).
- [x] In `ConfigField` (around line 352+), wrap the existing
  switch-case return values with a conditional outer `<div>`
  that, when `field.dead_code === true`, applies a red-tinted
  background, a left border, and a `title` attribute
  containing the tooltip text. Tooltip wording:

  > Dead-code knob: this field is exposed for historical
  > reasons but no runtime gate / decision / execution path
  > reads it. Editing has no effect. See `docs/strategies/_common-bot-parameters.md`
  > § Dead code in TRADER_RISK_DEFAULTS for the audit trail.

- [x] Use the same theme-agnostic palette pattern as the
  earlier 2026-05-09 fix to the `WalletTracker` /
  `DiscoveryPanel` tone classes:
  `bg-rose-500/15 border-rose-400/55 text-rose-300` for the
  dead-code wrapper. Visible on both light and dark
  backgrounds; the input itself remains the existing colour
  so values are still readable.
- [x] Add a tiny `(deprecated)` text annotation next to the
  field's `<Label>` so the marker is visible to keyboard /
  screen-reader users who don't see the colour.
- [x] Mark completed

### Task 3: Update the matrix's Dead-code section

- [x] In
  [`docs/strategies/_common-bot-parameters.md`](../strategies/_common-bot-parameters.md#dead-code-in-trader_risk_defaults)
  prepend a one-paragraph sentence to the
  `### Dead code in \`TRADER_RISK_DEFAULTS\`` subsection:

  > UI marker: as of plan 0031, the Bot → Risk Limits form
  > renders these five fields with a red-tinted background
  > and an `(deprecated)` label suffix. The schema flag
  > `dead_code: true` lives in
  > [`strategy_sdk.py` `TRADER_RISK_FIELDS_SCHEMA`](../../backend/services/strategy_sdk.py).
  > To unflag (i.e. wire a real consumer back in), remove the
  > flag in the schema, ship the gate / decision logic, and
  > update this section.

- [x] Mark completed

### Task 4: Sanity-check the change

- [x] `npm run typecheck` clean.
- [x] `python -c "from services.strategy_sdk import StrategySDK; dead = sorted(f['key'] for f in StrategySDK.TRADER_RISK_FIELDS_SCHEMA if f.get('dead_code')); print(dead)"` returns the expected five keys.
- [x] If the operator is willing to redeploy via
  `./deploy/sync_remote.sh`: open the Bot → Risk Limits
  flyout, confirm the five fields render with the red
  background and the tooltip is visible on hover. Otherwise
  defer the visual smoke to the next operator-driven deploy.
- [x] Mark completed

### Task 5: Close

- [x] All check-boxes above are `[x]`.
- [x] `git mv docs/plans/0031-flag-dead-code-knobs-in-ui.md
  docs/plans/completed/`.
- [x] Update [`plan-control-index.md`](plan-control-index.md):
  link target to `completed/0031-...md`.
- [x] Mark completed
