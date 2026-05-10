# Plan: Auto-resume orchestrator in shadow mode on application startup

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0021` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

Today every backend container restart hard-pauses the trader
orchestrator: [`backend/main.py:190-233`](../../backend/main.py:190)
`_reset_orchestrator_boot_state()` (called from `lifespan()` at
[`main.py:439`](../../backend/main.py:439)) unconditionally writes
`is_enabled=False, is_paused=True, mode='shadow'`, and clears
`selected_account_id`, `shadow_account_id`, `live_preflight`,
`live_arm`. Operator must then click **Resume all** + **Start** in
the UI (or `POST /api/workers/resume-all` then
`POST /api/trader-orchestrator/start`) for trading to resume.

This safety pattern is correct for **live** mode — never auto-resume
risk after a crash, OOM, or redeploy. For **shadow** mode it is
operationally noisy: every `./deploy/sync_remote.sh` interrupts
sandbox bots, and the operator has to re-arm a system that holds no
real money.

This plan narrows the safety: **hard-reset on startup only when the
prior persisted state was `live` mode OR `is_enabled=False`**. When
the prior state was `mode='shadow'` AND `is_enabled=True` AND
`is_paused=False`, preserve it and just refresh the snapshot row to
say "resumed in shadow on application startup" so the UI sees the
heartbeat. `live_preflight` and `live_arm` always get cleared
regardless — those are live-only flags and stale values across a
restart are dangerous.

### What "done" looks like

- A redeploy of the backend with a Sandbox shadow bot running
  (e.g. `Sandbox - Traders Copy Trade`) does NOT pause that bot.
  Worker restart still happens (rsync + container recreate), but
  on the next cycle the orchestrator loop picks up the existing
  control row and resumes. Operator does not click anything.
- A redeploy with the orchestrator previously in `live` mode (or
  previously stopped) still produces the existing safety reset:
  paused, shadow-forced, account cleared.
- `live_preflight` and `live_arm` are cleared in both branches —
  those flags must never survive a process restart.
- New regression tests pin both branches plus the live-flag-clearing
  invariant and the snapshot-message branch divergence.
- Footgun entry in
  [`docs/plans/architecture/trader-pipeline.md`](architecture/trader-pipeline.md)
  is updated to describe the new conditional behaviour;
  [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  gets a new dated entry.

## Context / References

- Boot-state reset hook: [`backend/main.py:190-233`](../../backend/main.py:190)
  (`_reset_orchestrator_boot_state`) and call site at
  [`main.py:439`](../../backend/main.py:439).
- Operator-side resume endpoint:
  [`backend/api/routes_trader_orchestrator.py:271-315`](../../backend/api/routes_trader_orchestrator.py:271)
  (`POST /api/trader-orchestrator/start`).
- Stop endpoint (the canonical way to enter "do hard-reset on next
  boot" state): [`routes_trader_orchestrator.py:318-338`](../../backend/api/routes_trader_orchestrator.py:318).
- Persistence helpers: `read_orchestrator_control`,
  `update_orchestrator_control`, `write_orchestrator_snapshot`
  (search `backend/services/trader_orchestrator_state.py`).
- Existing smoke tests for `main.py` lifespan:
  [`backend/tests/test_main_lifespan_smoke.py`](../../backend/tests/test_main_lifespan_smoke.py).
- Footgun docs: [`docs/plans/architecture/trader-pipeline.md`](architecture/trader-pipeline.md)
  `## Known footguns`, [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
  2026-05-07 entry.

## Validation Commands

- `cd backend && pytest tests/test_main_lifespan_smoke.py -q`
- `cd backend && ruff check`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose exec -T postgres psql -U homerun -d homerun -c "SELECT mode, is_enabled, is_paused, settings_json->'\''live_arm'\'' AS live_arm, settings_json->'\''live_preflight'\'' AS live_preflight FROM trader_orchestrator_control LIMIT 1"'` — after Task 3 deploy: with sandbox bot previously running in shadow, expect `mode='shadow', is_enabled=t, is_paused=f, live_arm=NULL, live_preflight=NULL`.

