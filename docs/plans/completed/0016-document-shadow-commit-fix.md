# Plan: Documentation hygiene for the shadow-execution commit fix

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0016` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Commit `936f96a4` (`fix(session_engine): ensure execution session
persistence with explicit commit in shadow mode`) landed on `main`
on 2026-05-09 as an emergency hotfix without a `Plan:` trailer. It
fixed a critical race in `_persist_execution_projection`: an
absent `await self.db.commit()` allowed the async session close to
roll back already-flushed `trader_orders` /
`execution_sessions` / `trader_positions` rows, manifesting as
`selected` decisions that never materialised into shadow trades.

Live verification (post-fix, 2-min soak): 0 → 13 open
`trader_positions` ($215 notional), 0 → 10 `trader_orders`
(`status=open`, $191), 10 `execution_sessions` (`status=completed`).
The full diagnosis, patch, and rollback recipe live in
[`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
under the `2026-05-09 ~07:30 UTC — shadow execute_signal commit
fix` entry.

This plan is the **post-hoc audit-trail record** for that work,
plus the architecture-note touch-ups the fix triggered (which
`/sync-docs 5` surfaced):

1. **`execution-and-fills.md` § Shadow path** is wrong about which
   table holds shadow fills. The note says
   `simulation_trades` / `simulation_positions` / `simulation_accounts`
   are the shadow ledger; the actual orchestrator-driven path
   writes to `trader_orders` (`mode='shadow'`) +
   `trader_positions` + `execution_sessions`. `simulation_*` is a
   legacy / standalone simulator path that is not exercised by
   the live orchestrator today (0 rows ever in this deployment).
2. **`trader-pipeline.md` Step 7** lists `Cox-PH limit_price_not_executable`
   as the dominant cause of `selected → 0 trader_orders` in
   shadow. The commit-fix introduced a second canonical cause —
   missing commit in the projection persister — that operators
   should know about; symptoms and diagnosis differ enough to
   warrant their own paragraph.

"Done" looks like: both arch-notes corrected, `Last verified`
bumped to today, this plan archived to `completed/`. The runtime
behaviour is already shipped; only documentation moves.

## Context / References

- Patch commit: `936f96a4` — `fix(session_engine): ensure execution session persistence with explicit commit in shadow mode`
- Operational journal entry: [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md) `2026-05-09 ~07:30 UTC — shadow execute_signal commit fix`
- Regression test: [`backend/tests/test_execution_session_engine.py::test_execute_signal_shadow_persists_with_commit_so_async_session_close_does_not_rollback`](../../backend/tests/test_execution_session_engine.py)
- Notes that drift: [`execution-and-fills.md`](architecture/execution-and-fills.md), [`trader-pipeline.md`](architecture/trader-pipeline.md)
- Plan 0014 introduced the `/sync-docs` command and the `Last verified` discipline this plan honours.

## Validation Commands

- `grep -q 'trader_orders.*mode.*shadow' docs/plans/architecture/execution-and-fills.md`
- `grep -q '_persist_execution_projection' docs/plans/architecture/execution-and-fills.md`
- `grep -q '_persist_execution_projection' docs/plans/architecture/trader-pipeline.md`
- `grep -q '936f96a4' docs/plans/architecture/execution-and-fills.md`
- `grep -q 'Last verified: 2026-05-09' docs/plans/architecture/execution-and-fills.md`
- `grep -q 'Last verified: 2026-05-09' docs/plans/architecture/trader-pipeline.md`

### Task 1: Correct the shadow ledger section in `execution-and-fills.md`

The "Shadow path (Cox-PH fill simulator)" key-files block claims
`execution_simulator.py` writes to `simulation_trades` /
`simulation_positions`. In the orchestrator-driven path this is
wrong — the orchestrator's `submit_execution_leg` writes to
`trader_orders` (`mode='shadow'`) and `trader_positions`, with
`execution_sessions` as the state machine. `simulation_*` is a
legacy simulator owned by `services/simulation/execution_simulator.py`
that the current pipeline does not exercise.

- [x] Edit `## Key files` Shadow path block: clarify that
      `execution_simulator.py` is the legacy standalone simulator
      (kept for replay/historical use) and that the **current**
      orchestrator-driven shadow path goes through
      `submit_execution_leg()` →
      `_persist_execution_projection()` →
      `trader_orders` + `trader_positions` + `execution_sessions`.
