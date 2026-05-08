"""Integration tests for the publish path of `source='traders'`.

Plan 0009 (`docs/plans/0009-fix-traders-source-on-normal.md`):
once `signal_bus._strategy_runtime_metadata` no longer routes
`source_key='traders'` through the deferred-state branch, every
`traders_copy_trade` opportunity must land in the runtime cache
with a non-NULL `runtime_sequence`, must NOT be deferred, must
publish a `runtime_signal_batch` to the `general` lane, and must
be visible via `intent_runtime.list_unconsumed_signals`.

These tests fail on `main` (where the publish path defers the
signal at line `intent_runtime.py:2186-2195`, leaving
`runtime_sequence=None`) and pass once Plan 0009 Task 3 lands.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from models.opportunity import Opportunity
from services.intent_runtime import IntentRuntime
from services.runtime_signal_queue import _default_lane_for_source


def _patch_traders_strategy_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_loaded = SimpleNamespace(
        instance=SimpleNamespace(
            source_key="traders",
            subscriptions=["trader_activity"],
        )
    )
    monkeypatch.setattr(
        "services.strategy_loader.strategy_loader.get_strategy",
        lambda _slug: fake_loaded,
    )


def _make_traders_opportunity() -> Opportunity:
    return Opportunity(
        strategy="custom_copy_trade",
        title="Leader bought YES on market-1",
        description="Copy-trade follow-on for fixture market-1.",
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
                "token_id": "trader-token-1",
                "outcome": "YES",
                "side": "buy",
                "price": 0.41,
            }
        ],
    )


@pytest.fixture
def captured_signal_batches(monkeypatch):
    """Capture the kwargs of every `publish_signal_batch` call.

    Mirrors the pattern used in
    `test_intent_runtime_ws_freshness.py` so we can verify lane
    routing via `runtime_signal_queue._default_lane_for_source`
    without poking at the global queue state.
    """
    batches: list[dict[str, object]] = []

    async def _publish_signal_batch(**kwargs):
        batches.append(dict(kwargs))
        return "batch-test"

    monkeypatch.setattr(
        "services.intent_runtime.publish_signal_batch",
        _publish_signal_batch,
    )
    monkeypatch.setattr(
        "services.intent_runtime.event_bus.publish",
        AsyncMock(return_value=None),
    )
    return batches


@pytest.mark.asyncio
async def test_publish_traders_opportunity_assigns_runtime_sequence_and_skips_deferred_state(
    monkeypatch, captured_signal_batches
):
    _patch_traders_strategy_loader(monkeypatch)

    runtime = IntentRuntime()
    runtime._ensure_hot_subscriptions = AsyncMock(return_value=None)

    published = await runtime.publish_opportunities(
        [_make_traders_opportunity()],
        source="traders",
        signal_type_override="copy_trade",
    )

    assert published == 1
    assert len(runtime._signals_by_id) == 1
    snapshot = next(iter(runtime._signals_by_id.values()))

    assert snapshot["source"] == "traders"
    assert snapshot["strategy_type"] == "custom_copy_trade"
    assert snapshot["signal_type"] == "copy_trade"
    assert snapshot["status"] == "pending"
    assert snapshot["deferred_until_ws"] is False, (
        "traders signals must not be born in the awaiting_post_arm_ws_tick "
        f"deferred state; got: {snapshot.get('deferred_reason')!r}"
    )
    assert snapshot["deferred_reason"] is None
    assert snapshot["runtime_sequence"] is not None, (
        "traders signals must be born with a non-NULL runtime_sequence so "
        "that consumers (orchestrator, fast-tier) can see them"
    )
    assert isinstance(snapshot["runtime_sequence"], int)

    # The runtime should have written `execution_armed_at` for traders
    # signals on the immediate path (mirrors the scanner ws_current
    # behaviour after a successful arm-time quote).
    assert snapshot["payload_json"].get("execution_armed_at")


@pytest.mark.asyncio
async def test_publish_traders_opportunity_routes_batch_to_general_lane(
    monkeypatch, captured_signal_batches
):
    _patch_traders_strategy_loader(monkeypatch)

    runtime = IntentRuntime()
    runtime._ensure_hot_subscriptions = AsyncMock(return_value=None)

    await runtime.publish_opportunities(
        [_make_traders_opportunity()],
        source="traders",
        signal_type_override="copy_trade",
    )

    assert captured_signal_batches, (
        "expected at least one publish_signal_batch call after publishing "
        "a traders-source opportunity; on `main` zero batches are produced "
        "because the deferred-state branch suppresses actionable publication"
    )
    batch = captured_signal_batches[0]
    assert batch["source"] == "traders"
    assert batch["event_type"] == "upsert_insert"
    assert isinstance(batch["signal_ids"], list) and batch["signal_ids"]
    snapshots = batch["signal_snapshots"]
    assert isinstance(snapshots, dict) and snapshots
    snapshot = next(iter(snapshots.values()))
    assert snapshot["source"] == "traders"
    assert snapshot["runtime_sequence"] is not None

    # Lane resolution: a `traders` source must route to the `general`
    # lane (the only non-`general` lane is `crypto`).
    assert _default_lane_for_source(str(batch["source"])) == "general"


@pytest.mark.asyncio
async def test_publish_traders_opportunity_visible_via_list_unconsumed_signals(
    monkeypatch, captured_signal_batches
):
    _patch_traders_strategy_loader(monkeypatch)

    runtime = IntentRuntime()
    runtime._ensure_hot_subscriptions = AsyncMock(return_value=None)

    await runtime.publish_opportunities(
        [_make_traders_opportunity()],
        source="traders",
        signal_type_override="copy_trade",
    )

    rows = await runtime.list_unconsumed_signals(
        trader_id="fixture-trader",
        sources=["traders"],
        statuses=["pending", "selected"],
        strategy_types_by_source={"traders": ["custom_copy_trade"]},
    )

    assert len(rows) == 1, (
        "post-fix invariant: a freshly-published traders signal must be "
        "immediately visible to `list_unconsumed_signals`. On `main` the "
        "deferred_until_ws/runtime_sequence-NULL filters at "
        "intent_runtime.py:2432, 2440-2442 hide it."
    )
    row = rows[0]
    row_source = getattr(row, "source", None) or row["source"]
    row_strategy = (
        getattr(row, "strategy_type", None)
        or getattr(row, "strategy", None)
        or row["strategy_type"]
    )
    assert str(row_source) == "traders"
    assert str(row_strategy) == "custom_copy_trade"


@pytest.mark.asyncio
async def test_publish_traders_opportunity_repeated_dedupes_into_existing_snapshot(
    monkeypatch, captured_signal_batches
):
    """Regression: re-publishing the same traders opportunity must
    upsert the existing snapshot rather than spawn a duplicate.
    Locks in that the existing-row branch (intent_runtime.py:2095+)
    also produces a non-NULL runtime_sequence under the fix.
    """
    _patch_traders_strategy_loader(monkeypatch)

    runtime = IntentRuntime()
    runtime._ensure_hot_subscriptions = AsyncMock(return_value=None)

    opportunity = _make_traders_opportunity()
    await runtime.publish_opportunities(
        [opportunity],
        source="traders",
        signal_type_override="copy_trade",
    )
    first_id = next(iter(runtime._signals_by_id.keys()))

    # Force a material change so the second publish takes the
    # existing-row reactivate/upsert branch (entry_price differs).
    opportunity_v2 = _make_traders_opportunity()
    opportunity_v2.positions_to_take[0]["price"] = 0.45
    opportunity_v2.total_cost = 0.45
    await runtime.publish_opportunities(
        [opportunity_v2],
        source="traders",
        signal_type_override="copy_trade",
    )

    assert len(runtime._signals_by_id) == 1, (
        "dedupe by stable_id+strategy+market_id must collapse re-published "
        "traders opportunities onto the same snapshot"
    )
    second_id = next(iter(runtime._signals_by_id.keys()))
    assert second_id == first_id

    snapshot = runtime._signals_by_id[second_id]
    assert snapshot["deferred_until_ws"] is False
    assert snapshot["runtime_sequence"] is not None