## Out of scope

- Adding an operator-facing settings toggle to disable auto-resume
  entirely. The conditional behaviour is correct by construction;
  if the operator wants the old hard-pause, they can `POST /stop`
  before redeploying — that persists `is_enabled=False, is_paused=True`,
  and the new logic will treat that as "stay paused".
- Changing the live-mode safety. Live always hard-resets; this
  plan does not touch that branch.
- Worker-level restart pause behaviour for non-orchestrator workers
  (scanner, news, weather, discovery, crypto, tracked_traders,
  events). Their pause-state is restored from persisted controls
  via the existing `should_pause` block at
  [`main.py:445-478`](../../backend/main.py:445); no change needed.

### Task 1: Conditional reset in `_reset_orchestrator_boot_state`

The current function unconditionally overwrites every field. Make
it inspect the prior persisted control row first and branch on
`(mode, is_enabled)`.

- [ ] In [`backend/main.py:190-233`](../../backend/main.py:190),
      modify `_reset_orchestrator_boot_state` to:
  1. Read current `control = await read_orchestrator_control(session)` first.
  2. Extract `prior_mode = str(control.get("mode") or "shadow").lower()`,
     `prior_enabled = bool(control.get("is_enabled"))`,
     `prior_paused = bool(control.get("is_paused"))`.
  3. Always build the live-flag-clearing settings dict
     `{"live_preflight": None, "live_arm": None}`. Do **not** clear
     `selected_account_id` or `shadow_account_id` in the auto-resume
     branch — preserving them is the whole point.
  4. **Branch A (auto-resume)** when
     `prior_mode == "shadow" AND prior_enabled AND not prior_paused`:
     call `update_orchestrator_control(session, settings_json={live_preflight:None, live_arm:None})`
     to clear only the live flags, preserving mode/enabled/paused/accounts.
     Snapshot `current_activity="Resumed in shadow on application startup"`,
     `running=False, enabled=True`.
  5. **Branch B (hard reset)** otherwise: keep the existing
     overwrite behaviour exactly — `is_enabled=False, is_paused=True,
     mode="shadow", requested_run_at=None`, full settings_json
     clear including `selected_account_id`/`shadow_account_id`.
     Snapshot `current_activity="Paused on application startup"`.
  6. The function must continue to also call
     `runtime_status.update_orchestrator(...)` with values matching
     the branch it took, so the in-memory status mirror is correct.