- [x] Edit `## Contracts` "Shadow path state machine" — describe
      `_persist_execution_projection` and the **mandatory**
      `await self.db.commit()` at the end of it (post-fix); without
      that commit, async session close rolls everything back.
- [x] Edit `## Known footguns` — add a new bullet: "Missing commit
      in `_persist_execution_projection`. Symptom: `selected`
      decisions exist, `trader_open_positions` blocker fires
      80%+ of cycles, `trader_orders` and `execution_sessions`
      tables empty despite the orchestrator log showing fills.
      Diagnosed and fixed in commit `936f96a4` (see
      `runtime-tweaks.md` 2026-05-09 entry); the regression test
      `test_execute_signal_shadow_persists_with_commit_so_async_session_close_does_not_rollback`
      pins the invariant. Watch for it on any future refactor of
      `session_engine.py`'s persistence path."
- [x] Note that `simulation_trades` / `simulation_positions` /
      `simulation_accounts` are the **legacy** simulator's
      ledger; mention they may be retired. Do not promise
      removal — that's a separate plan.
- [x] Bump `Last verified:` to `2026-05-09`.
- [x] Mark completed

### Task 2: Add commit-missing failure mode to `trader-pipeline.md` Step 7

Step 7 ("Order materialisation: selected → submitted") currently
attributes "selected decisions exist but `trader_orders` is empty"
in shadow mode "almost always" to Cox-PH `limit_price_not_executable`.
That is no longer the only canonical cause.

- [x] In `## Diagnostic playbook` Step 7, after the existing Cox-PH
      paragraph, add a second paragraph covering the
      `_persist_execution_projection` commit-missing failure mode:
      symptom signature (≥80% of cycles blocked on
      `trader_open_positions`, no rollback log line, no Cox-PH
      reason), diagnosis (check whether commit is reached in the
      projection persister; the fix added an INFO line
      `execution_session persisted` and an ERROR line
      `execution_session persist commit failed`), reference to
      the journal entry and commit `936f96a4`.
- [x] In `## Known footguns`, add a one-line entry mirroring the
      footgun added in `execution-and-fills.md` (so readers of
      either note find it).
- [x] Bump `Last verified:` to `2026-05-09`.
- [x] Mark completed

### Task 3: Out-of-scope drift surfaced by `/sync-docs 5` (record only)

The audit window also flagged drift not addressed here. Recording
so the next plan picks them up without a fresh audit:

- [x] Add an "Out of scope" subsection to this plan that lists the
      following untouched drift items, with their owning notes:
      - `backend/services/trader_orchestrator/decision_gates.py`
        (commits `cc002843`, `b19fa877`) — new test
        `test_trader_orchestrator_decision_gates.py` exists; the
        `trader-pipeline.md` Stage 5 § Decision dataclasses table
        is not affected, but the gate catalogue may benefit from
        a refresh once the new gates stabilise.
      - `backend/services/strategy_sdk.py` + `trader_risk` policy
        fields (`6ab5f3a6`) — `backend-architecture.md` Strategy
        SDK section may need a row.
      - `frontend/src/components/TradingPanel.tsx` +
        `apiTraders.ts` — new risk-policy panel surface;
        `frontend-architecture.md` may need a row.
      - `backend/services/trader_orchestrator_state.py`,
        `fast_submit.py`, `fast_trader_runtime.py` — minor edits;
        public surface unchanged from the notes' perspective.
- [x] Confirm none of these are emergencies (operator
      decision; default = defer to a future hardening plan).
- [x] Mark completed

### Task 4: Close-out

- [x] Run all Validation Commands locally; all pass.
- [x] `git log --grep='Plan: 0016'` shows the plan-trailer
      commits (this plan's deliverables, **not** the patch
      commit `936f96a4` — that one landed pre-plan and is
      cross-referenced from the plan body and the arch-notes).
- [x] `git mv docs/plans/0016-document-shadow-commit-fix.md
      docs/plans/completed/`.
- [x] Update the row in `plan-control-index.md` to point at the
      `completed/` path.
- [x] Push to `origin/main`.
- [x] Mark completed
