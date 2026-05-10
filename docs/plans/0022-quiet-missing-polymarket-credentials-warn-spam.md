# Plan: Quiet `missing_polymarket_credentials` reseeder warn spam

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0022` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

The WalletStateCache reseeder loop in
[`backend/workers/trader_reconciliation_worker.py`](../../backend/workers/trader_reconciliation_worker.py)
fires a `WARNING` every 5–30 seconds when Polymarket credentials
are missing — three emit sites in `_reseed_wallet_state_cache_from_rest`
(lines ~763, ~770, ~786). On a Sandbox-only deployment with no
live bots and no Polymarket private key configured, this floods
`worker-trading` with ~720 warn lines per hour with zero
operational signal.

The cache itself is a **live-mode-only** dependency — shadow
execution does not consume it for opening positions. So when
`live_execution_service.get_last_init_error()` is the literal
string `"missing_polymarket_credentials"`, the loop is operating
exactly as designed, and the cycle-by-cycle WARN is pure noise.

This plan replaces per-cycle warn spam with **transition-based
announcements**: log `WARNING` once when the loop enters or
exits the missing-credentials state, and `DEBUG` for the
intervening skip emits. Other init-error strings (transient HTTP
failures, gamma timeouts, etc.) keep the existing per-cycle
`WARNING` behaviour because those are real degradation signals
operators must see.

### What "done" looks like

- Steady-state `worker-trading` log volume drops by ~720
  WARN lines/hour during shadow-only operation. Verified by
  log-grep before and after deploy.
- One `WARNING` line at the moment the state transitions:
  - On loop start with creds missing: "polymarket credentials
    missing — demoting per-cycle reseeder warnings to DEBUG"
  - When creds appear later: "polymarket credentials now present
    — resuming standard reseeder logging"
- Other init-error strings (anything other than the literal
  `"missing_polymarket_credentials"`) keep the existing
  per-cycle `WARNING` — they signal real degradation, not
  configuration absence.
- Regression tests pin: (a) first cycle with missing creds emits
  one warn announcement + DEBUG skip, (b) subsequent cycles with
  same state emit DEBUG only, (c) transition out of missing-creds
  emits one resume warn, (d) other init errors keep WARN.
- `runtime-tweaks.md` records the deploy with a one-line rationale
  and rollback recipe.

## Context / References

- Reseeder loop and three warn sites:
  [`backend/workers/trader_reconciliation_worker.py:714-790`](../../backend/workers/trader_reconciliation_worker.py:714)
  (`_reseed_wallet_state_cache_from_rest`).
- Loop entry + interval constants:
  [`trader_reconciliation_worker.py:585-637`](../../backend/workers/trader_reconciliation_worker.py:585).
- Init-error producer:
  [`backend/services/live_execution_service.py:1168-2048`](../../backend/services/live_execution_service.py)
  (`_resolve_polymarket_credentials` + `_last_init_error` setter).
- Plan 0018 closing analysis surfaced this issue as benign-but-noisy
  ([`docs/plans/completed/0018-...`](completed/0018-fix-stuck-shadow-positions-traders-copy-trade.md)).
- Adjacent worker tests:
  [`backend/tests/test_trader_live_provider_reconciliation.py`](../../backend/tests/test_trader_live_provider_reconciliation.py),
  [`backend/tests/test_wallet_state_cache.py`](../../backend/tests/test_wallet_state_cache.py).

## Validation Commands

- `bash scripts/run_tests_remote.sh tests/test_wallet_cache_reseeder_quiet_mode.py`
- `bash scripts/run_tests_remote.sh tests/test_trader_live_provider_reconciliation.py`
- `ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since=10m worker-trading 2>&1 | grep -c "WalletStateCache reseeder skipped"'` — after deploy: expect 0 (or 1, if a state transition happened in the window).

## Out of scope

- Adding Polymarket credentials. That's an operator action, not
  a code change. This plan only quiets the noise that exists
  while creds are absent.
- Demoting other init-error strings. Only the literal
  `"missing_polymarket_credentials"` is configuration-absence;
  every other error string is a real failure that must stay at
  `WARNING`.
- Refactoring the wider reseeder loop. The cycle cadence,
  bootstrap interval, hard-budget, cancel-recovery — all stay
  exactly as they are. The fix is one transition-detector and
  three log-level swaps.
- Auto-detecting "no live trader exists" via DB query. The
  init-error string is sufficient and avoids a per-cycle DB hit;
  if creds appear, normal logging resumes regardless of trader
  state.

### Task 1: Transition-based logging in `_reseed_wallet_state_cache_from_rest`

Add a module-level state variable that tracks the last observed
`live_execution_service.get_last_init_error()` value, and a small
helper that decides whether the current error is the
configuration-absence sentinel. Each of the three current WARN
sites becomes "WARN if non-quiet, else DEBUG", with a one-shot
WARN at the transition.

- [ ] Above `_reseed_wallet_state_cache_from_rest`
      ([`trader_reconciliation_worker.py:714`](../../backend/workers/trader_reconciliation_worker.py:714)):
      add a module-level constant
      `_MISSING_CREDS_INIT_ERROR_SENTINEL = "missing_polymarket_credentials"`
      and a module-level mutable
      `_last_observed_reseeder_init_error: str | None = None`.
      Document with a 3-line comment why this shapes per-cycle
      logging (not a state machine — just a transition detector).