- [ ] Inline comment block above the function: 3-line note describing
      the two branches and why live always falls into Branch B (one
      sentence per branch + one for "live_preflight/live_arm always
      clear").
- [ ] Mark completed

### Task 2: Regression tests in `test_main_lifespan_smoke.py`

The smoke-test file's docstring already describes its scope as
"top-level main module" tests. Add unit tests for the new branch
logic alongside (mark them with a section comment header so the
file's smoke vs unit split is obvious).

- [ ] Append a section header comment block in
      [`backend/tests/test_main_lifespan_smoke.py`](../../backend/tests/test_main_lifespan_smoke.py)
      after the existing tests:
      `# --- _reset_orchestrator_boot_state branch tests ---`
      with one short paragraph (under 5 lines) explaining the auto-resume
      vs hard-reset split.
- [ ] Add `test_reset_orchestrator_boot_state_auto_resumes_shadow_when_previously_running`:
      mock `read_orchestrator_control` to return
      `{mode:"shadow", is_enabled:True, is_paused:False, settings_json:{selected_account_id:"acc-1", shadow_account_id:"acc-1", live_arm:{...}, live_preflight:{...}}}`,
      capture `update_orchestrator_control` and
      `write_orchestrator_snapshot` calls. Assert: `update` was
      called with **only** `settings_json={live_preflight:None, live_arm:None}`
      kwargs (no `is_enabled`/`is_paused`/`mode` overwrites);
      snapshot has `current_activity="Resumed in shadow on application startup"`
      and `enabled=True`.
- [ ] Add `test_reset_orchestrator_boot_state_hard_resets_when_prior_mode_was_live`:
      mock prior control with `mode="live"`. Assert: `update`
      called with the full hard-reset payload
      (`is_enabled=False, is_paused=True, mode="shadow", requested_run_at=None`,
      settings_json with `selected_account_id=None, shadow_account_id=None,
      live_preflight=None, live_arm=None`); snapshot has
      `current_activity="Paused on application startup"`,
      `enabled=False`.
- [ ] Add `test_reset_orchestrator_boot_state_hard_resets_when_previously_stopped`:
      mock prior `is_enabled=False, mode="shadow"`. Assert hard
      reset (operator explicitly stopped → don't auto-revive).
- [ ] Add `test_reset_orchestrator_boot_state_hard_resets_when_previously_paused`:
      mock prior `is_paused=True, is_enabled=True, mode="shadow"`.
      Assert hard reset (operator paused → don't auto-unpause).
- [ ] Add `test_reset_orchestrator_boot_state_always_clears_live_flags_in_auto_resume`:
      mock prior shadow+enabled with `live_arm={"armed_until":"..."}`,
      `live_preflight={"checks":[...]}`. Assert auto-resume branch
      still nulls both live flags in the `settings_json` patch.
      Pins the safety invariant.
- [ ] Add `test_reset_orchestrator_boot_state_runtime_status_mirrors_branch`:
      capture `runtime_status.update_orchestrator` calls in both
      branches; assert `enabled=True/control.is_enabled=True` mirror
      in auto-resume and `enabled=False/control.is_enabled=False`
      in hard-reset. Pins the in-memory mirror invariant.
- [ ] Run validation: `cd backend && pytest tests/test_main_lifespan_smoke.py -q`.
- [ ] Mark completed

### Task 3: Deploy, verify, update docs, close out

- [ ] Run `./deploy/sync_remote.sh` from the local checkout.
      Confirm `backend` container restarts cleanly.
- [ ] Pre-deploy state: confirm the Sandbox bot
      (`61dcbeb2b9bc42bd9e9635a09ae5e0c3`) is `mode='shadow'`,
      `is_enabled=true`, `is_paused=false` before deploy. Record
      the SQL output in this checkbox.
- [ ] Post-deploy state: re-query
      `trader_orchestrator_control` immediately after restart.
      Expected: `mode='shadow', is_enabled=t, is_paused=f,
      live_arm=NULL, live_preflight=NULL,
      selected_account_id` preserved from before. Record output.
- [ ] Negative-path verification: temporarily `POST /api/trader-orchestrator/stop`,
      then redeploy. Confirm the orchestrator stays paused
      (Branch B) and operator must `POST /start` to resume.
      Record output.
- [ ] Live-path verification (read-only via SQL — do NOT actually
      flip live mode): confirm via the test suite alone that the
      live branch behaviour is preserved
      (`test_reset_orchestrator_boot_state_hard_resets_when_prior_mode_was_live`
      passes). No production live test required — too risky.
- [ ] Update [`docs/plans/architecture/trader-pipeline.md`](architecture/trader-pipeline.md)
      `## Known footguns`: the existing entry about "boot pauses
      orchestrator" needs a paragraph saying "as of plan 0021,
      shadow+enabled+unpaused state is preserved across restarts;
      live and stopped states still hard-reset". Bump
      `Last verified:` to deploy date.
- [ ] Append a new entry to
      [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
      with the deploy date, surface
      (`backend/main.py::_reset_orchestrator_boot_state`),
      one-paragraph why, and rollback recipe (`git revert <SHA>`,
      redeploy — old hard-reset behaviour returns).
- [ ] `git mv docs/plans/0021-orchestrator-auto-resume-shadow-on-startup.md docs/plans/completed/`.
- [ ] Update the row in [`plan-control-index.md`](plan-control-index.md)
      to point at the `completed/` path.
- [ ] `git log --grep='Plan: 0021'` shows the full commit chain.
- [ ] Mark completed
