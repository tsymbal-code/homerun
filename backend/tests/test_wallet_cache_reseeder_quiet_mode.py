"""Tests for the WalletStateCache reseeder's transition-based logging.

Units under test
----------------
* ``_announce_reseeder_state_transition_if_changed`` — the transition
  detector that announces entry into and exit from the
  ``missing_polymarket_credentials`` state and returns whether to
  log per-cycle skips at DEBUG.
* ``_reseed_wallet_state_cache_from_rest`` — the reseed coroutine
  that consumes the transition detector to pick its skip-log level.

Why this file is separate from
``backend/tests/test_wallet_state_cache.py`` and
``backend/tests/test_trader_live_provider_reconciliation.py``
------------------------------------------------------------
The cache class (`WalletStateCache`) and the live-provider
reconciliation paths are unrelated subsystems with their own
sibling test files.  The reseeder loop's logging-level policy is its
own concern: it gates two emit sites in
``_reseed_wallet_state_cache_from_rest`` on the
``live_execution_service`` init-error string, with module-level
state tracking the last observed value.  Those mechanics deserve
focused tests rather than living as drive-by additions in either
of the adjacent files.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


from workers import trader_reconciliation_worker as worker  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Force the module-level last-observed value to ``None`` before each
    test so cases run independently of each other and of any prior
    import state."""
    monkeypatch.setattr(worker, "_last_observed_reseeder_init_error", None)
    yield


def _set_init_error(monkeypatch, value):
    """Patch ``live_execution_service.get_last_init_error`` to return
    ``value`` for the duration of one test."""
    monkeypatch.setattr(
        worker.live_execution_service,
        "get_last_init_error",
        lambda: value,
    )


def test_first_cycle_with_missing_creds_emits_one_warn_announcement(monkeypatch, caplog):
    _set_init_error(monkeypatch, worker._MISSING_CREDS_INIT_ERROR_SENTINEL)

    with caplog.at_level(logging.DEBUG, logger=worker.logger.logger.name):
        quiet = worker._announce_reseeder_state_transition_if_changed()

    assert quiet is True
    demote_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "demoting per-cycle warnings to DEBUG" in r.getMessage()
    ]
    assert len(demote_records) == 1, (
        "first cycle in missing-creds state must emit exactly one "
        "demote-to-DEBUG announcement"
    )


def test_second_cycle_with_same_missing_creds_emits_no_warn(monkeypatch, caplog):
    monkeypatch.setattr(
        worker,
        "_last_observed_reseeder_init_error",
        worker._MISSING_CREDS_INIT_ERROR_SENTINEL,
    )
    _set_init_error(monkeypatch, worker._MISSING_CREDS_INIT_ERROR_SENTINEL)

    with caplog.at_level(logging.DEBUG, logger=worker.logger.logger.name):
        quiet = worker._announce_reseeder_state_transition_if_changed()

    assert quiet is True
    warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warn_records == [], (
        "steady state in missing-creds must not emit any WARN line"
    )


def test_transition_out_of_missing_creds_emits_resume_warn(monkeypatch, caplog):
    monkeypatch.setattr(
        worker,
        "_last_observed_reseeder_init_error",
        worker._MISSING_CREDS_INIT_ERROR_SENTINEL,
    )
    _set_init_error(monkeypatch, None)

    with caplog.at_level(logging.DEBUG, logger=worker.logger.logger.name):
        quiet = worker._announce_reseeder_state_transition_if_changed()

    assert quiet is False
    resume_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "resuming standard logging" in r.getMessage()
    ]
    assert len(resume_records) == 1
    record = resume_records[0]
    extra = getattr(record, "extra_data", None) or {}
    assert extra.get("previous_state") == worker._MISSING_CREDS_INIT_ERROR_SENTINEL
    assert extra.get("current_state") is None


def test_other_init_errors_keep_warn(monkeypatch, caplog):
    """Non-sentinel error strings must NOT activate quiet mode — they
    are real degradation that operators must see at WARNING."""
    _set_init_error(monkeypatch, "gamma_timeout")

    with caplog.at_level(logging.DEBUG, logger=worker.logger.logger.name):
        quiet = worker._announce_reseeder_state_transition_if_changed()

    assert quiet is False
    # The transition `None -> gamma_timeout` is NOT the
    # missing-creds sentinel, so neither the demote nor the resume
    # announcement should fire.
    demote_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "demoting per-cycle warnings to DEBUG" in r.getMessage()
    ]
    assert demote_records == []


def test_re_entry_into_missing_creds_emits_announcement_again(monkeypatch, caplog):
    """Operators must see every transition into the quiet state, not
    just the first.  Sequence: None -> sentinel -> None -> sentinel
    => exactly two demote-announcements + one resume-announcement."""
    sentinel = worker._MISSING_CREDS_INIT_ERROR_SENTINEL
    sequence = [sentinel, None, sentinel]

    with caplog.at_level(logging.DEBUG, logger=worker.logger.logger.name):
        for value in sequence:
            _set_init_error(monkeypatch, value)
            worker._announce_reseeder_state_transition_if_changed()

    demote_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "demoting per-cycle warnings to DEBUG" in r.getMessage()
    ]
    resume_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "resuming standard logging" in r.getMessage()
    ]
    assert len(demote_records) == 2, "each entry into missing-creds must announce"
    assert len(resume_records) == 1, "the single exit must announce once"