- [ ] Add a helper
      `_announce_reseeder_state_transition_if_changed() -> bool`
      that:
      1. Reads `current = live_execution_service.get_last_init_error()`.
      2. Compares to the module global.
      3. If `current == _MISSING_CREDS_INIT_ERROR_SENTINEL` AND
         the previous value was different, logs WARN
         `"WalletStateCache reseeder: polymarket credentials missing — demoting per-cycle warnings to DEBUG until creds appear"`.
      4. If the previous value WAS the sentinel and `current` is
         different (anything — None, transient error, new sentinel
         value), logs WARN
         `"WalletStateCache reseeder: polymarket credentials state changed; resuming standard logging"`
         with `previous_state` and `current_state` payload fields.
      5. Updates the module global.
      6. Returns `True` if currently in quiet mode
         (`current == sentinel`), else `False`.
- [ ] In `_reseed_wallet_state_cache_from_rest`, immediately after
      `cache = get_wallet_state_cache()`: call the helper, bind
      result to `quiet_mode = _announce_reseeder_state_transition_if_changed()`.
- [ ] Replace each of the three `logger.warning(...)` skip sites
      (current lines ~763, ~770, ~786) with:
      `(logger.debug if quiet_mode else logger.warning)(...)`.
      Keep the message strings and structured fields exactly the
      same — only the level changes. Other warn sites in the same
      function (the `ensure_initialized` exception block at line
      751) are NOT skip-warns and stay at WARNING.
- [ ] Inline comment block (3-5 lines) above the three replaced
      sites: "When init-error is the configuration-absence sentinel
      (`missing_polymarket_credentials`), per-cycle skip warnings
      are demoted to DEBUG to avoid log-spam in shadow-only
      deployments. The transition into and out of this state is
      announced once at WARNING by `_announce_reseeder_state_transition_if_changed`."
- [ ] Mark completed

### Task 2: Regression tests in new `test_wallet_cache_reseeder_quiet_mode.py`

No existing test file covers the reseeder loop's logging
behaviour — `test_wallet_state_cache.py` tests the cache class,
`test_trader_live_provider_reconciliation.py` tests live-provider
reconciliation. Justify a new file by scope.

- [ ] Create `backend/tests/test_wallet_cache_reseeder_quiet_mode.py`
      with a 4-line module docstring naming
      `_announce_reseeder_state_transition_if_changed` and
      `_reseed_wallet_state_cache_from_rest` as the units under
      test, with one short paragraph on why a separate file
      (the loop's logging policy is its own concern, distinct
      from the cache it seeds and the live-provider it queries).
- [ ] Add `test_first_cycle_with_missing_creds_emits_one_warn_announcement`:
      reset the module state global to `None` (via `monkeypatch`),
      patch `live_execution_service.get_last_init_error` to return
      the sentinel, capture log output via `caplog` at DEBUG level,
      run the reseeder helper, assert exactly one WARN line
      containing the string `"demoting per-cycle warnings to DEBUG"`
      and at least one DEBUG line containing the per-cycle skip
      message.
- [ ] Add `test_second_cycle_with_same_missing_creds_emits_no_warn`:
      pre-set the module global to the sentinel, run the helper,
      assert ZERO `WARNING`-level lines from the reseeder logger.
- [ ] Add `test_transition_out_of_missing_creds_emits_resume_warn`:
      pre-set module global to the sentinel, patch init-error to
      return `None`, run the helper, assert one WARN line with
      `"resuming standard logging"` and `previous_state=missing_polymarket_credentials`
      / `current_state=None` (or however the structured field is named).
- [ ] Add `test_other_init_errors_keep_warn`: pre-set module
      global to `None`, patch init-error to return a transient
      string like `"gamma_timeout"`, run the helper, assert the
      per-cycle skip path emits at WARNING (not DEBUG). Pins that
      we only quiet the configuration-absence sentinel.
- [ ] Add `test_re_entry_into_missing_creds_emits_announcement_again`:
      patch sequence is `None → sentinel → None → sentinel`; assert
      exactly two demote-announcements and one resume-announcement
      (so operators see every transition, not just the first).
- [ ] Run validation:
      `bash scripts/run_tests_remote.sh tests/test_wallet_cache_reseeder_quiet_mode.py`.
- [ ] Mark completed

### Task 3: Deploy, verify log volume drop, update docs, close out

- [ ] Pre-deploy log-volume measurement:
      ```bash
      ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since=10m worker-trading 2>&1 | grep -c "WalletStateCache reseeder skipped"'
      ```
      Record the count in this checkbox.
- [ ] Run `./deploy/sync_remote.sh`. Confirm `worker-trading`
      restarts cleanly.
- [ ] Post-deploy log-volume measurement (run the same grep
      ~10 minutes after restart). Expected: 0 or 1 (only the
      transition announcement). Record the count.
- [ ] Confirm the announcement WARN appears exactly once near
      restart:
      ```bash
      ssh polyhome-1 'cd /home/polyhome/homerun && docker compose logs --since=15m worker-trading 2>&1 | grep "demoting per-cycle warnings to DEBUG" | wc -l'
      ```
      Expected: 1. Record the count.
- [ ] Append a new entry to
      [`docs/operational/runtime-tweaks.md`](../operational/runtime-tweaks.md)
      (`2026-05-10 ~HH:MM UTC — Plan 0022: quiet missing_polymarket_credentials reseeder spam`)
      with surface (`backend/workers/trader_reconciliation_worker.py::_reseed_wallet_state_cache_from_rest`),
      one-paragraph why (Plan 0018 surfaced this; cleanup),
      pre/post log-volume numbers, and rollback (`git revert <SHA>`,
      redeploy).
- [ ] `git mv docs/plans/0022-quiet-missing-polymarket-credentials-warn-spam.md docs/plans/completed/`.
- [ ] Update the row in [`plan-control-index.md`](plan-control-index.md)
      to point at the `completed/` path.
- [ ] `git log --grep='Plan: 0022'` shows the full commit chain.
- [ ] Mark completed
