import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models.database import Base, Trader, TraderEvent, TraderOrder
from tests.postgres_test_db import build_postgres_session_factory
from utils.utcnow import utcnow
from workers import trader_orchestrator_worker


async def _build_session_factory(_tmp_path: Path):
    return await build_postgres_session_factory(Base, "trader_orchestrator_shadow_backfill")


@pytest.mark.asyncio
async def test_backfill_passes_shadow_simulation_fee_and_slippage_to_ledger(tmp_path, monkeypatch):
    engine, session_factory = await _build_session_factory(tmp_path)
    try:
        async with session_factory() as session:
            now = datetime.utcnow()
            session.add(
                Trader(
                    id="trader-1",
                    name="Backfill Trader",
                    source_configs_json=[{"source_key": "crypto", "strategy_key": "btc_eth_maker_quote", "strategy_params": {}}],
                    risk_limits_json={},
                    metadata_json={},
                    is_enabled=True,
                    is_paused=False,
                    interval_seconds=60,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                TraderOrder(
                    id="order-1",
                    trader_id="trader-1",
                    signal_id=None,
                    source="crypto",
                    market_id="market-1",
                    market_question="Will this backfill?",
                    direction="buy_yes",
                    mode="shadow",
                    status="executed",
                    notional_usd=50.0,
                    entry_price=0.5,
                    effective_price=0.5,
                    payload_json={
                        "shadow_simulation": {
                            "estimated_fee_usd": 1.2,
                            "slippage_usd": 0.75,
                            "fill_ratio": 0.9,
                        }
                    },
                    created_at=now,
                    executed_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

            record_mock = AsyncMock(
                return_value={
                    "account_id": "shadow-1",
                    "trade_id": "trade-1",
                    "position_id": "position-1",
                }
            )
            monkeypatch.setattr(
                trader_orchestrator_worker.simulation_service,
                "record_orchestrator_shadow_fill",
                record_mock,
            )

            result = await trader_orchestrator_worker._backfill_simulation_ledger_for_active_shadow_orders(
                session,
                trader_id="trader-1",
                shadow_account_id="shadow-1",
            )
            await session.flush()

            order = await session.get(TraderOrder, "order-1")
            assert order is not None
            assert result["attempted"] == 1
            assert result["backfilled"] == 1
            assert result["errors"] == []
            assert isinstance((order.payload_json or {}).get("simulation_ledger"), dict)

            record_mock.assert_awaited_once()
            kwargs = record_mock.await_args.kwargs
            assert kwargs["execution_fee_usd"] == pytest.approx(1.2, rel=1e-9)
            assert kwargs["execution_slippage_usd"] == pytest.approx(0.75, rel=1e-9)
            assert kwargs["payload"]["shadow_simulation"]["fill_ratio"] == pytest.approx(0.9, rel=1e-9)
    finally:
        await engine.dispose()


def _seed_trader_event(
    *,
    trader_id: str,
    event_type: str = "shadow_ledger_backfill_failed",
    severity: str = "warn",
    created_at: datetime,
    seq: int,
) -> TraderEvent:
    return TraderEvent(
        id=f"evt-{trader_id}-{seq}",
        trader_id=trader_id,
        event_type=event_type,
        severity=severity,
        source="worker",
        message="prior",
        payload_json={},
        created_at=created_at,
    )


def _seed_trader_row(*, trader_id: str, now: datetime) -> Trader:
    return Trader(
        id=trader_id,
        name=f"Trader {trader_id}",
        source_configs_json=[
            {"source_key": "crypto", "strategy_key": "btc_eth_maker_quote", "strategy_params": {}}
        ],
        risk_limits_json={},
        metadata_json={},
        is_enabled=True,
        is_paused=False,
        interval_seconds=60,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_shadow_ledger_backfill_failed_emits_warn_below_threshold(tmp_path):
    engine, session_factory = await _build_session_factory(tmp_path)
    threshold = trader_orchestrator_worker.SHADOW_LEDGER_BACKFILL_FAILED_ESCALATION_THRESHOLD
    try:
        async with session_factory() as session:
            now = utcnow().replace(tzinfo=None)
            session.add(_seed_trader_row(trader_id="trader-warn", now=now))
            await session.flush()
            for i in range(threshold - 1):
                session.add(
                    _seed_trader_event(
                        trader_id="trader-warn",
                        created_at=now - timedelta(minutes=10),
                        seq=i,
                    )
                )
            await session.commit()

            severity, escalation = await trader_orchestrator_worker._resolve_shadow_ledger_backfill_severity(
                session,
                trader_id="trader-warn",
            )
            assert severity == "warn"
            assert escalation == {}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_shadow_ledger_backfill_failed_escalates_to_error_at_threshold(tmp_path):
    engine, session_factory = await _build_session_factory(tmp_path)
    threshold = trader_orchestrator_worker.SHADOW_LEDGER_BACKFILL_FAILED_ESCALATION_THRESHOLD
    try:
        async with session_factory() as session:
            now = utcnow().replace(tzinfo=None)
            session.add(_seed_trader_row(trader_id="trader-escalate", now=now))
            await session.flush()
            for i in range(threshold):
                session.add(
                    _seed_trader_event(
                        trader_id="trader-escalate",
                        created_at=now - timedelta(minutes=10),
                        seq=i,
                    )
                )
            await session.commit()

            severity, escalation = await trader_orchestrator_worker._resolve_shadow_ledger_backfill_severity(
                session,
                trader_id="trader-escalate",
            )
            assert severity == "error"
            assert escalation["escalated_from"] == "warn"
            assert escalation["prior_event_count"] == threshold
            assert escalation["escalation_threshold"] == threshold
            assert escalation["escalation_window_seconds"] == pytest.approx(
                trader_orchestrator_worker.SHADOW_LEDGER_BACKFILL_FAILED_ESCALATION_WINDOW_SECONDS,
                rel=1e-9,
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_shadow_ledger_backfill_failed_escalation_is_per_trader(tmp_path):
    engine, session_factory = await _build_session_factory(tmp_path)
    threshold = trader_orchestrator_worker.SHADOW_LEDGER_BACKFILL_FAILED_ESCALATION_THRESHOLD
    try:
        async with session_factory() as session:
            now = utcnow().replace(tzinfo=None)
            session.add(_seed_trader_row(trader_id="trader-noisy", now=now))
            session.add(_seed_trader_row(trader_id="trader-quiet", now=now))
            await session.flush()
            for i in range(threshold * 2):
                session.add(
                    _seed_trader_event(
                        trader_id="trader-noisy",
                        created_at=now - timedelta(minutes=10),
                        seq=i,
                    )
                )
            await session.commit()

            severity, escalation = await trader_orchestrator_worker._resolve_shadow_ledger_backfill_severity(
                session,
                trader_id="trader-quiet",
            )
            assert severity == "warn"
            assert escalation == {}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_shadow_ledger_backfill_failed_escalation_ignores_old_events(tmp_path):
    engine, session_factory = await _build_session_factory(tmp_path)
    threshold = trader_orchestrator_worker.SHADOW_LEDGER_BACKFILL_FAILED_ESCALATION_THRESHOLD
    try:
        async with session_factory() as session:
            now = utcnow().replace(tzinfo=None)
            session.add(_seed_trader_row(trader_id="trader-old", now=now))
            await session.flush()
            for i in range(threshold):
                session.add(
                    _seed_trader_event(
                        trader_id="trader-old",
                        created_at=now - timedelta(hours=2),
                        seq=i,
                    )
                )
            await session.commit()

            severity, escalation = await trader_orchestrator_worker._resolve_shadow_ledger_backfill_severity(
                session,
                trader_id="trader-old",
            )
            assert severity == "warn"
            assert escalation == {}
    finally:
        await engine.dispose()
