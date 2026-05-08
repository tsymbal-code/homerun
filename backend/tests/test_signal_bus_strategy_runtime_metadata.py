"""Unit tests for `signal_bus._strategy_runtime_metadata`.

Plan 0009 (`docs/plans/0009-fix-traders-source-on-normal.md`)
fixes the silent `else: ws_post_arm_tick` fallback so that
`source_key="traders"` no longer routes signals through the
deferred-state branch in `intent_runtime.publish_opportunities`.

These tests pin the post-fix invariants and act as the "red" of
the red-green refactor: they fail on `main` (which still returns
`ws_post_arm_tick` for `traders` and any unknown source) and pass
once Plan 0009 Task 3 lands.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from models.opportunity import Opportunity
from services import signal_bus


def _make_opportunity(strategy: str = "fixture_strategy") -> Opportunity:
    return Opportunity(
        strategy=strategy,
        title="Fixture opportunity",
        description="Used by signal_bus._strategy_runtime_metadata tests.",
        total_cost=0.41,
        expected_payout=1.0,
        gross_profit=0.09,
        fee=0.0,
        net_profit=0.09,
        roi_percent=9.0,
        markets=[{"id": "market-1", "question": "Will it happen?"}],
        positions_to_take=[
            {
                "market_id": "market-1",
                "token_id": "token-1",
                "outcome": "YES",
                "side": "buy",
                "price": 0.41,
            }
        ],
    )


def _patch_strategy_loader(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_key: str,
    subscriptions: list[str] | None = None,
) -> None:
    fake_loaded = SimpleNamespace(
        instance=SimpleNamespace(
            source_key=source_key,
            subscriptions=list(subscriptions or []),
        )
    )
    monkeypatch.setattr(
        "services.strategy_loader.strategy_loader.get_strategy",
        lambda _slug: fake_loaded,
    )


def test_strategy_runtime_metadata_crypto_returns_immediate(monkeypatch):
    _patch_strategy_loader(monkeypatch, source_key="crypto")
    metadata = signal_bus._strategy_runtime_metadata(_make_opportunity())
    assert metadata["source_key"] == "crypto"
    assert metadata["execution_activation"] == "immediate"


def test_strategy_runtime_metadata_scanner_returns_ws_current(monkeypatch):
    _patch_strategy_loader(monkeypatch, source_key="scanner")
    metadata = signal_bus._strategy_runtime_metadata(_make_opportunity())
    assert metadata["source_key"] == "scanner"
    assert metadata["execution_activation"] == "ws_current"


def test_strategy_runtime_metadata_traders_returns_immediate(monkeypatch):
    """Plan 0009 fix invariant. Fails on `main` where the `else`
    branch still returns ``ws_post_arm_tick`` for traders source.
    """
    _patch_strategy_loader(
        monkeypatch,
        source_key="traders",
        subscriptions=["trader_activity"],
    )
    metadata = signal_bus._strategy_runtime_metadata(_make_opportunity())
    assert metadata["source_key"] == "traders"
    assert metadata["execution_activation"] == "immediate"
    assert metadata["subscriptions"] == ["trader_activity"]


def test_strategy_runtime_metadata_unknown_source_defaults_to_immediate_and_warns(
    monkeypatch, caplog
):
    """Plan 0009 option-3 invariant. The pre-fix `else` branch
    silently produced ``ws_post_arm_tick``; after the fix unknown
    sources fall back to the safe ``immediate`` default and emit
    a warning so the operator notices.
    """
    _patch_strategy_loader(monkeypatch, source_key="brand_new_source")
    if hasattr(signal_bus, "_UNKNOWN_SOURCE_KEY_WARNED"):
        signal_bus._UNKNOWN_SOURCE_KEY_WARNED.discard("brand_new_source")

    with caplog.at_level(logging.WARNING, logger="signal_bus"):
        metadata = signal_bus._strategy_runtime_metadata(_make_opportunity())

    assert metadata["source_key"] == "brand_new_source"
    assert metadata["execution_activation"] == "immediate"
    warning_records = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "execution_activation" in record.getMessage()
    ]
    assert warning_records, (
        "expected a WARNING about an unknown strategy source_key; "
        "got: " + repr([r.getMessage() for r in caplog.records])
    )
    warning_message = warning_records[0].getMessage()
    assert "brand_new_source" in warning_message


def test_strategy_runtime_metadata_unknown_source_warns_only_once_per_source(
    monkeypatch, caplog
):
    """The warn-once-per-source-key invariant: under steady-state
    publish load (potentially thousands of opportunities/sec for
    a future source) we must not flood the log with warnings.
    """
    _patch_strategy_loader(monkeypatch, source_key="another_new_source")
    if hasattr(signal_bus, "_UNKNOWN_SOURCE_KEY_WARNED"):
        signal_bus._UNKNOWN_SOURCE_KEY_WARNED.discard("another_new_source")

    with caplog.at_level(logging.WARNING, logger="signal_bus"):
        for _ in range(5):
            signal_bus._strategy_runtime_metadata(_make_opportunity())

    matching = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "another_new_source" in record.getMessage()
    ]
    assert len(matching) == 1, (
        "expected exactly one warning per unknown source_key; "
        f"got {len(matching)} warnings: "
        + repr([r.getMessage() for r in matching])
    )


def test_strategy_runtime_metadata_returns_empty_when_strategy_missing(monkeypatch):
    """Defensive regression: when the loader cannot find the
    strategy (slug typo, hot-reload race) the function returns an
    empty dict and the caller falls back to its own defaults.
    """
    monkeypatch.setattr(
        "services.strategy_loader.strategy_loader.get_strategy",
        lambda _slug: None,
    )
    assert signal_bus._strategy_runtime_metadata(_make_opportunity()) == {}
