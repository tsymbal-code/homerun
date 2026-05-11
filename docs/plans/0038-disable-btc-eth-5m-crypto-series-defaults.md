# Plan: Disable BTC and ETH 5m crypto-series defaults

> **Plan policy.** This plan follows
> [`docs/plans/README.md`](README.md) — task format, validation
> commands, "Mark completed" pattern, move to
> [`completed/`](completed/) on close. Every commit produced by
> this plan carries a `Plan: 0038` git trailer (see
> [Commits and traceability](README.md#commits-and-traceability)).
> Ordering, category, and prerequisites for this plan live in
> [`plan-control-index.md`](plan-control-index.md).

## Overview

The operator wants the crypto lane to fetch only SOL 5m and XRP 5m
Polymarket series — the only ones the active `crypto_5m_midcycle`
trader trades. They cleared `btc_eth_hf_series_btc_5m` and
`btc_eth_hf_series_eth_5m` to empty strings via the Crypto Settings
Flyout. The DB rows are blank, but the runtime keeps fetching BTC and
ETH 5m markets anyway.

Root cause: [`backend/config.py:958-959`](../../backend/config.py)
treats an empty DB string as "restore class default" and re-injects
the hard-coded series IDs from [`config.py:426-427`](../../backend/config.py).
Stripping the defaults to `""` keeps that fallback intact for the
other 14 series but makes BTC 5m and ETH 5m honor the operator's
clear.

This is the narrow fix the operator approved. The broader "empty
string should always mean disabled" semantics on the apply-layer
remain a known footgun documented in
[`docs/plans/architecture/crypto-fast-binary-lane.md`](architecture/crypto-fast-binary-lane.md);
a generalized fix is out of scope for this plan.

**Done means:** `_get_series_configs()` returns no entry for BTC 5m
or ETH 5m on a fresh install (no DB row) and on a clear-from-UI
install (DB row with empty strings); the other 14 series keep their
defaults; tests cover both scenarios; the operator confirms post-
deploy that crypto-service logs no longer report
`Market rotation: ... BTC 5min, ETH 5min`.

## Context / References

- [`backend/config.py:420-437`](../../backend/config.py) — Settings
  class defaults for the 16 series IDs.
- [`backend/config.py:953-965`](../../backend/config.py) —
  `apply_app_settings` empty-string-fallback logic.
- [`backend/services/crypto_service.py:228-247`](../../backend/services/crypto_service.py)
  — `_get_series_configs()` reads `_cfg.BTC_ETH_HF_SERIES_*`.
- [`backend/services/crypto_service.py:604-608`](../../backend/services/crypto_service.py)
  — fetch-time filter that skips blank series IDs.
- [Architecture: crypto-fast-binary-lane](architecture/crypto-fast-binary-lane.md)

## Validation Commands

- `docker compose exec backend pytest -q backend/tests/test_crypto_service.py`
- `docker compose exec backend pytest -q backend/tests/test_config_database_path.py`
- `docker compose exec backend ruff check backend/config.py backend/tests/`

### Task 1: Clear BTC and ETH 5m defaults in Settings class

- [ ] In [`backend/config.py:426-427`](../../backend/config.py),
      replace `BTC_ETH_HF_SERIES_BTC_5M: str = "10684"` with
      `BTC_ETH_HF_SERIES_BTC_5M: str = ""` and
      `BTC_ETH_HF_SERIES_ETH_5M: str = "10683"` with
      `BTC_ETH_HF_SERIES_ETH_5M: str = ""`.
- [ ] Leave the inline comment block above the series IDs (lines
      420-421) describing them as "Polymarket series IDs for crypto
      up-or-down markets (editable in Settings)" and add a short
      note that BTC 5m and ETH 5m default to disabled per
      Plan 0038.

### Task 2: Regression test — empty defaults filter out at fetch boundary

- [ ] Add `test_get_series_configs_skips_empty_default_series` to
      [`backend/tests/test_crypto_service.py`](../../backend/tests/test_crypto_service.py).
      Assert that with the new defaults, the returned list contains
      SOL 5m and XRP 5m but no BTC 5m or ETH 5m entry; assert that
      the other 12 series (15m, 1h, 4h) are still present.
- [ ] Add a second test that monkey-patches `_cfg` to mimic the
      operator's clear-from-UI scenario (DB row sets the column to
      `""`) and verifies the same outcome — proving both paths reach
      the empty-skip filter.

### Task 3: Deploy and verify

- [ ] Run the validation commands locally.
- [ ] Run `./deploy/sync_remote.sh` to ship the change to
      `polyhome-1`.
- [ ] After redeploy, observe `worker-trading` logs for two new
      5-minute crypto cycles. Confirm `Market rotation: …` lines no
      longer include `BTC 5min` or `ETH 5min`; only SOL and XRP.
- [ ] Record the verdict (timestamp + log excerpt) in this plan
      file, then move it to `docs/plans/completed/`.
